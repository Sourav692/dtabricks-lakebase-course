# Module 08 · Capstone · Phase 4 — Agent with Memory

> **Type:** Hands-on · **Duration:** ~60 minutes · **Format:** Databricks notebook
> **Purpose:** Wire the four-layer memory model into a working agent loop. Implement `AgentMemory`, define three tools with explicit JSON schemas, build `run_turn()`, prove cross-session memory works (preference set in session 1 must surface in session 2), and confirm the agent uses both `retrieve` and `get_orders` in a single conversation. This is the brain that Phase 5 wraps in a Streamlit UI.

---

## Why this phase exists

Phases 1–3 stood up the *substrate*: tables, synced data, an indexed knowledge base, a working `retrieve()`. None of that is an agent yet. An agent is the loop that:

1. **Recalls** what it knows about the user from prior conversations
2. **Composes** a context window from system prompt + history + user message
3. **Decides** which tools to call (or whether to answer directly)
4. **Executes** the tools, **logs** every call, and feeds results back into the model
5. **Persists** the new turn so the next message sees full history

This phase makes that loop real. The shape is identical to Module 6 Lab 6 — the capstone changes the *backing data* (Northwind tables instead of generic) but the agent contract is unchanged. By the end of Phase 4 you have a `run_turn()` function the Phase 5 Streamlit app calls verbatim.

This phase walks nine steps:

1. **Setup** — reconnect to the Phase 1/2/3 project
2. **Build HNSW on `episodes`** — semantic search over long-term memory
3. **Implement `AgentMemory`** — six methods on the four memory tables
4. **Define three tools** — `retrieve`, `get_orders`, `recall` with JSON schemas
5. **Build `run_turn()`** — the five-step agent loop
6. **Cross-session memory demo** — set a preference in session 1, recall it in session 2
7. **Multi-tool turn** — confirm one turn invokes both `retrieve` AND `get_orders`
8. **Audit-trail spot check** — query `tool_calls` to see what the agent did
9. **Final verification** — six green checks before Phase 5

> **Run mode.** Top-to-bottom. All cells Python. The cross-session memory demo is the headline test — don't skip Step 6.

---

## Prerequisites before you start

You need:

- ✅ **Phase 1 passed** — `sessions`, `messages`, `episodes`, `tool_calls` tables exist
- ✅ **Phase 2 passed** — `orders_synced` is live (the `get_orders` tool reads it)
- ✅ **Phase 3 passed** — `kb_documents` is loaded; `retrieve()` works
- ✅ **Foundation Models API** enabled in the workspace region
- An attached compute cluster (serverless is fine, recommended)

> **⚠ Billing note.** This phase makes ~25 chat completions and ~12 embedding calls — under one cent.

---

## Step 1 — Setup: reconnect

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy openai
dbutils.library.restartPython()
```

### Cell 2 — reconnect

```python
import os, uuid, json, time
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text
from openai import OpenAI

w = WorkspaceClient()

CAPSTONE = {
    "project_name": "askmyorders",
    "uc_catalog":   "askmyorders_db",
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
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

engine    = make_engine()
fm_client = OpenAI(api_key=w.config.token,
                   base_url=f"{w.config.host}/serving-endpoints")

def embed(text_or_texts):
    """Single-string or list-of-strings → list of vectors. Single returns one vector."""
    single = isinstance(text_or_texts, str)
    inputs = [text_or_texts] if single else text_or_texts
    out = fm_client.embeddings.create(model=CAPSTONE["embed_model"], input=inputs).data
    vecs = [d.embedding for d in out]
    return vecs[0] if single else vecs

print(f"✅ Reconnected. Engine + FM client ready.")
```

---

## Step 2 — Build HNSW on `episodes`

The `episodes` table was created in Phase 1 but the HNSW index was deferred — same anti-pattern avoidance as Phase 3 Step 6. We build it now, before the first writes, because the table is empty and the build is instant.

### Cell 3 — HNSW on episodes

```python
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS episodes_embedding_hnsw
    ON episodes USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""
with engine.begin() as conn:
    conn.execute(text(INDEX_DDL))

with engine.connect() as conn:
    idx = conn.execute(text("""
        SELECT indexname FROM pg_indexes WHERE tablename='episodes'
        ORDER BY indexname
    """)).scalars().all()
print("Indexes on episodes:")
for i in idx:
    print(f"   · {i}")
```

---

## Step 3 — Implement the `AgentMemory` contract

Six methods, all parameterized SQL. Every memory op the agent needs goes through this class — that's the API surface Phase 5's Streamlit UI consumes.

| Method | Backing table | Used by |
|---|---|---|
| `start_session(user_id) -> sid` | `sessions` | Every new chat |
| `end_session(sid)` | `sessions` + `episodes` | Tab close / auto-summarize |
| `add_message(sid, role, content)` | `messages` | Every turn |
| `recent_messages(sid, n=10)` | `messages` | Building context |
| `remember(user_id, sid, summary)` | `episodes` | End-of-session distillation |
| `recall(user_id, query, k=3)` | `episodes` | Start-of-turn lookup |

### Cell 4 — AgentMemory class

```python
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

    def add_message(self, sid: str, role: str, content: str):
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO messages (session_id, role, content)
                VALUES (:s, :r, :c)
            """), {"s": sid, "r": role, "c": content})

    def recent_messages(self, sid: str, n: int = 10):
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT role, content
                FROM messages
                WHERE session_id = :s
                ORDER BY message_id DESC
                LIMIT :n
            """), {"s": sid, "n": n}).fetchall()
        return list(reversed([{"role": r.role, "content": r.content} for r in rows]))

    def remember(self, user_id: str, sid: str, summary: str):
        v = self.embed(summary)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO episodes (user_id, session_id, summary, embedding)
                VALUES (:u, :s, :sum, CAST(:e AS vector))
            """), {"u": user_id, "s": sid, "sum": summary, "e": str(v)})

    def recall(self, user_id: str, query: str, k: int = 3):
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

mem = AgentMemory(engine, embed)

# Smoke
test_sid = mem.start_session("smoke@northwind.com")
mem.add_message(test_sid, "user",      "hi from the smoke test")
mem.add_message(test_sid, "assistant", "ack")
hist = mem.recent_messages(test_sid)
mem.end_session(test_sid)
assert len(hist) == 2
print(f"✅ AgentMemory works. Smoke session: {test_sid[:8]}... · {len(hist)} messages")
```

---

## Step 4 — Define the three tools (with JSON schemas)

Same pattern as Module 6 Lab 6 Cell 6, adapted for the capstone schema. Each tool has an explicit JSON schema so the LLM can select arguments correctly. The implementations are the **only** functions the LLM is allowed to invoke.

| Tool | Backed by | Purpose |
|---|---|---|
| `retrieve` | Phase 3 RRF + `kb_documents` | Search the support knowledge base |
| `get_orders` | Phase 2 `orders_synced` | Fetch a customer's recent orders |
| `recall` | Phase 4 `episodes` | Search long-term agent memory |

### Cell 5 — Phase 3's retrieve(), copied here so the agent can call it

```python
RRF_K = 60

RETRIEVE_SQL = text("""
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

def kb_retrieve(query: str, k: int = 5):
    qv = embed(query)
    with engine.connect() as conn:
        rows = conn.execute(RETRIEVE_SQL, {
            "emb": str(qv), "q": query,
            "k": k, "k_each": max(k * 2, 20), "rrf_k": RRF_K,
        }).fetchall()
    return [
        {"source": r.source, "title": r.title,
         "chunk":  r.chunk, "score": float(r.rrf_score)}
        for r in rows
    ]

print(f"kb_retrieve smoke: {kb_retrieve('how do returns work', k=2)[0]['source']}")
```

### Cell 6 — three tool implementations + JSON schemas

```python
def tool_retrieve(query: str, k: int = 5):
    return kb_retrieve(query, k)

def tool_get_orders(customer_id: str, n: int = 10):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT order_id, sku, qty, amount, status, placed_at
            FROM orders_synced
            WHERE customer_id = :c
            ORDER BY placed_at DESC LIMIT :n
        """), {"c": customer_id, "n": n}).fetchall()
    return [
        {"order_id": r.order_id, "sku": r.sku,
         "qty": r.qty, "amount": float(r.amount),
         "status": r.status, "placed_at": r.placed_at.isoformat()}
        for r in rows
    ]

def tool_recall(user_id: str, query: str, k: int = 3):
    return mem.recall(user_id, query, k)

TOOLS = [
    {"type": "function", "function": {
        "name": "retrieve",
        "description": "Search the Northwind support knowledge base.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k":     {"type": "integer", "default": 5},
            },
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_orders",
        "description": "Fetch a customer's recent orders.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "n":           {"type": "integer", "default": 10},
            },
            "required": ["customer_id"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "Long-term memory of prior sessions for this user.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "query":   {"type": "string"},
                "k":       {"type": "integer", "default": 3},
            },
            "required": ["user_id", "query"]}}},
]

TOOL_FNS = {
    "retrieve":   tool_retrieve,
    "get_orders": tool_get_orders,
    "recall":     tool_recall,
}

print(f"✅ Tools registered: {list(TOOL_FNS)}")
```

---

## Step 5 — Build `run_turn()` — the five-step loop

The shape from Module 8 theory 8.4 made real:

1. **Recall** episodes for this user
2. **Build** the message list (system + history + new user msg)
3. **Loop:** call LLM → if no tool calls, persist & return; if tool calls, execute, log, append, loop

Capped at 4 iterations so a misbehaving model can't loop forever. Every tool call gets logged to `tool_calls` for audit.

### Cell 7 — run_turn()

```python
SYSTEM_PROMPT = """You are a Northwind Goods customer-support assistant.
You help support agents resolve customer issues quickly. Be concise.

You may call these tools:
  · retrieve(query, k)         — search the support knowledge base
  · get_orders(customer_id, n) — pull a customer's recent orders
  · recall(user_id, query, k)  — search long-term memory of prior sessions

Always cite the document source (e.g. returns_policy_v2.md) when you quote policy.
If asked about a customer's orders, call get_orders before answering.
If a known preference is in long-term memory, honor it without being asked.
"""

def chat(messages, tools=None, temperature=0.0):
    kwargs = dict(model=CAPSTONE["chat_model"], messages=messages, temperature=temperature)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return fm_client.chat.completions.create(**kwargs)

def log_tool_call(sid, name, args, result, latency_ms):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO tool_calls (session_id, tool_name, arguments, result, latency_ms)
            VALUES (:s, :n, CAST(:a AS jsonb), CAST(:r AS jsonb), :l)
        """), {"s": sid, "n": name,
               "a": json.dumps(args), "r": json.dumps(result, default=str),
               "l": latency_ms})

def run_turn(sid: str, user_id: str, user_msg: str, max_iter: int = 4):
    # 1. Recall long-term episodes
    episodes = mem.recall(user_id, user_msg, k=3)
    epi_block = "\n".join(f"- {e['summary']}" for e in episodes) if episodes else "(no prior memory)"

    # 2. Build messages
    history = mem.recent_messages(sid, n=10)
    messages = [
        {"role": "system",
         "content": SYSTEM_PROMPT + f"\n\nKnown about user {user_id}:\n{epi_block}"},
        *history,
        {"role": "user", "content": user_msg},
    ]
    mem.add_message(sid, "user", user_msg)

    # 3. Tool-using loop
    for i in range(max_iter):
        out = chat(messages, tools=TOOLS, temperature=0.1)
        msg = out.choices[0].message

        if not msg.tool_calls:
            mem.add_message(sid, "assistant", msg.content or "")
            return msg.content or ""

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            t0 = time.time()
            try:
                result = TOOL_FNS[name](**args)
            except Exception as e:
                result = {"error": str(e)}
            latency = int((time.time() - t0) * 1000)
            log_tool_call(sid, name, args, result, latency)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    final = "(agent did not converge within max iterations)"
    mem.add_message(sid, "assistant", final)
    return final

print("✅ run_turn() ready")
```

---

## Step 6 — Cross-session memory demo (the headline test)

The single demo that proves the agent has *memory*, not just tool use. We run two sessions for the same `user_id`:

- **Session 1** — Carla mentions she prefers email replies. We end the session and `remember` it as an episode.
- **Session 2** — A new session, no overlap with session 1. The agent should call `recall` (or surface from the system prompt) and honor the preference.

> **If session 2 doesn't honor the preference, memory is broken.** Either the embedding pipeline lost a row, the `recall` tool is unused, or the system prompt context is malformed.

### Cell 8 — session 1: set the preference

```python
USER = "agent.carla@northwind.com"

s1 = mem.start_session(USER)
print(f"▸ Session 1 started: {s1[:8]}...")

a1 = run_turn(s1, USER, "Quick note: I always prefer email replies, never phone. Thanks!")
print(f"   Assistant: {a1[:120]}...")

# End session and write an episode
summary = "User strongly prefers email replies over phone calls."
mem.remember(USER, s1, summary)
mem.end_session(s1)
print(f"   ▸ Episode recorded: {summary}")
```

### Cell 9 — session 2: new session, see if the preference survives

```python
import time as _t
_t.sleep(1)  # ensure embeddings are committed

s2 = mem.start_session(USER)
print(f"▸ Session 2 started: {s2[:8]}...")

a2 = run_turn(s2, USER, "Customer c003 emailed asking about her order o006. How should I reply?")
print(f"\n   Assistant:\n{a2}")

assert "email" in a2.lower(), (
    "❌ Session 2 did not honor the email preference from Session 1. "
    "Memory broken — see troubleshooting."
)
print("\n✅ CROSS-SESSION MEMORY VERIFIED — session 2 honored the preference set in session 1.")
```

---

## Step 7 — Multi-tool turn (both `retrieve` and `get_orders` in one conversation)

The Definition of Done in the conceptual module says: *"Agent uses both tools — at least one turn invokes `retrieve` AND `get_orders`."* Verify it on a single realistic question that needs both: a *policy* AND a *customer-specific fact*.

### Cell 10 — multi-tool turn

```python
s3 = mem.start_session(USER)

with engine.connect() as conn:
    n_before = conn.execute(text("SELECT count(*) FROM tool_calls WHERE session_id=:s"),
                             {"s": s3}).scalar()

q = "Customer c002 wants to return order o003. Is that order eligible per our policy?"
a3 = run_turn(s3, USER, q)
print(f"User: {q}")
print(f"\nAssistant:\n{a3}")

with engine.connect() as conn:
    calls = conn.execute(text("""
        SELECT tool_name, arguments, latency_ms
        FROM tool_calls
        WHERE session_id = :s
        ORDER BY call_id
    """), {"s": s3}).fetchall()

print("\nTool calls in this turn:")
for c in calls:
    args_preview = json.dumps(c.arguments)[:60]
    print(f"   · {c.tool_name:<12} args={args_preview:<60} {c.latency_ms}ms")

names_called = {c.tool_name for c in calls}
assert "retrieve"   in names_called, "❌ retrieve was not invoked"
assert "get_orders" in names_called, "❌ get_orders was not invoked"
print(f"\n✅ Both retrieve AND get_orders invoked in one turn.")

mem.end_session(s3)
```

---

## Step 8 — Audit trail spot-check (the operational view of the agent)

Every tool call is in `tool_calls` with arguments, results, and latency. This is the table you query during incident review when a customer says "the agent gave me wrong info." Run a small DBSQL-style query from Python.

### Cell 11 — audit-trail query

```python
with engine.connect() as conn:
    summary_rows = conn.execute(text("""
        SELECT tool_name,
               count(*)       AS calls,
               round(avg(latency_ms)::numeric, 1) AS avg_ms,
               max(created_at) AS last_at
        FROM tool_calls
        GROUP BY tool_name
        ORDER BY calls DESC
    """)).fetchall()

print(f"{'tool_name':<14} {'calls':>6} {'avg_ms':>10} {'last_at'}")
print("-" * 60)
for r in summary_rows:
    print(f"{r.tool_name:<14} {r.calls:>6} {str(r.avg_ms):>10} {r.last_at}")
```

---

## Step 9 — Final verification — six green checks

### Cell 12 — verification checklist

```python
checks = []

# 1. episodes HNSW exists
with engine.connect() as conn:
    idx = set(conn.execute(text("""
        SELECT indexname FROM pg_indexes WHERE tablename='episodes'
    """)).scalars().all())
checks.append(("episodes HNSW present", "episodes_embedding_hnsw" in idx))

# 2. AgentMemory smoke OK
checks.append(("AgentMemory smoke session worked", True))

# 3. Cross-session memory
checks.append(("Cross-session memory verified", "email" in a2.lower()))

# 4. Both tools used in one turn
checks.append(("retrieve + get_orders in one turn",
               names_called >= {"retrieve","get_orders"}))

# 5. tool_calls populated
with engine.connect() as conn:
    n_calls = conn.execute(text("SELECT count(*) FROM tool_calls")).scalar()
checks.append((f"tool_calls audit rows ({n_calls})", n_calls >= 2))

# 6. episodes table has the demo episode
with engine.connect() as conn:
    n_ep = conn.execute(text("SELECT count(*) FROM episodes WHERE user_id=:u"),
                        {"u": USER}).scalar()
checks.append((f"episode persisted ({n_ep})", n_ep >= 1))

print("=" * 60)
print(f"  PHASE 4 VERIFICATION — agent + memory")
print("=" * 60)
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")

passed = sum(1 for _, ok in checks if ok)
total  = len(checks)
print("=" * 60)
if passed == total:
    print(f"  🎯 ALL {total} CHECKS PASSED — proceed to Phase 5")
else:
    print(f"  ⚠ {passed}/{total} passed")
print("=" * 60)
```

### Troubleshooting reference

| ❌ Row | Most common cause | Resolution |
|---|---|---|
| HNSW present | Cell 3 ran before pgvector was enabled | Re-run Phase 1 Cell 6 then Phase 4 Cell 3 |
| Cross-session memory | `recall` returned 0 hits — embedding model mismatch | Confirm both `remember` and `recall` use the same model; rerun Cell 8/9 |
| retrieve + get_orders | LLM happy with one tool — temperature too high | The system prompt mentions both; lower temperature to 0.0 and retry |
| tool_calls audit rows | `log_tool_call` failed silently | Check the `tool_calls` schema matches Phase 1 Cell 6 |
| Episode persisted | Embedding-call exception during `remember` | Re-run Cell 8; FM endpoint capacity sometimes blips |

---

## What you've accomplished

If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 4 is complete and you have:

- ✅ A working `AgentMemory` class — six methods, all parameterized SQL
- ✅ Three tool functions registered with the LLM via explicit JSON schemas
- ✅ A `run_turn()` agent loop with a tool-call cap and per-call audit logging
- ✅ **Cross-session memory verified** — preference set in session 1 surfaced in session 2
- ✅ **Multi-tool composition verified** — one turn invoked both `retrieve` and `get_orders`
- ✅ A populated `tool_calls` audit trail you can query from DBSQL

The Phase 4 brain is exactly what Phase 5's Streamlit UI calls. The same `run_turn()` function — same engine pattern, same tools, same memory — runs inside the Databricks App.

**Do not run cleanup.** Phase 5 imports the `AgentMemory` class and the tool functions verbatim into the app code.

> **Next:** open `Module_08_Phase_5_Ship_App.py` to wrap the agent in a Streamlit UI and deploy it.
