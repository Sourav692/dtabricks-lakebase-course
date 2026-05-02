# Module 08 · Capstone · Phase 2 — Hydrate from the Lakehouse

> **Type:** Hands-on · **Duration:** ~45 minutes · **Format:** Databricks notebook
> **Purpose:** Wire `customers` and `orders` into Lakebase as **synced tables** sourced from `main.gold.*` Delta. Add Postgres-side indexes for the app's query patterns. Prove sub-30 ms row reads. Verify Delta-side writes propagate within ~10 seconds. Phase 4's agent uses these tables through the `get_orders` tool.

---

## Why this phase exists

Phase 1 set the substrate. Phase 2 is where Lakebase stops being an empty Postgres and starts being the operational read surface for the app. Two things have to land before we can talk to the agent:

1. **Customer + order data** has to flow from the lakehouse (where analysts curate it) into Lakebase (where the app reads it) — without reverse-ETL pipelines, without a nightly batch, without writing connector code.
2. **The query patterns** the agent will use — last-N orders by customer, range scan over `placed_at` — have to return in single-digit milliseconds, every time.

Synced tables solve (1) with one SDK call per table, in CONTINUOUS mode, freshness measured in seconds. Postgres-side indexes solve (2) — the synced table ships with the PK index; you add the rest.

This phase walks eight steps:

1. **Setup** — reconnect to the Phase 1 project, open an engine
2. **Build seed Delta sources** — `main.gold.customers` and `main.gold.orders` with PK + CDF
3. **Create synced table** — `customers_synced` (CONTINUOUS, PK = `customer_id`)
4. **Verify** rows landed in Lakebase
5. **Add Postgres indexes** — B-tree on `customer_id`, BRIN on `placed_at`
6. **Freshness test** — write to Delta, watch it appear in Lakebase
7. **Latency test** — confirm last-10-orders SELECT returns under 30 ms
8. **Final verification** — six green checks before Phase 3

> **Run mode.** Top-to-bottom. Cells are Python with `%sql` where appropriate. Re-runnable — every Delta `CREATE` uses `CREATE TABLE IF NOT EXISTS` and synced-table calls are idempotent.

---

## Prerequisites before you start

You need:

- ✅ **Phase 1 passed** with all 6 ✅ checks. The `askmyorders` project, the `askmyorders_db` UC catalog, and the seven base tables on `main` must be alive.
- ✅ `CREATE TABLE` privilege on `main.gold` (or use a different schema and update `SOURCE_SCHEMA`)
- An attached compute cluster (serverless is fine, recommended)

You do **not** need:

- New compute size — `CU_1` from Phase 1 handles the entire capstone
- Streaming sources — the seed data here is small and committed in MERGE statements

---

## Step 1 — Setup: reconnect to the Phase 1 project

No new infrastructure. Same project, same UC catalog, same engine pattern as Phase 1 Cell 4.

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()
```

### Cell 2 — reconnect to the askmyorders project

```python
import os, uuid, time
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

CAPSTONE = {
    "project_name":   "askmyorders",
    "uc_catalog":     "askmyorders_db",
    "user":           w.current_user.me().user_name,
    "host":           w.config.host,
    "source_catalog": "main",
    "source_schema":  "gold",
}

existing = {p.name: p for p in w.database.list_database_projects()}
if CAPSTONE["project_name"] not in existing:
    raise RuntimeError(
        f"Project '{CAPSTONE['project_name']}' not found. Run Phase 1 first."
    )
project = existing[CAPSTONE["project_name"]]
assert project.state == "READY"

def make_engine(branch: str = "main"):
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[CAPSTONE["project_name"]],
    )
    db_name = CAPSTONE["project_name"] if branch == "main" \
              else f"{CAPSTONE['project_name']}_{branch}"
    url = (
        f"postgresql+psycopg://{CAPSTONE['user']}:{cred.token}"
        f"@{project.read_write_dns}:5432/{db_name}?sslmode=require"
    )
    return create_engine(url, pool_pre_ping=True, pool_size=2)

engine = make_engine("main")

with engine.connect() as conn:
    user = conn.execute(text("SELECT current_user")).scalar()
print(f"✅ Reconnected to '{CAPSTONE['project_name']}'.main as {user}")
```

---

## Step 2 — Build the Delta sources

In a real Northwind scenario these tables already exist in `main.gold.*`. For the lab we create them ourselves with a small but representative dataset, plus the two prerequisites synced tables require:

- **Primary key constraint** — synced tables refuse to start without one
- **Change Data Feed (CDF) enabled** — the Lakebase pipeline reads it to detect changes

> **The PK + CDF requirement is the #1 reason synced-table pipelines fail to provision.** Forget either, and the manager state stays `PROVISIONING` forever. Verify both with the assertion at the end of this step.

### Cell 3 — create seed Delta sources

```python
sc = CAPSTONE["source_catalog"]
ss = CAPSTONE["source_schema"]

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {sc}.{ss}")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {sc}.{ss}.customers (
    customer_id  STRING NOT NULL,
    name         STRING,
    email        STRING,
    region       STRING,
    tier         STRING,
    created_at   TIMESTAMP,
    CONSTRAINT customers_pk PRIMARY KEY (customer_id) RELY
)
USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {sc}.{ss}.orders (
    order_id     STRING NOT NULL,
    customer_id  STRING NOT NULL,
    sku          STRING,
    qty          INT,
    amount       DECIMAL(10,2),
    status       STRING,
    placed_at    TIMESTAMP,
    CONSTRAINT orders_pk PRIMARY KEY (order_id) RELY
)
USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

# Seed data — 5 customers, 12 orders, idempotent via MERGE
spark.sql(f"""
MERGE INTO {sc}.{ss}.customers AS t
USING (
    SELECT * FROM VALUES
        ('c001', 'Alice Chen',   'alice@example.com', 'EMEA', 'gold',     TIMESTAMP'2024-08-01 09:00:00'),
        ('c002', 'Bob Patel',    'bob@example.com',   'AMER', 'silver',   TIMESTAMP'2024-09-15 13:20:00'),
        ('c003', 'Carla Romero', 'carla@example.com', 'AMER', 'gold',     TIMESTAMP'2024-11-04 18:45:00'),
        ('c004', 'Devesh Singh', 'devesh@example.com','APAC', 'platinum', TIMESTAMP'2024-12-22 10:10:00'),
        ('c005', 'Eli Schwartz', 'eli@example.com',   'EMEA', 'silver',   TIMESTAMP'2025-01-30 08:00:00')
    AS s(customer_id, name, email, region, tier, created_at)
) AS s
ON  t.customer_id = s.customer_id
WHEN NOT MATCHED THEN INSERT *
""")

spark.sql(f"""
MERGE INTO {sc}.{ss}.orders AS t
USING (
    SELECT * FROM VALUES
        ('o001','c001','SKU-A12',1, 49.99,'shipped',  TIMESTAMP'2025-02-01 10:00:00'),
        ('o002','c001','SKU-B07',2,119.50,'shipped',  TIMESTAMP'2025-02-14 14:30:00'),
        ('o003','c002','SKU-A12',1, 49.99,'returned', TIMESTAMP'2025-02-18 09:15:00'),
        ('o004','c002','SKU-C44',1, 24.00,'shipped',  TIMESTAMP'2025-03-02 16:00:00'),
        ('o005','c003','SKU-B07',1, 59.75,'shipped',  TIMESTAMP'2025-03-12 11:00:00'),
        ('o006','c003','SKU-D11',3, 89.97,'pending',  TIMESTAMP'2025-04-01 12:00:00'),
        ('o007','c004','SKU-A12',2, 99.98,'shipped',  TIMESTAMP'2025-04-10 13:30:00'),
        ('o008','c004','SKU-E22',1,199.00,'shipped',  TIMESTAMP'2025-04-18 09:45:00'),
        ('o009','c005','SKU-B07',1, 59.75,'shipped',  TIMESTAMP'2025-04-25 17:20:00'),
        ('o010','c005','SKU-C44',2, 48.00,'pending',  TIMESTAMP'2025-04-28 10:10:00'),
        ('o011','c001','SKU-D11',1, 29.99,'shipped',  TIMESTAMP'2025-04-30 08:30:00'),
        ('o012','c003','SKU-A12',1, 49.99,'shipped',  TIMESTAMP'2025-05-01 14:00:00')
    AS s(order_id, customer_id, sku, qty, amount, status, placed_at)
) AS s
ON  t.order_id = s.order_id
WHEN NOT MATCHED THEN INSERT *
""")

# Verify PK + CDF
def assert_pk_and_cdf(table):
    desc = spark.sql(f"DESCRIBE EXTENDED {sc}.{ss}.{table}").collect()
    text_blob = "\n".join(str(r) for r in desc).lower()
    assert "primary key" in text_blob or "constraint" in text_blob, f"{table} missing PK"
    cdf = spark.sql(f"""
        SELECT properties['delta.enableChangeDataFeed'] AS cdf
        FROM (DESCRIBE DETAIL {sc}.{ss}.{table})
    """).collect()
    assert cdf and (cdf[0]['cdf'] == 'true'), f"{table} CDF not enabled"

assert_pk_and_cdf("customers")
assert_pk_and_cdf("orders")

n_c = spark.sql(f"SELECT count(*) FROM {sc}.{ss}.customers").collect()[0][0]
n_o = spark.sql(f"SELECT count(*) FROM {sc}.{ss}.orders").collect()[0][0]
print(f"✅ Delta sources ready. customers={n_c}  orders={n_o}  PK + CDF verified.")
```

**Expected output:**

```
✅ Delta sources ready. customers=5  orders=12  PK + CDF verified.
```

---

## Step 3 — Create the synced tables

One SDK call per table. Each call provisions a managed CDC pipeline that pushes Delta-side changes into Lakebase. The `CONTINUOUS` mode keeps the pipeline running so freshness is on the order of seconds.

**Provisioning takes ~60–120 seconds per table.** The cell polls until `state=ACTIVE`.

> **Concept reminder:** the synced table you query inside Postgres is a *real* Postgres table — same indexes, same `pg_catalog`, same JOIN behavior. The "managed" part is the pipeline that keeps it fresh. From the app's perspective, it's just Postgres.

### Cell 4 — create the synced tables

```python
from databricks.sdk.service.database import (
    SyncedDatabaseTable, SyncedTableSpec, NewPipelineSpec, SyncedTableSchedulingPolicy,
)

target_catalog = CAPSTONE["uc_catalog"]
target_schema  = "public"
src_full = lambda t: f"{CAPSTONE['source_catalog']}.{CAPSTONE['source_schema']}.{t}"

def get_synced_state(name):
    try:
        st = w.database.get_synced_database_table(
            name=f"{target_catalog}.{target_schema}.{name}"
        )
        return st.data_synchronization_status.detailed_state \
               if st.data_synchronization_status else "UNKNOWN"
    except Exception:
        return None

def create_synced_idempotent(name, src_table, primary_keys, timeseries_key=None):
    state = get_synced_state(name)
    if state and state != "FAILED":
        print(f"⏸ {name} already exists — state={state}")
        return

    print(f"⏳ Creating synced table {name} from {src_full(src_table)}...")
    spec = SyncedTableSpec(
        source_table_full_name=src_full(src_table),
        primary_key_columns=primary_keys,
        timeseries_key=timeseries_key,
        scheduling_policy=SyncedTableSchedulingPolicy.CONTINUOUS,
        new_pipeline_spec=NewPipelineSpec(
            storage_catalog=CAPSTONE["source_catalog"],
            storage_schema="default",
        ),
    )
    w.database.create_synced_database_table(
        synced_table=SyncedDatabaseTable(
            name=f"{target_catalog}.{target_schema}.{name}",
            spec=spec,
        )
    )
    deadline = time.time() + 240
    while time.time() < deadline:
        state = get_synced_state(name)
        if state == "ACTIVE":
            break
        print(f"   ...state={state}")
        time.sleep(10)
    print(f"✅ {name} state={state}")

create_synced_idempotent("customers_synced", src_table="customers",
                         primary_keys=["customer_id"])

create_synced_idempotent("orders_synced", src_table="orders",
                         primary_keys=["order_id"], timeseries_key="placed_at")
```

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| State stuck on `PROVISIONING` >5 min | PK or CDF missing on Delta source | Re-run Cell 3; the assertion would have caught it |
| `source_table not found` | Wrong catalog/schema in `SOURCE_SCHEMA` | Update `CAPSTONE["source_schema"]` and re-run |
| `pipeline_id not assigned` | Region capacity issue | Open the pipeline in UC Catalog Explorer; contact support |

---

## Step 4 — Verify rows landed in Lakebase

The pipeline's first run does a snapshot, then switches to incremental CDC. After `state=ACTIVE`, the snapshot completes within seconds for our small dataset. Both tables should have the same row counts as the Delta sources.

### Cell 5 — verify sync from the Postgres side

```python
with engine.connect() as conn:
    pg_c = conn.execute(text("SELECT count(*) FROM customers_synced")).scalar()
    pg_o = conn.execute(text("SELECT count(*) FROM orders_synced")).scalar()
    sample = conn.execute(text("""
        SELECT customer_id, name, region, tier
        FROM customers_synced
        ORDER BY customer_id LIMIT 3
    """)).fetchall()

print(f"Lakebase rows: customers_synced={pg_c}  orders_synced={pg_o}")
print()
print("Sample customers:")
for r in sample:
    print(f"   · {r.customer_id:<6} {r.name:<18} {r.region}  {r.tier}")

assert pg_c == 5
assert pg_o == 12
print("\n✅ Row counts match Delta sources.")
```

---

## Step 5 — Add Postgres-side indexes for the app's query patterns

The synced table comes with a primary-key index (`order_id`) automatically. Phase 4's agent calls `get_orders(customer_id)` — which would scan without help. We add:

- **B-tree on `customer_id`** — equality lookup for the agent's tool
- **BRIN on `placed_at`** — range scan for the customer-context panel ("last 30 days")

> **Why BRIN, not B-tree, for `placed_at`?** Time-series data is inserted in roughly chronological order. BRIN stores one summary block per range of pages — tiny on disk, fast for range scans. A B-tree would be 50× larger for the same query speed.

### Cell 6 — add indexes on the synced table

```python
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS orders_synced_customer_idx
    ON orders_synced (customer_id);

CREATE INDEX IF NOT EXISTS orders_synced_placed_brin
    ON orders_synced USING BRIN (placed_at);
"""
with engine.begin() as conn:
    for stmt in INDEX_DDL.split(";"):
        if stmt.strip():
            conn.execute(text(stmt))

with engine.connect() as conn:
    idx = conn.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE tablename = 'orders_synced'
        ORDER BY indexname
    """)).scalars().all()

print("Indexes on orders_synced:")
for i in idx:
    print(f"   · {i}")
```

---

## Step 6 — Freshness test (write to Delta, watch Lakebase update)

The headline claim of synced tables is "writes propagate in seconds." Verify it on your own data. We update an existing order, then poll Postgres until the change shows.

### Cell 7 — freshness test

```python
import datetime as dt

# 1. Snapshot the current value in Lakebase
with engine.connect() as conn:
    before = conn.execute(text(
        "SELECT status FROM orders_synced WHERE order_id='o010'"
    )).scalar()
print(f"Lakebase before:  o010.status = {before!r}")

# 2. Write to Delta
spark.sql(f"""
    UPDATE {CAPSTONE['source_catalog']}.{CAPSTONE['source_schema']}.orders
    SET status = 'shipped'
    WHERE order_id = 'o010'
""")
write_at = dt.datetime.now()
print(f"⏱ Delta write committed at {write_at.isoformat()}")

# 3. Poll Lakebase
print("Polling Lakebase for the new value...")
deadline = time.time() + 30
seen_at = None
while time.time() < deadline:
    with engine.connect() as conn:
        cur = conn.execute(text(
            "SELECT status FROM orders_synced WHERE order_id='o010'"
        )).scalar()
    if cur == 'shipped':
        seen_at = dt.datetime.now()
        elapsed = (seen_at - write_at).total_seconds()
        print(f"✅ Synced in {elapsed:.1f}s — Lakebase now shows status='shipped'")
        break
    time.sleep(1)
else:
    print(f"⚠ Did not see the change within 30s. Pipeline may be paused.")

# Restore for repeatability
spark.sql(f"""
    UPDATE {CAPSTONE['source_catalog']}.{CAPSTONE['source_schema']}.orders
    SET status = 'pending'
    WHERE order_id = 'o010'
""")
```

**Expected output:**

```
Lakebase before:  o010.status = 'pending'
⏱ Delta write committed at 2026-05-02T10:48:00.123
Polling Lakebase for the new value...
✅ Synced in 4.2s — Lakebase now shows status='shipped'
```

---

## Step 7 — Latency test (the app's query pattern)

The `get_orders` tool in Phase 4 will run roughly this query for every chat turn that asks about orders. It must be fast — under 30 ms is the bar. Run it 5 times to amortize cold-cache effects, then report the average.

### Cell 8 — measure last-10-orders latency

```python
QUERY = text("""
    SELECT order_id, sku, qty, amount, status, placed_at
    FROM orders_synced
    WHERE customer_id = :cid
    ORDER BY placed_at DESC
    LIMIT 10
""")

import time as _t
n_runs = 5
elapsed = []
with engine.connect() as conn:
    for i in range(n_runs):
        t0 = _t.perf_counter()
        rows = conn.execute(QUERY, {"cid": "c001"}).fetchall()
        elapsed.append((_t.perf_counter() - t0) * 1000)

avg = sum(elapsed) / n_runs
print(f"Last-10-orders for c001 — {len(rows)} rows")
print(f"Per-run latency (ms): {[round(e,1) for e in elapsed]}")
print(f"Average: {avg:.1f} ms   (target: < 30 ms)")
```

---

## Step 8 — Final verification — six green checks

### Cell 9 — verification checklist

```python
checks = []

# 1. Both synced tables ACTIVE
for tn in ("customers_synced", "orders_synced"):
    state = get_synced_state(tn)
    checks.append((f"{tn} state=ACTIVE", state == "ACTIVE"))

# 2. Row counts match
with engine.connect() as conn:
    pg_c = conn.execute(text("SELECT count(*) FROM customers_synced")).scalar()
    pg_o = conn.execute(text("SELECT count(*) FROM orders_synced")).scalar()
checks.append((f"customers row count ({pg_c})", pg_c == 5))
checks.append((f"orders row count ({pg_o})",    pg_o == 12))

# 3. Indexes present
with engine.connect() as conn:
    idx = set(conn.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE tablename='orders_synced'
    """)).scalars().all())
need = {"orders_synced_customer_idx", "orders_synced_placed_brin"}
checks.append((f"orders_synced indexes ({len(need & idx)}/{len(need)})", need.issubset(idx)))

# 4. Latency ≤ 30 ms
checks.append((f"last-10-orders latency {avg:.1f}ms ≤ 30ms", avg <= 30))

print("=" * 60)
print(f"  PHASE 2 VERIFICATION — synced tables")
print("=" * 60)
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")

passed = sum(1 for _, ok in checks if ok)
total  = len(checks)
print("=" * 60)
if passed == total:
    print(f"  🎯 ALL {total} CHECKS PASSED — proceed to Phase 3")
else:
    print(f"  ⚠ {passed}/{total} passed")
print("=" * 60)
```

### Troubleshooting reference

| ❌ Row | Most common cause | Resolution |
|---|---|---|
| state=ACTIVE | PK or CDF missing on Delta source | Re-run Cell 3; the `assert_pk_and_cdf` check would have caught it |
| state stuck `PROVISIONING` >5 min | Region capacity issue | Check pipeline page in UC Catalog Explorer; contact support |
| Row count mismatch | First snapshot still in flight | Wait 30s and re-run Cell 5 |
| Indexes | Cell 6 transaction rolled back | Re-run Cell 6; the DDL is idempotent |
| Latency > 30 ms | First query is cold-cache; runs 2–5 warm up | Look at runs 2–5, not run 1 |

---

## What you've accomplished

If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 2 is complete and you have:

- ✅ Two CONTINUOUS synced tables — `customers_synced` and `orders_synced` — landing in Lakebase from `main.gold.*` Delta with no reverse-ETL
- ✅ Verified PK + CDF on both Delta sources — the prerequisites the synced-table pipeline checks before starting
- ✅ Postgres-side B-tree + BRIN indexes for the agent's query patterns
- ✅ Measured freshness — Delta writes appear in Lakebase within seconds (you watched it happen)
- ✅ Measured latency — last-10-orders for one customer returns under 30 ms

The agent's `get_orders` tool in Phase 4 is now backed by real, indexed, sub-30-ms data.

**Do not run cleanup.** Phase 3 needs the same project, the same UC catalog, and the empty `kb_documents` table from Phase 1.

> **Next:** open `Module_08_Phase_3_Knowledge_Base.py` and load the support documents into pgvector.
