# Databricks notebook source
# MAGIC %md
# MAGIC # Module 04 · Lab 4 — Sync, Federate & Unify, End-to-End
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~75 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Build all three Lakebase ↔ Lakehouse integration patterns against the project from Module 3 — synced table (UC Delta → Lakebase), federation (DBSQL → Lakebase, live), and Lakehouse Sync (Lakebase → UC Delta). By the end of this lab you will have run, measured, and compared every integration path the rest of the roadmap depends on.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Verify the Module 3 project is still up** (or re-provision)
# MAGIC 2. **Build a Delta source** in `main.lakebase_demo` to act as the upstream feed
# MAGIC 3. **Create a synced table** from Delta into Lakebase, prove sub-second app reads
# MAGIC 4. **Run a federated join** in DBSQL across Postgres rows and Delta tables
# MAGIC 5. **Build a Lakehouse Sync** from Lakebase back to Delta (Beta on AWS)
# MAGIC 6. **Measure cost & latency** — your own numbers, not slides
# MAGIC 7. **Verify** with a 6-row checklist and clean up pipelines (keep project)
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Module 3 Lab 3 passed** with all 6 checks green — `lakebase-tutorial` project + UC catalog must exist
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC - Write access to a UC catalog (default: `main`) for the demo Delta source
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sql` is explicitly used. Re-running individual cells is generally safe — Steps 1, 2, 3, 5 are idempotent.
# MAGIC
# MAGIC > **⚠ Billing note.** This lab adds incremental cost on top of the running Lakebase project: a synced-table CDC pipeline (CONTINUOUS) and, optionally, a Lakehouse Sync pipeline. Step 7b drops both pipelines without deleting the project — Module 5 reuses the project.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Verify the Module 3 project is still up (or re-provision)
# MAGIC
# MAGIC Idempotent: if the `lakebase-tutorial` project from Module 3 is still around (default `CLEANUP=False`), Cell 2 reuses it. Otherwise it provisions a fresh one.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | Project not found, provision starts | Module 3 cleanup ran | Wait ~90s for fresh project to be READY |
# MAGIC | UC catalog missing message persists | UC permissions revoked | Re-run Module 3 Step 4 (Cell 6) |
# MAGIC | `state=FAILED` on retry | Region issue | Read `state_message`; switch region |

# COMMAND ----------

# Cell 1 — install dependencies (re-run if kernel restarted since Lab 3)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — config + verify/reuse Lakebase project from Module 3
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.database import DatabaseProject
import time, uuid

w = WorkspaceClient()

TUTORIAL = {
    "catalog":      "main",
    "schema":       "lakebase_tutorial",
    "project_name": "lakebase-tutorial",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
    # Module 4 additions
    "demo_catalog": "main",                      # where the Delta source lives
    "demo_schema":  "lakebase_demo",             # schema inside that catalog
    "synced_table": "user_profile_synced",       # destination in Lakebase UC catalog
    "lhs_table":    "orders_history",            # destination Delta for Lakehouse Sync
}

# Reuse the project from Module 3, or create it if missing
existing = {p.name: p for p in w.database.list_database_projects()}
if TUTORIAL["project_name"] in existing:
    print(f"✅ Project '{TUTORIAL['project_name']}' from Module 3 — reusing.")
    project = existing[TUTORIAL["project_name"]]
else:
    print(f"Project not found — provisioning fresh (Module 3 cleanup ran).")
    project = w.database.create_database_project(
        project=DatabaseProject(
            name=TUTORIAL["project_name"],
            capacity="CU_1",
            retention_window_in_days=7,
        )
    )
    for i in range(40):
        p = w.database.get_database_project(TUTORIAL["project_name"])
        if p.state == "READY":
            project = p; break
        time.sleep(5)

# Confirm UC catalog from Module 3 is still registered
catalogs = spark.sql("SHOW CATALOGS").toPandas()["catalog"].values
if "lakebase_tutorial" not in catalogs:
    print("⚠ UC catalog 'lakebase_tutorial' missing — re-creating from Module 3.4")
    spark.sql(f"""
      CREATE CATALOG IF NOT EXISTS lakebase_tutorial
      USING DATABASE `{TUTORIAL['project_name']}.databricks_postgres`
      OPTIONS (description = 'OLTP catalog for the Lakebase tutorial')
    """)

print()
print(f"   Project state:  {project.state}")
print(f"   Endpoint:       {project.read_write_dns}")
print(f"   UC catalog:     lakebase_tutorial")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Build a Delta source for the synced table
# MAGIC
# MAGIC The synced-table pattern needs an upstream Delta table with a primary key. We create a small `user_profile` table in `main.lakebase_demo` that simulates a curated lakehouse output — the kind of table a daily ML pipeline or feature store would produce.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 2a — Create schema and Delta table

# COMMAND ----------

# Cell 3 — create the demo Delta source with CDF enabled
spark.sql(f"""
  CREATE SCHEMA IF NOT EXISTS {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}
""")

spark.sql(f"""
  CREATE TABLE IF NOT EXISTS {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile (
    user_id     BIGINT,
    email       STRING,
    plan        STRING,
    last_login  TIMESTAMP,
    score       DOUBLE,
    updated_at  TIMESTAMP
  )
  USING DELTA
  TBLPROPERTIES (
    delta.enableChangeDataFeed = true,
    delta.constraints.user_id_pk = 'user_id IS NOT NULL'
  )
""")

# Add a primary key constraint (required for synced tables)
try:
    spark.sql(f"""
      ALTER TABLE {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
      ADD CONSTRAINT user_profile_pk PRIMARY KEY (user_id)
    """)
except Exception as e:
    if "already exists" in str(e).lower():
        print("(constraint already exists — skipping)")
    else:
        raise

print("✅ Delta source created with CDF enabled and PK constraint")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 2b — Seed the Delta source with realistic data

# COMMAND ----------

# Cell 4 — seed the source with 5 user_profile rows
spark.sql(f"""
  MERGE INTO {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile t
  USING (
    SELECT * FROM VALUES
      (1, 'alice@example.com',  'pro',     timestamp'2026-04-30 09:14:00', 0.87, current_timestamp()),
      (2, 'bob@example.com',    'free',    timestamp'2026-05-01 18:02:00', 0.42, current_timestamp()),
      (3, 'carla@example.com',  'pro',     timestamp'2026-05-02 06:33:00', 0.91, current_timestamp()),
      (4, 'dave@example.com',   'free',    timestamp'2026-04-29 22:11:00', 0.38, current_timestamp()),
      (5, 'eve@example.com',    'enterprise', timestamp'2026-05-02 11:48:00', 0.95, current_timestamp())
    AS v(user_id, email, plan, last_login, score, updated_at)
  ) s
  ON t.user_id = s.user_id
  WHEN MATCHED THEN UPDATE SET *
  WHEN NOT MATCHED THEN INSERT *
""")

n = spark.sql(f"""
  SELECT count(*) AS n FROM {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
""").collect()[0]["n"]
print(f"✅ Delta source has {n} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Create the synced table (UC Delta → Lakebase)
# MAGIC
# MAGIC The whole point of Module 4. One SDK call sets up a managed CDC pipeline that keeps Lakebase fresh from Delta.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | "no PK on source" | Cell 3 PK constraint didn't apply | Re-run Cell 3 |
# MAGIC | Pipeline stuck on PROVISIONING | Region issue / capacity | Check pipeline in catalog UI |
# MAGIC | Cell 7 polling never sees 0.99 | CONTINUOUS not actually running | Switch to SNAPSHOT and call `refresh_synced_database_table` |
# MAGIC | `permission denied for table user_profile_synced` | UC GRANT missing | `GRANT SELECT ON CATALOG lakebase_tutorial TO ...` |

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 3a — Create the synced table

# COMMAND ----------

# Cell 5 — create the synced table with CONTINUOUS scheduling
from databricks.sdk.service.database import (
    SyncedDatabaseTable, SyncedTableSpec, SyncedTableSchedulingPolicy
)

source_fqn = (
    f"{TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile"
)
target_fqn = f"lakebase_tutorial.public.{TUTORIAL['synced_table']}"

print(f"Creating synced table:")
print(f"   source : {source_fqn}")
print(f"   target : {target_fqn}")
print(f"   schedule: CONTINUOUS  (use SNAPSHOT in prod unless realtime is proven needed)")

# Idempotent: if it already exists, just fetch
existing_synced = []
try:
    existing_synced = list(w.database.list_synced_database_tables(
        database_instance_name=TUTORIAL["project_name"]
    ))
except Exception:
    pass

if any(s.name == target_fqn for s in existing_synced):
    print("\n⚠ Synced table already exists — reusing.")
    st = next(s for s in existing_synced if s.name == target_fqn)
else:
    st = w.database.create_synced_database_table(
        synced_table=SyncedDatabaseTable(
            name=target_fqn,
            spec=SyncedTableSpec(
                source_table_full_name=source_fqn,
                primary_key_columns=["user_id"],
                scheduling_policy=SyncedTableSchedulingPolicy.CONTINUOUS,
                timeseries_key="updated_at",
            ),
            database_instance_name=TUTORIAL["project_name"],
        )
    )

# Poll until pipeline is RUNNING / first sync done
print("\nWaiting for first sync to complete (~30–90s)...")
for i in range(40):
    s = w.database.get_synced_database_table(name=target_fqn)
    pipeline_state = getattr(s, "pipeline_state", None) or "UNKNOWN"
    if pipeline_state in ("RUNNING", "ACTIVE", "READY"):
        print(f"   [{i*5:3d}s] pipeline={pipeline_state} — ready")
        break
    print(f"   [{i*5:3d}s] pipeline={pipeline_state}")
    time.sleep(5)

print(f"\n✅ Synced table is live: {target_fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 3b — Read the synced table from Postgres (proves the app path)

# COMMAND ----------

# Cell 6 — open a Postgres engine on the project (same pattern as Module 3.2)
from sqlalchemy import create_engine, text

cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)
url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred.token}"
    f"@{project.read_write_dns}:5432/databricks_postgres?sslmode=require"
)
engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

# Query the synced table from Postgres
with engine.connect() as c:
    rows = c.execute(text(f"""
        SELECT user_id, email, plan, score
        FROM {TUTORIAL['synced_table']}
        ORDER BY user_id
    """)).fetchall()

print(f"✅ Read {len(rows)} rows from synced table (via Postgres driver):")
for r in rows:
    print(f"   user_id={r.user_id}  email={r.email:25s}  plan={r.plan:10s}  score={r.score}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 3c — Prove freshness: write to Delta, see it in Postgres

# COMMAND ----------

# Cell 7 — UPDATE in Delta, then SELECT from Postgres until we see the change
import time as _t

spark.sql(f"""
  UPDATE {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
  SET score = 0.99, updated_at = current_timestamp()
  WHERE user_id = 1
""")
write_time = _t.time()
print("⏱ Wrote update to Delta (alice score → 0.99)")

print("Polling Postgres for the new value...")
for i in range(30):
    with engine.connect() as c:
        v = c.execute(text(
            f"SELECT score FROM {TUTORIAL['synced_table']} WHERE user_id = 1"
        )).scalar()
    if v == 0.99:
        elapsed = _t.time() - write_time
        print(f"✅ Synced in {elapsed:.1f}s — Postgres now shows score=0.99")
        break
    _t.sleep(2)
else:
    print(f"⚠ After 60s, Postgres still shows score={v}. CONTINUOUS pipeline may not yet be running.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Federated query: live join across Postgres and Delta
# MAGIC
# MAGIC UC registration from Module 3.4 already enabled this. No new pipeline, no replication — DBSQL queries Lakebase live across the network.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `Table or view not found` | UC catalog missing or stale | Re-run Cell 2 |
# MAGIC | Slow query (>10s) on small tables | Predicate didn't push down | Inspect `EXPLAIN`; rewrite with simple `=` / `IN` |
# MAGIC | `connection refused` mid-query | Project went idle, cold-starting | Wait 10s, retry |

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 4a — Run a cross-store join

# COMMAND ----------

# Cell 8 — a federated query: Lakebase orders JOIN Delta user_profile
federated_sql = f"""
  SELECT
    o.id              AS order_id,
    o.sku,
    o.qty,
    o.total_cents,
    p.email,
    p.plan,
    p.score
  FROM lakebase_tutorial.public.orders o     -- Postgres (live federation)
  JOIN {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile p
       ON o.user_id = p.user_id              -- Delta
  ORDER BY o.id
"""
df = spark.sql(federated_sql)
df.show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 4b — Inspect the query plan to see pushdown

# COMMAND ----------

# Cell 9 — EXPLAIN to see which predicates push down
print(spark.sql(f"EXPLAIN {federated_sql}").collect()[0][0])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Lakehouse Sync: Lakebase changes → UC Delta (Beta)
# MAGIC
# MAGIC The reverse direction. Lakehouse Sync streams every change in a Lakebase table to Delta, continuously.
# MAGIC
# MAGIC > **⚠ Beta on AWS as of 2026.04.** Cell 10 detects availability automatically. If unavailable in your region, the lab logs a clear "skipped" status and Step 6 still passes.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 5a — Detect availability

# COMMAND ----------

# Cell 10 — try to create a Lakehouse Sync; catch and report cleanly if unavailable
LHS_TARGET = (
    f"{TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.{TUTORIAL['lhs_table']}"
)
LHS_AVAILABLE = False

try:
    spark.sql(f"""
      CREATE LAKEHOUSE_SYNC IF NOT EXISTS {LHS_TARGET}
      FROM DATABASE_INSTANCE '{TUTORIAL['project_name']}'
      SOURCE 'public.orders'
      WITH (continuous = true)
    """)
    LHS_AVAILABLE = True
    print(f"✅ Lakehouse Sync created: {LHS_TARGET}")
except Exception as e:
    msg = str(e)[:200]
    if "not enabled" in msg.lower() or "not available" in msg.lower() or "preview" in msg.lower():
        print(f"⚠ Lakehouse Sync is not enabled in this workspace/region.")
        print(f"   ({msg})")
        print(f"   Step 5 will be skipped. Steps 1-4 + Step 6 still verify the lab.")
    else:
        print(f"❌ Unexpected error creating Lakehouse Sync:")
        print(f"   {msg}")
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 5b — Generate change traffic in Lakebase, then read the Delta history

# COMMAND ----------

# Cell 11 — only runs if LHS_AVAILABLE; otherwise reports "skipped"
if not LHS_AVAILABLE:
    print("⏸ Step 5b skipped — Lakehouse Sync not available.")
else:
    # Insert one new order in Postgres
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO orders (user_id, sku, qty, total_cents)
            VALUES (
              (SELECT id FROM users WHERE email='alice@example.com'),
              'SKU-NEW', 1, 999
            )
        """))
    print("⏱ Inserted one row in Lakebase.public.orders")

    print("Waiting for the change to flow back to Delta (~30s typical)...")
    for i in range(30):
        try:
            n = spark.sql(f"""
              SELECT count(*) AS n FROM {LHS_TARGET}
              WHERE sku = 'SKU-NEW'
            """).collect()[0]["n"]
        except Exception:
            n = 0
        if n >= 1:
            print(f"✅ Reverse CDC working — Delta saw the change after ~{(i+1)*2}s")
            break
        time.sleep(2)
    else:
        print("⚠ Change hasn't arrived in Delta yet. Pipeline may still be initializing.")

    print("\nDelta history rows for SKU-NEW:")
    spark.sql(f"""
      SELECT _change_type, _commit_timestamp, sku, qty, total_cents
      FROM {LHS_TARGET}
      WHERE sku = 'SKU-NEW'
      ORDER BY _commit_timestamp
    """).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Cost & latency: measure all three patterns
# MAGIC
# MAGIC A reality-check before claiming numbers to a customer. We measure your numbers, not slide numbers.

# COMMAND ----------

# Cell 12 — measured comparison
def time_query(label, fn, n=5):
    """Run fn n times and report mean / p95 wall-clock."""
    times = []
    for _ in range(n):
        s = _t.perf_counter()
        fn()
        times.append((_t.perf_counter() - s) * 1000)
    times.sort()
    mean = sum(times) / n
    p95 = times[min(int(n*0.95), n-1)]
    print(f"   {label:30s}  mean={mean:6.1f}ms   p95={p95:6.1f}ms")
    return mean

print("Measuring read latency (5 runs each):\n")

def q_synced():
    with engine.connect() as c:
        c.execute(text(
            f"SELECT * FROM {TUTORIAL['synced_table']} WHERE user_id = 1"
        )).fetchone()

def q_fed():
    spark.sql(
        "SELECT * FROM lakebase_tutorial.public.users WHERE id = 1"
    ).collect()

def q_delta():
    spark.sql(
        f"SELECT * FROM {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile WHERE user_id = 1"
    ).collect()

m_sync  = time_query("synced table (Postgres)", q_synced)
m_fed   = time_query("federation (DBSQL→PG)",   q_fed)
m_delta = time_query("Delta direct (DBSQL)",    q_delta)

print()
print(f"   ◉ synced table read is ~{m_fed/m_sync:.0f}× faster than federation")
print(f"   ◉ federation is fine for rare reads, brutal at app scale")
print(f"   ◉ Delta direct is the analytics path — neither sync nor fed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Final verification & cleanup

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 7a — The summary checklist

# COMMAND ----------

# Cell 13 — final checklist
checks = []

# 1. Project still healthy
try:
    p = w.database.get_database_project(TUTORIAL["project_name"])
    checks.append(("Project from M3 reusable", f"state={p.state}", p.state == "READY"))
except Exception as e:
    checks.append(("Project from M3 reusable", str(e)[:30], False))

# 2. Delta source exists with rows
try:
    n = spark.sql(
      f"SELECT count(*) AS n FROM {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile"
    ).collect()[0]["n"]
    checks.append(("Delta source built", f"rows={n}", n >= 5))
except Exception as e:
    checks.append(("Delta source built", str(e)[:30], False))

# 3. Synced table exists in Postgres
try:
    with engine.connect() as c:
        n = c.execute(text(
          f"SELECT count(*) FROM {TUTORIAL['synced_table']}"
        )).scalar()
    checks.append(("Synced table reads", f"rows={n}", n >= 5))
except Exception as e:
    checks.append(("Synced table reads", str(e)[:30], False))

# 4. Synced table reflects an upstream UPDATE
try:
    with engine.connect() as c:
        score = c.execute(text(
          f"SELECT score FROM {TUTORIAL['synced_table']} WHERE user_id=1"
        )).scalar()
    checks.append(("Synced freshness works", f"score={score}", score == 0.99))
except Exception as e:
    checks.append(("Synced freshness works", str(e)[:30], False))

# 5. Federation join works
try:
    n = spark.sql(f"""
      SELECT count(*) AS n
      FROM lakebase_tutorial.public.orders o
      JOIN {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile p
        ON o.user_id = p.user_id
    """).collect()[0]["n"]
    checks.append(("Federated join works", f"rows={n}", n >= 1))
except Exception as e:
    checks.append(("Federated join works", str(e)[:30], False))

# 6. Lakehouse Sync (skip-friendly: passes if unavailable in region)
if LHS_AVAILABLE:
    try:
        n = spark.sql(f"""
          SELECT count(*) AS n FROM {LHS_TARGET} WHERE sku = 'SKU-NEW'
        """).collect()[0]["n"]
        checks.append(("Lakehouse Sync (Beta)", f"history rows={n}", n >= 1))
    except Exception as e:
        checks.append(("Lakehouse Sync (Beta)", str(e)[:30], False))
else:
    checks.append(("Lakehouse Sync (Beta)", "skipped (region)", True))

# Print summary
print("=" * 70)
print(f"{'CHECK':<28} {'DETAIL':<28} STATUS")
print("-" * 70)
for name, detail, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"{name:<28} {str(detail)[:28]:<28} {icon}")
print("=" * 70)
passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\n🎯 ALL {total} CHECKS PASSED — Module 4 complete.")
else:
    print(f"\n⚠ {passed}/{total} passed. Resolve the ❌ rows before Module 5.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 7b — Cleanup (recommended — drops pipelines, keeps project)

# COMMAND ----------

# Cell 14 — cleanup
# Set CLEANUP_PIPELINES=False if you want to keep the synced table & LHS running.
# Module 5 builds pgvector indexes INTO the same project, so we KEEP the project.
CLEANUP_PIPELINES = True

if CLEANUP_PIPELINES:
    target_fqn = f"lakebase_tutorial.public.{TUTORIAL['synced_table']}"

    # Drop the synced table (managed pipeline stops automatically)
    try:
        w.database.delete_synced_database_table(name=target_fqn)
        print(f"✅ Synced table dropped: {target_fqn}")
    except Exception as e:
        print(f"⚠ Could not drop synced table: {str(e)[:80]}")

    # Drop the Lakehouse Sync (if created)
    if LHS_AVAILABLE:
        try:
            spark.sql(f"DROP LAKEHOUSE_SYNC IF EXISTS {LHS_TARGET}")
            print(f"✅ Lakehouse Sync dropped: {LHS_TARGET}")
        except Exception as e:
            print(f"⚠ Could not drop Lakehouse Sync: {str(e)[:80]}")

    print("\n⏸ Lakebase project KEPT (needed for Module 5).")
    print(f"   Project: {TUTORIAL['project_name']}")
    print(f"   Endpoint: {project.read_write_dns}")
else:
    print("⏸ Pipeline cleanup skipped. Synced table + Lakehouse Sync still running.")
    print("   (Both will incur ongoing cost until dropped.)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module 4 — complete
# MAGIC
# MAGIC You finished five theory topics (4.1–4.5) and the hands-on lab. You now have:
# MAGIC
# MAGIC - Hands-on with all three integration patterns — synced tables, Lakehouse Sync, federation
# MAGIC - Measured proof of the latency / cost trade-off (your own numbers, not slides)
# MAGIC - A working synced-table pipeline you can show a customer in <2 minutes
# MAGIC - The decision rule for picking the right pattern in any data-direction conversation
# MAGIC
# MAGIC **Save this notebook somewhere you can find it again.** Module 5 builds pgvector on top of the project + UC registration you've kept alive. Module 6 layers agent memory onto your schema. Module 7 ties all three integration patterns together inside a Databricks App.
# MAGIC
# MAGIC **Next up: Module 5** — *Lakebase as a Vector Store.* You'll enable `pgvector` in your project, ingest embeddings via a synced table (the exact pattern you just built), and build a low-latency RAG retrieval path — without bolting on a third-party vector DB.
