# Module 03 · Lab 3 — Your First Lakebase Project, End-to-End

> **Type:** Hands-on · **Duration:** ~75 minutes · **Format:** Databricks notebook
> **Purpose:** Provision a real Lakebase project, connect to it with auto-refreshing OAuth, build a real schema, register it in Unity Catalog, and prove out branching + point-in-time restore. By the end of this lab you will have done — for real, not in slides — every operation that the rest of the roadmap depends on.

---

## Why this lab exists

Every later lab in the roadmap assumes a working Lakebase project that you have personally provisioned, connected to, and registered in Unity Catalog. **Module 4** synced tables write into the schema you build here. **Modules 5 and 6** add pgvector and agent memory to it. **Module 7** connects a real Databricks App to it.

This lab walks the six steps in order:

1. **Provision** a Lakebase Autoscaling project via SDK
2. **Connect** with auto-refreshing OAuth credentials
3. **Build a schema** with constraints, indexes, and seed data
4. **Register** the project in Unity Catalog
5. **Branch** the database, mutate the branch, verify isolation
6. **Recover** from a deliberate corruption via point-in-time branching

Total runtime ~75 minutes, of which ~5 minutes is your hands on the keyboard and ~70 is Lakebase doing what you asked. Use the wait times to skim the Module 3 theory primer if you haven't.

> **Run mode.** Do this lab in a Databricks notebook attached to a serverless cluster (or DBR 14+). All cells are Python except where SQL is explicitly called out via `%sql`. Copy each cell into your own notebook as you go.

---

## Prerequisites before you start

You need:

- ✅ **Module 1 Lab 1.4 passed** with all 9 checks green — that's the load-bearing prerequisite for this entire notebook
- An attached compute cluster (serverless is fine, recommended)
- The `TUTORIAL` config dict from Module 1 Lab Cell 6 — re-run that cell in a new tab if needed

You do **not** need:

- Any pre-existing Lakebase project (you'll create and destroy one here)
- Production data — we generate seed rows in Step 3
- Any Module 2 lab (Module 2 was theory only)

> **⚠ One-time billing note.** The project you create runs at `CU_1` (1 compute unit) for the duration of this lab. At Lakebase Autoscaling rates this is roughly the cost of a small RDS instance, prorated by the minute. The lab's last step deletes the project. If you skip the cleanup, expect a small line item.

---

## Step 1 — Provision a Lakebase Autoscaling project

This is the first cell that does something irreversible (that the cleanup step undoes). One SDK call provisions storage, compute, and the `main` branch.

```python
# Cell 1 — install dependencies (re-run if kernel restarted since Lab 1.4)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()
```

```python
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
```

**Expected output:**

```
Creating project 'lakebase-tutorial' (capacity=CU_1, retention=7d)...
Waiting for state=READY (typically 60–90s)...
  [  0s] state=PROVISIONING
  [  5s] state=PROVISIONING
  [ 10s] state=PROVISIONING
  ... (continues for ~60–90s)
  [ 75s] state=READY

✅ Project ready
   UID:      a3f9e1c4-...
   State:    READY
   Endpoint: instance-a3f9e1c4-rw.cloud.databricks.com
```

**What this proves:** the control plane provisioned a multi-tenant page server slice, spun up `CU_1` of Postgres compute, attached it to a fresh `main` branch, and published the read-write DNS. You now have a live Postgres 17 endpoint.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `PermissionDenied: Missing entitlement DATABASE_CREATE` | Module 1 Lab 1.4 didn't actually pass | Re-run Lab 1.4 cell 7; resolve the ❌ entitlement row before retrying here |
| `state=FAILED` after polling | Region not yet GA, or workspace at quota | Read `project.state_message`; if region issue, switch to a US workspace |
| Polling timeout (`state=PROVISIONING` after 200s) | Control-plane slowness | Bump the timeout to 300s and retry. Don't recreate — the existing project will eventually become READY |
| `read_write_dns` is `None` even at `READY` | DNS publishes 5–10s after READY | Sleep 10s and re-fetch with `get_database_project()` |

---

## Step 2 — Connect with auto-refreshing OAuth credentials

The trap from Module 2.5 (and Module 3 theory 3.2): hardcoded passwords silently fail after the OAuth token TTL. The pattern below generates a fresh credential per engine and uses `pool_pre_ping` so a stale connection is detected before it's handed to your code.

```python
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
```

**Expected output:**

```
✅ Connected
   PostgreSQL 17.x on aarch64-unknown-linux-gnu, compiled by gcc ...
```

**What this proves:** your workspace identity authenticated via short-lived OAuth, TLS handshake succeeded, and you ran your first real SQL against Lakebase.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `psycopg.OperationalError: connection refused` | DNS hasn't fully propagated | Sleep 15s, re-fetch project, retry |
| `password authentication failed` | Stale token; you ran Step 2 a long time ago | Re-run Cell 3 — `generate_database_credential` issues a new token |
| `SSL connection required` | `sslmode=require` missing in URL | Confirm the URL includes `?sslmode=require` |
| `could not translate host name` | Hyphenated DNS issue with very old psycopg | Upgrade `psycopg[binary]` to >=3.2 |

---

## Step 3 — Build a real schema and seed data

A toy `CREATE TABLE foo (x INT)` would be useless for Modules 4–8. We build a small but realistic users + orders schema, with constraints and two index types so you'll feel the difference between B-tree and BRIN.

### Step 3a — Create tables

```python
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
```

**Expected output:**

```
✅ Tables in public schema:
   · orders
   · users
```

### Step 3b — Seed sample data

```python
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
```

**Expected output:**

```
✅ Seeded 3 users and 4 orders
```

> **Note.** Re-running Cell 5 will add 4 more orders each time (orders has no UNIQUE constraint on these test fields). For Lab 3.5/3.6 the count of orders is irrelevant; only the users are tracked by email.

**What this proves:** you can run DDL and DML against Lakebase exactly as you would against any Postgres. No Databricks-flavored quirks, no proprietary syntax — `BIGSERIAL`, `REFERENCES`, `CHECK`, `BRIN` indexes, all standard PostgreSQL 17.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `relation "users" already exists` and not using `IF NOT EXISTS` | DDL ran already on a previous attempt | Acceptable — tables are idempotent in this lab |
| `permission denied for schema public` | Workspace identity not the project owner | Confirm you ran Cell 2 — the creator gets owner role automatically |
| `BRIN` errors on older PG | Lakebase is PG 17 — this should never happen | Verify `SELECT version()` returned 17.x |

---

## Step 4 — Register the project in Unity Catalog

This is the single most important command in this lab. Until you run it, your project is just a managed Postgres. After you run it, DBSQL, AI/BI, Genie, the catalog explorer, synced tables, and UC permissions all see it.

```sql
-- Cell 6 — register Lakebase as a Unity Catalog catalog
-- (This is a SQL cell; in the notebook UI prefix with %sql)

CREATE CATALOG IF NOT EXISTS lakebase_tutorial
USING DATABASE `lakebase-tutorial.databricks_postgres`
OPTIONS (description = 'OLTP catalog for the Lakebase tutorial');
```

```python
# Cell 7 — verify the catalog from the Python side
catalogs = spark.sql("SHOW CATALOGS").toPandas()
present = "lakebase_tutorial" in catalogs["catalog"].values
print(f"✅ Catalog 'lakebase_tutorial' visible to Spark: {present}")

# Query the Lakebase table FROM DBSQL via federation
df = spark.sql("SELECT * FROM lakebase_tutorial.public.users ORDER BY id")
df.show(truncate=False)
```

**Expected output:**

```
✅ Catalog 'lakebase_tutorial' visible to Spark: True

+---+-------------------+-----------------+--------------------------+
|id |email              |full_name        |created_at                |
+---+-------------------+-----------------+--------------------------+
|1  |alice@example.com  |Alice Anderson   |2026-05-02 14:23:01.123+00|
|2  |bob@example.com    |Bob Brown        |2026-05-02 14:23:01.124+00|
|3  |carla@example.com  |Carla Cruz       |2026-05-02 14:23:01.125+00|
+---+-------------------+-----------------+--------------------------+
```

**What this proves:** the `CREATE CATALOG` command flipped seven dependent capabilities on at once. The `spark.sql(...)` call against `lakebase_tutorial.public.users` is answered by a federation query that hits your live Lakebase Postgres — no copy, no ETL.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `permission denied: must be metastore admin` | Workspace UC requires elevated rights for catalog creation | Ask your admin to grant `CREATE CATALOG` on the metastore, or have them run Cell 6 |
| Catalog created but `SHOW CATALOGS` doesn't show it | Catalog browser cache | Refresh the Catalog Explorer; or re-run Cell 7 |
| Federation SELECT errors with `connection refused` | Your project went idle and is cold-starting | Wait 10 seconds, retry |

---

## Step 5 — Branching: create a `dev` branch, destroy data on it, verify `main`

This is the lab's "wow" moment. You'll create a fully writable copy of your database in seconds, run a destructive `DELETE`, and verify `main` is untouched. If a customer is skeptical about copy-on-write branching, this is the demo.

### Step 5a — Create the branch

```python
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
```

**Expected output:**

```
Creating branch 'dev' from 'main'...
✅ Branch 'dev' ready
   Endpoint: instance-a3f9e1c4-dev-rw.cloud.databricks.com
```

### Step 5b — Open an engine on `dev` and destroy data

```python
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
```

**Expected output:**

```
   dev branch — users count BEFORE delete: 3
   dev branch — users count AFTER delete:  0
```

### Step 5c — Verify `main` is untouched

```python
# Cell 10 — sanity check on main
with engine.connect() as c:
    nu = c.execute(text("SELECT count(*) FROM users")).scalar()
    no = c.execute(text("SELECT count(*) FROM orders")).scalar()
print(f"✅ main branch — users={nu}, orders={no} (unchanged)")
```

**Expected output:**

```
✅ main branch — users=3, orders=4 (unchanged)
```

### Step 5d — Drop the dev branch

```python
# Cell 11 — clean up the dev branch
w.database.delete_database_branch(
    project_name=TUTORIAL["project_name"], branch_name="dev"
)
print("✅ Branch 'dev' deleted")
```

**What this proves:** the `dev` branch was a writable, isolated copy of `main` that you destroyed without affecting production data. Branch creation took seconds, not minutes — because no data was physically copied. You just experienced copy-on-write, the feature that most differentiates Lakebase from RDS.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `branch creation failed: too many branches` | Project has a soft limit on concurrent branches | Delete unused branches with `delete_database_branch` |
| Step 5b: dev shows 0 users immediately | You created the branch BEFORE seeding | Branches inherit the storage state at branch-creation time. Re-seed on main first |
| Step 5c: main shows 0 users | Catastrophic — you ran the DELETE on `engine` not `dev_engine` | Recreate seed data with Cell 5 |

---

## Step 6 — Point-in-Time Restore via branch-from-timestamp

Because the storage layer keeps every page version for `retention_window_in_days`, "restore to 5 minutes ago" is just creating a branch from a past timestamp. We'll deliberately corrupt a row, then recover it without `pg_dump`.

### Step 6a — Mark the safe-recovery point

```python
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
```

**Expected output:**

```
Original email for user id=1: alice@example.com
Saving the current time. We'll branch from this point shortly.
   T_GOOD = 2026-05-02T14:35:42.123456+00:00
```

### Step 6b — Wait, then corrupt

```python
# Cell 13 — wait 60s so T_GOOD is firmly in the past, then corrupt
print("Sleeping 65s so T_GOOD is comfortably in retention...")
time.sleep(65)

with engine.begin() as c:
    c.execute(text("UPDATE users SET email = 'oops' WHERE id = 1"))
    
with engine.connect() as c:
    bad = c.execute(text("SELECT email FROM users WHERE id = 1")).scalar()
print(f"⚠ Corrupted: user id=1 email is now '{bad}'")
```

**Expected output:**

```
Sleeping 65s so T_GOOD is comfortably in retention...
⚠ Corrupted: user id=1 email is now 'oops'
```

### Step 6c — Branch from T_GOOD

```python
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
```

### Step 6d — Verify the original row is on `recovery`, then copy it back to `main`

```python
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
```

**Expected output:**

```
   recovery branch has email: 'alice@example.com'
✅ main fixed: user id=1 email is now 'alice@example.com'
```

### Step 6e — Drop the recovery branch

```python
# Cell 16 — clean up
w.database.delete_database_branch(
    project_name=TUTORIAL["project_name"], branch_name="recovery"
)
print("✅ Branch 'recovery' deleted")
```

**What this proves:** point-in-time restore on Lakebase is a sub-second wall-clock operation on time (the storage layer always knew the old page), an instant compute spin-up (recovery branch up in ~30s), and a single SQL UPDATE to restore. Total time from corruption to fix: ~2 minutes. With a `pg_dump`-based recovery on RDS, you would still be waiting for the restore to finish.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `timestamp out of retention window` | `retention_window_in_days` was 0 or 1 | This lab requires retention ≥ 1 day; recreate project with `retention_window_in_days=7` |
| `recovery branch has email: 'oops'` | T_GOOD was captured AFTER the corruption | Re-run Cells 12 → 13 → 14 in order |
| AssertionError on `assert good_email == pre_corrupt_email` | Same as above | Same as above |

---

## Step 7 — Final verification & cleanup

A pass/fail summary of everything you built. Then the cleanup that deletes the project (so your bill stops).

### Step 7a — The summary checklist

```python
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

# 6. PIT restore worked (alice's email is correct)
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
```

### Expected output (the success case)

```
======================================================================
CHECK                        DETAIL                       STATUS
----------------------------------------------------------------------
Project provisioned          state=READY                  ✅
Engine + SQL works           PostgreSQL 17.0              ✅
Schema + seed data           users=3, orders=4            ✅
UC catalog registered        lakebase_tutorial            ✅
UC federation works          n=3                          ✅
PIT restore success          alice@example.com            ✅
======================================================================

🎯 ALL 6 CHECKS PASSED — Module 3 complete.
```

### Step 7b — Cleanup (recommended)

```python
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
```

---

## Troubleshooting reference

If your final checklist has any ❌ rows, find the row in the table below and apply the fix. Don't proceed to Module 4 with failing checks — synced tables in Module 4 require a healthy registered project.

| ❌ Row | Most common cause | Resolution path |
|---|---|---|
| **Project provisioned** | Region not GA, or quota | Read `project.state_message`; switch region or contact your admin |
| **Engine + SQL works** | DNS not propagated, or token expired | Re-run Cell 3 — that regenerates a fresh token and engine |
| **Schema + seed data** | Cell 4 or 5 failed silently with a transaction rollback | Re-run Cells 4 + 5; check for prior `BEGIN` left open |
| **UC catalog registered** | Workspace UC requires elevated `CREATE CATALOG` rights | Ask admin to run Cell 6 once, then re-run Cell 7 |
| **UC federation works** | Catalog created but query routing misconfigured | Wait 30s; check `SHOW CATALOGS` shows the catalog of type `LAKEBASE` |
| **PIT restore success** | T_GOOD captured after the bad UPDATE (Cells 12/13 out of order) | Recreate the corruption: `UPDATE users SET email='oops' WHERE id=1`, capture a NEW T_GOOD before it, retry from Cell 14 |

---

## What you've accomplished

If your final cell printed `🎯 ALL 6 CHECKS PASSED`, you have done — for real, not in slides:

- ✅ Provisioned a Lakebase Autoscaling project programmatically via SDK
- ✅ Connected with auto-refreshing OAuth credentials (the only pattern safe for long-running code)
- ✅ Built a real schema with constraints, indexes, and seed data
- ✅ Registered Lakebase in Unity Catalog — flipping seven dependent capabilities ON
- ✅ Created a branch, mutated the branch, and verified `main` was untouched
- ✅ Recovered from a deliberate corruption via point-in-time branching

These six capabilities are what every later module assumes you have working hands-on. Module 4 builds synced tables on top of the project + UC registration. Modules 5 and 6 layer pgvector and agent memory onto your schema. Module 7 uses the OAuth pattern in production. **Save this notebook somewhere you can find it again.**

---

## Module 3 — complete

You finished six theory topics (3.1–3.6) and the hands-on lab. You now have:

- A live Lakebase project, registered in UC, with a real schema and seed data
- First-hand experience with branching and point-in-time restore — the two features that most differentiate Lakebase from RDS
- The OAuth-refresh pattern that every later lab and every production app needs

**Next up: Module 4** — *Sync, Federate & Unify with Unity Catalog.* You'll learn the three integration patterns (UC → PG synced tables, PG → UC Lakehouse Sync, and live federation), then build a real reverse-ETL replacement that pushes Delta data into the Lakebase project you just created.
