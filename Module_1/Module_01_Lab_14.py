# Databricks notebook source
# MAGIC %md
# MAGIC # Module 01 · Lab 1.4 — Workspace Prerequisites & Sanity Check
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~30 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Verify your workspace is ready to run every other lab in this Lakebase Mastery roadmap. If this notebook completes end-to-end with all ✅, every Module 2–9 lab will work. If anything fails, the diagnostic table at the end tells you exactly which lever to pull.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook validates
# MAGIC
# MAGIC 1. **Databricks SDK** is installed at a version that exposes the `database` namespace (≥ 0.30.0)
# MAGIC 2. Your user has the **DATABASE_CREATE** entitlement (or you're a workspace admin)
# MAGIC 3. Your workspace is in a **region where Lakebase Autoscaling is generally available**
# MAGIC 4. The **Foundation Models API** is reachable (Modules 5 and 6 require embeddings + chat)
# MAGIC 5. You have a **Unity Catalog schema** to use as a sandbox for the rest of the tutorial
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Attach this notebook to a serverless cluster (recommended) or a standard cluster on DBR 14+. All cells are Python except where SQL is explicitly called out via `%sql`. Run the cells top-to-bottom, fix anything that fails using the troubleshooting table inline, and finish with the automated checklist in Cell 7.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Install the SDK and dependencies
# MAGIC
# MAGIC Lakebase requires `databricks-sdk >= 0.30.0` for the `database` namespace, plus a few common Postgres clients you'll use in later modules.
# MAGIC
# MAGIC **Expected behavior:** the cell completes in 30–90 seconds, then the Python kernel restarts automatically.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `Could not find a version that satisfies databricks-sdk>=0.30.0` | Cluster air-gapped from PyPI | Install from a workspace artifact repo, or talk to your platform team |
# MAGIC | `Failed to install psycopg` | Older runtime missing system libs | Use a newer Databricks Runtime (DBR 14+) or serverless |
# MAGIC | Cell hangs forever | Cluster still spinning up | Wait for cluster status green, then re-run |

# COMMAND ----------

# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Confirm your identity and workspace
# MAGIC
# MAGIC Verify the SDK is wired up correctly and you can see your own workspace context.
# MAGIC
# MAGIC **Expected output (yours will differ in the obvious way):**
# MAGIC
# MAGIC ```
# MAGIC User:       you@example.com
# MAGIC Workspace:  https://your-workspace.cloud.databricks.com
# MAGIC Auth type:  oauth
# MAGIC ```
# MAGIC
# MAGIC **What this proves:** the SDK can authenticate, find your workspace, and identify you. If this works, every later Lakebase API call will too.
# MAGIC
# MAGIC | Symptom on failure | Meaning | Fix |
# MAGIC |---|---|---|
# MAGIC | `default auth: cannot configure default credentials` | SDK can't find auth context | Re-attach to the cluster; ensure you're logged in to Databricks |
# MAGIC | `403 Forbidden` on `current_user.me()` | Workspace identity misconfigured | Talk to a workspace admin |
# MAGIC | Empty `user_name` field | Authenticated as a service principal | Acceptable; note this for Module 7 (OBO assumes user identity) |

# COMMAND ----------

# Cell 2 — identity check
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

print("User:      ", w.current_user.me().user_name)
print("Workspace: ", w.config.host)
print("Auth type: ", w.config.auth_type)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Verify the DATABASE_CREATE entitlement
# MAGIC
# MAGIC The ability to create Lakebase projects is gated by the `DATABASE_CREATE` entitlement. The cleanest way to check is to *try* listing projects — if that call doesn't error, you're authorized to operate on the database namespace.
# MAGIC
# MAGIC **Expected output:** zero or more projects listed without error. An empty list (`Found 0 existing project(s):`) is fine — it just means nobody has created one in this workspace yet.
# MAGIC
# MAGIC **What this proves:** you have permission to query the database namespace, and Lakebase is enabled in your workspace's region.
# MAGIC
# MAGIC > ⚠ **Region check is the #1 thing RSAs forget.** If this cell errors with a region-related message, *every other lab in the roadmap will fail in the same way*. Resolve this now, not in Module 3.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `PermissionDenied: Missing entitlement DATABASE_CREATE` | User lacks the entitlement | Admin → Identity & Access → Entitlements; grant DATABASE_CREATE |
# MAGIC | `NotFound` or "Lakebase is not available in this region" | Region doesn't have Lakebase Autoscaling yet | Use a workspace in a supported region (US widely available) |
# MAGIC | `ConnectionError` | Network issue | Reconnect to the cluster; check VPN/proxy if applicable |

# COMMAND ----------

# Cell 3 — entitlement & region check
projects = list(w.database.list_database_projects())
print(f"Found {len(projects)} existing project(s):")
for p in projects:
    print(f"  · {p.name} ({p.uid})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Verify Foundation Models API access
# MAGIC
# MAGIC Modules 5 (vector search) and 6 (agent memory) both call the Foundation Models API for embeddings and chat. Cells 4a and 4b verify both endpoints are reachable.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 4a — Embeddings
# MAGIC
# MAGIC **Expected output:**
# MAGIC ```
# MAGIC Model:           databricks-bge-large-en
# MAGIC Vector dim:      1024
# MAGIC First 3 values:  [0.0123, -0.0456, 0.0789]   # actual numbers will differ
# MAGIC ```
# MAGIC
# MAGIC **What this proves:** Foundation Models embeddings are reachable, returning the expected 1024-dim BGE vectors. Module 5's RAG pipeline will work.

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 4b — Chat completion
# MAGIC
# MAGIC **Expected output:** a single sentence describing OLTP. Exact wording will differ run-to-run; what matters is that the call succeeded.
# MAGIC
# MAGIC **What this proves:** Foundation Models chat is reachable. Module 6's agent loop will work.
# MAGIC
# MAGIC | Symptom on failure (4a or 4b) | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `404 model not found` | Foundation Models not enabled, or different model names | Check `w.serving_endpoints.list()`; talk to admin |
# MAGIC | `429 Too many requests` | Rate-limited at workspace level | Wait 30s and retry |
# MAGIC | `403 Forbidden` | User lacks `Can Query` on the serving endpoints | Serving → endpoint → permissions → grant Can Query |

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Reserve a Unity Catalog schema for tutorial assets
# MAGIC
# MAGIC Every later lab will write something to UC: synced table targets, Lakehouse Sync targets, registered Lakebase catalogs. You want a single, clearly-labeled schema to keep all that tutorial work together.
# MAGIC
# MAGIC > **Customize as needed.** If you don't have permissions on `main`, edit the `CATALOG` and `SCHEMA` widgets at the top of this section to point at a catalog you do own (for example, `users` and `your_user_lakebase_tutorial`). The labs only require *some* schema you can write to.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `CREATE SCHEMA permission denied` | You don't own the catalog | Use a catalog you own; ask admin for USE CATALOG + CREATE SCHEMA |
# MAGIC | `Catalog 'main' not found` | Workspace uses a non-default catalog name | Run `SHOW CATALOGS` and pick one |

# COMMAND ----------

# Cell 5a — set up parameters using widgets so you can change them without editing code
dbutils.widgets.text("catalog", "main",              "UC catalog (must be one you own)")
dbutils.widgets.text("schema",  "lakebase_tutorial", "Schema name for tutorial assets")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA  = dbutils.widgets.get("schema")
print(f"Will use:  {CATALOG}.{SCHEMA}")

# COMMAND ----------

# Cell 5b — create or confirm the tutorial schema
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"SHOW SCHEMAS IN {CATALOG}").show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Save your tutorial config
# MAGIC
# MAGIC So you don't have to re-type the same constants in every Module 2–9 lab, save them as workspace-scoped variables now. Keep this cell as the second cell of every notebook in the roadmap. The labs assume `TUTORIAL["project_name"]` is `lakebase-tutorial` — change it once here if you want a different name and the rest follows.

# COMMAND ----------

# Cell 6 — tutorial constants you'll reuse
TUTORIAL = {
    "catalog":      CATALOG,
    "schema":       SCHEMA,
    "project_name": "lakebase-tutorial",
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
}

print("Tutorial config:")
for k, v in TUTORIAL.items():
    print(f"  {k:14s}  {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — The pass/fail checklist
# MAGIC
# MAGIC This cell re-checks every prerequisite and prints a single line per check. The lab is **complete** when every line ends in ✅.
# MAGIC
# MAGIC ### Expected output (the success case)
# MAGIC
# MAGIC ```
# MAGIC ========================================================================
# MAGIC CHECK                            DETAIL                       STATUS
# MAGIC ------------------------------------------------------------------------
# MAGIC Databricks SDK ≥ 0.30.0          0.34.0                       ✅
# MAGIC psycopg importable               yes                          ✅
# MAGIC sqlalchemy importable            yes                          ✅
# MAGIC pgvector importable              yes                          ✅
# MAGIC Workspace identity               you@example.com              ✅
# MAGIC DATABASE_CREATE & region         ok                           ✅
# MAGIC Foundation Models embed          dim=1024                     ✅
# MAGIC Foundation Models chat           ok                           ✅
# MAGIC Tutorial UC schema               main.lakebase_tutorial       ✅
# MAGIC ========================================================================
# MAGIC
# MAGIC 🎯 ALL 9 CHECKS PASSED — workspace is ready for Module 2 onwards.
# MAGIC ```

# COMMAND ----------

# Cell 7 — final automated checklist
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

# Check 2-4: psycopg + sqlalchemy + pgvector importable
for mod in ["psycopg", "sqlalchemy", "pgvector"]:
    try:
        importlib.import_module(mod)
        checks.append((f"{mod} importable", "yes", True))
    except Exception as e:
        checks.append((f"{mod} importable", str(e)[:28], False))

# Check 5: identity
try:
    me = w.current_user.me().user_name
    checks.append(("Workspace identity", me, bool(me)))
except Exception as e:
    checks.append(("Workspace identity", str(e)[:28], False))

# Check 6: DATABASE_CREATE / region
try:
    list(w.database.list_database_projects())
    checks.append(("DATABASE_CREATE & region", "ok", True))
except Exception as e:
    checks.append(("DATABASE_CREATE & region", str(e)[:28], False))

# Check 7: embeddings
try:
    client = w.serving_endpoints.get_open_ai_client()
    r = client.embeddings.create(model="databricks-bge-large-en", input=["ping"])
    checks.append(("Foundation Models embed", f"dim={len(r.data[0].embedding)}", True))
except Exception as e:
    checks.append(("Foundation Models embed", str(e)[:28], False))

# Check 8: chat
try:
    r = client.chat.completions.create(
        model="databricks-meta-llama-3-3-70b-instruct",
        messages=[{"role": "user", "content": "ok?"}],
        max_tokens=5,
    )
    checks.append(("Foundation Models chat", "ok", True))
except Exception as e:
    checks.append(("Foundation Models chat", str(e)[:28], False))

# Check 9: tutorial schema
try:
    spark.sql(f"DESCRIBE SCHEMA {TUTORIAL['catalog']}.{TUTORIAL['schema']}").collect()
    checks.append(("Tutorial UC schema", f"{TUTORIAL['catalog']}.{TUTORIAL['schema']}", True))
except Exception as e:
    checks.append(("Tutorial UC schema", str(e)[:28], False))

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
    print("   See the troubleshooting table below.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Master troubleshooting reference
# MAGIC
# MAGIC If your final checklist has any ❌ rows, find the row in the table below and apply the fix. **Don't proceed to Module 2 with failing checks** — Module 3's labs will fail on the same root cause and you'll waste an hour debugging.
# MAGIC
# MAGIC | ❌ Failing row | Most common cause | Resolution path |
# MAGIC |---|---|---|
# MAGIC | **SDK ≥ 0.30.0** | Older SDK pinned in cluster libraries | Re-run `%pip install --upgrade databricks-sdk` in cell 1, then `dbutils.library.restartPython()` |
# MAGIC | **psycopg / sqlalchemy / pgvector importable** | Install didn't take | Re-run cell 1; ensure `restartPython()` ran; verify with `import psycopg` in a fresh cell |
# MAGIC | **Workspace identity** | Auth context lost after kernel restart | Detach + reattach the cluster, then re-run from cell 2 |
# MAGIC | **DATABASE_CREATE & region** (entitlement) | User lacks entitlement | Admin Console → Identity & Access → grant DATABASE_CREATE to your user/group |
# MAGIC | **DATABASE_CREATE & region** (region) | Lakebase Autoscaling not yet GA in this region | Use a workspace in a supported region; Module 9 includes a region matrix |
# MAGIC | **FM embed / FM chat** (404) | Foundation Models endpoint not provisioned | `w.serving_endpoints.list()` to see what's available; talk to admin |
# MAGIC | **FM embed / FM chat** (403) | User lacks Can Query on the endpoint | Serving → endpoint → permissions → grant Can Query |
# MAGIC | **Tutorial UC schema** | No CREATE SCHEMA on `main` | Use a catalog you own; update the `catalog` widget and re-run from Step 5 |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Lab 1.4 — complete when…
# MAGIC
# MAGIC If your final cell printed `🎯 ALL 9 CHECKS PASSED`, you can:
# MAGIC
# MAGIC - ✅ Programmatically authenticate as yourself in the workspace
# MAGIC - ✅ Provision Lakebase resources via SDK (the gate for Module 3)
# MAGIC - ✅ Generate embeddings and call chat models (the gate for Modules 5 & 6)
# MAGIC - ✅ Write to a UC schema (the gate for Modules 4 & 8)
# MAGIC - ✅ Use psycopg, SQLAlchemy, and pgvector at the Python layer (the gate for everything)
# MAGIC
# MAGIC **That's every prerequisite for the entire roadmap, validated in one notebook.** Save this notebook somewhere you can find it again — if any later lab errors mysteriously, re-running this checklist is the fastest way to diagnose what changed.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Module 1 — complete
# MAGIC
# MAGIC You finished the three theory topics (1.1–1.3) and the prerequisite lab (1.4). You now have:
# MAGIC
# MAGIC - The vocabulary to describe Lakebase to a customer
# MAGIC - A workspace verified ready for every lab ahead
# MAGIC - The decision rule for Autoscaling vs Provisioned
# MAGIC
# MAGIC **Next up: Module 2** — the architecture deep dive. Separated compute and storage, copy-on-write branching internals, scale-to-zero mechanics, and the OAuth token TTL trap that ruins Friday afternoons.
