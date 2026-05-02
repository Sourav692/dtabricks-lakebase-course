# Databricks notebook source
# MAGIC %md
# MAGIC # Module 07 · Lab 7 — Powering a Databricks App with Lakebase, End-to-End
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~90 minutes · **Format:** Databricks notebook + Databricks App deploy
# MAGIC > **Purpose:** Take the operational store you built in Module 3 (and optionally the agent from Module 6) and ship it as a real Databricks App. Write the `app.yaml` manifest, build the per-request engine factory, deploy with `databricks apps deploy`, and prove with a teammate that OBO is doing its job — alice's connection runs as alice, bob's as bob, and the same RLS policy enforces the wall.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — reconnect to the Module 3 project, verify the Databricks CLI, confirm Apps is available
# MAGIC 2. **Scaffold** — create the `app/` directory: `app.yaml`, `app.py`, `db.py`, `requirements.txt`
# MAGIC 3. **Write `db.py`** — the per-request engine factory (Pattern 1 from theory 7.3)
# MAGIC 4. **Write `app.py`** — Streamlit UI: "My Orders" + Identity Check + optional chat
# MAGIC 5. **Write `app.yaml`** — Lakebase resource binding, env vars, no secrets
# MAGIC 6. **Add an RLS policy** — secure the `orders` table by `current_user` so OBO has something to enforce
# MAGIC 7. **Deploy** — `databricks apps deploy --source-code-path ./app lakebase-orders`
# MAGIC 8. **Two-user verification & cleanup** — confirm OBO works, then optionally clean up
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Module 3 Lab 3 passed** with `CLEANUP=False` — this lab uses that project and its `orders` table
# MAGIC - **Optionally Module 6 Lab 6 passed** with `CLEANUP_MEMORY=False` — if you want the chat panel to use real agent memory; if not, the chat panel falls back to a simple stub
# MAGIC - **Databricks CLI v0.220+** installed (workspace cluster usually has it; if not, Cell 8b uses the SDK as a fallback)
# MAGIC - **A teammate with workspace access** — required for Step 8's two-user verification (the trap is silent until a second identity hits the app)
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sh` shells out. **Step 7 runs `databricks apps deploy` — this is a real deploy that takes 2–4 minutes and creates a live URL.** Don't skip Step 8: the OBO trap from theory 7.3 is silent in dev. The verification step is what catches it.
# MAGIC
# MAGIC > **⚠ Billing note.** Databricks Apps runtime is billed by hour while the app is running. This lab leaves the app deployed at the end so you can demo it; Step 8c's cleanup tears it down.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: reconnect and verify the Apps tooling
# MAGIC
# MAGIC Three jobs: re-establish the SQLAlchemy engine on the Module 3 project (notebook side, for schema/RLS work), verify the `databricks` CLI is installed and authenticated, and confirm Databricks Apps is available in this workspace.

# COMMAND ----------

# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — reconnect to the Module 3 project and verify CLI + Apps
import os, uuid, json, subprocess
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

TUTORIAL = {
    "project_name": "lakebase-tutorial",
    "app_name":     "lakebase-orders",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
}

# 1. Project from Module 3 still alive?
existing = {p.name: p for p in w.database.list_database_projects()}
if TUTORIAL["project_name"] not in existing:
    raise RuntimeError(
        f"Project '{TUTORIAL['project_name']}' not found. "
        f"Run Module 3 Lab 3 first (with CLEANUP=False)."
    )
project = existing[TUTORIAL["project_name"]]
assert project.state == "READY", f"Project state is {project.state}, not READY"

# 2. Open an engine for schema work in this notebook
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)
url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred.token}"
    f"@{project.read_write_dns}:5432/databricks_postgres?sslmode=require"
)
engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

# 3. orders table from Module 3 still alive?
with engine.connect() as conn:
    n_orders = conn.execute(text("SELECT count(*) FROM orders")).scalar()

# 4. Databricks CLI present and authenticated?
try:
    out = subprocess.run(
        ["databricks", "--version"],
        capture_output=True, text=True, timeout=10
    )
    cli_ok = out.returncode == 0
    cli_ver = (out.stdout or out.stderr).strip()
except Exception as e:
    cli_ok, cli_ver = False, str(e)[:60]

# 5. Apps available in this workspace?
try:
    apps = list(w.apps.list())
    apps_ok = True
except Exception as e:
    apps_ok = False
    apps = []

print(f"PROJECT '{TUTORIAL['project_name']}'  state={project.state}")
print(f"   Endpoint:    {project.read_write_dns}")
print(f"   orders rows: {n_orders}")
print(f"   CLI:         {cli_ver if cli_ok else 'unavailable: ' + cli_ver}")
print(f"   Apps API:    {'available' if apps_ok else 'unavailable'}")
print(f"   Existing apps: {len(apps)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### If it fails
# MAGIC
# MAGIC | Symptom | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `Project 'lakebase-tutorial' not found` | Module 3 cleanup ran | Re-run Module 3 Lab 3 with `CLEANUP=False` |
# MAGIC | `relation "orders" does not exist` | Module 3 schema dropped | Re-run Module 3 Lab 3, Step 3 |
# MAGIC | CLI shows `unavailable` | Not in cluster's PATH | Use Cell 8b (SDK fallback) instead of Cell 8 |
# MAGIC | Apps API: `unavailable` | Apps not enabled in workspace | Ask your admin to enable Databricks Apps; this lab cannot proceed without it |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Scaffold the app directory
# MAGIC
# MAGIC Lay down the four files that make a Databricks App: `app.yaml` (manifest), `app.py` (Streamlit entrypoint), `db.py` (the per-request engine factory), `requirements.txt` (deps). We write them into the notebook workspace so the deploy step can pick them up.

# COMMAND ----------

# Cell 3 — create the app/ directory tree
APP_DIR = f"/Workspace/Users/{TUTORIAL['user']}/lakebase-orders-app"

os.makedirs(APP_DIR, exist_ok=True)

# Touch the four files so they exist; we'll fill them in Cells 4-6
for f in ["app.yaml", "app.py", "db.py", "requirements.txt"]:
    p = f"{APP_DIR}/{f}"
    if not os.path.exists(p):
        open(p, "w").close()

print(f"App directory ready: {APP_DIR}")
print("Files:")
for f in sorted(os.listdir(APP_DIR)):
    size = os.path.getsize(f"{APP_DIR}/{f}")
    print(f"   {f:<22} ({size} B)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Write `db.py` (the per-request engine factory)
# MAGIC
# MAGIC This is **Pattern 1 from theory 7.3**. Every request creates a fresh engine from the OBO env vars the runtime injects. Per-request cost is one TLS + one auth round-trip (~30–80ms). Easy to review. Almost impossible to misuse.
# MAGIC
# MAGIC **What to look for in this file:**
# MAGIC - `get_engine()` reads env vars on every call — never cached
# MAGIC - No module-level `engine = create_engine(...)` — that's the trap
# MAGIC - `current_user()` reads from `PGUSER` only, never from session state

# COMMAND ----------

# Cell 4 — write db.py (per-request engine factory)
DB_PY = '''"""
db.py — per-request engine factory.

Pattern 1 from Module 7 theory 7.3:
- A fresh SQLAlchemy engine is built on EVERY request from the OBO env vars
  the Apps runtime injects (PGUSER, DATABRICKS_TOKEN, PGHOST, PGDATABASE).
- This guarantees alice's queries run as alice and bob's as bob.
- Cost: ~30-80ms per request for the TLS + auth round-trip. Acceptable for
  almost every workload that isn't doing thousands of QPS.

DO NOT cache the engine globally. The whole reason this works correctly is
that each request rebuilds it from the (request-scoped) env vars.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_engine() -> Engine:
    """Build a per-request engine from the current OBO env vars."""
    user  = os.environ["PGUSER"]
    token = os.environ["DATABRICKS_TOKEN"]
    host  = os.environ["PGHOST"]
    db    = os.environ["PGDATABASE"]
    url = (
        f"postgresql+psycopg://{user}:{token}"
        f"@{host}:5432/{db}?sslmode=require"
    )
    # pool_size=2 is a sensible per-request cap; pool_pre_ping validates
    # the connection on checkout (cheap insurance against scale events).
    return create_engine(url, pool_pre_ping=True, pool_size=2)


def current_user() -> str:
    """The end user this request is running as. Comes from the env var,
    NOT from a session cookie or anything else - that's the whole point.
    """
    return os.environ["PGUSER"]
'''

with open(f"{APP_DIR}/db.py", "w") as f:
    f.write(DB_PY)

print(f"Wrote db.py ({len(DB_PY)} B)")
print("Key thing to notice: get_engine() reads env vars EVERY call.")
print("Never wrap this in @st.cache_resource - that's the trap from 7.3.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Write `app.py` (the Streamlit UI)
# MAGIC
# MAGIC The UI has three tabs:
# MAGIC - **My Orders** — shows rows for the logged-in user. The query has *no `WHERE` clause* — RLS does the filtering.
# MAGIC - **Identity Check** — shows `current_user`, `PGUSER`, and the count of rows visible. This is the panel you and your teammate compare in Step 8.
# MAGIC - **Chat (M6)** — uses Module 6 agent memory if present; otherwise shows a stub. Keeps the lab runnable for people who haven't done Module 6.

# COMMAND ----------

# Cell 5 — write app.py (Streamlit entrypoint)
APP_PY = '''"""
app.py - Streamlit UI for Lakebase Orders.

Demonstrates the OBO pattern from Module 7 theory:
- Every connection runs as the end user (current_user resolves to alice/bob).
- The orders query has NO WHERE clause - RLS filters by current_user automatically.
- The Identity Check tab shows you what's actually happening on the connection.
"""
import os
import streamlit as st
from sqlalchemy import text
from db import get_engine, current_user

st.set_page_config(page_title="Lakebase Orders", layout="wide")

# Sidebar - identity at a glance
st.sidebar.title("Lakebase Orders")
st.sidebar.caption(f"Signed in as **{current_user()}**")
st.sidebar.markdown("---")
st.sidebar.markdown(
    "Module 7 lab demo - Apps + Lakebase + OBO. "
    "Open this app from a teammate's account "
    "to verify identity flows correctly."
)

tab_orders, tab_id, tab_chat = st.tabs(["My Orders", "Identity Check", "Chat (M6)"])


# Tab 1 - My Orders
with tab_orders:
    st.header("My orders")
    st.caption(
        "The query below is `SELECT * FROM orders` - no WHERE clause. "
        "RLS rewrites it to filter by `current_user`."
    )

    # NOTE: get_engine() rebuilds from env vars on every request. This is
    # the whole point of Pattern 1 - alice gets her connection, bob gets his.
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, sku, qty, total_cents, placed_at
            FROM orders
            ORDER BY placed_at DESC
            LIMIT 50
        """)).fetchall()

    if not rows:
        st.info(
            "No orders visible to you. Either the RLS policy filters them out, "
            "or no orders exist for your user. Check the Identity tab."
        )
    else:
        st.dataframe(
            [dict(r._mapping) for r in rows],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"Showing {len(rows)} order(s) for {current_user()}.")


# Tab 2 - Identity Check (the demo panel for Step 8 verification)
with tab_id:
    st.header("Identity check")
    st.caption(
        "PGUSER (the env var) and current_user (the Postgres session) must agree. "
        "If they differ, you have shipped the trap from theory 7.3."
    )

    with get_engine().connect() as conn:
        pg_current_user = conn.execute(text("SELECT current_user")).scalar()
        pg_session_user = conn.execute(text("SELECT session_user")).scalar()
        n_visible       = conn.execute(text("SELECT count(*) FROM orders")).scalar()

    col1, col2 = st.columns(2)
    with col1:
        st.metric("PGUSER (env var)", os.environ.get("PGUSER", "-"))
        st.metric("current_user (Postgres)", pg_current_user)
        st.metric("session_user (Postgres)", pg_session_user)
    with col2:
        st.metric("Orders visible to me", n_visible)
        st.metric("PGHOST", os.environ.get("PGHOST", "-"))

    if os.environ.get("PGUSER") == pg_current_user:
        st.success(
            "OBO is working. The Postgres connection is authenticated "
            "as the end user, not as a service account."
        )
    else:
        st.error(
            "MISMATCH. PGUSER and current_user disagree. "
            "Check db.py - most likely a global engine was cached."
        )


# Tab 3 - Chat (uses Module 6 agent memory if available)
with tab_chat:
    st.header("Chat about your orders")
    st.caption(
        "If you completed Module 6, this tab uses agent memory and remembers "
        "context across sessions. Otherwise it is a simple stub."
    )

    with get_engine().connect() as conn:
        m6 = conn.execute(text("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_name IN ('sessions', 'messages', 'episodes')
        """)).scalar()
    has_memory = m6 == 3

    if not has_memory:
        st.info(
            "Module 6 memory tables not found. Skipping chat. "
            "Run Module 6 Lab 6 with CLEANUP_MEMORY=False to enable."
        )
    else:
        if "msgs" not in st.session_state:
            st.session_state["msgs"] = []
        for m in st.session_state["msgs"]:
            with st.chat_message(m["role"]):
                st.write(m["content"])
        prompt = st.chat_input("Ask about your orders...")
        if prompt:
            st.session_state["msgs"].append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.write(prompt)
            with st.chat_message("assistant"):
                # Stub: in a real Module 6+7 build this calls run_turn().
                reply = (
                    f"You asked: {prompt!r}. In a real build this would call "
                    f"run_turn(engine, embed, llm, sid, "
                    f"{current_user()!r}, prompt)."
                )
                st.write(reply)
                st.session_state["msgs"].append(
                    {"role": "assistant", "content": reply}
                )
'''

with open(f"{APP_DIR}/app.py", "w") as f:
    f.write(APP_PY)

print(f"Wrote app.py ({len(APP_PY)} B)")
print("Tabs: My Orders | Identity Check | Chat (M6 optional)")
print("The Identity Check tab is what you verify in Step 8.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Write `app.yaml` and `requirements.txt`
# MAGIC
# MAGIC The manifest is the only place you bind the Lakebase project. Notice: **no secrets, no passwords, no DB connection string in the file** — everything is identity-driven through the resource binding.

# COMMAND ----------

# Cell 6 — write app.yaml + requirements.txt
APP_YAML = f'''command: ["streamlit", "run", "app.py"]

env:
  - name: PGHOST
    valueFrom: {TUTORIAL["project_name"]}
  - name: PGDATABASE
    value: databricks_postgres

resources:
  - name: {TUTORIAL["project_name"]}
    description: "Module 3 Lakebase project - operational store"
    database:
      instance_name: {TUTORIAL["project_name"]}
      permission: CAN_USE
'''

REQS_TXT = '''streamlit>=1.32.0
sqlalchemy>=2.0.0
psycopg[binary]>=3.1.0
'''

with open(f"{APP_DIR}/app.yaml", "w") as f:
    f.write(APP_YAML)
with open(f"{APP_DIR}/requirements.txt", "w") as f:
    f.write(REQS_TXT)

print(f"Wrote app.yaml ({len(APP_YAML)} B)")
print(f"Wrote requirements.txt ({len(REQS_TXT)} B)")
print()
print("-" * 60)
print("app.yaml content (note: no passwords, no secrets):")
print("-" * 60)
print(APP_YAML)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Add an RLS policy to `orders`
# MAGIC
# MAGIC Right now the `orders` table from Module 3 has no RLS — every connection sees every row. That works in dev (one user) but defeats the entire point of OBO. Add a policy so `current_user` actually has something to enforce, then verify it works on the notebook side before deploying.
# MAGIC
# MAGIC **The policy.** Each user sees only the orders whose `user_email` matches their workspace identity. We backfill some rows for you and add two for a fictional second user so the demo has variety.

# COMMAND ----------

# Cell 7 — add user_email column (if missing), seed, and apply RLS
with engine.begin() as conn:
    # 1. Add user_email column if it doesn't exist (idempotent)
    conn.execute(text("""
        ALTER TABLE orders
        ADD COLUMN IF NOT EXISTS user_email TEXT
    """))

    # 2. Backfill: assign the first 3 existing rows to current user
    conn.execute(text("""
        UPDATE orders SET user_email = :me
        WHERE id IN (SELECT id FROM orders ORDER BY id LIMIT 3)
          AND (user_email IS NULL OR user_email = '')
    """), {"me": TUTORIAL["user"]})

    # 3. Add a couple of rows for a fictional second user
    conn.execute(text("""
        INSERT INTO orders (sku, qty, total_cents, user_email)
        VALUES
            ('LB-9047X', 2, 18000, 'demo-other@example.com'),
            ('LB-1234',  1,  4500, 'demo-other@example.com')
        ON CONFLICT DO NOTHING
    """))

    # 4. Drop and recreate the RLS policy (idempotent)
    conn.execute(text("DROP POLICY IF EXISTS orders_self ON orders"))
    conn.execute(text("""
        CREATE POLICY orders_self ON orders
            USING (user_email = current_user)
    """))

    # 5. Enable RLS on the table
    conn.execute(text("ALTER TABLE orders ENABLE ROW LEVEL SECURITY"))

# Verify on the notebook side: current_user IS your email, so you should
# see only the rows you own.
with engine.connect() as conn:
    visible = conn.execute(text("SELECT count(*) FROM orders")).scalar()
    by_user = conn.execute(text("""
        SELECT user_email, count(*) AS n
        FROM orders
        GROUP BY user_email
        ORDER BY n DESC
    """)).fetchall()

print(f"RLS policy 'orders_self' active on `orders`")
print(f"   Rows visible to YOU ({TUTORIAL['user']}): {visible}")
print(f"   (You should see only your rows because the notebook itself")
print(f"    runs as your identity - same model the App will use.)")
print()
print("Distribution across users in the underlying table:")
for row in by_user:
    marker = " <- YOU" if row.user_email == TUTORIAL["user"] else ""
    print(f"   {(row.user_email or '(NULL)'):<32} {row.n}{marker}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### What just happened
# MAGIC
# MAGIC - `orders` got a `user_email` column populated with your identity for some rows and `demo-other@example.com` for others.
# MAGIC - The RLS policy `orders_self` rewrites every query against `orders` to add `WHERE user_email = current_user`.
# MAGIC - `ALTER TABLE … ENABLE ROW LEVEL SECURITY` activates it.
# MAGIC - **You only see your rows from this notebook**, because the notebook's Postgres connection is authenticated as you. When the App connects as alice, alice sees alice's rows. When it connects as bob, bob sees bob's. **No `WHERE` clause anywhere in `app.py` is doing this work** — RLS is.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Deploy the app
# MAGIC
# MAGIC Two ways: (a) the `databricks` CLI from a notebook shell cell, or (b) the SDK's Apps API. Both end with the same artifact — a running app at a URL the workspace surfaces.

# COMMAND ----------

# Cell 8 — deploy via the Databricks CLI (preferred when available)
deploy_cmd = (
    f"databricks apps deploy "
    f"--source-code-path {APP_DIR} "
    f"{TUTORIAL['app_name']}"
)

print("Running:")
print(f"  $ {deploy_cmd}")
print()
print("This takes 2-4 minutes the first time (image build + container start).")
print("Subsequent deploys are faster (~30-60s).")
print()

try:
    out = subprocess.run(
        deploy_cmd.split(),
        capture_output=True, text=True, timeout=600
    )
    print("STDOUT:", out.stdout)
    if out.stderr:
        print("STDERR:", out.stderr)
    if out.returncode != 0:
        print(f"Exit code {out.returncode} - check stderr above. Try Cell 8b.")
except FileNotFoundError:
    print("`databricks` CLI not found on this cluster. Use Cell 8b instead.")
except subprocess.TimeoutExpired:
    print("Deploy timed out at 10 min. Check the Apps UI manually.")

# COMMAND ----------

# Cell 8b — deploy via the SDK (fallback if the CLI is unavailable)
from databricks.sdk.service.apps import App, AppResource, AppResourceDatabase

try:
    app_obj = w.apps.create_and_wait(
        app=App(
            name=TUTORIAL["app_name"],
            description="Module 7 lab - Lakebase Orders demo",
            resources=[
                AppResource(
                    name=TUTORIAL["project_name"],
                    description="Module 3 Lakebase project",
                    database=AppResourceDatabase(
                        instance_name=TUTORIAL["project_name"],
                        permission="CAN_USE",
                    ),
                )
            ],
        )
    )
    print(f"App created: {app_obj.name}")

    deployment = w.apps.deploy_and_wait(
        app_name=TUTORIAL["app_name"],
        source_code_path=APP_DIR,
    )
    print(f"Deployed.  state={deployment.status.state}")
    print(f"   URL:   {app_obj.url}")
except Exception as e:
    msg = str(e)
    if "already exists" in msg or "ALREADY_EXISTS" in msg:
        # App already exists - just redeploy
        deployment = w.apps.deploy_and_wait(
            app_name=TUTORIAL["app_name"],
            source_code_path=APP_DIR,
        )
        print(f"Redeployed existing app.  state={deployment.status.state}")
    else:
        raise

# COMMAND ----------

# Cell 9 — surface the app URL for you and your teammate
app = w.apps.get(name=TUTORIAL["app_name"])
print("=" * 70)
print(f"  APP NAME: {app.name}")
state = app.compute_status.state if app.compute_status else "unknown"
print(f"  STATE:    {state}")
print(f"  URL:      {app.url}")
print("=" * 70)
print()
print("Open the URL in your browser - sign in with workspace SSO.")
print("Send the URL to a teammate; they sign in with THEIR workspace identity.")
print("You should each see different orders + different identity values.")
print("That's what Step 8 verifies.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — The two-user verification (the trap-killer)
# MAGIC
# MAGIC This is the single most important step in Module 7. The connection-pool trap from theory 7.3 is *silent* in dev. It only manifests when a second identity hits the app. **Don't skip this.**
# MAGIC
# MAGIC ### 8a — What you do
# MAGIC
# MAGIC 1. Open the app URL (printed above) in your browser. Click the **Identity Check** tab.
# MAGIC 2. Read off three values: `PGUSER` (env), `current_user` (Postgres), `Orders visible to me`.
# MAGIC 3. Send the same URL to your teammate. They click **Identity Check** in their own browser.
# MAGIC 4. Compare. **All three values must differ between you and your teammate.**
# MAGIC
# MAGIC ### 8b — What you should see
# MAGIC
# MAGIC | What | YOU should see | YOUR TEAMMATE should see |
# MAGIC |---|---|---|
# MAGIC | `PGUSER` | your email | their email |
# MAGIC | `current_user` (Postgres) | your email | their email |
# MAGIC | Orders visible | rows where `user_email = you` | rows where `user_email = them` (likely 0 unless you backfilled) |
# MAGIC
# MAGIC If your teammate's panel shows YOUR identity, **you have shipped the trap**. Re-check `db.py` — most likely a global `engine = create_engine(...)` got introduced somewhere outside `get_engine()`.
# MAGIC
# MAGIC ### 8c — Programmatic checklist

# COMMAND ----------

# Cell 10 — final pass/fail checklist
checks = []

# 1. App exists and is running
try:
    a = w.apps.get(name=TUTORIAL["app_name"])
    state = a.compute_status.state if a.compute_status else "unknown"
    ok = state in ("RUNNING", "ACTIVE")
    checks.append(("App deployed and running", state, ok))
except Exception as e:
    checks.append(("App deployed and running", str(e)[:30], False))

# 2. RLS policy exists
try:
    with engine.connect() as conn:
        n = conn.execute(text("""
            SELECT count(*) FROM pg_policies
            WHERE tablename = 'orders' AND policyname = 'orders_self'
        """)).scalar()
    checks.append(("RLS policy 'orders_self'", f"{n} found", n == 1))
except Exception as e:
    checks.append(("RLS policy 'orders_self'", str(e)[:30], False))

# 3. RLS is enabled on the table
try:
    with engine.connect() as conn:
        enabled = conn.execute(text("""
            SELECT relrowsecurity FROM pg_class WHERE relname = 'orders'
        """)).scalar()
    checks.append(("RLS enabled on orders", "yes" if enabled else "no", bool(enabled)))
except Exception as e:
    checks.append(("RLS enabled on orders", str(e)[:30], False))

# 4. RLS demonstrably filters: more users in DB than the notebook sees
try:
    with engine.connect() as conn:
        mine = conn.execute(text("SELECT count(*) FROM orders")).scalar()
        all_users = conn.execute(text("""
            SELECT count(DISTINCT user_email) FROM orders
        """)).scalar()
    ok = mine > 0 and all_users >= 2
    checks.append(("RLS filters notebook session",
                   f"{mine} visible, {all_users} users", ok))
except Exception as e:
    checks.append(("RLS filters notebook session", str(e)[:30], False))

# 5. db.py uses Pattern 1 (no global, no @st.cache_resource on engine)
try:
    src = open(f"{APP_DIR}/db.py").read()
    has_factory = "def get_engine" in src
    no_global   = "engine = create_engine" not in src.split("def get_engine")[0]
    no_cache    = "@st.cache_resource" not in src
    ok = has_factory and no_global and no_cache
    checks.append(("db.py uses Pattern 1 (no global)",
                   "factory only" if ok else "issue", ok))
except Exception as e:
    checks.append(("db.py uses Pattern 1 (no global)", str(e)[:30], False))

# 6. app.yaml has no secrets
try:
    src = open(f"{APP_DIR}/app.yaml").read().lower()
    has_secret = any(t in src for t in ["password:", "secret:", "token:"])
    checks.append(("app.yaml has no secrets",
                   "clean" if not has_secret else "found secret",
                   not has_secret))
except Exception as e:
    checks.append(("app.yaml has no secrets", str(e)[:30], False))

# Print
print("=" * 70)
print(f"{'CHECK':<38} {'DETAIL':<22} STATUS")
print("-" * 70)
for name, detail, ok in checks:
    icon = "PASS" if ok else "FAIL"
    print(f"{name:<38} {str(detail)[:22]:<22} {icon}")
print("=" * 70)
passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\nALL {total} CHECKS PASSED - Module 7 complete.")
    print("Do not skip the manual two-user verification (8a + 8b).")
else:
    print(f"\n{passed}/{total} passed. Resolve the FAIL rows before Module 8.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8d — Cleanup (optional)
# MAGIC
# MAGIC Module 8 (the capstone) reuses the same `app/` skeleton, the same per-request engine pattern, and the same RLS approach. **Most readers leave `CLEANUP_APP=False`** so they can keep demoing the app. If you do clean up, the Module 3 project, Module 5 documents, Module 6 memory tables, and the RLS policy all stay — only the App is removed.

# COMMAND ----------

# Cell 11 — cleanup
CLEANUP_APP = False

if CLEANUP_APP:
    try:
        w.apps.delete(name=TUTORIAL["app_name"])
        print(f"Deleted app '{TUTORIAL['app_name']}'.")
        print("Lakebase project, RLS policy, and all schemas preserved.")
    except Exception as e:
        print(f"Delete failed: {e}")
else:
    print("Cleanup skipped. App still running - keep demoing.")
    print(f"   App:    {TUTORIAL['app_name']}")
    print(f"   URL:    (re-run Cell 9 to print)")
    print(f"   Cost:   Apps runtime is billed by hour while running.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your final cell printed `ALL 6 CHECKS PASSED` AND the manual two-user verification confirmed distinct identities, you have done — for real, not in slides:
# MAGIC
# MAGIC - Wrote a real `app.yaml` that binds the Lakebase project with **zero secrets**
# MAGIC - Built a per-request engine factory (`db.py`) that follows Pattern 1 from theory 7.3
# MAGIC - Wrote a Streamlit UI (`app.py`) that **demonstrates RLS-enforced isolation with no `WHERE` clause** — the wall is in Postgres, not the app
# MAGIC - Added an RLS policy on `orders` so OBO has something concrete to enforce
# MAGIC - Deployed the app via the Databricks CLI (or SDK) and got a live URL
# MAGIC - **Verified with a teammate that two different workspace identities produce two different views of the same data** — the trap-killer step
# MAGIC - Confirmed `db.py` uses no global engine and `app.yaml` carries no secrets — the two static checks every Lakebase-app PR review should perform
# MAGIC
# MAGIC These six capabilities are exactly what every customer Apps + Lakebase build needs. Module 8 (capstone) wraps the Module 6 agent loop into this same skeleton — same `db.py`, same `app.yaml` pattern, same two-user verification. **Save this notebook somewhere you can find it again.**
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## Module 7 — complete
# MAGIC
# MAGIC You finished four theory topics (7.1–7.4) and the hands-on lab. **Next up: Module 8** — the *capstone*. You'll glue Modules 3, 4, 5, 6, and 7 together into a single Streamlit-deployed customer-support agent. The per-request engine pattern from this lab is the substrate the entire capstone is built on.
