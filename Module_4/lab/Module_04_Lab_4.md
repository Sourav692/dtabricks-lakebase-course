# Module 04 · Lab 4 — Sync, Federate & Unify, End-to-End

> **Type:** Hands-on · **Duration:** ~75 minutes · **Format:** Databricks notebook
> **Purpose:** Build all three Lakebase ↔ Lakehouse integration patterns against the project you provisioned in Module 3 — synced table (UC Delta → Lakebase), federation (DBSQL → Lakebase, live), and Lakehouse Sync (Lakebase → UC Delta). By the end of this lab you will have run, measured, and compared every integration path the rest of the roadmap depends on.

---

## Why this lab exists

Module 3 gave you a live Lakebase project, registered in Unity Catalog. **Module 4 is where that registration starts to pay off.** Until you wire up the integration patterns, your Lakebase is just managed Postgres. After this lab, your Lakebase is a *lakehouse-native* OLTP store — talking to Delta in both directions and answering federated queries from DBSQL.

Every later lab in the roadmap assumes a working synced-table pattern. **Module 5** lands embeddings into Lakebase via a synced table. **Module 6** uses Lakehouse Sync to capture agent traces. **Module 7** ties all three together inside a Databricks App.

This lab walks the five steps in order:

1. **Verify the Module 3 project is still up** (or re-provision)
2. **Build a Delta source** in `main.lakebase_demo` to act as the upstream feed
3. **Create a synced table** from Delta into Lakebase, prove sub-second app reads
4. **Run a federated join** in DBSQL across Postgres rows and Delta tables
5. **Build a Lakehouse Sync** from Lakebase back to Delta (Beta on AWS)

A 6th step compares cost and latency, and a 7th does the final verification and optional cleanup.

Total runtime ~75 minutes, of which about 5 minutes is your hands on the keyboard and ~70 is Databricks' managed pipelines doing the work. Use the wait times to skim the Module 4 theory primer if you haven't.

> **Run mode.** Do this lab in a Databricks notebook attached to a serverless cluster (or DBR 14+). All cells are Python except where SQL is explicitly called out via `%sql`. Copy each cell into your own notebook as you go.

---

## Prerequisites before you start

You need:

- ✅ **Module 3 Lab 3 passed** with all 6 checks green — the `lakebase-tutorial` project must be live and the `lakebase_tutorial` UC catalog must exist
- An attached compute cluster (serverless is fine, recommended)
- Workspace permission to create Delta tables in a writable catalog (we use `main` by default; change `DEMO_CATALOG` in Cell 2 if you need a different one)

You do **not** need:

- A new Lakebase project — we reuse the one from Module 3
- An existing Delta source — Step 2 creates one
- Any Lakehouse Sync entitlement upfront — we'll detect availability and skip Step 5 gracefully if Beta isn't enabled in your region

> **⚠ Billing note.** This lab adds incremental cost on top of the running Lakebase project: a synced-table CDC pipeline (CONTINUOUS) and, optionally, a Lakehouse Sync pipeline. Step 7b includes a cleanup cell that drops both pipelines without deleting the project itself — leave the project running for Module 5.

---

## Step 1 — Verify the Module 3 project is still up (or re-provision)

This step is idempotent — if you completed Module 3 with `CLEANUP=False` (the default), the project is still there and Cell 2 will detect it. If you cleaned up, it will provision a fresh one.

```python
# Cell 1 — install dependencies (re-run if kernel restarted since Lab 3)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()
```

```python
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
```

**Expected output:**

```
✅ Project 'lakebase-tutorial' from Module 3 — reusing.

   Project state:  READY
   Endpoint:       instance-a3f9e1c4-rw.cloud.databricks.com
   UC catalog:     lakebase_tutorial
```

**What this proves:** the project from Module 3 is intact, healthy, and still registered in UC. We can build integration pipelines on top of it without re-provisioning.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| Project not found and provision starts | Module 3 cleanup ran | Wait ~90s for the fresh project to be `READY` — Cell 2 polls automatically |
| UC catalog missing message persists | UC permissions revoked since Module 3 | Re-run Module 3 Step 4 (Cell 6) — the `CREATE CATALOG` command |
| `state=FAILED` on retry | Region issue | Read `project.state_message`; switch region or contact admin |

---

## Step 2 — Build a Delta source for the synced table

The synced-table pattern needs an upstream Delta table with a primary key. We create a small `user_profile` table in `main.lakebase_demo` that simulates a curated lakehouse output — the kind of table a daily ML pipeline or feature store would produce.

### Step 2a — Create schema and Delta table

```python
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
spark.sql(f"""
  ALTER TABLE {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
  ADD CONSTRAINT user_profile_pk PRIMARY KEY (user_id)
""")

print("✅ Delta source created with CDF enabled and PK constraint")
```

**Expected output:**

```
✅ Delta source created with CDF enabled and PK constraint
```

> **Note.** If you've run this lab before, `ADD CONSTRAINT` may error with "constraint already exists". That's fine — it's idempotent on re-runs.

### Step 2b — Seed the Delta source with realistic data

```python
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

# Confirm
n = spark.sql(f"""
  SELECT count(*) AS n FROM {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
""").collect()[0]["n"]
print(f"✅ Delta source has {n} rows")
```

**Expected output:**

```
✅ Delta source has 5 rows
```

**What this proves:** you have a real, CDF-enabled Delta table with a primary key — exactly what the synced-table pipeline requires upstream. Re-runnable via MERGE without duplicating rows.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `ALTER TABLE ... ADD CONSTRAINT` errors with "feature not enabled" | DBR runtime predates PK constraints | Use DBR 14.0+ or serverless |
| `permission denied` on `CREATE SCHEMA` | No write access to `main` catalog | Change `demo_catalog` in Cell 2 to a catalog you can write to |
| Existing rows but `n=0` | Wrong catalog / schema name | Verify `TUTORIAL["demo_catalog"]` matches what you created |

---

## Step 3 — Create the synced table (UC Delta → Lakebase)

The whole point of Module 4. One SDK call sets up a managed CDC pipeline that keeps Lakebase fresh from Delta — and your app gets to query Lakebase like any Postgres table.

### Step 3a — Create the synced table

```python
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
    state = getattr(s, "data_synchronization_status", None)
    pipeline_state = getattr(s, "pipeline_state", None) or "UNKNOWN"
    if pipeline_state in ("RUNNING", "ACTIVE", "READY"):
        print(f"   [{i*5:3d}s] pipeline={pipeline_state} — ready")
        break
    print(f"   [{i*5:3d}s] pipeline={pipeline_state}")
    time.sleep(5)

print(f"\n✅ Synced table is live: {target_fqn}")
```

**Expected output:**

```
Creating synced table:
   source : main.lakebase_demo.user_profile
   target : lakebase_tutorial.public.user_profile_synced
   schedule: CONTINUOUS  (use SNAPSHOT in prod unless realtime is proven needed)

Waiting for first sync to complete (~30–90s)...
   [  0s] pipeline=PROVISIONING
   [  5s] pipeline=PROVISIONING
   ...
   [ 60s] pipeline=RUNNING — ready

✅ Synced table is live: lakebase_tutorial.public.user_profile_synced
```

### Step 3b — Read the synced table from Postgres (proves the app path)

This is the moment that "Lakebase as lakehouse-native OLTP" becomes concrete. The synced table is just a Postgres table — your app driver connects, runs SELECT, gets results in milliseconds.

```python
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
```

**Expected output:**

```
✅ Read 5 rows from synced table (via Postgres driver):
   user_id=1  email=alice@example.com         plan=pro         score=0.87
   user_id=2  email=bob@example.com           plan=free        score=0.42
   user_id=3  email=carla@example.com         plan=pro         score=0.91
   user_id=4  email=dave@example.com          plan=free        score=0.38
   user_id=5  email=eve@example.com           plan=enterprise  score=0.95
```

### Step 3c — Prove freshness — write to Delta, see it in Postgres

```python
# Cell 7 — UPDATE in Delta, then SELECT from Postgres until we see the change
import time as _t

# Update alice's score in Delta
spark.sql(f"""
  UPDATE {TUTORIAL['demo_catalog']}.{TUTORIAL['demo_schema']}.user_profile
  SET score = 0.99, updated_at = current_timestamp()
  WHERE user_id = 1
""")
write_time = _t.time()
print("⏱ Wrote update to Delta (alice score → 0.99)")

# Poll Postgres until the synced table reflects the change
print("Polling Postgres for the new value...")
for i in range(30):  # 30 * 2s = 60s max
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
```

**Expected output:**

```
⏱ Wrote update to Delta (alice score → 0.99)
Polling Postgres for the new value...
✅ Synced in 8.4s — Postgres now shows score=0.99
```

**What this proves:** the entire pipeline is working — Delta CDF → managed pipeline → Lakebase upsert → app sees the new value in seconds. You just replaced reverse-ETL.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `create_synced_database_table` fails with "no PK on source" | Cell 3 PK constraint didn't apply | Re-run Cell 3 — the constraint is required |
| Pipeline state stuck on `PROVISIONING` for >5 min | Region issue / capacity | Check the pipeline in the UC catalog UI; contact support if stuck |
| Cell 7 polling never sees `0.99` | `CONTINUOUS` not actually running | Switch to `SNAPSHOT` and call `w.database.refresh_synced_database_table(...)` manually |
| `permission denied for table user_profile_synced` | UC GRANT missing on the catalog | `GRANT SELECT ON CATALOG lakebase_tutorial TO ` your user/group |

---

## Step 4 — Federated query: live join across Postgres and Delta

UC registration from Module 3.4 already enabled this. No new pipeline, no replication — DBSQL queries Lakebase live across the network.

### Step 4a — Run a cross-store join

```python
# Cell 8 — a federated query: Lakebase orders JOIN Delta user_profile
# (Recall: Module 3 created `lakebase_tutorial.public.users` and `.orders` —
#  we join those Postgres rows with the Delta source we built in Step 2.)

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
```

**Expected output:**

```
+--------+--------+---+-----------+--------------------+----+-----+
|order_id|sku     |qty|total_cents|email               |plan|score|
+--------+--------+---+-----------+--------------------+----+-----+
|1       |SKU-101 |2  |4998       |alice@example.com   |pro |0.99 |
|2       |SKU-205 |1  |1299       |alice@example.com   |pro |0.99 |
|3       |SKU-101 |5  |12495      |bob@example.com     |free|0.42 |
|4       |SKU-309 |3  |8997       |carla@example.com   |pro |0.91 |
+--------+--------+---+-----------+--------------------+----+-----+
```

> **Why alice's score is 0.99.** Step 3c updated it. The federation read pulls *current* Postgres rows — not a snapshot. That's the defining property of federation: every query is live.

### Step 4b — Inspect the query plan to see pushdown

```python
# Cell 9 — EXPLAIN to see which predicates push down
print(spark.sql(f"EXPLAIN {federated_sql}").collect()[0][0])
```

**Expected output (abbreviated):**

```
== Physical Plan ==
*(2) Project [...]
+- *(2) BroadcastHashJoin [user_id], [user_id], Inner, BuildRight
   :- *(2) Filter isnotnull(user_id)
   :  +- BatchScan ... lakebase_tutorial.public.orders
   :       PushedFilters: [IsNotNull(user_id)]    ← pushed to Postgres
   +- BroadcastExchange ...
      +- *(1) Filter isnotnull(user_id)
         +- *(1) ColumnarToRow
            +- FileScan parquet ... user_profile  ← Delta side
```

**What this proves:** the `IsNotNull(user_id)` predicate is pushed down to Postgres — only matching rows travel the wire. The Delta side is read normally as Parquet. The join itself happens in DBSQL.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `Table or view not found: lakebase_tutorial.public.orders` | UC catalog missing or stale | Re-run Cell 2 — it re-creates the catalog on demand |
| Slow query (>10s) on small tables | Predicate didn't push down | Inspect `EXPLAIN`; rewrite the predicate to use simple `=` / `IN` |
| `connection refused` mid-query | Project went idle and is cold-starting | Wait 10s, retry — first query post-idle has cold-start latency |

---

## Step 5 — Lakehouse Sync: Lakebase changes → UC Delta (Beta)

The reverse direction. Lakehouse Sync streams every change in a Lakebase table to Delta, continuously. We capture writes to `lakebase_tutorial.public.orders` (the table from Module 3) into a Delta history table.

> **⚠ Beta on AWS as of 2026.04.** Cell 10 detects availability automatically. If unavailable in your region, the lab logs a clear "skipped" status and Step 6 still passes — you can come back when Lakehouse Sync is GA in your region.

### Step 5a — Detect availability

```python
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
```

**Expected output (when Beta is enabled):**

```
✅ Lakehouse Sync created: main.lakebase_demo.orders_history
```

**Expected output (when Beta is not enabled):**

```
⚠ Lakehouse Sync is not enabled in this workspace/region.
   (Lakehouse Sync is in Beta and is not enabled for this region...)
   Step 5 will be skipped. Steps 1-4 + Step 6 still verify the lab.
```

### Step 5b — Generate change traffic in Lakebase, then read the Delta history

```python
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

    # Wait for it to appear in Delta
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

    # Show the change feed
    print("\nDelta history rows for SKU-NEW:")
    spark.sql(f"""
      SELECT _change_type, _commit_timestamp, sku, qty, total_cents
      FROM {LHS_TARGET}
      WHERE sku = 'SKU-NEW'
      ORDER BY _commit_timestamp
    """).show(truncate=False)
```

**Expected output (when LHS_AVAILABLE):**

```
⏱ Inserted one row in Lakebase.public.orders
Waiting for the change to flow back to Delta (~30s typical)...
✅ Reverse CDC working — Delta saw the change after ~14s

Delta history rows for SKU-NEW:
+------------+--------------------------+---------+---+-----------+
|_change_type|_commit_timestamp         |sku      |qty|total_cents|
+------------+--------------------------+---------+---+-----------+
|insert      |2026-05-02 14:42:18.231+00|SKU-NEW  |1  |999        |
+------------+--------------------------+---------+---+-----------+
```

**What this proves:** writes to your OLTP store flow into Delta automatically. No Debezium, no Kafka, no Spark Structured Streaming pipeline you maintain — the lakehouse picks them up via the storage layer's existing change stream.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `CREATE LAKEHOUSE_SYNC` parse error | DBR runtime predates the syntax | Use serverless DBSQL or DBR 14.3+ |
| `feature not enabled` | Beta not on for this region | Skip Step 5; everything else still validates |
| Insert succeeds but Delta empty after 60s | Pipeline still warming up | Sleep 30s more and re-check; first event is slowest |
| `_change_type` column missing | Pipeline created in non-CDF mode | Drop and re-create with `WITH (continuous = true)` |

---

## Step 6 — Cost & latency: measure all three patterns

A reality-check before claiming numbers to a customer. We measure your numbers, not slide numbers.

```python
# Cell 12 — measured comparison
import time as _t

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

# Pattern 1: Synced table read via Postgres driver (the "app path")
def q_synced():
    with engine.connect() as c:
        c.execute(text(
            f"SELECT * FROM {TUTORIAL['synced_table']} WHERE user_id = 1"
        )).fetchone()

# Pattern 2: Federation (DBSQL queries Postgres live)
def q_fed():
    spark.sql(
        "SELECT * FROM lakebase_tutorial.public.users WHERE id = 1"
    ).collect()

# Pattern 3: Direct Delta read (the "non-app" path)
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
```

**Expected output (illustrative):**

```
Measuring read latency (5 runs each):

   synced table (Postgres)         mean=   3.2ms   p95=   5.8ms
   federation (DBSQL→PG)           mean= 412.7ms   p95= 587.3ms
   Delta direct (DBSQL)            mean= 235.4ms   p95= 298.1ms

   ◉ synced table read is ~129× faster than federation
   ◉ federation is fine for rare reads, brutal at app scale
   ◉ Delta direct is the analytics path — neither sync nor fed
```

**What this proves:** the latency claim from Module 4.5 is real. Synced tables read at Postgres speed (~ms). Federation pays a network round-trip + DBSQL planning cost on every query. Use synced tables for app reads, federation for rare reads, and choose the right pattern *with measured numbers* — not guesses.

> **Note.** The first DBSQL query of a session always pays warmup cost. The numbers above stabilize after the first run. If `m_fed` looks unreasonably high, run Cell 12 a second time.

---

## Step 7 — Final verification & cleanup

A pass/fail summary for everything Module 4 built. Then an optional cleanup that drops the synced table and Lakehouse Sync without touching the Lakebase project itself (Module 5 still needs it).

### Step 7a — The summary checklist

```python
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
```

### Expected output (the success case)

```
======================================================================
CHECK                        DETAIL                       STATUS
----------------------------------------------------------------------
Project from M3 reusable     state=READY                  ✅
Delta source built           rows=5                       ✅
Synced table reads           rows=5                       ✅
Synced freshness works       score=0.99                   ✅
Federated join works         rows=4                       ✅
Lakehouse Sync (Beta)        history rows=1               ✅
======================================================================

🎯 ALL 6 CHECKS PASSED — Module 4 complete.
```

### Step 7b — Cleanup (recommended — drops pipelines, keeps project)

```python
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
```

---

## Module 4 — complete

You finished five theory topics (4.1–4.5) and the hands-on lab. You now have:

- Hands-on with all three integration patterns — synced tables, Lakehouse Sync, federation
- Measured proof of the latency / cost trade-off (your own numbers, not slides)
- A working synced-table pipeline you can show a customer in <2 minutes
- The decision rule for picking the right pattern in any data-direction conversation

**Save this notebook somewhere you can find it again.** Module 5 builds pgvector on top of the project + UC registration you've kept alive. Module 6 layers agent memory onto your schema. Module 7 ties all three integration patterns together inside a Databricks App.

**Next up: Module 5** — *Lakebase as a Vector Store.* You'll enable `pgvector` in your project, ingest embeddings via a synced table (the exact pattern you just built), and build a low-latency RAG retrieval path — without bolting on a third-party vector DB.
