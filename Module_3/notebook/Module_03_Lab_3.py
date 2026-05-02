# Databricks notebook source
# MAGIC %md
# MAGIC # Module 03 · Lab 3 — Your First Lakebase Project, End-to-End
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~75 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Provision a real Lakebase project, connect to it with auto-refreshing OAuth, build a real schema, register it in Unity Catalog, and prove out branching + point-in-time restore. By the end of this lab you will have done — for real, not in slides — every operation that the rest of the roadmap depends on.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Provision** a Lakebase Autoscaling project via SDK
# MAGIC 2. **Connect** with auto-refreshing OAuth credentials
# MAGIC 3. **Build a schema** with constraints, indexes, and seed data
# MAGIC 4. **Register** the project in Unity Catalog
# MAGIC 5. **Branch** the database, mutate the branch, verify isolation
# MAGIC 6. **Recover** from a deliberate corruption via point-in-time branching
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Module 1 Lab 1.4 passed** with all 9 checks green
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sql` is explicitly used. Re-running individual cells is generally safe — Steps 1, 3, 4 are written to be idempotent.
# MAGIC
# MAGIC > **⚠ Billing note.** This lab provisions a `CU_1` Lakebase project. Step 7b includes a cleanup cell — set `CLEANUP=True` if you want to delete the project at the end. Most readers leave `CLEANUP=False` so Module 4 can reuse the project.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Provision a Lakebase Autoscaling project
# MAGIC
# MAGIC One SDK call kicks off five things on the control plane: API accept → storage allocation → Postgres compute spin-up → `main` branch creation → DNS publishing. Total wall-clock is typically 60–90 seconds.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `PermissionDenied: Missing entitlement DATABASE_CREATE` | Module 1 Lab 1.4 didn't actually pass | Re-run Lab 1.4 cell 7; resolve the ❌ row before retrying |
# MAGIC | `state=FAILED` after polling | Region not yet GA, or workspace at quota | Read `project.state_message`; switch region if needed |
# MAGIC | Polling timeout | Control-plane slowness | Bump timeout; the project will eventually become READY |

# COMMAND ----------

# Cell 1 — install dependencies (re-run if kernel restarted since Lab 1.4)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — tutorial config + project creation
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.database import DatabaseProject
import time

w = WorkspaceClient()

TUTORIAL = {
    "catalog":      "main",
    "schema":       "lakebase_tutorial",
    "project_name": "lakebase-tutorial",
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
}

# Idempotent: if the project already exists, just fetch it.
existing = {p.name: p for p in w.database.list_database_projects()}
if TUTORIAL["project_name"] in existing:
    print(f"Project '{TUTORIAL['project_name']}' already exists — reusing.")
    project = existing[TUTORIAL["project_name"]]
else:
    print(f"Creating project '{TUTORIAL['project_name']}' (capacity=CU_1, retention=7d)...")
    project = w.database.create_database_project(
        project=DatabaseProject(
            name=TUTORIAL["project_name"],
            capacity="CU_1",
            retention_window_in_days=7,
        )
    )

# Poll until READY
print("Waiting for state=READY (typically 60–90s)...")
for i in range(40):  # 40 * 5s = 200s max
    p = w.database.get_database_project(TUTORIAL["project_name"])
    if p.state == "READY":
        project = p
        break
    time.sleep(5)
    print(f"  [{i*5:3d}s] state={p.state}")
else:
    raise RuntimeError(f"Project did not reach READY: state={p.state}")

print()
print(f"✅ Project ready")
print(f"   UID:      {project.uid}")
print(f"   State:    {project.state}")
print(f"   Endpoint: {project.read_write_dns}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Connect with auto-refreshing OAuth credentials
# MAGIC
# MAGIC The trap from Module 2.5: hardcoded passwords silently fail after the OAuth token TTL (~1 hour). The pattern below generates a fresh credential per engine and uses `pool_pre_ping` so a stale connection is detected before it's handed to your code.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `connection refused` | DNS hasn't fully propagated | Sleep 15s, refetch project, retry |
# MAGIC | `password authentication failed` | Stale token | Re-run this cell to regenerate the token |
# MAGIC | `SSL connection required` | sslmode=require missing | Confirm the URL includes `?sslmode=require` |

# COMMAND ----------

# Cell 3 — open a SQLAlchemy engine with auto-refresh
import uuid
from sqlalchemy import create_engine, text
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
project = w.database.get_database_project(TUTORIAL["project_name"])

# Generate a fresh OAuth token for this engine
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)

url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred.token}"
    f"@{project.read_write_dns}:5432/databricks_postgres"
    f"?sslmode=require"
)

# pool_pre_ping=True = before handing a pooled connection to your code,
# SQLAlchemy sends a cheap SELECT 1. If it fails (e.g. token expired
# between checkouts), SQLAlchemy discards and reopens.
engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

with engine.connect() as conn:
    version = conn.execute(text("SELECT version()")).scalar()
    print(f"✅ Connected")
    print(f"   {version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Build a real schema and seed data
# MAGIC
# MAGIC A toy `CREATE TABLE foo (x INT)` would be useless for Modules 4–8. We build a small but realistic users + orders schema, with constraints and two index types so you'll feel the difference between B-tree and BRIN.
# MAGIC
# MAGIC ### Step 3a — Create tables

# COMMAND ----------

# Cell 4 — schema DDL
DDL = """
CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    full_name   TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    sku         TEXT NOT NULL,
    qty         INT CHECK (qty > 0),
    total_cents BIGINT,
    placed_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_placed ON orders USING BRIN(placed_at);
"""

with engine.begin() as conn:
    for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
        conn.execute(text(stmt))

# Confirm
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' ORDER BY tablename
    """)).fetchall()
    print("✅ Tables in public schema:")
    for r in rows:
        print(f"   · {r.tablename}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 3b — Seed sample data

# COMMAND ----------

# Cell 5 — seed data (idempotent: ON CONFLICT skips dupes)
SEED = """
INSERT INTO users (email, full_name) VALUES
  ('alice@example.com', 'Alice Anderson'),
  ('bob@example.com',   'Bob Brown'),
  ('carla@example.com', 'Carla Cruz')
ON CONFLICT (email) DO NOTHING;

INSERT INTO orders (user_id, sku, qty, total_cents) VALUES
  ((SELECT id FROM users WHERE email='alice@example.com'), 'SKU-101', 2, 4998),
  ((SELECT id FROM users WHERE email='alice@example.com'), 'SKU-205', 1, 1299),
  ((SELECT id FROM users WHERE email='bob@example.com'),   'SKU-101', 5, 12495),
  ((SELECT id FROM users WHERE email='carla@example.com'), 'SKU-309', 3, 8997);
"""

with engine.begin() as conn:
    for stmt in [s.strip() for s in SEED.split(";") if s.strip()]:
        conn.execute(text(stmt))

with engine.connect() as conn:
    nu = conn.execute(text("SELECT count(*) FROM users")).scalar()
    no = conn.execute(text("SELECT count(*) FROM orders")).scalar()

print(f"✅ Seeded {nu} users and {no} orders")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Register the project in Unity Catalog
# MAGIC
# MAGIC The single most important command in this lab. Until you run it, your project is just a managed Postgres. After you run it, DBSQL, AI/BI, Genie, the catalog explorer, synced tables, and UC permissions all see it.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `permission denied: must be metastore admin` | UC requires elevated rights for catalog creation | Ask an admin to grant `CREATE CATALOG` on the metastore, or have them run the SQL cell |
# MAGIC | Catalog created but `SHOW CATALOGS` doesn't show it | Catalog browser cache | Refresh Catalog Explorer; re-run verification cell |

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Cell 6 — register Lakebase as a Unity Catalog catalog
# MAGIC CREATE CATALOG IF NOT EXISTS lakebase_tutorial
# MAGIC USING DATABASE `lakebase-tutorial.databricks_postgres`
# MAGIC OPTIONS (description = 'OLTP catalog for the Lakebase tutorial');

# COMMAND ----------

# Cell 7 — verify the catalog from the Python side
catalogs = spark.sql("SHOW CATALOGS").toPandas()
present = "lakebase_tutorial" in catalogs["catalog"].values
print(f"✅ Catalog 'lakebase_tutorial' visible to Spark: {present}")

# Query the Lakebase table FROM DBSQL via federation
df = spark.sql("SELECT * FROM lakebase_tutorial.public.users ORDER BY id")
df.show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Branching: create a `dev` branch, destroy data on it, verify `main`
# MAGIC
# MAGIC The lab's "wow" moment. You'll create a fully writable copy of your database in seconds, run a destructive `DELETE`, and verify `main` is untouched.
# MAGIC
# MAGIC ### Step 5a — Create the branch

# COMMAND ----------

# Cell 8 — create the dev branch from main
from databricks.sdk.service.database import DatabaseBranch

print("Creating branch 'dev' from 'main'...")
branch = w.database.create_database_branch(
    project_name=TUTORIAL["project_name"],
    branch=DatabaseBranch(name="dev"),
)

# Poll until the branch is READY
for i in range(30):
    b = w.database.get_database_branch(
        project_name=TUTORIAL["project_name"], branch_name="dev"
    )
    if b.state == "READY":
        branch = b
        break
    time.sleep(3)

print(f"✅ Branch 'dev' ready")
print(f"   Endpoint: {branch.read_write_dns}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 5b — Open an engine on `dev` and destroy data

# COMMAND ----------

# Cell 9 — connect to the dev branch
cred_dev = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)

dev_url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred_dev.token}"
    f"@{branch.read_write_dns}:5432/databricks_postgres?sslmode=require"
)
dev_engine = create_engine(dev_url, pool_pre_ping=True)

# Confirm dev sees the same seed data
with dev_engine.connect() as c:
    rows = c.execute(text("SELECT count(*) FROM users")).scalar()
    print(f"   dev branch — users count BEFORE delete: {rows}")

# DESTRUCTIVE: wipe all users on dev only
with dev_engine.begin() as c:
    c.execute(text("DELETE FROM orders"))
    c.execute(text("DELETE FROM users"))

with dev_engine.connect() as c:
    rows = c.execute(text("SELECT count(*) FROM users")).scalar()
    print(f"   dev branch — users count AFTER delete:  {rows}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 5c — Verify `main` is untouched

# COMMAND ----------

# Cell 10 — sanity check on main
with engine.connect() as c:
    nu = c.execute(text("SELECT count(*) FROM users")).scalar()
    no = c.execute(text("SELECT count(*) FROM orders")).scalar()
print(f"✅ main branch — users={nu}, orders={no} (unchanged)")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 5d — Drop the dev branch

# COMMAND ----------

# Cell 11 — clean up the dev branch
w.database.delete_database_branch(
    project_name=TUTORIAL["project_name"], branch_name="dev"
)
print("✅ Branch 'dev' deleted")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Point-in-Time Restore via branch-from-timestamp
# MAGIC
# MAGIC Because the storage layer keeps every page version for `retention_window_in_days`, "restore to 5 minutes ago" is just creating a branch from a past timestamp. We'll deliberately corrupt a row, then recover it without `pg_dump`.
# MAGIC
# MAGIC ### Step 6a — Mark the safe-recovery point

# COMMAND ----------

# Cell 12 — capture a "known good" timestamp BEFORE we corrupt anything
from datetime import datetime, timezone, timedelta

with engine.connect() as c:
    pre_corrupt_email = c.execute(text(
        "SELECT email FROM users WHERE id = 1"
    )).scalar()

print(f"Original email for user id=1: {pre_corrupt_email}")
print("Saving the current time. We'll branch from this point shortly.")
T_GOOD = datetime.now(timezone.utc)
print(f"   T_GOOD = {T_GOOD.isoformat()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 6b — Wait, then corrupt

# COMMAND ----------

# Cell 13 — wait 60s so T_GOOD is firmly in the past, then corrupt
print("Sleeping 65s so T_GOOD is comfortably in retention...")
time.sleep(65)

with engine.begin() as c:
    c.execute(text("UPDATE users SET email = 'oops' WHERE id = 1"))

with engine.connect() as c:
    bad = c.execute(text("SELECT email FROM users WHERE id = 1")).scalar()
print(f"⚠ Corrupted: user id=1 email is now '{bad}'")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 6c — Branch from T_GOOD

# COMMAND ----------

# Cell 14 — create a recovery branch from T_GOOD
print(f"Creating branch 'recovery' FROM TIMESTAMP {T_GOOD.isoformat()}...")
recovery = w.database.create_database_branch(
    project_name=TUTORIAL["project_name"],
    branch=DatabaseBranch(
        name="recovery",
        parent_branch_name="main",
        parent_timestamp=T_GOOD,  # branch-from-timestamp
    ),
)
for i in range(30):
    b = w.database.get_database_branch(
        project_name=TUTORIAL["project_name"], branch_name="recovery"
    )
    if b.state == "READY":
        recovery = b; break
    time.sleep(3)

print(f"✅ Branch 'recovery' ready @ {recovery.read_write_dns}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 6d — Verify recovery and copy back

# COMMAND ----------

# Cell 15 — open recovery, confirm the original email is intact, then copy it back
cred_r = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)
recovery_url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred_r.token}"
    f"@{recovery.read_write_dns}:5432/databricks_postgres?sslmode=require"
)
recovery_engine = create_engine(recovery_url, pool_pre_ping=True)

with recovery_engine.connect() as c:
    good_email = c.execute(text(
        "SELECT email FROM users WHERE id = 1"
    )).scalar()
print(f"   recovery branch has email: '{good_email}'")
assert good_email == pre_corrupt_email, "recovery did not preserve the original!"

# Copy the good value back to main
with engine.begin() as c:
    c.execute(text("UPDATE users SET email = :e WHERE id = 1"), {"e": good_email})

with engine.connect() as c:
    fixed = c.execute(text("SELECT email FROM users WHERE id = 1")).scalar()
print(f"✅ main fixed: user id=1 email is now '{fixed}'")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 6e — Drop the recovery branch

# COMMAND ----------

# Cell 16 — clean up
w.database.delete_database_branch(
    project_name=TUTORIAL["project_name"], branch_name="recovery"
)
print("✅ Branch 'recovery' deleted")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Final verification & cleanup

# COMMAND ----------

# Cell 17 — final checklist
checks = []

# 1. Project exists and is READY
try:
    p = w.database.get_database_project(TUTORIAL["project_name"])
    checks.append(("Project provisioned", f"state={p.state}", p.state == "READY"))
except Exception as e:
    checks.append(("Project provisioned", str(e)[:30], False))

# 2. Engine connects and SELECT version() works
try:
    with engine.connect() as c:
        v = c.execute(text("SELECT version()")).scalar()
    checks.append(("Engine + SQL works", v.split(",")[0], "PostgreSQL 17" in v))
except Exception as e:
    checks.append(("Engine + SQL works", str(e)[:30], False))

# 3. Schema exists with seed data
try:
    with engine.connect() as c:
        nu = c.execute(text("SELECT count(*) FROM users")).scalar()
        no = c.execute(text("SELECT count(*) FROM orders")).scalar()
    checks.append(("Schema + seed data", f"users={nu}, orders={no}", nu >= 3 and no >= 4))
except Exception as e:
    checks.append(("Schema + seed data", str(e)[:30], False))

# 4. UC catalog visible
try:
    cats = spark.sql("SHOW CATALOGS").toPandas()["catalog"].values
    ok = "lakebase_tutorial" in cats
    checks.append(("UC catalog registered", "lakebase_tutorial" if ok else "missing", ok))
except Exception as e:
    checks.append(("UC catalog registered", str(e)[:30], False))

# 5. Federation SELECT works
try:
    n = spark.sql("SELECT count(*) AS n FROM lakebase_tutorial.public.users").collect()[0]["n"]
    checks.append(("UC federation works", f"n={n}", n >= 3))
except Exception as e:
    checks.append(("UC federation works", str(e)[:30], False))

# 6. PIT restore worked
try:
    with engine.connect() as c:
        e = c.execute(text("SELECT email FROM users WHERE id=1")).scalar()
    checks.append(("PIT restore success", e, e == "alice@example.com"))
except Exception as ex:
    checks.append(("PIT restore success", str(ex)[:30], False))

# Print
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
    print(f"\n🎯 ALL {total} CHECKS PASSED — Module 3 complete.")
else:
    print(f"\n⚠ {passed}/{total} passed. Resolve the ❌ rows before Module 4.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Cleanup (optional — keep the project for Module 4)

# COMMAND ----------

# Cell 18 — cleanup
# Set CLEANUP = False if you want to keep the project for Module 4 hands-on.
# Module 4 builds synced tables INTO this project, so most readers KEEP it.
CLEANUP = False

if CLEANUP:
    print("Dropping UC catalog...")
    spark.sql("DROP CATALOG IF EXISTS lakebase_tutorial CASCADE")

    print(f"Deleting Lakebase project '{TUTORIAL['project_name']}'...")
    w.database.delete_database_project(name=TUTORIAL["project_name"])
    print("✅ Cleanup complete — billing stops within minutes.")
else:
    print("⏸ Cleanup skipped. The project will be reused in Module 4.")
    print(f"   Project: {TUTORIAL['project_name']}")
    print(f"   Endpoint: {project.read_write_dns}")
    print()
    print("   To delete later, set CLEANUP=True and re-run this cell, OR")
    print("   visit Compute → Lakebase in the workspace UI.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module 3 — complete
# MAGIC
# MAGIC You finished six theory topics (3.1–3.6) and the hands-on lab. You now have:
# MAGIC
# MAGIC - A live Lakebase project, registered in UC, with a real schema and seed data
# MAGIC - First-hand experience with branching and point-in-time restore — the two features that most differentiate Lakebase from RDS
# MAGIC - The OAuth-refresh pattern that every later lab and every production app needs
# MAGIC
# MAGIC **Next up: Module 4** — *Sync, Federate & Unify with Unity Catalog.* You'll learn the three integration patterns (UC → PG synced tables, PG → UC Lakehouse Sync, and live federation), then build a real reverse-ETL replacement that pushes Delta data into the Lakebase project you just created.
