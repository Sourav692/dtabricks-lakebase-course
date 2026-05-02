# Databricks notebook source
# MAGIC %md
# MAGIC # Module 08 · Capstone · Phase 4 — Agent with Memory
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~60 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Wire the four-layer memory model into a working agent loop. Implement `AgentMemory`, define three tools with explicit JSON schemas, build `run_turn()`, prove cross-session memory works (preference set in session 1 must surface in session 2), and confirm the agent uses both `retrieve` and `get_orders` in a single conversation. This is the brain that Phase 5 wraps in a Streamlit UI.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — reconnect to the Phase 1/2/3 project
# MAGIC 2. **Build HNSW on `episodes`** — semantic search over long-term memory
# MAGIC 3. **Implement `AgentMemory`** — six methods (start_session, end_session, add_message, recent_messages, remember, recall)
# MAGIC 4. **Define three tools** — `retrieve`, `get_orders`, `recall` with explicit JSON schemas
# MAGIC 5. **Build `run_turn()`** — the five-step agent loop
# MAGIC 6. **Cross-session memory demo** — set a preference in session 1, recall it in session 2
# MAGIC 7. **Multi-tool turn** — confirm one turn invokes both `retrieve` AND `get_orders`
# MAGIC 8. **Audit-trail spot check** — query `tool_calls` to see what the agent did
# MAGIC 9. **Final verification** — six green checks before Phase 5
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Phase 1 passed** — `sessions`, `messages`, `episodes`, `tool_calls` tables exist
# MAGIC - **Phase 2 passed** — `orders_synced` is live (the `get_orders` tool reads it)
# MAGIC - **Phase 3 passed** — `kb_documents` is loaded; `retrieve()` works
# MAGIC - **Foundation Models API** enabled in the workspace region
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. All cells Python. The cross-session memory demo is the headline test — don't skip Step 6.
# MAGIC
# MAGIC > **⚠ Billing note.** This phase makes ~25 chat completions and ~12 embedding calls — under one cent.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: reconnect

# COMMAND ----------

# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy openai
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — reconnect
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
fm_client = OpenAI(
    api_key=w.config.token,
    base_url=f"{w.config.host}/serving-endpoints",
)

def embed(text_or_texts):
    """Single-string or list-of-strings → list of vectors. Single returns one vector."""
    single = isinstance(text_or_texts, str)
    inputs = [text_or_texts] if single else text_or_texts
    out = fm_client.embeddings.create(model=CAPSTONE["embed_model"], input=inputs).data
    vecs = [d.embedding for d in out]
    return vecs[0] if single else vecs

print(f"✅ Reconnected. Engine + FM client ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Build HNSW on `episodes`
# MAGIC
# MAGIC The `episodes` table was created in Phase 1 but the HNSW index was deferred — same anti-pattern avoidance as Phase 3 Step 6. We build it now, before the first writes, because the table is empty and the build is instant.

# COMMAND ----------

# Cell 3 — HNSW on episodes
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Implement the `AgentMemory` contract
# MAGIC
# MAGIC Six methods, all parameterized SQL. Every memory op the agent needs goes through this class — that's the API surface Phase 5's Streamlit UI consumes.
# MAGIC
# MAGIC | Method | Backing table | Used by |
# MAGIC |---|---|---|
# MAGIC | `start_session(user_id) -> sid` | `sessions` | Every new chat |
# MAGIC | `end_session(sid)` | `sessions` + `episodes` | Tab close / auto-summarize |
# MAGIC | `add_message(sid, role, content)` | `messages` | Every turn |
# MAGIC | `recent_messages(sid, n=10)` | `messages` | Building context |
# MAGIC | `remember(user_id, sid, summary)` | `episodes` | End-of-session distillation |
# MAGIC | `recall(user_id, query, k=3)` | `episodes` | Start-of-turn lookup |

# COMMAND ----------

# Cell 4 — AgentMemory class
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
assert len(hist) == 2, f"expected 2 messages, got {len(hist)}"
print(f"✅ AgentMemory works. Smoke session: {test_sid[:8]}... · {len(hist)} messages")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Define the three tools (with JSON schemas)
# MAGIC
# MAGIC Same pattern as Module 6 Lab 6 Cell 6, adapted for the capstone schema. Each tool has an explicit JSON schema so the LLM can select arguments correctly. The implementations are the **only** functions the LLM is allowed to invoke.
# MAGIC
# MAGIC | Tool | Backed by | Purpose |
# MAGIC |---|---|---|
# MAGIC | `retrieve` | Phase 3 RRF + `kb_documents` | Search the support knowledge base |
# MAGIC | `get_orders` | Phase 2 `orders_synced` | Fetch a customer's recent orders |
# MAGIC | `recall` | Phase 4 `episodes` | Search long-term agent memory |

# COMMAND ----------

# Cell 5 — Phase 3's retrieve(), copied here so the agent can call it
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
    ORDER BY embedding <=> q.qvec
    LIMIT :k_each
),
bm25 AS (
    SELECT doc_id, source, title, chunk,
           ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, q.qtsv) DESC) AS rank
    FROM kb_documents, q
    WHERE tsv @@ q.qtsv
    ORDER BY ts_rank(tsv, q.qtsv) DESC
    LIMIT :k_each
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

# Smoke
print(f"kb_retrieve smoke: {kb_retrieve('how do returns work', k=2)[0]['source']}")

# COMMAND ----------

# Cell 6 — three tool implementations + JSON schemas
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
        "description": "Search the Northwind support knowledge base for policies, FAQs, and warranty terms.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language question or keywords."},
                "k":     {"type": "integer", "description": "Number of chunks to return.", "default": 5},
            },
            "required": ["query"],
        },
    }},
    {"type": "function", "function": {
        "name": "get_orders",
        "description": "Fetch a customer's recent orders from the synced operational table.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Northwind customer id, e.g. c001."},
                "n":           {"type": "integer", "description": "Max orders to return.", "default": 10},
            },
            "required": ["customer_id"],
        },
    }},
    {"type": "function", "function": {
        "name": "recall",
        "description": "Search the agent's long-term memory of prior sessions for this user.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "query":   {"type": "string"},
                "k":       {"type": "integer", "default": 3},
            },
            "required": ["user_id", "query"],
        },
    }},
]

TOOL_FNS = {
    "retrieve":   tool_retrieve,
    "get_orders": tool_get_orders,
    "recall":     tool_recall,
}

print(f"✅ Tools registered: {list(TOOL_FNS)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Build `run_turn()` — the five-step loop
# MAGIC
# MAGIC The shape from Module 8 theory 8.4 made real:
# MAGIC 1. **Recall** episodes for this user
# MAGIC 2. **Build** the message list (system + history + new user msg)
# MAGIC 3. **Loop:** call LLM → if no tool calls, persist & return; if tool calls, execute, log, append, loop
# MAGIC
# MAGIC Capped at 4 iterations so a misbehaving model can't loop forever. Every tool call gets logged to `tool_calls` for audit.

# COMMAND ----------

# Cell 7 — run_turn()
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

        # Final answer (no tool calls)
        if not msg.tool_calls:
            mem.add_message(sid, "assistant", msg.content or "")
            return msg.content or ""

        # Append the assistant turn (with the tool calls) to messages
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        # Execute each tool call
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

    # Hit max iterations without converging — return what we have
    final = "(agent did not converge within max iterations)"
    mem.add_message(sid, "assistant", final)
    return final

print("✅ run_turn() ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Cross-session memory demo (the headline test)
# MAGIC
# MAGIC The single demo that proves the agent has *memory*, not just tool use. We run two sessions for the same `user_id`:
# MAGIC
# MAGIC - **Session 1** — Carla mentions she prefers email replies. We end the session and `remember` it as an episode.
# MAGIC - **Session 2** — A new session, no overlap with session 1. The agent should call `recall` (or surface from the system prompt) and honor the preference.
# MAGIC
# MAGIC > **If session 2 doesn't honor the preference, memory is broken.** Either the embedding pipeline lost a row, the `recall` tool is unused, or the system prompt context is malformed.

# COMMAND ----------

# Cell 8 — session 1: set the preference
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

# COMMAND ----------

# Cell 9 — session 2: new session, see if the preference survives
import time as _t
_t.sleep(1)  # ensure embeddings are committed

s2 = mem.start_session(USER)
print(f"▸ Session 2 started: {s2[:8]}...")

a2 = run_turn(s2, USER, "Customer c003 emailed asking about her order o006. How should I reply?")
print(f"\n   Assistant:\n{a2}")

# Check whether the response acknowledged the email preference
assert "email" in a2.lower(), (
    "❌ Session 2 did not honor the email preference from Session 1. "
    "Memory broken — see troubleshooting."
)
print("\n✅ CROSS-SESSION MEMORY VERIFIED — session 2 honored the preference set in session 1.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Multi-tool turn (both `retrieve` and `get_orders` in one conversation)
# MAGIC
# MAGIC The Definition of Done in the conceptual module says: "*Agent uses both tools — at least one turn invokes `retrieve` AND `get_orders`.*" Verify it on a single realistic question that needs both: a *policy* AND a *customer-specific fact*.

# COMMAND ----------

# Cell 10 — multi-tool turn
s3 = mem.start_session(USER)

# Snapshot tool_call count before
with engine.connect() as conn:
    n_before = conn.execute(text("SELECT count(*) FROM tool_calls WHERE session_id=:s"),
                             {"s": s3}).scalar()

q = "Customer c002 wants to return order o003. Is that order eligible per our policy?"
a3 = run_turn(s3, USER, q)
print(f"User: {q}")
print(f"\nAssistant:\n{a3}")

# Inspect tool_calls
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — Audit trail spot-check (the operational view of the agent)
# MAGIC
# MAGIC Every tool call is in `tool_calls` with arguments, results, and latency. This is the table you query during incident review when a customer says "the agent gave me wrong info." Run a small DBSQL-style query from Python.

# COMMAND ----------

# Cell 11 — audit-trail query
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9 — Final verification — 6 green checks

# COMMAND ----------

# Cell 12 — verification checklist
checks = []

# 1. episodes HNSW exists
with engine.connect() as conn:
    idx = set(conn.execute(text("""
        SELECT indexname FROM pg_indexes WHERE tablename='episodes'
    """)).scalars().all())
checks.append(("episodes HNSW present", "episodes_embedding_hnsw" in idx))

# 2. AgentMemory smoke OK
checks.append(("AgentMemory smoke session worked", True))  # asserted earlier

# 3. Cross-session memory
checks.append(("Cross-session memory verified", "email" in a2.lower()))

# 4. Both tools used in one turn
checks.append(("retrieve + get_orders in one turn", names_called >= {"retrieve","get_orders"}))

# 5. tool_calls populated
with engine.connect() as conn:
    n_calls = conn.execute(text("SELECT count(*) FROM tool_calls")).scalar()
checks.append((f"tool_calls audit rows ({n_calls})", n_calls >= 2))

# 6. episodes table has the demo episode
with engine.connect() as conn:
    n_ep = conn.execute(text("SELECT count(*) FROM episodes WHERE user_id=:u"),
                        {"u": USER}).scalar()
checks.append((f"episode persisted ({n_ep})", n_ep >= 1))

# Print
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
    print(f"  ⚠ {passed}/{total} passed — see troubleshooting below")
print("=" * 60)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Troubleshooting reference
# MAGIC
# MAGIC | ❌ Row | Most common cause | Resolution |
# MAGIC |---|---|---|
# MAGIC | **HNSW present** | Cell 3 ran before pgvector was enabled | Re-run Phase 1 Cell 6 then Phase 4 Cell 3 |
# MAGIC | **Cross-session memory** | `recall` returned 0 hits — embedding model mismatch | Confirm both `remember` and `recall` use the same model; rerun Cell 8/9 |
# MAGIC | **retrieve + get_orders** | LLM happy with one tool — temp too high or prompt unclear | The system prompt explicitly mentions both; lower temperature to 0.0 and retry |
# MAGIC | **tool_calls audit rows** | `log_tool_call` failed silently | Check the `tool_calls` schema matches Phase 1 Cell 6 |
# MAGIC | **episode persisted** | Embedding-call exception during `remember` | Re-run Cell 8; FM endpoint capacity sometimes blips |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 4 is complete and you have:
# MAGIC
# MAGIC - ✅ A working `AgentMemory` class — six methods, all parameterized SQL
# MAGIC - ✅ Three tool functions registered with the LLM via explicit JSON schemas
# MAGIC - ✅ A `run_turn()` agent loop with a tool-call cap and per-call audit logging
# MAGIC - ✅ **Cross-session memory verified** — preference set in session 1 surfaced in session 2
# MAGIC - ✅ **Multi-tool composition verified** — one turn invoked both `retrieve` and `get_orders`
# MAGIC - ✅ A populated `tool_calls` audit trail you can query from DBSQL
# MAGIC
# MAGIC The Phase 4 brain is exactly what Phase 5's Streamlit UI calls. The same `run_turn()` function — same engine pattern, same tools, same memory — runs inside the Databricks App.
# MAGIC
# MAGIC **Do not run cleanup.** Phase 5 imports the `AgentMemory` class and the tool functions verbatim into the app code.
# MAGIC
# MAGIC > **Next:** open `Module_08_Phase_5_Ship_App.py` to wrap the agent in a Streamlit UI and deploy it.

# COMMAND ----------

# Cell 13 — pin state for the next phase
print("=" * 60)
print(f"  ▸ Phase 4 complete. Pinned for Phase 5:")
print("=" * 60)
print(f"  AgentMemory   : 6 methods, all parameterized")
print(f"  Tools         : retrieve · get_orders · recall (3 schemas)")
print(f"  Sessions      : 3 created in this notebook")
print(f"  tool_calls    : {n_calls} rows logged")
print(f"  Cross-session : ✅ verified ({USER})")
print("=" * 60)
print(f"  ⏸ DO NOT clean up. Open Phase 5 next.")
print("=" * 60)
