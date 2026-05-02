# Databricks notebook source
# MAGIC %md
# MAGIC # Module 08 · Capstone · Phase 1 — Foundation & Provisioning
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~30 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Stand up the `askmyorders` Lakebase project, create a `dev` branch, register in Unity Catalog, and apply the full DDL — operational tables, knowledge-base table, and the four memory tables — in one place. This is the gate for every other phase of the capstone.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — install deps, open the Databricks SDK
# MAGIC 2. **Provision** the Lakebase project `askmyorders` (CU_1, 7-day retention, HA off)
# MAGIC 3. **Branch** `dev` off `main`
# MAGIC 4. **Apply DDL** — `customers`, `orders`, `returns`, `resolutions` (operational)
# MAGIC 5. **Apply DDL** — `vector` extension + `kb_documents` (knowledge base) + `sessions`, `messages`, `episodes`, `tool_calls` (memory)
# MAGIC 6. **Register** Lakebase in Unity Catalog as catalog `askmyorders_db`
# MAGIC 7. **Final verification** — six green checks before Phase 2
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Modules 1–7 completed** (the conceptual Module 8 walkthrough should be fresh)
# MAGIC - **Module 1 Lab 1.4 passed** — confirms your workspace can provision Lakebase, embed via Foundation Models, and write to UC
# MAGIC - **Foundation Models API** enabled in the workspace region (Phase 3 needs it)
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC - `CREATE CATALOG` privilege on the metastore (or an admin available to run Cell 8 once)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. All cells are Python or `%sql`. Re-running individual cells is safe — every DDL uses `IF NOT EXISTS`.
# MAGIC
# MAGIC > **⚠ Billing note.** Provisioning starts billing for `CU_1` immediately. Subsequent phases (2–5) reuse this same project — do **not** clean up at the end of any phase except after Phase 5 is verified.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: install dependencies and open the SDK
# MAGIC
# MAGIC Same pattern as Module 3 Lab 3. The SDK is the only required dependency for this phase — the project DDL runs through it.

# COMMAND ----------

# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — open the SDK and pin per-capstone constants
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
print()
print(f"Workspace user: {CAPSTONE['user']}")
print(f"Workspace host: {CAPSTONE['host']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Provision the `askmyorders` Lakebase project
# MAGIC
# MAGIC Compute size `CU_1` is enough for the entire capstone — the data is small. Retention is 7 days (the minimum) because we don't need the longer PIT window. HA is off for now; the stretch goal in Module 8 wrap-up has you turn it on.
# MAGIC
# MAGIC Provisioning takes ~60–90 seconds. The cell polls until state is `READY`.

# COMMAND ----------

# Cell 3 — provision the Lakebase project (idempotent)
existing = {p.name: p for p in w.database.list_database_projects()}

if CAPSTONE["project_name"] in existing:
    project = existing[CAPSTONE["project_name"]]
    print(f"⏸ Project '{CAPSTONE['project_name']}' already exists — reusing.")
    print(f"   state: {project.state}")
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
    # Poll for READY
    deadline = time.time() + 180
    while project.state != "READY" and time.time() < deadline:
        time.sleep(5)
        project = w.database.get_database_project(name=CAPSTONE["project_name"])
        print(f"   ...state={project.state}")
    print(f"✅ Project READY: {project.read_write_dns}")

assert project.state == "READY", f"Project not READY: {project.state} — {getattr(project, 'state_message', '')}"
print()
print(f"Endpoint: {project.read_write_dns}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Open an engine on the `main` branch
# MAGIC
# MAGIC We do all DDL on `main`. The branch from Step 4 is for *iteration* — you'd use it in real work to test schema changes safely. The capstone schema lands on `main` because every later phase reads from `main`.
# MAGIC
# MAGIC > **The credential pattern below — `generate_database_credential` per request — is the only safe pattern for long-running notebooks. It's the same pattern from Module 3 Lab 3 Cell 3 and Module 7 Lab 7 `db.py`.**

# COMMAND ----------

# Cell 4 — engine on the main branch
def make_engine(branch: str = "main"):
    """Open a fresh psycopg engine on the named branch with a fresh OAuth token."""
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[CAPSTONE["project_name"]],
    )
    # Branch suffix is appended to the database name when not main
    db_name = CAPSTONE["project_name"] if branch == "main" else f"{CAPSTONE['project_name']}_{branch}"
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Create a `dev` branch (the demo)
# MAGIC
# MAGIC One-line proof that branching works. We don't deploy to `dev` — Phases 2–5 all run against `main` — but every customer who sees the capstone asks "can I really branch this?" and you can show them.
# MAGIC
# MAGIC Branch creation is copy-on-write at the storage layer; no data is duplicated.

# COMMAND ----------

# Cell 5 — create the dev branch (idempotent)
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
    # Poll briefly
    for _ in range(12):
        time.sleep(5)
        branches = [b.name for b in w.database.list_database_branches(
            database_project_name=CAPSTONE["project_name"]
        )]
        if "dev" in branches:
            break
    print(f"✅ Branch 'dev' created (copy-on-write — no data duplicated)")

# Verify
branches = [b.name for b in w.database.list_database_branches(
    database_project_name=CAPSTONE["project_name"]
)]
print(f"\nBranches on '{CAPSTONE['project_name']}': {sorted(branches)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Apply DDL (operational + KB + memory) on `main`
# MAGIC
# MAGIC We apply **everything in one go** — operational tables, the KB table, and the four memory tables — because the capstone is one product. The schema split is illustrative only; in real customer work this would be three migrations.
# MAGIC
# MAGIC **What you should notice:**
# MAGIC - `customers` and `orders` are NOT created here — they will be **synced tables** in Phase 2, sourced from `main.gold.*` Delta. We only create the *targets* of app writes here (`returns`, `resolutions`) plus the *internal* schemas (`kb_documents`, memory tables).
# MAGIC - Every table has a primary key. Synced tables in Phase 2 *require* it; the convention is enforced everywhere for consistency.
# MAGIC - `kb_documents.embedding` is `vector(1024)` — matches the dimensionality of `databricks-bge-large-en` from Module 5.

# COMMAND ----------

# Cell 6 — apply the full DDL (operational + KB + memory)
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
-- HNSW index built in Phase 4 (after first writes) for speed.
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

# Verify
with engine.connect() as conn:
    tables = conn.execute(text("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)).scalars().all()

print("✅ DDL applied. Tables on main:")
for t in tables:
    print(f"   · {t}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Register Lakebase in Unity Catalog as `askmyorders_db`
# MAGIC
# MAGIC This is the move that flips Lakebase from "managed Postgres" into "lakehouse-native OLTP." After this cell runs:
# MAGIC
# MAGIC - Tables show up in the Catalog Explorer and are queryable from DBSQL
# MAGIC - GRANTs work against workspace identities
# MAGIC - **Phase 2 (synced tables) requires this — it cannot proceed without UC registration**
# MAGIC
# MAGIC > **If you don't have `CREATE CATALOG` privilege**, ask an admin to run this once for you. The rest of the capstone runs as your normal user.

# COMMAND ----------

# Cell 7 — register Lakebase as a UC catalog (idempotent)
catalog_name = CAPSTONE["uc_catalog"]
project_name = CAPSTONE["project_name"]

# Check if already registered
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

# Verify federation works — query a table we just created
with_uc = spark.sql(f"SHOW TABLES IN {catalog_name}.public").collect()
print(f"\nUC sees {len(with_uc)} tables in {catalog_name}.public:")
for r in with_uc:
    print(f"   · {r['tableName']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Final verification — 6 green checks
# MAGIC
# MAGIC If every row prints ✅, Phase 1 is **done** and you are cleared to start Phase 2. If any row prints ❌, fix it before continuing — the next phases assume each one is true.

# COMMAND ----------

# Cell 8 — verification checklist
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
    exts = set(conn.execute(text(
        "SELECT extname FROM pg_extension"
    )).scalars().all())
checks.append((f"pgvector + pgcrypto enabled",
               {"vector", "pgcrypto"}.issubset(exts)))

# 6. UC catalog registered + federates
try:
    cnt = spark.sql(f"SHOW TABLES IN {CAPSTONE['uc_catalog']}.public").count()
    checks.append((f"UC catalog '{CAPSTONE['uc_catalog']}' federates ({cnt} tables)", cnt >= 7))
except Exception as e:
    checks.append((f"UC catalog federates ({e})", False))

# Print
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
    print(f"  ⚠ {passed}/{total} passed — see troubleshooting below")
print("=" * 60)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Troubleshooting reference
# MAGIC
# MAGIC | ❌ Row | Most common cause | Resolution |
# MAGIC |---|---|---|
# MAGIC | **Project READY** | Region not GA, or quota exhausted | Read `project.state_message`; switch region or contact admin |
# MAGIC | **Branch 'dev' present** | First poll timed out | Re-run Cell 5 — the API call is idempotent |
# MAGIC | **Engine connects to main** | OAuth token expired during a long pause | Re-run Cell 4 — generates a fresh token |
# MAGIC | **All 7 tables present** | Cell 6 transaction rolled back silently | Run Cell 6 again; check for prior `BEGIN` left open |
# MAGIC | **pgvector + pgcrypto** | Old Postgres image; rare in a fresh CU_1 | Open a support ticket; reference Module 5 Lab 5 |
# MAGIC | **UC catalog federates** | No `CREATE CATALOG` privilege on the metastore | Have an admin run Cell 7; subsequent phases run as you |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 1 is complete and you have:
# MAGIC
# MAGIC - ✅ A live `askmyorders` Lakebase project on `CU_1`, registered in Unity Catalog
# MAGIC - ✅ A `dev` branch demonstrating copy-on-write isolation
# MAGIC - ✅ The full capstone schema — operational write tables, KB table with `vector(1024)`, four memory tables — applied on `main`
# MAGIC - ✅ A working credential pattern (`generate_database_credential` per request) that mirrors Module 7's per-request engine factory
# MAGIC - ✅ Federation verified: tables visible from DBSQL via `askmyorders_db.public.*`
# MAGIC
# MAGIC **Do not run cleanup.** Phase 2 needs this exact project alive.
# MAGIC
# MAGIC > **Next:** open `Module_08_Phase_2_Sync_Tables.py` and run the synced-table pipeline.

# COMMAND ----------

# Cell 9 — pin state for the next phase
print("=" * 60)
print(f"  ▸ Phase 1 complete. Pinned for Phase 2:")
print("=" * 60)
print(f"  Project name : {CAPSTONE['project_name']}")
print(f"  UC catalog   : {CAPSTONE['uc_catalog']}")
print(f"  Endpoint     : {project.read_write_dns}")
print(f"  Branches     : {sorted(branches)}")
print(f"  Tables on main: returns, resolutions, kb_documents,")
print(f"                  sessions, messages, episodes, tool_calls")
print("=" * 60)
print(f"  ⏸ DO NOT clean up. Open Phase 2 next.")
print("=" * 60)
