# Module 08 · Capstone · Phase 5 — Ship as App + Close the Loop

> **Type:** Hands-on · **Duration:** ~45 minutes · **Format:** Databricks notebook + Databricks App deploy
> **Purpose:** Wrap the Phase 4 agent in a Streamlit UI, deploy it as a real Databricks App with the per-request OBO engine pattern from Module 7, configure Lakehouse Sync to stream `resolutions` back to Delta, and build a DBSQL dashboard over the Delta sink. After this phase, the AskMyOrders loop is closed end-to-end.

---

## Why this phase exists

Phase 4 left you with a `run_turn()` function that works in a notebook. That's not yet a product. A product is something a support agent — not the data engineer — can use, on a real URL, with their own identity. This phase makes that real:

- Wrap `run_turn()` in a Streamlit UI with a chat panel and a customer-context panel
- Use the **per-request OBO engine factory** from Module 7 — every database call runs as the workspace user who's looking at the screen
- Deploy with `databricks apps deploy`, get a live URL
- Run the **two-user verification** that proves OBO is doing its job (the trap-killer step)
- Configure **Lakehouse Sync** to stream `resolutions` back to Delta — closing the analytics loop
- Build a DBSQL dashboard over the Delta sink so leadership can see resolution trends

This phase walks twelve steps. It's the longest, but the prior four phases did the hard work — almost everything here is integration.

1. **Setup** — reconnect, verify CLI + Apps API
2. **Scaffold** the `askmyorders/app/` directory
3. **Write `db.py`** — per-request OBO engine factory (Module 7 Pattern 1)
4. **Write `memory.py`** — `AgentMemory` from Phase 4
5. **Write `retrieve.py`** + `agent.py` — `kb_retrieve()`, `run_turn()`, tools
6. **Write `app.py`** — Streamlit two-pane UI
7. **Write `app.yaml`** + `requirements.txt` — Lakebase resource binding, no secrets
8. **Deploy** with `databricks apps deploy`
9. **Two-user verification** — the trap-killer (manual step with a teammate)
10. **Configure Lakehouse Sync** — `resolutions` → `main.silver.support_resolutions`
11. **Build the DBSQL dashboard** — three tiles
12. **Final verification** — nine green checks (matches the Definition of Done)

> **Run mode.** Top-to-bottom. Cells are Python except for shell-outs at deploy time. **Step 8 runs `databricks apps deploy` — this creates a live URL and bills Apps runtime by hour.** Step 12 leaves the app deployed so you can demo it.

---

## Prerequisites before you start

You need:

- ✅ **Phases 1–4 passed.** This phase builds on every previous phase.
- ✅ **Databricks CLI v0.220+** installed (workspace cluster usually has it; SDK fallback in Cell 10b)
- ✅ **A teammate with workspace access** — required for Step 9's two-user verification
- ✅ **Databricks Apps enabled** in the workspace
- An attached compute cluster (serverless is fine, recommended)

> **⚠ Billing note.** Apps runtime is ~$0.10/hour while running. Lakehouse Sync (Beta in some regions) adds a small managed-pipeline cost. Step 13 (optional cleanup) tears everything down when you're done demoing.

---

## Step 1 — Setup: reconnect, verify CLI, verify Apps

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy
dbutils.library.restartPython()
```

### Cell 2 — reconnect + tooling check

```python
import os, uuid, time, subprocess, json
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

CAPSTONE = {
    "project_name": "askmyorders",
    "uc_catalog":   "askmyorders_db",
    "app_name":     "askmyorders",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
    "lh_target_catalog": "main",
    "lh_target_schema":  "silver",
    "lh_target_table":   "support_resolutions",
}

existing = {p.name: p for p in w.database.list_database_projects()}
project = existing[CAPSTONE["project_name"]]
assert project.state == "READY"

def make_engine():
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[CAPSTONE["project_name"]],
    )
    url = (
        f"postgresql+psycopg://{CAPSTONE['user']}:{cred.token}"
        f"@{project.read_write_dns}:5432/{CAPSTONE['project_name']}?sslmode=require"
    )
    return create_engine(url, pool_pre_ping=True, pool_size=2)

engine = make_engine()

# Verify Phase 4 artefacts are alive
with engine.connect() as conn:
    n_kb     = conn.execute(text("SELECT count(*) FROM kb_documents")).scalar()
    n_orders = conn.execute(text("SELECT count(*) FROM orders_synced")).scalar()
    n_eps    = conn.execute(text("SELECT count(*) FROM episodes")).scalar()

# CLI check
try:
    out = subprocess.run(["databricks", "--version"],
                         capture_output=True, text=True, timeout=10)
    cli_ok, cli_ver = out.returncode == 0, (out.stdout or out.stderr).strip()
except Exception as e:
    cli_ok, cli_ver = False, str(e)[:60]

# Apps API check
try:
    apps = list(w.apps.list())
    apps_ok = True
except Exception as e:
    apps_ok, apps = False, []

print(f"PROJECT '{CAPSTONE['project_name']}'  state={project.state}")
print(f"   kb_documents:   {n_kb} chunks")
print(f"   orders_synced:  {n_orders} rows")
print(f"   episodes:       {n_eps} rows")
print(f"   CLI:            {cli_ver if cli_ok else 'unavailable'}")
print(f"   Apps API:       {'available' if apps_ok else 'unavailable'}")

assert n_kb >= 8 and n_orders == 12 and n_eps >= 1
```

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `kb_documents=0` | Phase 3 cleanup ran | Re-run Phase 3 |
| `episodes=0` | Phase 4 not yet executed | Run Phase 4 to completion first |
| CLI `unavailable` | Not in cluster's PATH | Use Cell 10b (SDK fallback) instead |
| Apps API `unavailable` | Apps not enabled | Ask admin to enable; lab cannot proceed without it |

---

## Step 2 — Scaffold the app directory

The directory layout from the Module 8 conceptual walkthrough — `app/` with seven files. We write everything into the workspace so `databricks apps deploy` can pick them up.

### Cell 3 — scaffold askmyorders/app/

```python
APP_DIR = f"/Workspace/Users/{CAPSTONE['user']}/askmyorders/app"
os.makedirs(APP_DIR, exist_ok=True)

for f in ["app.yaml", "app.py", "db.py", "memory.py", "retrieve.py", "agent.py", "requirements.txt"]:
    p = f"{APP_DIR}/{f}"
    if not os.path.exists(p):
        open(p, "w").close()

print(f"App directory ready: {APP_DIR}")
for f in sorted(os.listdir(APP_DIR)):
    size = os.path.getsize(f"{APP_DIR}/{f}")
    print(f"   {f:<20} ({size} B)")
```

---

## Step 3 — Write `db.py` (the per-request OBO engine factory)

Verbatim Module 7 Pattern 1. Every request creates a fresh engine from the OBO env vars the runtime injects. **No global engine. No `@st.cache_resource` on `get_engine`.** This is the #1 trap from Module 7.3 and the capstone is where we get it right.

**What to look for:**

- `get_engine()` reads env vars on every call — never cached
- No module-level `engine = create_engine(...)` — that's the trap
- `current_user()` reads from `PGUSER` only, never from session state

### Cell 4 — write db.py

```python
DB_PY = '''"""
db.py — per-request engine factory.

Pattern 1 from Module 7 theory 7.3, used unchanged in the capstone.
Every request rebuilds the engine from the OBO env vars (PGUSER,
DATABRICKS_TOKEN, PGHOST, PGDATABASE) the Databricks Apps runtime
injects per request, scoped to the END USER.

DO NOT cache the engine globally.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_engine() -> Engine:
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
    return os.environ["PGUSER"]
'''
with open(f"{APP_DIR}/db.py", "w") as f:
    f.write(DB_PY)
print(f"Wrote db.py ({len(DB_PY)} B)")
```

---

## Step 4 — Write `memory.py` (the `AgentMemory` class from Phase 4)

Same six methods, copied verbatim. The class accepts an `engine` and an `embed_fn` so it remains testable and per-request engines compose cleanly.

### Cell 5 — write memory.py

```python
MEMORY_PY = '''"""memory.py — AgentMemory class. Copied from Phase 4 of the capstone."""
from sqlalchemy import text


class AgentMemory:
    def __init__(self, engine, embed_fn):
        self.engine = engine
        self.embed  = embed_fn

    def start_session(self, user_id: str) -> str:
        with self.engine.begin() as conn:
            sid = conn.execute(text("""
                INSERT INTO sessions (user_id) VALUES (:u)
                RETURNING session_id
            """), {"u": user_id}).scalar()
        return str(sid)

    def end_session(self, sid: str):
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE sessions SET ended_at = now() WHERE session_id = :s"
            ), {"s": sid})

    def add_message(self, sid, role, content):
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO messages (session_id, role, content)
                VALUES (:s, :r, :c)
            """), {"s": sid, "r": role, "c": content})

    def recent_messages(self, sid, n=10):
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT role, content FROM messages
                WHERE session_id = :s
                ORDER BY message_id DESC LIMIT :n
            """), {"s": sid, "n": n}).fetchall()
        return list(reversed([{"role": r.role, "content": r.content} for r in rows]))

    def remember(self, user_id, sid, summary):
        v = self.embed(summary)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO episodes (user_id, session_id, summary, embedding)
                VALUES (:u, :s, :sum, CAST(:e AS vector))
            """), {"u": user_id, "s": sid, "sum": summary, "e": str(v)})

    def recall(self, user_id, query, k=3):
        v = self.embed(query)
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT summary, created_at
                FROM episodes
                WHERE user_id = :u
                ORDER BY embedding <=> CAST(:e AS vector)
                LIMIT :k
            """), {"u": user_id, "e": str(v), "k": k}).fetchall()
        return [{"summary": r.summary, "created_at": r.created_at} for r in rows]
'''
with open(f"{APP_DIR}/memory.py", "w") as f:
    f.write(MEMORY_PY)
print(f"Wrote memory.py ({len(MEMORY_PY)} B)")
```

---

## Step 5 — Write `retrieve.py` and `agent.py`

Two tight files: `retrieve.py` holds `kb_retrieve()` (Phase 3 RRF). `agent.py` holds the three tool functions, the JSON schemas, and `run_turn()` from Phase 4. The skeletons are copied verbatim — they were already production-shaped.

### Cell 6 — write retrieve.py

```python
RETRIEVE_PY = '''"""retrieve.py — RRF over kb_documents (Phase 3)."""
from sqlalchemy import text

RRF_K = 60

_RETRIEVE_SQL = text("""
WITH q AS (
    SELECT CAST(:emb AS vector) AS qvec,
           plainto_tsquery('english', :q) AS qtsv
),
vec AS (
    SELECT doc_id, source, title, chunk,
           ROW_NUMBER() OVER (ORDER BY embedding <=> q.qvec) AS rank
    FROM kb_documents, q
    ORDER BY embedding <=> q.qvec LIMIT :k_each
),
bm25 AS (
    SELECT doc_id, source, title, chunk,
           ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, q.qtsv) DESC) AS rank
    FROM kb_documents, q
    WHERE tsv @@ q.qtsv
    ORDER BY ts_rank(tsv, q.qtsv) DESC LIMIT :k_each
),
fused AS (
    SELECT doc_id, source, title, chunk,
           SUM(1.0 / (:rrf_k + rank)) AS rrf_score
    FROM (SELECT * FROM vec UNION ALL SELECT * FROM bm25) u
    GROUP BY doc_id, source, title, chunk
)
SELECT source, title, chunk, rrf_score
FROM fused ORDER BY rrf_score DESC LIMIT :k
""")


def kb_retrieve(engine, embed_fn, query, k=5):
    qv = embed_fn(query)
    with engine.connect() as conn:
        rows = conn.execute(_RETRIEVE_SQL, {
            "emb": str(qv), "q": query,
            "k": k, "k_each": max(k * 2, 20), "rrf_k": RRF_K,
        }).fetchall()
    return [{"source": r.source, "title": r.title,
             "chunk":  r.chunk,  "score": float(r.rrf_score)}
            for r in rows]
'''
with open(f"{APP_DIR}/retrieve.py", "w") as f:
    f.write(RETRIEVE_PY)
print(f"Wrote retrieve.py ({len(RETRIEVE_PY)} B)")
```

### Cell 7 — write agent.py

```python
AGENT_PY = '''"""agent.py — three tools + run_turn(). From Phase 4 of the capstone."""
import os, json, time
from sqlalchemy import text
from openai import OpenAI

from db       import get_engine
from memory   import AgentMemory
from retrieve import kb_retrieve

_HOST  = os.environ.get("DATABRICKS_HOST",  "")
_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "databricks-bge-large-en")
CHAT_MODEL  = os.environ.get("CHAT_MODEL",  "databricks-meta-llama-3-3-70b-instruct")

_fm = OpenAI(api_key=_TOKEN, base_url=f"{_HOST}/serving-endpoints")


def embed(s):
    out = _fm.embeddings.create(model=EMBED_MODEL, input=[s]).data
    return out[0].embedding


SYSTEM_PROMPT = """You are a Northwind Goods customer-support assistant.
You help support agents resolve customer issues quickly. Be concise.

You may call these tools:
  · retrieve(query, k)         — search the support knowledge base
  · get_orders(customer_id, n) — pull a customer\\'s recent orders
  · recall(user_id, query, k)  — search long-term memory of prior sessions

Always cite the document source when you quote policy.
If asked about a customer\\'s orders, call get_orders before answering.
If a known preference is in long-term memory, honor it without being asked.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "retrieve",
        "description": "Search the Northwind support KB.",
        "parameters": {"type": "object",
            "properties": {"query": {"type": "string"},
                           "k":     {"type": "integer", "default": 5}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_orders",
        "description": "Recent orders for a customer.",
        "parameters": {"type": "object",
            "properties": {"customer_id": {"type": "string"},
                           "n":           {"type": "integer", "default": 10}},
            "required": ["customer_id"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "Long-term memory of prior sessions.",
        "parameters": {"type": "object",
            "properties": {"user_id": {"type": "string"},
                           "query":   {"type": "string"},
                           "k":       {"type": "integer", "default": 3}},
            "required": ["user_id", "query"]}}},
]


def _log_tool_call(engine, sid, name, args, result, latency_ms):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO tool_calls (session_id, tool_name, arguments, result, latency_ms)
            VALUES (:s, :n, CAST(:a AS jsonb), CAST(:r AS jsonb), :l)
        """), {"s": sid, "n": name,
               "a": json.dumps(args),
               "r": json.dumps(result, default=str),
               "l": latency_ms})


def run_turn(sid, user_id, user_msg, max_iter=4):
    engine = get_engine()
    mem    = AgentMemory(engine, embed)

    def tool_retrieve(query, k=5):
        return kb_retrieve(engine, embed, query, k)

    def tool_get_orders(customer_id, n=10):
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT order_id, sku, qty, amount, status, placed_at
                FROM orders_synced WHERE customer_id = :c
                ORDER BY placed_at DESC LIMIT :n
            """), {"c": customer_id, "n": n}).fetchall()
        return [{"order_id": r.order_id, "sku": r.sku, "qty": r.qty,
                 "amount": float(r.amount), "status": r.status,
                 "placed_at": r.placed_at.isoformat()} for r in rows]

    def tool_recall(user_id, query, k=3):
        return mem.recall(user_id, query, k)

    fns = {"retrieve": tool_retrieve, "get_orders": tool_get_orders, "recall": tool_recall}

    episodes = mem.recall(user_id, user_msg, k=3)
    epi = "\\n".join(f"- {e[\\'summary\\']}" for e in episodes) if episodes else "(no prior memory)"

    history = mem.recent_messages(sid, n=10)
    messages = [{"role": "system",
                 "content": SYSTEM_PROMPT + f"\\n\\nKnown about user {user_id}:\\n{epi}"},
                *history,
                {"role": "user", "content": user_msg}]
    mem.add_message(sid, "user", user_msg)

    for _ in range(max_iter):
        out = _fm.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOLS,
            tool_choice="auto", temperature=0.1)
        msg = out.choices[0].message
        if not msg.tool_calls:
            mem.add_message(sid, "assistant", msg.content or "")
            return msg.content or ""
        messages.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            t0 = time.time()
            try:
                result = fns[tc.function.name](**args)
            except Exception as e:
                result = {"error": str(e)}
            _log_tool_call(engine, sid, tc.function.name, args, result,
                           int((time.time() - t0) * 1000))
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)})

    final = "(agent did not converge within max iterations)"
    mem.add_message(sid, "assistant", final)
    return final
'''
with open(f"{APP_DIR}/agent.py", "w") as f:
    f.write(AGENT_PY)
print(f"Wrote agent.py ({len(AGENT_PY)} B)")
```

---

## Step 6 — Write `app.py` (Streamlit two-pane UI)

The UI has three regions:

- **Left** — chat panel (the `run_turn()` loop streamed into Streamlit)
- **Right top** — customer context panel (live `customers_synced` + `orders_synced` lookup)
- **Right bottom** — "Save resolution" form (writes to `resolutions` — the only thing the app *writes*)

**What to look for:** every database call goes through `get_engine()` from `db.py` — there's *no module-level engine* anywhere. The trap from Module 7.3 stays sealed shut.

### Cell 8 — write app.py

```python
APP_PY = '''"""app.py — AskMyOrders Streamlit UI."""
import os
import streamlit as st
from sqlalchemy import text

from db    import get_engine, current_user
from agent import run_turn


st.set_page_config(page_title="AskMyOrders", layout="wide")
st.title("AskMyOrders · Northwind Support")
st.caption(f"Signed in as: {current_user()}")


if "sid" not in st.session_state:
    eng = get_engine()
    with eng.begin() as conn:
        sid = conn.execute(text("""
            INSERT INTO sessions (user_id) VALUES (:u) RETURNING session_id
        """), {"u": current_user()}).scalar()
    st.session_state.sid = str(sid)
    st.session_state.history = []

if "selected_customer" not in st.session_state:
    st.session_state.selected_customer = "c001"


left, right = st.columns([3, 2])


# ════════ LEFT — chat ════════
with left:
    st.subheader("Chat")
    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])

    user_msg = st.chat_input("Ask about an order, a policy, or anything…")
    if user_msg:
        st.session_state.history.append({"role": "user", "content": user_msg})
        with st.chat_message("user"):
            st.markdown(user_msg)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                reply = run_turn(st.session_state.sid, current_user(), user_msg)
            st.markdown(reply)
        st.session_state.history.append({"role": "assistant", "content": reply})


# ════════ RIGHT — customer context + resolution ════════
with right:
    st.subheader("Customer Context")

    eng = get_engine()
    with eng.connect() as conn:
        customers = conn.execute(text("""
            SELECT customer_id, name, region, tier
            FROM customers_synced ORDER BY customer_id
        """)).fetchall()

    cid_options = [c.customer_id for c in customers]
    if cid_options:
        sel = st.selectbox(
            "Customer ID", options=cid_options,
            index=cid_options.index(st.session_state.selected_customer)
                  if st.session_state.selected_customer in cid_options else 0,
        )
        st.session_state.selected_customer = sel
        info = next(c for c in customers if c.customer_id == sel)
        st.markdown(
            f"**{info.name}**  \\nRegion: {info.region}  \\nTier: {info.tier}"
        )

        with eng.connect() as conn:
            orders = conn.execute(text("""
                SELECT order_id, sku, status, amount, placed_at
                FROM orders_synced WHERE customer_id = :c
                ORDER BY placed_at DESC LIMIT 5
            """), {"c": sel}).fetchall()
        if orders:
            st.write("Recent orders:")
            st.table([
                {"Order": o.order_id, "SKU": o.sku,
                 "Status": o.status, "Amount": f"${o.amount}"}
                for o in orders
            ])
    else:
        st.info("No customers visible (RLS may be filtering).")

    st.divider()
    st.subheader("Save Resolution")
    with st.form("save_resolution", clear_on_submit=True):
        category = st.selectbox(
            "Category",
            ["return", "shipping", "warranty", "billing", "other"],
        )
        summary = st.text_area("Summary", height=100,
                               placeholder="Issued return label for opened laptop, 14-day window honored.")
        ok = st.form_submit_button("Save")
        if ok and summary.strip():
            with eng.begin() as conn:
                conn.execute(text("""
                    INSERT INTO resolutions
                        (session_id, customer_id, category, summary, resolved_by)
                    VALUES (:s, :c, :cat, :sum, :u)
                """), {"s": st.session_state.sid,
                       "c": st.session_state.selected_customer,
                       "cat": category, "sum": summary,
                       "u": current_user()})
            st.success("Resolution saved · streams to Delta via Lakehouse Sync")
'''
with open(f"{APP_DIR}/app.py", "w") as f:
    f.write(APP_PY)
print(f"Wrote app.py ({len(APP_PY)} B)")
```

---

## Step 7 — Write `app.yaml` and `requirements.txt`

`app.yaml` binds the Lakebase project as a resource — that's what tells the runtime to inject `PGHOST`, `PGUSER`, `PGDATABASE`, and (per request) `DATABRICKS_TOKEN`. **No secrets in this file.** That's the OBO contract; if you reach for a `password:` field you've drifted off the path.

### Cell 9 — write app.yaml + requirements.txt

```python
APP_YAML = f'''# app.yaml — AskMyOrders manifest
# Binds the Lakebase project. Runtime injects OBO env vars per request.
# DO NOT add secrets here. The OBO contract is the auth.
command:
  - streamlit
  - run
  - app.py
  - --server.port=8000
  - --server.address=0.0.0.0

env:
  - name: EMBED_MODEL
    value: "{CAPSTONE['embed_model']}"
  - name: CHAT_MODEL
    value: "{CAPSTONE['chat_model']}"

resources:
  - name: askmyorders-db
    description: "Lakebase project for the AskMyOrders capstone"
    database:
      database_name: "{CAPSTONE['project_name']}"
      permission: "CAN_CONNECT_AND_CREATE"
'''

REQUIREMENTS = """streamlit>=1.32
sqlalchemy>=2.0
psycopg[binary]>=3.1
openai>=1.30
databricks-sdk>=0.30
"""

with open(f"{APP_DIR}/app.yaml", "w") as f:
    f.write(APP_YAML)
with open(f"{APP_DIR}/requirements.txt", "w") as f:
    f.write(REQUIREMENTS)

# Static check — no secrets in app.yaml
yaml_text = open(f"{APP_DIR}/app.yaml").read().lower()
banned = ["password:", "token:", "secret:", "apikey:", "api_key:"]
hits = [b for b in banned if b in yaml_text]
assert not hits, f"❌ Secret-like field in app.yaml: {hits}."
print("✅ Static check passed — app.yaml carries no secrets")
```

---

## Step 8 — Deploy with `databricks apps deploy`

The deploy takes 2–4 minutes — the runtime builds an image, pushes it, and starts a container. The cell prints the live URL when ready.

### Cell 10 — deploy via CLI (preferred)

```python
import shlex

if cli_ok:
    existing_app_names = [a.name for a in w.apps.list()]
    if CAPSTONE["app_name"] not in existing_app_names:
        from databricks.sdk.service.apps import App
        print(f"Creating Apps object '{CAPSTONE['app_name']}'...")
        w.apps.create(App(name=CAPSTONE["app_name"]))
    else:
        print(f"⏸ App '{CAPSTONE['app_name']}' already exists — will redeploy.")

    deploy_cmd = (
        f"databricks apps deploy {CAPSTONE['app_name']} "
        f"--source-code-path {shlex.quote(APP_DIR)}"
    )
    print(f"\n$ {deploy_cmd}\n")
    proc = subprocess.run(deploy_cmd.split(), capture_output=True, text=True, timeout=600)
    print(proc.stdout[-2000:])
    if proc.returncode != 0:
        print(f"⚠ deploy returned {proc.returncode}")
        print(proc.stderr[-2000:])
else:
    print("CLI unavailable — use Cell 10b for the SDK fallback.")
```

### Cell 10b — SDK fallback

Skip this cell if Cell 10 succeeded.

```python
if not cli_ok:
    from databricks.sdk.service.apps import App, AppDeployment
    existing_app_names = [a.name for a in w.apps.list()]
    if CAPSTONE["app_name"] not in existing_app_names:
        w.apps.create(App(name=CAPSTONE["app_name"]))
    w.apps.deploy(
        app_name=CAPSTONE["app_name"],
        app_deployment=AppDeployment(source_code_path=APP_DIR, mode="SNAPSHOT"),
    )
    print("Deploy submitted via SDK. Poll w.apps.get(...) for state.")
```

### Cell 11 — wait for ACTIVE + report URL

```python
deadline = time.time() + 360
app = None
while time.time() < deadline:
    app = w.apps.get(name=CAPSTONE["app_name"])
    if app.compute_status and app.compute_status.state == "ACTIVE":
        break
    print(f"   ...status={app.compute_status.state if app.compute_status else '?'}")
    time.sleep(15)

print(f"\n{'=' * 60}")
print(f"  App: {app.name}")
print(f"  URL: {app.url}")
print(f"  Status: {app.compute_status.state if app.compute_status else 'UNKNOWN'}")
print(f"{'=' * 60}")
```

---

## Step 9 — Two-user verification (the OBO trap-killer)

Module 7's most important lesson: the connection-pool trap is **silent in dev**. You only catch it when a second identity hits the app and starts seeing the first user's data.

**You do this manually, with a teammate.** No notebook cell can verify it for you.

1. Open the URL printed above in your browser. Note the value of `Signed in as:` at the top of the UI.
2. Pick a customer (e.g. `c001`). Note the recent orders.
3. Have a teammate (different workspace user) open the same URL.
4. They should see *their* identity in `Signed in as:`. The data they see depends on whatever GRANTs / RLS policies you have on the synced tables.
5. Save a resolution from each user. Verify in the next step that the `resolved_by` column shows two different identities.

> **If both users see the exact same `Signed in as:` value, OBO is broken.** The most likely cause is a global engine you forgot to remove. Re-read `db.py` line by line.

### Cell 12 — confirm resolutions reflect multiple identities

```python
import time as _t
print("Waiting 30s for you and your teammate to each save a resolution from the App...")
_t.sleep(30)

with engine.connect() as conn:
    distinct_users = conn.execute(text(
        "SELECT DISTINCT resolved_by FROM resolutions"
    )).scalars().all()
    n_res = conn.execute(text("SELECT count(*) FROM resolutions")).scalar()

print(f"\nresolutions table: {n_res} rows · {len(distinct_users)} distinct resolved_by values")
for u in distinct_users:
    print(f"   · {u}")

if len(distinct_users) >= 2:
    print("\n✅ TWO-USER VERIFICATION — OBO is doing its job.")
else:
    print("\n⚠ Only one identity in resolved_by. Repeat with a teammate before continuing.")
```

---

## Step 10 — Configure Lakehouse Sync (resolutions → Delta)

The reverse of Phase 2. We stream rows from Lakebase (`resolutions` — written by the app) back to Delta (`main.silver.support_resolutions` — analyst-readable). This is what closes the loop: operational data flows in via synced tables, app-generated data flows out via Lakehouse Sync.

> **Lakehouse Sync may be Beta in some regions.** If your region doesn't have it, this cell prints a deferral note and the rest of the lab still works — you just won't have the Delta sink. Document the gap in your customer notes.

### Cell 13 — configure Lakehouse Sync

```python
try:
    target = (
        f"{CAPSTONE['lh_target_catalog']}."
        f"{CAPSTONE['lh_target_schema']}."
        f"{CAPSTONE['lh_target_table']}"
    )

    spark.sql(f"""
        CREATE SCHEMA IF NOT EXISTS
        {CAPSTONE['lh_target_catalog']}.{CAPSTONE['lh_target_schema']}
    """)

    pipeline_id = None
    try:
        result = w.api_client.do(
            "POST",
            f"/api/2.0/database/instances/{CAPSTONE['project_name']}/lakehouse-sync-pipelines",
            body={
                "source_table":          f"public.resolutions",
                "destination_full_name": target,
                "scheduling_policy":     "CONTINUOUS",
                "primary_keys":          ["resolution_id"],
            },
        )
        pipeline_id = result.get("pipeline_id")
        print(f"✅ Lakehouse Sync pipeline created: {pipeline_id}")
        print(f"   Source:       public.resolutions (Lakebase)")
        print(f"   Destination:  {target} (Delta)")
    except Exception as inner:
        print(f"ℹ Lakehouse Sync API not available in this region: {inner}")
        print("   Manual fallback: configure via UC Catalog UI → Sync to Delta button")

    LH_OK = pipeline_id is not None
except Exception as outer:
    print(f"⚠ Lakehouse Sync setup error: {outer}")
    LH_OK = False
```

---

## Step 11 — Build the DBSQL dashboard

Three tiles. The dashboard reads `main.silver.support_resolutions` (Delta) when Lakehouse Sync is alive, or directly from `askmyorders_db.public.resolutions` (federated) otherwise. Either way, no application change is needed — UC presents both surfaces.

### Cell 14 — print the three SQL queries

```python
SOURCE = (
    f"{CAPSTONE['lh_target_catalog']}.{CAPSTONE['lh_target_schema']}.{CAPSTONE['lh_target_table']}"
    if LH_OK else
    f"{CAPSTONE['uc_catalog']}.public.resolutions"
)

TILES = {
    "Tile 1 · Resolutions per day": f"""
SELECT date_trunc('day', created_at) AS day,
       count(*) AS resolutions
FROM {SOURCE}
GROUP BY 1 ORDER BY 1 DESC LIMIT 30
""",
    "Tile 2 · Top categories": f"""
SELECT category, count(*) AS n
FROM {SOURCE}
GROUP BY category ORDER BY n DESC
""",
    "Tile 3 · Avg time-to-resolve (proxy)": f"""
WITH paired AS (
    SELECT r.resolution_id,
           extract(epoch FROM (r.created_at - s.started_at)) AS seconds_to_resolve
    FROM {SOURCE} r
    JOIN {CAPSTONE['uc_catalog']}.public.sessions s
      ON s.session_id = r.session_id
)
SELECT round(avg(seconds_to_resolve)::numeric, 1) AS avg_seconds,
       count(*) AS resolutions_with_session
FROM paired
""",
}

print(f"DBSQL queries — paste into a new dashboard against:")
print(f"   Source: {SOURCE}\n")
print("=" * 60)
for name, q in TILES.items():
    print(f"\n# {name}")
    print(q.strip())
    print()
```

---

## Step 12 — Final verification — nine Definition-of-Done checks

This matches the 9 checkpoints from Module 8 conceptual content. **All 9 must be ✅ for the capstone to be officially complete.**

### Cell 15 — DoD checklist

```python
checks = []

# 1. Project + branches
project_now = w.database.get_database_project(name=CAPSTONE["project_name"])
branches = [b.name for b in w.database.list_database_branches(
    database_project_name=CAPSTONE["project_name"]
)]
checks.append(("01 · Project + main and dev branches",
               project_now.state == "READY" and "dev" in branches))

# 2. UC catalog visible
try:
    uc_count = spark.sql(f"SHOW TABLES IN {CAPSTONE['uc_catalog']}.public").count()
    checks.append((f"02 · UC catalog visible ({uc_count} tables)", uc_count >= 7))
except Exception:
    checks.append(("02 · UC catalog visible", False))

# 3. Synced tables
def synced_state(n):
    try:
        s = w.database.get_synced_database_table(
            name=f"{CAPSTONE['uc_catalog']}.public.{n}"
        )
        return s.data_synchronization_status.detailed_state \
               if s.data_synchronization_status else "?"
    except Exception:
        return None
states = {n: synced_state(n) for n in ("customers_synced", "orders_synced")}
checks.append((f"03 · Synced tables ACTIVE  ({states})",
               all(s == "ACTIVE" for s in states.values())))

# 4. KB + hybrid retrieval
with engine.connect() as conn:
    n_kb_now = conn.execute(text("SELECT count(*) FROM kb_documents")).scalar()
checks.append((f"04 · KB chunks ≥ 8 ({n_kb_now})", n_kb_now >= 8))

# 5. Cross-session memory (verified in Phase 4)
with engine.connect() as conn:
    n_eps_now = conn.execute(text("SELECT count(*) FROM episodes")).scalar()
checks.append((f"05 · Episodes recorded ({n_eps_now})", n_eps_now >= 1))

# 6. Both tools used
with engine.connect() as conn:
    tool_names_seen = set(conn.execute(text(
        "SELECT DISTINCT tool_name FROM tool_calls"
    )).scalars().all())
checks.append((f"06 · retrieve + get_orders both used ({tool_names_seen})",
               {"retrieve","get_orders"}.issubset(tool_names_seen)))

# 7. App deployed + OBO scoped
app_now = w.apps.get(name=CAPSTONE["app_name"])
app_active = app_now.compute_status and app_now.compute_status.state == "ACTIVE"
with engine.connect() as conn:
    distinct_resolvers = conn.execute(text(
        "SELECT count(DISTINCT resolved_by) FROM resolutions"
    )).scalar()
checks.append((f"07 · App ACTIVE + ≥2 distinct resolved_by ({distinct_resolvers})",
               app_active and distinct_resolvers >= 2))

# 8. Lakehouse Sync (or documented fallback)
checks.append((f"08 · Lakehouse Sync configured", LH_OK))

# 9. DBSQL dashboard
checks.append((f"09 · DBSQL dashboard SQL prepared (3 tiles)", len(TILES) == 3))

print("=" * 70)
print(f"  PHASE 5 + DEFINITION OF DONE — askmyorders capstone")
print("=" * 70)
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")

passed = sum(1 for _, ok in checks if ok)
total  = len(checks)
print("=" * 70)
if passed == total:
    print(f"  🏆 ALL 9 CHECKPOINTS PASSED — capstone COMPLETE")
elif passed >= 7:
    print(f"  🎯 {passed}/{total} passed — likely Lakehouse Sync Beta gap; document and proceed")
else:
    print(f"  ⚠ {passed}/{total} passed")
print("=" * 70)
```

### Troubleshooting reference

| ❌ Row | Most common cause | Resolution |
|---|---|---|
| App ACTIVE + ≥2 resolved_by | Two-user step skipped | Open URL with a teammate; both save a resolution; re-run Cell 12 + 15 |
| App ACTIVE stuck `STARTING` | Image build/push slow | Wait 5 min and re-run Cell 11 |
| Synced tables ACTIVE | Pipeline degraded since Phase 2 | UC Catalog Explorer → table → Sync status → resume |
| Lakehouse Sync configured | Beta not in region | Document gap in customer notes; rest still proves architecture |
| retrieve + get_orders | Audit log emptied | Re-run a multi-tool query in the App and re-check |

---

## (Optional) Step 13 — Cleanup

The default is **leave everything running** so you can demo the app on customer calls. When you're done, set `CLEANUP=True` in the cell below to tear down Apps, Lakehouse Sync, the synced tables, the UC catalog, and the Lakebase project.

> **⚠ This is destructive.** All data in the capstone — KB chunks, agent memory, resolutions — is deleted. Run only when you genuinely don't need the build any more.

### Cell 16 — optional cleanup

```python
CLEANUP = False  # ← set True ONLY when you're done demoing

if CLEANUP:
    # 1. App
    try:
        w.apps.delete(name=CAPSTONE["app_name"])
        print(f"✓ Deleted app {CAPSTONE['app_name']}")
    except Exception as e:
        print(f"  ! App delete failed: {e}")

    # 2. Synced tables
    for tn in ("customers_synced", "orders_synced"):
        try:
            w.database.delete_synced_database_table(
                name=f"{CAPSTONE['uc_catalog']}.public.{tn}"
            )
            print(f"✓ Dropped synced table {tn}")
        except Exception as e:
            print(f"  ! {tn} drop failed: {e}")

    # 3. UC catalog
    try:
        spark.sql(f"DROP CATALOG IF EXISTS {CAPSTONE['uc_catalog']} CASCADE")
        print(f"✓ Dropped UC catalog {CAPSTONE['uc_catalog']}")
    except Exception as e:
        print(f"  ! UC catalog drop failed: {e}")

    # 4. Lakebase project
    try:
        w.database.delete_database_project(name=CAPSTONE["project_name"])
        print(f"✓ Deleted Lakebase project {CAPSTONE['project_name']}")
    except Exception as e:
        print(f"  ! Lakebase project delete failed: {e}")

    print("\n🧹 Cleanup complete — billing stops within minutes.")
else:
    print("⏸ CLEANUP=False — capstone left running.")
    print(f"   App URL: {app.url if app else '(re-run Cell 11)'}")
    print(f"   To tear down: set CLEANUP=True and re-run this cell.")
```

---

## What you've accomplished — the capstone is done

If you saw `🏆 ALL 9 CHECKPOINTS PASSED` (or `🎯 7+ with documented Beta gap`), the AskMyOrders capstone is **complete**. You have, on real infrastructure, in your own workspace:

- ✅ A live `askmyorders` Lakebase project, registered in Unity Catalog, with main + dev branches
- ✅ Two CONTINUOUS synced tables propagating Delta changes into Lakebase in seconds
- ✅ A `pgvector` knowledge base with HNSW + GIN, queried via RRF
- ✅ A four-table memory schema with cross-session recall verified
- ✅ A working `run_turn()` agent loop that composes all three tools
- ✅ A deployed Databricks App at a real URL, **OBO-scoped per user** (verified with a teammate)
- ✅ Lakehouse Sync (or a documented gap) closing the loop back to Delta
- ✅ DBSQL queries ready for the leadership dashboard

You have personally exercised every concept from Modules 1–7. **You can lead a customer through this exact build with full conviction.** That conviction is what closes design reviews.

> **Up next: Module 9** — *RSA Toolkit*. You turn this build into the conversation: comparison cheat sheets, design-review templates, and the limits worth flagging on the first call.
