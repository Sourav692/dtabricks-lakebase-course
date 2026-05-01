# Module 01 · Lab 1.4 — Workspace Prerequisites & Sanity Check

> **Type:** Hands-on · **Duration:** ~30 minutes · **Format:** Databricks notebook
> **Purpose:** Verify your workspace is ready to run every other lab in this roadmap. If this lab passes end-to-end, every Module 2–9 lab will work. If it fails, the diagnostic table at the end tells you exactly which lever to pull.

---

## Why this lab exists

Every later lab in this roadmap assumes five things are true about your environment:

1. The **Databricks SDK** is installed at a recent enough version to expose the `database` namespace
2. Your user has the **DATABASE_CREATE** entitlement (or you're a workspace admin)
3. Your workspace is in a **region where Lakebase Autoscaling is generally available**
4. The **Foundation Models API** is reachable (Modules 5 and 6 require embeddings + chat)
5. You have a **Unity Catalog schema** to use as a sandbox for the rest of the tutorial

If any of those is wrong, the natural first place you'd discover it is halfway through Module 3 when `create_database_project()` returns a confusing error. We'd rather you discover it now, in 30 quiet minutes, than there.

> **Run mode.** Do this lab in a Databricks notebook attached to a serverless cluster. All cells run in Python except where SQL is explicitly called out. Copy each cell into your own notebook as you go.

---

## Prerequisites before you start

You need:

- Access to a Databricks workspace where you can create resources
- An attached compute cluster (serverless is fine, and recommended)
- Permissions to create a Unity Catalog schema, OR an existing schema you own

You do **not** need:

- An existing Lakebase project (you'll create one in Module 3)
- Any data loaded anywhere
- Foundation Models endpoints to be specifically configured (they're available by default in supported regions)

---

## Step 1 — Install the SDK and dependencies

Lakebase requires `databricks-sdk >= 0.30.0` for the `database` namespace, plus a few common Postgres clients you'll use in later modules.

```python
# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()
```

**Expected behavior:** the cell completes in 30–90 seconds. The Python kernel restarts automatically (that's the `restartPython()` line).

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: Could not find a version that satisfies the requirement databricks-sdk>=0.30.0` | Your cluster is air-gapped from PyPI | Install from a workspace artifact repo, or talk to your platform team about PyPI proxy access |
| `Failed to install psycopg` | Older runtime missing system libs | Use a newer Databricks Runtime (DBR 14+) or serverless |
| Cell hangs forever | Cluster still spinning up | Wait for the cluster status to go green, then re-run |

---

## Step 2 — Confirm your identity and workspace

Now we verify the SDK is wired up correctly and you can see your own workspace context.

```python
# Cell 2 — identity check
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

print("User:      ", w.current_user.me().user_name)
print("Workspace: ", w.config.host)
print("Auth type: ", w.config.auth_type)
```

**Expected output (yours will differ in the obvious way):**

```
User:       you@example.com
Workspace:  https://your-workspace.cloud.databricks.com
Auth type:  oauth
```

**What this proves:** the SDK can authenticate, find your workspace, and identify you. If this works, every later Lakebase API call will too.

**If it fails:**

| Symptom | Meaning | Fix |
|---|---|---|
| `default auth: cannot configure default credentials` | SDK can't find auth context | Re-attach to the cluster; ensure you're logged in to Databricks |
| `403 Forbidden` on `current_user.me()` | Workspace identity is misconfigured | Talk to a workspace admin |
| Empty `user_name` field | You're authenticated as a service principal, not a user | Acceptable, but note this for Module 7 (OBO patterns assume user identity) |

---

## Step 3 — Verify the DATABASE_CREATE entitlement

The ability to create Lakebase projects is gated by the `DATABASE_CREATE` entitlement. The cleanest way to check is to *try* listing projects — if that call doesn't error, you're authorized to operate on the database namespace.

```python
# Cell 3 — entitlement & region check
projects = list(w.database.list_database_projects())
print(f"Found {len(projects)} existing project(s):")
for p in projects:
    print(f"  · {p.name} ({p.uid})")
```

**Expected output:** zero or more projects listed without error. An empty list (`Found 0 existing project(s)`) is fine — it just means nobody has created one in this workspace yet.

**What this proves:** you have permission to query the database namespace, and Lakebase is enabled in your workspace's region.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `PermissionDenied: Missing entitlement DATABASE_CREATE` | Your user lacks the entitlement | Admin → Identity & Access → Entitlements; grant DATABASE_CREATE to your user or a group you're in |
| `NotFound` or "Lakebase is not available in this region" | Region doesn't yet have Lakebase Autoscaling | Check the docs region matrix; for tutorial purposes ask your admin to use a US region workspace |
| `ConnectionError` | Network issue | Reconnect to the cluster; check VPN/proxy if applicable |

> **⚠ Region check is the #1 thing RSAs forget.** If this cell errors with a region-related message, *every other lab in the roadmap will fail in the same way*. Resolve this now, not in Module 3.

---

## Step 4 — Verify Foundation Models API access

Modules 5 (vector search) and 6 (agent memory) both call the Foundation Models API for embeddings and chat. Let's make sure both endpoints are reachable.

### Step 4a — Embeddings

```python
# Cell 4a — embeddings sanity check
client = w.serving_endpoints.get_open_ai_client()

resp = client.embeddings.create(
    model="databricks-bge-large-en",
    input=["Lakebase brings OLTP into the lakehouse."],
)

vec = resp.data[0].embedding
print(f"Model:           databricks-bge-large-en")
print(f"Vector dim:      {len(vec)}")
print(f"First 3 values:  {vec[:3]}")
```

**Expected output:**

```
Model:           databricks-bge-large-en
Vector dim:      1024
First 3 values:  [0.0123, -0.0456, 0.0789]   # actual numbers will differ
```

**What this proves:** Foundation Models embeddings are reachable, returning the expected 1024-dim BGE vectors. Module 5's RAG pipeline will work.

### Step 4b — Chat completion

```python
# Cell 4b — chat sanity check
out = client.chat.completions.create(
    model="databricks-meta-llama-3-3-70b-instruct",
    messages=[
        {"role": "system", "content": "Reply in exactly one sentence."},
        {"role": "user", "content": "What is OLTP, in one sentence?"},
    ],
    temperature=0.0,
    max_tokens=80,
)
print(out.choices[0].message.content)
```

**Expected output:** a single sentence describing OLTP. The exact wording will differ run-to-run; what matters is that the call succeeded.

**What this proves:** Foundation Models chat is reachable. Module 6's agent loop will work.

**If either 4a or 4b fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `404 model not found` | Foundation Models not enabled in this workspace, or different model names in your region | Talk to admin; check `w.serving_endpoints.list()` for available models |
| `429 Too many requests` | Rate-limited at the workspace level | Wait 30s and retry; for tutorial-scale work this should not persist |
| `403 Forbidden` | Your user lacks `Can Query` on the serving endpoints | Admin → Serving → endpoint → grant Can Query |

---

## Step 5 — Reserve a Unity Catalog schema for tutorial assets

Every later lab will write something to UC: synced table targets, Lakehouse Sync targets, registered Lakebase catalogs. You want a single, clearly-labeled schema to keep all that tutorial work together.

```python
# Cell 5 — create or confirm a tutorial schema
spark.sql("CREATE SCHEMA IF NOT EXISTS main.lakebase_tutorial")
spark.sql("SHOW SCHEMAS IN main").show(truncate=False)
```

**Expected output:** a list of schemas in `main`, with `lakebase_tutorial` somewhere in it.

**Customize as needed.** If you don't have permissions on `main`, substitute a catalog you do own (for example `users.${your_user}.lakebase_tutorial`). The labs only require *some* schema you can write to — the exact path is yours.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `CREATE SCHEMA permission denied` | You don't own the catalog | Use a catalog you do own, or ask an admin to grant USE CATALOG + CREATE SCHEMA |
| `Catalog 'main' not found` | Workspace uses a non-default catalog name | Run `SHOW CATALOGS` and pick one |

---

## Step 6 — Save your tutorial config (optional but recommended)

So you don't have to re-type the same constants in every Module 2–9 lab, save them as workspace-scoped variables now.

```python
# Cell 6 — tutorial constants you'll reuse
TUTORIAL = {
    "catalog":        "main",
    "schema":         "lakebase_tutorial",
    "project_name":   "lakebase-tutorial",
    "embed_model":    "databricks-bge-large-en",
    "chat_model":     "databricks-meta-llama-3-3-70b-instruct",
    "user":           w.current_user.me().user_name,
    "host":           w.config.host,
}
print("Tutorial config:")
for k, v in TUTORIAL.items():
    print(f"  {k:14s}  {v}")
```

Keep this cell as the second cell of every notebook in the roadmap. The labs assume `TUTORIAL["project_name"]` is `lakebase-tutorial` — change it once here if you want a different name and the rest follows.

---

## Step 7 — The pass/fail checklist

Run this final cell. It re-checks every prerequisite and prints a single line per check. The lab is **complete** when every line ends in `✅`.

```python
# Cell 7 — final checklist
import importlib

checks = []

# Check 1: SDK version
try:
    import databricks.sdk
    v = databricks.sdk.version.__version__
    ok = tuple(int(x) for x in v.split(".")[:2]) >= (0, 30)
    checks.append(("Databricks SDK ≥ 0.30.0", v, ok))
except Exception as e:
    checks.append(("Databricks SDK ≥ 0.30.0", str(e), False))

# Check 2: psycopg + sqlalchemy + pgvector importable
for mod in ["psycopg", "sqlalchemy", "pgvector"]:
    try:
        importlib.import_module(mod)
        checks.append((f"{mod} importable", "yes", True))
    except Exception as e:
        checks.append((f"{mod} importable", str(e), False))

# Check 3: identity
try:
    me = w.current_user.me().user_name
    checks.append(("Workspace identity", me, bool(me)))
except Exception as e:
    checks.append(("Workspace identity", str(e), False))

# Check 4: DATABASE_CREATE / region
try:
    list(w.database.list_database_projects())
    checks.append(("DATABASE_CREATE & region", "ok", True))
except Exception as e:
    checks.append(("DATABASE_CREATE & region", str(e)[:60], False))

# Check 5: embeddings
try:
    client = w.serving_endpoints.get_open_ai_client()
    r = client.embeddings.create(model="databricks-bge-large-en", input=["ping"])
    checks.append(("Foundation Models embed", f"dim={len(r.data[0].embedding)}", True))
except Exception as e:
    checks.append(("Foundation Models embed", str(e)[:60], False))

# Check 6: chat
try:
    r = client.chat.completions.create(
        model="databricks-meta-llama-3-3-70b-instruct",
        messages=[{"role":"user","content":"ok?"}],
        max_tokens=5,
    )
    checks.append(("Foundation Models chat", "ok", True))
except Exception as e:
    checks.append(("Foundation Models chat", str(e)[:60], False))

# Check 7: tutorial schema
try:
    spark.sql(f"DESCRIBE SCHEMA {TUTORIAL['catalog']}.{TUTORIAL['schema']}").collect()
    checks.append(("Tutorial UC schema", f"{TUTORIAL['catalog']}.{TUTORIAL['schema']}", True))
except Exception as e:
    checks.append(("Tutorial UC schema", str(e)[:60], False))

# Print result
print("=" * 72)
print(f"{'CHECK':<32} {'DETAIL':<28} STATUS")
print("-" * 72)
for name, detail, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"{name:<32} {str(detail)[:28]:<28} {icon}")
print("=" * 72)

passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\n🎯 ALL {total} CHECKS PASSED — workspace is ready for Module 2 onwards.")
else:
    print(f"\n⚠ {passed}/{total} passed. Resolve the ❌ rows before moving on.")
```

### Expected output (the success case)

```
========================================================================
CHECK                            DETAIL                       STATUS
------------------------------------------------------------------------
Databricks SDK ≥ 0.30.0          0.34.0                       ✅
psycopg importable               yes                          ✅
sqlalchemy importable            yes                          ✅
pgvector importable              yes                          ✅
Workspace identity               you@example.com              ✅
DATABASE_CREATE & region         ok                           ✅
Foundation Models embed          dim=1024                     ✅
Foundation Models chat           ok                           ✅
Tutorial UC schema               main.lakebase_tutorial       ✅
========================================================================

🎯 ALL 9 CHECKS PASSED — workspace is ready for Module 2 onwards.
```

---

## Troubleshooting reference

If your final checklist has any ❌ rows, find the row in the table below and apply the fix. Don't proceed to Module 2 with failing checks — Module 3's labs will fail on the same root cause and you'll waste an hour debugging.

| ❌ Row | Most common cause | Resolution path |
|---|---|---|
| **SDK ≥ 0.30.0** | Older SDK pinned in cluster libraries | Re-run `%pip install --upgrade databricks-sdk` in cell 1, then `dbutils.library.restartPython()` |
| **psycopg / sqlalchemy / pgvector importable** | Install didn't take | Re-run cell 1; ensure `restartPython()` ran; verify with `import psycopg` in a fresh cell |
| **Workspace identity** | Auth context lost after kernel restart | Detach + reattach the cluster, then re-run from cell 2 |
| **DATABASE_CREATE & region** (entitlement) | User lacks entitlement | Admin Console → Identity & Access → your user/group → grant DATABASE_CREATE |
| **DATABASE_CREATE & region** (region) | Lakebase Autoscaling not yet GA in your region | Use a workspace in a supported region; Module 9 includes a region matrix |
| **FM embed / FM chat** (404) | Foundation Models endpoint not provisioned | `w.serving_endpoints.list()` to see what's available; talk to admin |
| **FM embed / FM chat** (403) | User lacks Can Query on the endpoint | Serving → endpoint → permissions → grant Can Query |
| **Tutorial UC schema** | No CREATE SCHEMA on `main` | Use a catalog you own; update `TUTORIAL['catalog']` accordingly |

---

## What you've accomplished

If your final cell printed `🎯 ALL 9 CHECKS PASSED`, you can:

- ✅ Programmatically authenticate as yourself in the workspace
- ✅ Provision Lakebase resources via SDK (the gate for Module 3)
- ✅ Generate embeddings and call chat models (the gate for Modules 5 & 6)
- ✅ Write to a UC schema (the gate for Modules 4 & 8)
- ✅ Use psycopg, SQLAlchemy, and pgvector at the Python layer (the gate for everything)

**That's every prerequisite for the entire roadmap, validated in one notebook.** Save this notebook somewhere you can find it again — if any later lab errors mysteriously, re-running this checklist is the fastest way to diagnose what changed.

---

## Module 1 — complete

You finished the three theory topics (1.1–1.3) and the prerequisite lab (1.4). You now have:

- The vocabulary to describe Lakebase to a customer
- A workspace verified ready for every lab ahead
- The decision rule for Autoscaling vs Provisioned

**Next up: Module 2** — the architecture deep dive. Separated compute and storage, copy-on-write branching internals, scale-to-zero mechanics, and the OAuth token TTL trap that ruins Friday afternoons.
