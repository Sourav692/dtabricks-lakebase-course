# Module 07 · Lab 7 — Powering a Databricks App with Lakebase, End-to-End

> **Type:** Hands-on · **Duration:** ~90 minutes · **Format:** Databricks notebook + Databricks App deploy
> **Purpose:** Take the operational store you built in Module 3 (and optionally the agent from Module 6) and ship it as a real Databricks App. Write the `app.yaml` manifest, build the per-request engine factory, deploy with `databricks apps deploy`, and prove with a teammate that OBO is doing its job — alice's connection runs as alice, bob's as bob, and the same RLS policy enforces the wall.

---

## Why this lab exists

Module 7 theory taught you *why* Apps + Lakebase eliminates the seam tax (7.1), *how* the OBO runtime contract works (7.2), *the* connection-pool trap and the two correct patterns (7.3), and *the four shapes* most Lakebase-backed apps fall into (7.4). This lab makes you **execute every line** so you can demo Apps + Lakebase on a customer call without reaching for slides — and, just as important, **catch the trap on your own code review**.

This lab walks the eight steps in order:

1. **Setup** — reconnect to the Module 3 project, verify the Databricks CLI, confirm Apps is available
2. **Scaffold** the `app/` directory: `app.yaml`, `app.py`, `db.py`, `requirements.txt`
3. **Write `db.py`** — the per-request engine factory (Pattern 1 from theory 7.3)
4. **Write `app.py`** — Streamlit UI: "My Orders" + Identity Check + optional chat
5. **Write `app.yaml`** — Lakebase resource binding, env vars, no secrets
6. **Add an RLS policy** on `orders` so OBO has something concrete to enforce
7. **Deploy** with `databricks apps deploy`
8. **Two-user verification & cleanup** — the trap-killer

Total runtime ~90 minutes. About 15 minutes is your hands on the keyboard; the rest is the deploy build, image push, and you walking through the verification with a teammate. **The two-user step (8a + 8b) is non-negotiable** — the trap is silent until a second identity hits the app.

> **Run mode.** Do this lab in a Databricks notebook attached to a serverless cluster (or DBR 14+). All cells are Python except where `%sh` is used. Step 7 runs a real deploy that costs ~1 cent and creates a live URL.

---

## Prerequisites before you start

You need:

- ✅ **Module 3 Lab 3 passed** with `CLEANUP=False` — this lab uses that project and its `orders` table
- ✅ **Optionally Module 6 Lab 6 passed** with `CLEANUP_MEMORY=False` — if you want the chat panel to use real agent memory; if not, the chat panel falls back to a simple stub
- ✅ **Databricks CLI v0.220+** installed (workspace cluster usually has it; if not, Cell 8b uses the SDK as a fallback)
- ✅ **A teammate with workspace access** — required for Step 8's two-user verification
- An attached compute cluster (serverless is fine, recommended)

You do **not** need:

- A new Lakebase project (you reuse `lakebase-tutorial` from Module 3)
- Module 4 (synced tables are independent of OBO)
- Module 5 (vectors are independent of OBO; chat tab is optional)
- An external IdP or auth service (workspace OAuth is the only auth)

> **⚠ Billing note.** Databricks Apps runtime is billed by hour while the app is running. This lab leaves the app deployed at the end so you can demo it; Step 8d's cleanup tears it down.

---

## Step 1 — Setup: reconnect and verify the Apps tooling

Three jobs: re-establish the SQLAlchemy engine on the Module 3 project (notebook side, for schema/RLS work), verify the `databricks` CLI is installed and authenticated, and confirm Databricks Apps is available in this workspace.

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()
```

### Cell 2 — reconnect, then verify CLI + Apps

```python
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
assert project.state == "READY"

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

# 4. Databricks CLI?
try:
    out = subprocess.run(["databricks", "--version"],
                         capture_output=True, text=True, timeout=10)
    cli_ok, cli_ver = out.returncode == 0, (out.stdout or out.stderr).strip()
except Exception as e:
    cli_ok, cli_ver = False, str(e)[:60]

# 5. Apps API?
try:
    apps = list(w.apps.list())
    apps_ok = True
except Exception as e:
    apps_ok, apps = False, []

print(f"PROJECT '{TUTORIAL['project_name']}'  state={project.state}")
print(f"   Endpoint:    {project.read_write_dns}")
print(f"   orders rows: {n_orders}")
print(f"   CLI:         {cli_ver if cli_ok else 'unavailable'}")
print(f"   Apps API:    {'available' if apps_ok else 'unavailable'}")
```

**What this proves:** the Module 3 project is alive, OAuth still works, you have a writable engine, the CLI is reachable, and the workspace has Apps enabled.

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `Project 'lakebase-tutorial' not found` | Module 3 cleanup ran | Re-run Module 3 Lab 3 with `CLEANUP=False` |
| `relation "orders" does not exist` | Module 3 schema dropped | Re-run Module 3 Lab 3, Step 3 |
| CLI shows `unavailable` | Not in cluster's PATH | Use Cell 8b (SDK fallback) instead of Cell 8 |
| Apps API: `unavailable` | Apps not enabled in workspace | Ask your admin to enable Databricks Apps |

---

## Step 2 — Scaffold the app directory

Lay down the four files that make a Databricks App: `app.yaml` (manifest), `app.py` (Streamlit entrypoint), `db.py` (the per-request engine factory), `requirements.txt` (deps).

### Cell 3 — create the app/ directory tree

```python
APP_DIR = f"/Workspace/Users/{TUTORIAL['user']}/lakebase-orders-app"

os.makedirs(APP_DIR, exist_ok=True)
for f in ["app.yaml", "app.py", "db.py", "requirements.txt"]:
    p = f"{APP_DIR}/{f}"
    if not os.path.exists(p):
        open(p, "w").close()

print(f"App directory ready: {APP_DIR}")
for f in sorted(os.listdir(APP_DIR)):
    size = os.path.getsize(f"{APP_DIR}/{f}")
    print(f"   {f:<22} ({size} B)")
```

**Expected output:**

```
App directory ready: /Workspace/Users/you@co.com/lakebase-orders-app
   app.py                 (0 B)
   app.yaml               (0 B)
   db.py                  (0 B)
   requirements.txt       (0 B)
```

The four files are placeholders; Cells 4–6 fill them in.

---

## Step 3 — Write `db.py` (the per-request engine factory)

This is **Pattern 1 from theory 7.3**. Every request creates a fresh engine from the OBO env vars the runtime injects. Per-request cost is one TLS + one auth round-trip (~30–80ms). Easy to review. Almost impossible to misuse.

**What to look for in this file:**

- `get_engine()` reads env vars on every call — never cached
- No module-level `engine = create_engine(...)` — that's the trap
- `current_user()` reads from `PGUSER` only, never from session state

### Cell 4 — write db.py

```python
DB_PY = '''"""
db.py — per-request engine factory.

Pattern 1 from Module 7 theory 7.3:
- A fresh SQLAlchemy engine is built on EVERY request from the OBO env vars
  the Apps runtime injects (PGUSER, DATABRICKS_TOKEN, PGHOST, PGDATABASE).
- This guarantees alice's queries run as alice and bob's as bob.
- Cost: ~30-80ms per request for the TLS + auth round-trip.

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
    return create_engine(url, pool_pre_ping=True, pool_size=2)


def current_user() -> str:
    """The end user this request is running as."""
    return os.environ["PGUSER"]
'''

with open(f"{APP_DIR}/db.py", "w") as f:
    f.write(DB_PY)
print(f"Wrote db.py ({len(DB_PY)} B)")
```

---

## Step 4 — Write `app.py` (the Streamlit UI)

The UI has three tabs:

- **My Orders** — shows rows for the logged-in user. The query has *no `WHERE` clause* — RLS does the filtering.
- **Identity Check** — shows `current_user`, `PGUSER`, and the count of rows visible. **This is the panel you and your teammate compare in Step 8.**
- **Chat (M6)** — uses Module 6 agent memory if present; otherwise shows a stub.

### Cell 5 — write app.py

The full file content is in the notebook (Cell 5). The structurally important parts to read:

```python
# Sidebar identity at a glance
st.sidebar.caption(f"Signed in as **{current_user()}**")

# Tab 1 — My Orders. NOTE the SELECT has no WHERE clause.
with get_engine().connect() as conn:
    rows = conn.execute(text("""
        SELECT id, sku, qty, total_cents, placed_at
        FROM orders
        ORDER BY placed_at DESC
        LIMIT 50
    """)).fetchall()

# Tab 2 — Identity Check. The values that MUST agree.
with get_engine().connect() as conn:
    pg_current_user = conn.execute(text("SELECT current_user")).scalar()
    pg_session_user = conn.execute(text("SELECT session_user")).scalar()
    n_visible       = conn.execute(text("SELECT count(*) FROM orders")).scalar()

if os.environ.get("PGUSER") == pg_current_user:
    st.success("OBO is working.")
else:
    st.error("MISMATCH. Check db.py — most likely a global engine was cached.")
```

The full file is ~140 lines and it lives in the notebook's Cell 5. Two things to internalize:

1. **No `WHERE` clause anywhere in the orders query.** RLS does the filtering.
2. **Every tab calls `get_engine()` again.** The engine is rebuilt from env vars on every request.

---

## Step 5 — Write `app.yaml` and `requirements.txt`

The manifest is the only place you bind the Lakebase project. Notice: **no secrets, no passwords, no DB connection string** — everything is identity-driven.

### Cell 6 — write app.yaml + requirements.txt

```python
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
```

**Read the manifest carefully.** No `password:` field. No `token:` field. No connection string. The only credential the runtime needs comes from the resource binding — `valueFrom: lakebase-tutorial` tells it to fill `PGHOST` from the Lakebase project named `lakebase-tutorial`. Per-user authorization happens at the Postgres row level via the OAuth token, not anywhere in this file.

---

## Step 6 — Add an RLS policy to `orders`

Right now the `orders` table from Module 3 has no RLS — every connection sees every row. That works in dev (one user) but defeats the entire point of OBO. Add a policy so `current_user` has something to enforce, then verify on the notebook side before deploying.

### Cell 7 — add column, seed, apply RLS

```python
with engine.begin() as conn:
    # 1. Add user_email column if missing (idempotent)
    conn.execute(text("""
        ALTER TABLE orders
        ADD COLUMN IF NOT EXISTS user_email TEXT
    """))

    # 2. Backfill 3 rows to the current user
    conn.execute(text("""
        UPDATE orders SET user_email = :me
        WHERE id IN (SELECT id FROM orders ORDER BY id LIMIT 3)
          AND (user_email IS NULL OR user_email = '')
    """), {"me": TUTORIAL["user"]})

    # 3. Add 2 rows for a fictional second user
    conn.execute(text("""
        INSERT INTO orders (sku, qty, total_cents, user_email)
        VALUES
            ('LB-9047X', 2, 18000, 'demo-other@example.com'),
            ('LB-1234',  1,  4500, 'demo-other@example.com')
        ON CONFLICT DO NOTHING
    """))

    # 4. RLS policy
    conn.execute(text("DROP POLICY IF EXISTS orders_self ON orders"))
    conn.execute(text("""
        CREATE POLICY orders_self ON orders
            USING (user_email = current_user)
    """))
    conn.execute(text("ALTER TABLE orders ENABLE ROW LEVEL SECURITY"))

# Verify on the notebook side
with engine.connect() as conn:
    visible = conn.execute(text("SELECT count(*) FROM orders")).scalar()
    by_user = conn.execute(text("""
        SELECT user_email, count(*) AS n
        FROM orders GROUP BY user_email ORDER BY n DESC
    """)).fetchall()

print(f"RLS policy 'orders_self' active")
print(f"   Rows visible to YOU ({TUTORIAL['user']}): {visible}")
for row in by_user:
    marker = " <- YOU" if row.user_email == TUTORIAL["user"] else ""
    print(f"   {(row.user_email or '(NULL)'):<32} {row.n}{marker}")
```

**What just happened.** `orders` has a `user_email` column, an RLS policy `orders_self` that filters by `current_user`, and RLS is enabled. From this notebook you only see your own rows — because the notebook itself runs as your identity, the same model the App will use. **No `WHERE` clause** in the app's query is doing this work; RLS is.

---

## Step 7 — Deploy the app

Two ways: (a) the `databricks` CLI from a notebook shell cell, or (b) the SDK's Apps API. Both end with the same artifact — a running app at a URL the workspace surfaces.

### Cell 8 — deploy via the Databricks CLI (preferred)

```python
deploy_cmd = (
    f"databricks apps deploy "
    f"--source-code-path {APP_DIR} "
    f"{TUTORIAL['app_name']}"
)

print(f"Running: $ {deploy_cmd}")
print("This takes 2-4 minutes the first time.")

try:
    out = subprocess.run(deploy_cmd.split(),
                         capture_output=True, text=True, timeout=600)
    print("STDOUT:", out.stdout)
    if out.stderr: print("STDERR:", out.stderr)
except FileNotFoundError:
    print("`databricks` CLI not found. Use Cell 8b instead.")
except subprocess.TimeoutExpired:
    print("Deploy timed out. Check the Apps UI manually.")
```

### Cell 8b — deploy via the SDK (fallback)

```python
from databricks.sdk.service.apps import App, AppResource, AppResourceDatabase

try:
    app_obj = w.apps.create_and_wait(app=App(
        name=TUTORIAL["app_name"],
        description="Module 7 lab - Lakebase Orders demo",
        resources=[AppResource(
            name=TUTORIAL["project_name"],
            description="Module 3 Lakebase project",
            database=AppResourceDatabase(
                instance_name=TUTORIAL["project_name"],
                permission="CAN_USE",
            ),
        )],
    ))
    deployment = w.apps.deploy_and_wait(
        app_name=TUTORIAL["app_name"],
        source_code_path=APP_DIR,
    )
    print(f"Deployed.  state={deployment.status.state}")
except Exception as e:
    if "already exists" in str(e):
        deployment = w.apps.deploy_and_wait(
            app_name=TUTORIAL["app_name"],
            source_code_path=APP_DIR,
        )
        print(f"Redeployed.  state={deployment.status.state}")
    else:
        raise
```

### Cell 9 — surface the URL

```python
app = w.apps.get(name=TUTORIAL["app_name"])
state = app.compute_status.state if app.compute_status else "unknown"
print(f"  APP NAME: {app.name}")
print(f"  STATE:    {state}")
print(f"  URL:      {app.url}")
```

---

## Step 8 — The two-user verification (the trap-killer)

This is the single most important step in Module 7. **The connection-pool trap from theory 7.3 is silent in dev.** It only manifests when a second identity hits the app.

### 8a — What you do

1. Open the app URL (printed by Cell 9) in your browser. Click the **Identity Check** tab.
2. Read off three values: `PGUSER`, `current_user`, `Orders visible to me`.
3. Send the URL to a teammate. They click **Identity Check** in their own browser.
4. Compare. **All three values must differ between you and your teammate.**

### 8b — What you should see

| What | YOU should see | YOUR TEAMMATE should see |
|---|---|---|
| `PGUSER` | your email | their email |
| `current_user` (Postgres) | your email | their email |
| Orders visible | rows where `user_email = you` | rows where `user_email = them` (likely 0) |

**If your teammate's panel shows YOUR identity, you have shipped the trap.** Re-check `db.py` — most likely a global `engine = create_engine(...)` got introduced somewhere outside `get_engine()`.

### 8c — Programmatic checklist

### Cell 10 — final pass/fail

```python
checks = []

# 1. App exists and is running
try:
    a = w.apps.get(name=TUTORIAL["app_name"])
    state = a.compute_status.state if a.compute_status else "unknown"
    checks.append(("App deployed and running", state, state in ("RUNNING","ACTIVE")))
except Exception as e:
    checks.append(("App deployed and running", str(e)[:30], False))

# 2. RLS policy exists
try:
    with engine.connect() as conn:
        n = conn.execute(text("""
            SELECT count(*) FROM pg_policies
            WHERE tablename='orders' AND policyname='orders_self'
        """)).scalar()
    checks.append(("RLS policy 'orders_self'", f"{n} found", n == 1))
except Exception as e:
    checks.append(("RLS policy 'orders_self'", str(e)[:30], False))

# 3. RLS enabled on orders
try:
    with engine.connect() as conn:
        enabled = conn.execute(text("""
            SELECT relrowsecurity FROM pg_class WHERE relname='orders'
        """)).scalar()
    checks.append(("RLS enabled on orders", "yes" if enabled else "no", bool(enabled)))
except Exception as e:
    checks.append(("RLS enabled on orders", str(e)[:30], False))

# 4. RLS demonstrably filters
try:
    with engine.connect() as conn:
        mine = conn.execute(text("SELECT count(*) FROM orders")).scalar()
        all_users = conn.execute(text(
            "SELECT count(DISTINCT user_email) FROM orders"
        )).scalar()
    ok = mine > 0 and all_users >= 2
    checks.append(("RLS filters notebook session",
                   f"{mine} visible, {all_users} users", ok))
except Exception as e:
    checks.append(("RLS filters notebook session", str(e)[:30], False))

# 5. db.py uses Pattern 1
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
    print(f"{name:<38} {str(detail)[:22]:<22} {'PASS' if ok else 'FAIL'}")
print("=" * 70)
passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\nALL {total} CHECKS PASSED - Module 7 complete.")
else:
    print(f"\n{passed}/{total} passed. Resolve FAIL rows before Module 8.")
```

**Expected output (the success case):**

```
======================================================================
CHECK                                  DETAIL                 STATUS
----------------------------------------------------------------------
App deployed and running               RUNNING                PASS
RLS policy 'orders_self'               1 found                PASS
RLS enabled on orders                  yes                    PASS
RLS filters notebook session           3 visible, 2 users     PASS
db.py uses Pattern 1 (no global)       factory only           PASS
app.yaml has no secrets                clean                  PASS
======================================================================

ALL 6 CHECKS PASSED - Module 7 complete.
```

### 8d — Cleanup (optional)

```python
CLEANUP_APP = False

if CLEANUP_APP:
    try:
        w.apps.delete(name=TUTORIAL["app_name"])
        print(f"Deleted app '{TUTORIAL['app_name']}'.")
    except Exception as e:
        print(f"Delete failed: {e}")
else:
    print("Cleanup skipped. App still running - keep demoing.")
    print(f"   App:  {TUTORIAL['app_name']}")
    print("   Cost: Apps runtime billed by hour while running.")
```

Module 8 (the capstone) reuses the same `app/` skeleton, the same per-request engine pattern, and the same RLS approach. **Most readers leave `CLEANUP_APP=False`** so they can keep demoing the app.

---

## Troubleshooting reference

If your final checklist has any FAIL rows, find the row in the table below and apply the fix. Don't proceed to Module 8 with failing checks — the capstone reuses this exact app skeleton.

| FAIL Row | Most common cause | Resolution path |
|---|---|---|
| **App deployed and running** | Cell 8 errored out, or app is starting | Wait 60s and re-run Cell 9. If state stays `STOPPED`, check Cell 8 stderr |
| **RLS policy 'orders_self'** | Cell 7 errored mid-DDL | Re-run Cell 7 — it's idempotent |
| **RLS enabled on orders** | `ALTER TABLE … ENABLE ROW LEVEL SECURITY` was skipped | Re-run Cell 7's last `ALTER TABLE` line |
| **RLS filters notebook session** | Only 1 user_email value in the table | Re-run Cell 7 — it inserts the `demo-other@example.com` rows |
| **db.py uses Pattern 1 (no global)** | Someone edited `db.py` and added a global engine | Re-run Cell 4 to overwrite |
| **app.yaml has no secrets** | A `password:` or `token:` line was added by hand | Re-run Cell 6 to overwrite |

---

## What you've accomplished

If your final cell printed `ALL 6 CHECKS PASSED` AND the manual two-user verification confirmed distinct identities, you have done — for real, not in slides:

- ✅ Wrote a real `app.yaml` that binds the Lakebase project with **zero secrets**
- ✅ Built a per-request engine factory (`db.py`) that follows Pattern 1 from theory 7.3
- ✅ Wrote a Streamlit UI (`app.py`) that **demonstrates RLS-enforced isolation with no `WHERE` clause** — the wall is in Postgres, not the app
- ✅ Added an RLS policy on `orders` so OBO has something concrete to enforce
- ✅ Deployed the app via the Databricks CLI (or SDK) and got a live URL
- ✅ **Verified with a teammate that two different workspace identities produce two different views of the same data** — the trap-killer step
- ✅ Confirmed `db.py` uses no global engine and `app.yaml` carries no secrets — the two static checks every Lakebase-app PR review should perform

These six capabilities are exactly what every customer Apps + Lakebase build needs. Module 8 (capstone) wraps the Module 6 agent loop into this same skeleton — same `db.py`, same `app.yaml` pattern, same two-user verification. **Save this notebook somewhere you can find it again.**

---

## Module 7 — complete

You finished four theory topics (7.1–7.4) and the hands-on lab. You now have:

- A live Databricks App, bound to the Module 3 Lakebase project, deployed at a URL your workspace can reach
- An RLS-protected `orders` table proving identity flows from browser → runtime → Postgres connection without a single `WHERE` clause in app code
- A reviewable `db.py` and `app.yaml` that pass every static check on a Lakebase-app PR
- The muscle memory to spot the connection-pool trap on sight

**Next up: Module 8** — the *capstone*. You'll glue Modules 3, 4, 5, 6, and 7 together into a single Streamlit-deployed customer-support agent. The per-request engine pattern from this lab is the substrate the entire capstone is built on.
