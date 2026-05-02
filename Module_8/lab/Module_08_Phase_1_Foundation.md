# Module 08 · Capstone · Phase 1 — Foundation & Provisioning

> **Type:** Hands-on · **Duration:** ~30 minutes · **Format:** Databricks notebook
> **Purpose:** Stand up the `askmyorders` Lakebase project, create a `dev` branch, register in Unity Catalog, and apply the full DDL — operational tables, knowledge-base table, and the four memory tables — in one place. This is the gate for every other phase of the capstone.

---

## Why this phase exists

The capstone (Module 8) is built in five phases that run in strict order. Phase 1 lays the foundation every later phase depends on:

- A live Lakebase project on `CU_1`, registered in Unity Catalog as `askmyorders_db`
- A `dev` branch demonstrating copy-on-write isolation (the demo customers always ask about)
- The full schema for the entire app — operational write tables, the KB table with `vector(1024)`, and the four memory tables — applied on `main` once

If any of these is wrong, Phase 2's synced tables won't provision, Phase 3's vector load will fail dimensionality checks, Phase 4's memory writes will hit missing tables, and Phase 5's app will boot into errors. Get Phase 1 green before opening any other notebook.

This phase walks seven steps:

1. **Setup** — install deps, open the Databricks SDK
2. **Provision** the Lakebase project `askmyorders` (CU_1, 7-day retention, HA off)
3. **Branch** `dev` off `main`
4. **Open an engine** on `main` for DDL work
5. **Apply DDL** — operational tables + KB + memory in one transaction
6. **Register** Lakebase in Unity Catalog as `askmyorders_db`
7. **Final verification** — six green checks before Phase 2

> **Run mode.** Top-to-bottom in a Databricks notebook attached to a serverless cluster (or DBR 14+). All cells are Python or `%sql`. Re-running individual cells is safe — every DDL uses `IF NOT EXISTS`.

---

## Prerequisites before you start

You need:

- ✅ **Modules 1–7 completed** (the conceptual Module 8 walkthrough should be fresh)
- ✅ **Module 1 Lab 1.4 passed** — confirms your workspace can provision Lakebase, embed via Foundation Models, and write to UC
- ✅ **Foundation Models API** enabled in the workspace region (Phase 3 needs it)
- ✅ `CREATE CATALOG` privilege on the metastore (or an admin available to run Cell 7 once)
- An attached compute cluster (serverless is fine, recommended)

You do **not** need:

- The Module 3 `lakebase-tutorial` project — the capstone provisions a fresh `askmyorders` project so customer demos start from a clean slate
- Module 4's federation work — the capstone uses synced tables, configured fresh in Phase 2

> **⚠ Billing note.** Provisioning starts billing for `CU_1` immediately. Subsequent phases (2–5) reuse this same project — do **not** clean up at the end of any phase except after Phase 5 is verified.

---

## Step 1 — Setup: install dependencies and open the SDK

Same pattern as Module 3 Lab 3 Cell 1–2. The SDK is the only required dependency for this phase — the project DDL runs through it.

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()
```

### Cell 2 — open the SDK and pin per-capstone constants

```python
import os, uuid, time
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

CAPSTONE = {
    "project_name": "askmyorders",
    "uc_catalog":   "askmyorders_db",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
    # Foundation Models — used in Phase 3, declared here for the smoke test
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
}

print("CAPSTONE config:")
for k, v in CAPSTONE.items():
    print(f"   {k:<14} = {v}")
```

**What this proves:** the SDK is reachable, the workspace user is correctly resolved, and the constants every later phase will reuse are pinned in one place.

---

## Step 2 — Provision the `askmyorders` Lakebase project

Compute size `CU_1` is enough for the entire capstone — the data is small. Retention is 7 days (the minimum) because we don't need a longer PIT window. HA is off for now; the stretch goal in the Module 8 wrap-up has you turn it on.

Provisioning takes ~60–90 seconds. The cell polls until state is `READY`.

### Cell 3 — provision the Lakebase project (idempotent)

```python
existing = {p.name: p for p in w.database.list_database_projects()}

if CAPSTONE["project_name"] in existing:
    project = existing[CAPSTONE["project_name"]]
    print(f"⏸ Project '{CAPSTONE['project_name']}' already exists — reusing.")
else:
    print(f"⏳ Creating project '{CAPSTONE['project_name']}' (CU_1, 7d retention)...")
    from databricks.sdk.service.database import DatabaseProject

    project = w.database.create_database_project(
        DatabaseProject(
            name=CAPSTONE["project_name"],
            capacity="CU_1",
            retention_window_in_days=7,
        )
    )
    deadline = time.time() + 180
    while project.state != "READY" and time.time() < deadline:
        time.sleep(5)
        project = w.database.get_database_project(name=CAPSTONE["project_name"])
        print(f"   ...state={project.state}")
    print(f"✅ Project READY: {project.read_write_dns}")

assert project.state == "READY"
```

**Expected output:**

```
⏳ Creating project 'askmyorders' (CU_1, 7d retention)...
   ...state=PROVISIONING
   ...state=PROVISIONING
   ...state=READY
✅ Project READY: instance-...lakebase.databricks.com
```

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| State stays `PROVISIONING` >5 min | Region capacity issue | Read `project.state_message`; switch region or contact admin |
| `quota exceeded` | Lakebase project quota hit | Delete unused projects in Catalog Explorer |
| `Lakebase not GA in this region` | Workspace region missing Lakebase | Move workspace to a GA region |

---

## Step 3 — Open an engine on `main`

We do all DDL on `main`. The branch from Step 4 is for *iteration* — you'd use it in real customer work to test schema changes safely. The capstone schema lands on `main` because every later phase reads from `main`.

> **The credential pattern below — `generate_database_credential` per request — is the only safe pattern for long-running notebooks. It mirrors Module 3 Lab 3 Cell 3 and the Module 7 Lab 7 `db.py` factory.**

### Cell 4 — engine on the main branch

```python
def make_engine(branch: str = "main"):
    """Open a fresh psycopg engine on the named branch with a fresh OAuth token."""
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
    version = conn.execute(text("SELECT version()")).scalar()
    user    = conn.execute(text("SELECT current_user")).scalar()

print(f"✅ Connected to main · current_user={user}")
print(f"   {version[:80]}...")
```

**What this proves:** OAuth works, the engine connects to `main`, and `current_user` reflects the workspace identity (not a generic Postgres role).

---

## Step 4 — Create a `dev` branch (the demo)

One-line proof that branching works. We don't deploy to `dev` — Phases 2–5 all run against `main` — but every customer who sees the capstone asks "can I really branch this?" and you can show them.

Branch creation is **copy-on-write** at the storage layer; no data is duplicated. That's the talking point.

### Cell 5 — create the dev branch (idempotent)

```python
existing_branches = [
    b.name for b in w.database.list_database_branches(
        database_project_name=CAPSTONE["project_name"]
    )
]

if "dev" in existing_branches:
    print(f"⏸ Branch 'dev' already exists — reusing.")
else:
    print(f"⏳ Creating branch 'dev' off 'main'...")
    from databricks.sdk.service.database import DatabaseBranch
    w.database.create_database_branch(
        database_project_name=CAPSTONE["project_name"],
        database_branch=DatabaseBranch(name="dev", parent_branch_name="main"),
    )
    for _ in range(12):
        time.sleep(5)
        branches = [b.name for b in w.database.list_database_branches(
            database_project_name=CAPSTONE["project_name"]
        )]
        if "dev" in branches:
            break
    print(f"✅ Branch 'dev' created (copy-on-write — no data duplicated)")
```

---

## Step 5 — Apply DDL on `main` (operational + KB + memory)

We apply **everything in one go** — operational tables, the KB table, and the four memory tables — because the capstone is one product. The schema split is illustrative only; in real customer work this would be three migrations.

**What you should notice:**

- `customers` and `orders` are NOT created here — they will be **synced tables** in Phase 2, sourced from `main.gold.*` Delta. We only create the *targets* of app writes here (`returns`, `resolutions`) plus the *internal* schemas (`kb_documents`, memory tables).
- Every table has a primary key. Synced tables in Phase 2 *require* it; the convention is enforced everywhere for consistency.
- `kb_documents.embedding` is `vector(1024)` — matches the dimensionality of `databricks-bge-large-en` from Module 5.

### Cell 6 — apply the full DDL

```python
DDL = """
-- ── extensions ────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── operational tables we WRITE to from the app ──────────
-- (customers / orders come from synced tables in Phase 2)
CREATE TABLE IF NOT EXISTS returns (
    return_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id      TEXT NOT NULL,
    customer_id   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID,
    customer_id   TEXT NOT NULL,
    category      TEXT NOT NULL,
    summary       TEXT NOT NULL,
    resolved_by   TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS resolutions_created_idx
    ON resolutions (created_at DESC);
CREATE INDEX IF NOT EXISTS resolutions_customer_idx
    ON resolutions (customer_id);

-- ── knowledge base (filled in Phase 3) ───────────────────
CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source        TEXT NOT NULL,
    title         TEXT,
    chunk         TEXT NOT NULL,
    embedding     vector(1024),
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk)) STORED,
    metadata      JSONB DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Indexes built in Phase 3 (after bulk-load) for speed.

-- ── memory layer (used in Phase 4) ───────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    session_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    message_id    BIGSERIAL PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_session_idx ON messages (session_id, message_id);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL,
    session_id    UUID,
    summary       TEXT NOT NULL,
    embedding     vector(1024),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- HNSW built in Phase 4 (after first writes).
CREATE INDEX IF NOT EXISTS episodes_user_idx ON episodes (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id       BIGSERIAL PRIMARY KEY,
    session_id    UUID,
    tool_name     TEXT NOT NULL,
    arguments     JSONB NOT NULL,
    result        JSONB,
    latency_ms    INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_session_idx ON tool_calls (session_id, created_at);
"""

with engine.begin() as conn:
    for stmt in DDL.split(";"):
        if stmt.strip():
            conn.execute(text(stmt))

with engine.connect() as conn:
    tables = conn.execute(text("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)).scalars().all()

print("✅ DDL applied. Tables on main:")
for t in tables:
    print(f"   · {t}")
```

**Expected output:**

```
✅ DDL applied. Tables on main:
   · episodes
   · kb_documents
   · messages
   · resolutions
   · returns
   · sessions
   · tool_calls
```

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `type "vector" does not exist` | Old image, rare on `CU_1` | Open a support ticket; reference Module 5 Lab 5 |
| `gen_random_uuid() does not exist` | Old Postgres without `pgcrypto` | The DDL above creates `pgcrypto` first; re-run the full block |
| `relation "..." already exists` | Re-running the lab — totally fine | DDL is `IF NOT EXISTS`; nothing to do |

---

## Step 6 — Register Lakebase in Unity Catalog as `askmyorders_db`

This is the move that flips Lakebase from "managed Postgres" into "lakehouse-native OLTP." After this cell runs:

- Tables show up in the Catalog Explorer and are queryable from DBSQL
- GRANTs work against workspace identities
- **Phase 2 (synced tables) requires this — it cannot proceed without UC registration**

> **If you don't have `CREATE CATALOG` privilege**, ask an admin to run this once for you. The rest of the capstone runs as your normal user.

### Cell 7 — register Lakebase as a UC catalog

```python
catalog_name = CAPSTONE["uc_catalog"]
project_name = CAPSTONE["project_name"]

existing_catalogs = [c.name for c in w.catalogs.list()]

if catalog_name in existing_catalogs:
    print(f"⏸ UC catalog '{catalog_name}' already exists — reusing.")
else:
    spark.sql(f"""
        CREATE CATALOG {catalog_name}
        USING LAKEBASE
        OPTIONS (
            project_name = '{project_name}',
            branch       = 'main'
        )
    """)
    print(f"✅ Registered Lakebase project '{project_name}' as UC catalog '{catalog_name}'")

with_uc = spark.sql(f"SHOW TABLES IN {catalog_name}.public").collect()
print(f"\nUC sees {len(with_uc)} tables in {catalog_name}.public:")
for r in with_uc:
    print(f"   · {r['tableName']}")
```

**What this proves:** federation is two-way. The seven tables you just created via SQLAlchemy are now visible in DBSQL through `askmyorders_db.public.*`.

---

## Step 7 — Final verification — six green checks

If every row prints ✅, Phase 1 is **done** and you are cleared to start Phase 2. If any row prints ❌, fix it before continuing — the next phases assume each one is true.

### Cell 8 — verification checklist

```python
checks = []

# 1. Project READY
project = w.database.get_database_project(name=CAPSTONE["project_name"])
checks.append(("Project READY", project.state == "READY"))

# 2. dev branch exists
branches = [b.name for b in w.database.list_database_branches(
    database_project_name=CAPSTONE["project_name"]
)]
checks.append(("Branch 'dev' present", "dev" in branches))

# 3. Engine connects
try:
    with engine.connect() as conn:
        ok = conn.execute(text("SELECT 1")).scalar() == 1
    checks.append(("Engine connects to main", ok))
except Exception as e:
    checks.append((f"Engine connects to main ({e})", False))

# 4. Operational + KB tables exist
with engine.connect() as conn:
    tables = set(conn.execute(text("""
        SELECT tablename FROM pg_tables WHERE schemaname='public'
    """)).scalars().all())
expected = {"returns", "resolutions", "kb_documents",
            "sessions", "messages", "episodes", "tool_calls"}
checks.append((f"All 7 tables present ({len(expected & tables)}/{len(expected)})",
               expected.issubset(tables)))

# 5. pgvector + pgcrypto extensions
with engine.connect() as conn:
    exts = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars().all())
checks.append(("pgvector + pgcrypto enabled", {"vector","pgcrypto"}.issubset(exts)))

# 6. UC catalog federates
try:
    cnt = spark.sql(f"SHOW TABLES IN {CAPSTONE['uc_catalog']}.public").count()
    checks.append((f"UC catalog '{CAPSTONE['uc_catalog']}' federates ({cnt} tables)", cnt >= 7))
except Exception as e:
    checks.append((f"UC catalog federates ({e})", False))

print("=" * 60)
print(f"  PHASE 1 VERIFICATION — {CAPSTONE['project_name']}")
print("=" * 60)
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")

passed = sum(1 for _, ok in checks if ok)
total  = len(checks)
print("=" * 60)
if passed == total:
    print(f"  🎯 ALL {total} CHECKS PASSED — proceed to Phase 2")
else:
    print(f"  ⚠ {passed}/{total} passed")
print("=" * 60)
```

### Troubleshooting reference

| ❌ Row | Most common cause | Resolution |
|---|---|---|
| Project READY | Region not GA, or quota exhausted | Read `project.state_message`; switch region or contact admin |
| Branch 'dev' present | First poll timed out | Re-run Cell 5 — the API call is idempotent |
| Engine connects | OAuth token expired during a long pause | Re-run Cell 4 — generates a fresh token |
| All 7 tables present | Cell 6 transaction rolled back silently | Run Cell 6 again; check for prior `BEGIN` left open |
| pgvector + pgcrypto | Old Postgres image; rare on a fresh `CU_1` | Open a support ticket; reference Module 5 Lab 5 |
| UC catalog federates | No `CREATE CATALOG` privilege | Have an admin run Cell 7; subsequent phases run as you |

---

## What you've accomplished

If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 1 is complete and you have:

- ✅ A live `askmyorders` Lakebase project on `CU_1`, registered in Unity Catalog
- ✅ A `dev` branch demonstrating copy-on-write isolation
- ✅ The full capstone schema — operational write tables, KB table with `vector(1024)`, four memory tables — applied on `main`
- ✅ A working credential pattern (`generate_database_credential` per request) that mirrors the Module 7 per-request engine factory
- ✅ Federation verified: tables visible from DBSQL via `askmyorders_db.public.*`

**Do not run cleanup.** Phase 2 needs this exact project alive.

> **Next:** open `Module_08_Phase_2_Sync_Tables.py` and run the synced-table pipeline.
