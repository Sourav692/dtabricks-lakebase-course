# Databricks notebook source
# MAGIC %md
# MAGIC # Module 06 · Lab 6 — Lakebase as Memory Store for AI Agents, End-to-End
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~90 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Layer four-tier agent memory onto the Lakebase project from Module 3 (now also carrying the `documents` table from Module 5). Build the `sessions`, `messages`, `episodes`, `tool_calls` schema, implement an `AgentMemory` Python contract, define three tools with explicit JSON schemas, run a full agent loop, and prove cross-session memory works end-to-end.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — reuse the Module 3 + Module 5 project, install deps, open an engine
# MAGIC 2. **Build** the four-table memory schema (sessions, messages, episodes, tool_calls)
# MAGIC 3. **Implement** the `AgentMemory` Python contract — six methods
# MAGIC 4. **Define** the three tools with explicit JSON schemas
# MAGIC 5. **Build** the agent loop — `run_turn()` ties everything together
# MAGIC 6. **Cross-session memory demo** — set "I prefer email" in session 1, prove session 2 honors it
# MAGIC 7. **Audit-trail spot check** — query `tool_calls` from Python (and DBSQL)
# MAGIC 8. **Final verification & cleanup**
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Module 3 Lab 3 passed** with `CLEANUP=False` — this lab uses that project
# MAGIC - **Module 5 Lab 5 passed** with `CLEANUP_DOCS=False` — the `documents` table is the *semantic* memory layer
# MAGIC - **Foundation Models API** enabled in the workspace region
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sql` is used. Re-running individual cells is safe — Steps 2, 3, 4, 5 are written to be idempotent.
# MAGIC
# MAGIC > **⚠ Billing note.** This lab makes ~30 chat completions and ~10 embedding calls — well under one cent at Foundation Models pricing. Lakebase compute is the same `CU_1` instance from Module 3. Step 8b leaves the project alive so Module 7 can ship the agent as a Databricks App.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: reuse the Module 3 + Module 5 project
# MAGIC
# MAGIC You are not provisioning a new project. You are picking up the same `lakebase-tutorial` project from Module 3 (with the `documents` table still alive from Module 5) and opening a fresh engine on it.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `Project 'lakebase-tutorial' not found` | Module 3 cleanup ran | Re-run Module 3 Lab 3 with `CLEANUP=False` |
# MAGIC | `Table 'documents' not found` | Module 5 cleanup ran | Re-run Module 5 Lab 5 with `CLEANUP_DOCS=False` |
# MAGIC | `password authentication failed` | OAuth token expired | Re-run Cell 2; `generate_database_credential` issues a fresh token |
# MAGIC | `connection refused` | Project went idle | Wait 10–15s; Lakebase auto-resumes on first connection |

# COMMAND ----------

# Cell 1 — install dependencies (re-run if the kernel restarted)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector mlflow
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — reload the TUTORIAL config and re-open the engine on the Module 3 project
import uuid, time, json
from dataclasses import dataclass
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

TUTORIAL = {
    "catalog":      "main",
    "schema":       "lakebase_tutorial",
    "project_name": "lakebase-tutorial",
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
    "embed_dim":    1024,
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
}

# Confirm the project from Module 3 still exists.
existing = {p.name: p for p in w.database.list_database_projects()}
if TUTORIAL["project_name"] not in existing:
    raise RuntimeError(
        f"Project '{TUTORIAL['project_name']}' not found. "
        f"Run Module 3 Lab 3 first (with CLEANUP=False)."
    )
project = existing[TUTORIAL["project_name"]]
assert project.state == "READY", f"Project state is {project.state}, not READY"

# Fresh OAuth token + SQLAlchemy engine
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)
url = (
    f"postgresql+psycopg://{TUTORIAL['user']}:{cred.token}"
    f"@{project.read_write_dns}:5432/databricks_postgres?sslmode=require"
)
engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

# Confirm the Module 5 documents table still exists — we need it as semantic memory.
with engine.connect() as conn:
    pg_version = conn.execute(text("SELECT version()")).scalar()
    docs_count = conn.execute(text("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_name = 'documents'
    """)).scalar()
if docs_count == 0:
    raise RuntimeError(
        "Table 'documents' not found. "
        "Run Module 5 Lab 5 first (with CLEANUP_DOCS=False)."
    )

print(f"✅ Reconnected to '{TUTORIAL['project_name']}'")
print(f"   {pg_version.split(',')[0]}")
print(f"   Endpoint:  {project.read_write_dns}")
print(f"   Module 5 'documents' table present (semantic layer ready)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Build the four-table memory schema
# MAGIC
# MAGIC The schema from Module 6 theory 6.2. Four tables, the foreign-key/cascade choices, the HNSW index on episodes, the composite index on messages. All idempotent.

# COMMAND ----------

# Cell 3 — agent memory schema
DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    agent_name  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    ended_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT CHECK (role IN ('system','user','assistant','tool')),
    content     TEXT,
    tool_call   JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS episodes (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    summary     TEXT,
    embedding   vector(1024),
    importance  REAL DEFAULT 0.5,
    last_seen   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_epi_hnsw ON episodes
    USING hnsw (embedding vector_cosine_ops)
    WITH  (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_epi_user ON episodes(user_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID REFERENCES sessions(id),
    tool_name   TEXT,
    args        JSONB,
    result      JSONB,
    duration_ms INT,
    ok          BOOL,
    ts          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tc_session ON tool_calls(session_id, ts);
"""

with engine.begin() as conn:
    for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
        conn.execute(text(stmt))

# Confirm
with engine.connect() as conn:
    tabs = conn.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_name IN ('sessions','messages','episodes','tool_calls')
        ORDER BY table_name
    """)).fetchall()
    idx = conn.execute(text("""
        SELECT tablename, indexname FROM pg_indexes
        WHERE tablename IN ('sessions','messages','episodes','tool_calls')
        ORDER BY tablename, indexname
    """)).fetchall()

print("✅ Memory tables created:")
for t in tabs:
    print(f"   · {t.table_name}")
print("\n✅ Indexes:")
for i in idx:
    print(f"   · {i.tablename:<12} {i.indexname}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Implement the `AgentMemory` Python contract
# MAGIC
# MAGIC Six methods from Module 6 theory 6.3. Every SQL statement is parameterized. Engine and embed function are both injected so the class is testable.
# MAGIC
# MAGIC **Step 3a** — define the Foundation Models embed helper (same as Module 5).
# MAGIC **Step 3b** — define the `AgentMemory` class with six methods.

# COMMAND ----------

# Cell 4 — embed helper (Foundation Models API, same as Module 5)
oai = w.serving_endpoints.get_open_ai_client()

def embed(texts):
    """Embed a list of strings into 1024-dim vectors via bge-large-en."""
    if isinstance(texts, str):
        texts = [texts]
    out = oai.embeddings.create(
        model=TUTORIAL["embed_model"],
        input=texts,
    )
    return [d.embedding for d in out.data]

# Sanity check
v = embed(["agent memory test"])[0]
assert len(v) == TUTORIAL["embed_dim"], f"Expected 1024, got {len(v)}"
print(f"✅ Embedder ready · dim={len(v)} · first 4 floats: {v[:4]}")

# COMMAND ----------

# Cell 5 — the AgentMemory contract (Module 6 theory 6.3)
@dataclass
class Episode:
    summary: str
    importance: float
    distance: float

@dataclass
class Msg:
    role: str
    content: str

class AgentMemory:
    """Six-method contract over the four-table schema.
    Engine + embed_fn are injected for testability.
    """

    def __init__(self, engine, embed_fn):
        self.e = engine
        self.embed = embed_fn

    # ── short-term ────────────────────────────────────────────
    def start_session(self, user_id, agent="assistant") -> str:
        with self.e.begin() as c:
            sid = c.execute(text("""
                INSERT INTO sessions(user_id, agent_name)
                VALUES (:u, :a) RETURNING id
            """), dict(u=user_id, a=agent)).scalar()
        return str(sid)

    def end_session(self, sid):
        with self.e.begin() as c:
            c.execute(text("UPDATE sessions SET ended_at = now() WHERE id = :s"),
                      dict(s=sid))

    def add_message(self, sid, role, content, tool_call=None):
        with self.e.begin() as c:
            c.execute(text("""
                INSERT INTO messages(session_id, role, content, tool_call)
                VALUES (:s, :r, :c, :t)
            """), dict(
                s=sid, r=role, c=content,
                t=json.dumps(tool_call) if tool_call else None,
            ))

    def recent_messages(self, sid, n=20):
        with self.e.connect() as c:
            rows = c.execute(text("""
                SELECT role, content FROM messages
                WHERE session_id = :s
                ORDER BY created_at DESC
                LIMIT :n
            """), dict(s=sid, n=n)).fetchall()
        return [Msg(r.role, r.content) for r in rows[::-1]]

    # ── long-term episodic ────────────────────────────────────
    def remember(self, user_id, summary, importance=0.5):
        v = self.embed([summary])[0]
        with self.e.begin() as c:
            c.execute(text("""
                INSERT INTO episodes(user_id, summary, embedding, importance)
                VALUES (:u, :s, CAST(:e AS vector), :i)
            """), dict(u=user_id, s=summary, e=str(v), i=importance))

    def recall(self, user_id, query, k=5):
        qv = self.embed([query])[0]
        with self.e.connect() as c:
            rows = c.execute(text("""
                SELECT summary, importance, embedding <=> CAST(:q AS vector) AS dist
                FROM episodes
                WHERE user_id = :u
                ORDER BY (embedding <=> CAST(:q AS vector)) - importance * 0.1
                LIMIT :k
            """), dict(u=user_id, q=str(qv), k=k)).fetchall()
        return [Episode(r.summary, r.importance, r.dist) for r in rows]

    # ── procedural / audit ────────────────────────────────────
    def log_tool_call(self, sid, tool_name, args, result, duration_ms, ok):
        with self.e.begin() as c:
            c.execute(text("""
                INSERT INTO tool_calls(session_id, tool_name, args, result,
                                      duration_ms, ok)
                VALUES (:s, :n, :a, :r, :d, :o)
            """), dict(
                s=sid, n=tool_name,
                a=json.dumps(args), r=json.dumps(result),
                d=duration_ms, o=ok,
            ))

# Smoke test the contract
mem = AgentMemory(engine, embed)
sid_smoke = mem.start_session("smoke@test.com")
mem.add_message(sid_smoke, "user", "ping")
mem.add_message(sid_smoke, "assistant", "pong")
hist = mem.recent_messages(sid_smoke)
assert len(hist) == 2 and hist[0].role == "user", "Contract smoke test failed"
mem.end_session(sid_smoke)
print(f"✅ AgentMemory contract works (smoke session: {sid_smoke[:8]}…)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Define the three tools with explicit JSON schemas
# MAGIC
# MAGIC Three tools, matching Module 6 theory 6.4: `retrieve` (semantic), `get_orders` (operational), `recall` (long-term memory). Each has an explicit JSON schema the LLM can read.

# COMMAND ----------

# Cell 6 — tool definitions and implementations
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "retrieve",
            "description": "Search the support knowledge base (Module 5 documents). "
                           "Use for product info, policies, FAQ.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": "Fetch recent orders for a customer email "
                           "from the operational app database (Module 3 schema).",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search the agent's long-term memories about THIS user. "
                           "Use when the user asks 'what do you remember about me?'",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
]

# ── Tool implementations ────────────────────────────────────
def tool_retrieve(query: str, k: int = 3):
    """Hybrid RRF over Module 5 documents (vector + BM25)."""
    qv = embed([query])[0]
    with engine.connect() as conn:
        rows = conn.execute(text("""
            WITH v AS (
              SELECT id, ROW_NUMBER() OVER
                (ORDER BY embedding <=> CAST(:qv AS vector)) AS rk
              FROM documents
              ORDER BY embedding <=> CAST(:qv AS vector) LIMIT 50
            ),
            k AS (
              SELECT id, ROW_NUMBER() OVER
                (ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC) AS rk
              FROM documents
              WHERE tsv @@ plainto_tsquery('english', :q) LIMIT 50
            )
            SELECT d.title, d.content
            FROM documents d
            LEFT JOIN v USING(id) LEFT JOIN k USING(id)
            WHERE v.id IS NOT NULL OR k.id IS NOT NULL
            ORDER BY COALESCE(1.0/(60 + v.rk), 0)
                   + COALESCE(1.0/(60 + k.rk), 0) DESC
            LIMIT :k
        """), dict(qv=str(qv), q=query, k=k)).fetchall()
    return [{"title": r.title, "snippet": r.content[:240]} for r in rows]

def tool_get_orders(email: str):
    """Operational lookup against the Module 3 orders table."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT o.sku, o.qty, o.total_cents, o.placed_at
            FROM orders o JOIN users u ON o.user_id = u.id
            WHERE u.email = :e
            ORDER BY o.placed_at DESC
            LIMIT 5
        """), dict(e=email)).fetchall()
    return [{"sku": r.sku, "qty": r.qty,
             "total_cents": r.total_cents,
             "placed_at": r.placed_at.isoformat()} for r in rows]

def tool_recall(user_id: str, query: str, k: int = 3):
    """Vector recall over THIS user's episodes."""
    eps = mem.recall(user_id, query, k=k)
    return [{"summary": e.summary, "importance": float(e.importance)} for e in eps]

# Smoke test the tools
print("✅ retrieve smoke:", tool_retrieve("return policy", k=1))
print("✅ get_orders smoke:", tool_get_orders("alice@example.com"))
print("✅ recall smoke:", tool_recall("smoke@test.com", "anything", k=1))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Build the agent loop
# MAGIC
# MAGIC The five-step loop from Module 6 theory 6.4: recall → build → LLM → tools → write. Each call is a clearly-named helper so the loop reads top-to-bottom.

# COMMAND ----------

# Cell 7 — the agent loop
def chat(messages, tools=None, temperature=0.0):
    """Thin wrapper over the chat-completion endpoint."""
    kwargs = dict(
        model=TUTORIAL["chat_model"],
        messages=messages,
        temperature=temperature,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return oai.chat.completions.create(**kwargs)

def execute_tool(sid, user_id, name, args):
    """Run one tool, log it, return the result string for the LLM."""
    t0 = time.time()
    ok = True
    result = None
    try:
        if name == "retrieve":
            result = tool_retrieve(args["query"])
        elif name == "get_orders":
            result = tool_get_orders(args["email"])
        elif name == "recall":
            result = tool_recall(user_id, args["query"])
        else:
            ok = False
            result = {"error": f"unknown tool: {name}"}
    except Exception as ex:
        ok = False
        result = {"error": str(ex)}
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        mem.log_tool_call(sid, name, args, result, duration_ms, ok)
    return result

def run_turn(sid, user_id, user_msg):
    """One full turn — Module 6 theory 6.4 in code."""

    # 1. Recall top-3 long-term episodes
    episodes = mem.recall(user_id, user_msg, k=3)
    epi_text = "\n".join(f"- {e.summary}" for e in episodes) or "(no prior memories)"

    # 2. Build the message stack
    history = mem.recent_messages(sid, n=10)
    messages = [
        {"role": "system",
         "content": (
             "You are a Northwind support assistant. "
             "Be concise. Cite sources from retrieve results when used.\n"
             f"Known about this user (long-term memory):\n{epi_text}"
         )},
        *[{"role": m.role, "content": m.content} for m in history],
        {"role": "user", "content": user_msg},
    ]
    mem.add_message(sid, "user", user_msg)

    # 3. Call the LLM with tool definitions
    out = chat(messages, tools=TOOLS)
    assistant_msg = out.choices[0].message

    # 4. If the LLM asked for tools, execute them and call again
    if assistant_msg.tool_calls:
        messages.append({
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [tc.model_dump() for tc in assistant_msg.tool_calls],
        })
        for tc in assistant_msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = execute_tool(sid, user_id, tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
            mem.add_message(sid, "tool",
                            json.dumps({"name": tc.function.name, "result": result}),
                            tool_call={"id": tc.id, "name": tc.function.name})

        out = chat(messages, tools=TOOLS)
        assistant_msg = out.choices[0].message

    # 5. Write the assistant's reply
    reply = assistant_msg.content or ""
    mem.add_message(sid, "assistant", reply)
    return reply

print("✅ run_turn defined")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — The cross-session memory demo
# MAGIC
# MAGIC The killer demo. Two sessions, same user. In session 1, the user states a preference. In session 2, the agent recalls it without being told again.
# MAGIC
# MAGIC - **Step 6a** — session 1: state the preference
# MAGIC - **Step 6b** — distill session 1 into an episode (the nightly job, run on demand)
# MAGIC - **Step 6c** — session 2: prove the agent remembers

# COMMAND ----------

# Cell 8 — Step 6a: Session 1: user states a preference
user_email = "demo.user@example.com"

sid1 = mem.start_session(user_email, agent="northwind-support")
print(f"▸ Session 1 started: {sid1[:8]}…\n")

reply = run_turn(sid1, user_email,
    "Hi, I have a quick question. Just so you know — I prefer concise email replies, "
    "no chit-chat. What's your return policy?")
print(f"AGENT: {reply}\n")

reply = run_turn(sid1, user_email,
    "Got it, thanks. Please remember the email-reply thing for next time.")
print(f"AGENT: {reply}")

mem.end_session(sid1)
print(f"\n▸ Session 1 ended.")

# COMMAND ----------

# Cell 9 — Step 6b: distill the just-ended session into one episode
def distill(sid):
    """Read all messages from the session, ask LLM to summarize, store as episode."""
    with engine.connect() as conn:
        msgs = conn.execute(text("""
            SELECT role, content FROM messages
            WHERE session_id = :s ORDER BY created_at
        """), dict(s=sid)).fetchall()
        user_id = conn.execute(text(
            "SELECT user_id FROM sessions WHERE id = :s"
        ), dict(s=sid)).scalar()

    transcript = "\n".join(f"{m.role}: {m.content}" for m in msgs)

    prompt = (
        "Summarize this user's conversation in 3 sentences. "
        "Focus on stated preferences, durable facts, and decisions. "
        "Skip pleasantries and chitchat. "
        "After the summary, write a separate line:\n"
        "IMPORTANCE: <0.0-1.0>\n"
        "based on how useful this is for future sessions.\n\n"
        f"--- TRANSCRIPT ---\n{transcript}\n--- END ---"
    )
    out = chat([{"role": "user", "content": prompt}])
    raw = out.choices[0].message.content

    # Parse summary + importance
    lines = raw.strip().split("\n")
    importance = 0.5
    summary_lines = []
    for line in lines:
        if line.upper().startswith("IMPORTANCE"):
            try:
                importance = float(line.split(":", 1)[1].strip())
                importance = max(0.0, min(1.0, importance))
            except Exception:
                pass
        else:
            summary_lines.append(line)
    summary = " ".join(s.strip() for s in summary_lines if s.strip())

    mem.remember(user_id, summary, importance=importance)
    return summary, importance

summary1, imp1 = distill(sid1)
print(f"✅ Episode written")
print(f"   Summary:    {summary1}")
print(f"   Importance: {imp1}")

with engine.connect() as conn:
    n = conn.execute(text(
        "SELECT count(*) FROM episodes WHERE user_id = :u"
    ), dict(u=user_email)).scalar()
print(f"   Episodes for {user_email}: {n}")

# COMMAND ----------

# Cell 10 — Step 6c: Session 2: same user, fresh session, no preference restated
sid2 = mem.start_session(user_email, agent="northwind-support")
print(f"▸ Session 2 started: {sid2[:8]}…\n")

reply = run_turn(sid2, user_email,
    "Hey, what are my recent orders?")
print(f"AGENT: {reply}\n")
print("─" * 60)

# Check what episodes were recalled at the start of this session.
recalled = mem.recall(user_email, "what are my recent orders?", k=3)
print(f"\n▸ Episodes recalled into session 2's system prompt:")
for r in recalled:
    print(f"   · imp={r.importance:.2f} dist={r.distance:.3f} :: {r.summary[:80]}…")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Audit-trail spot check
# MAGIC
# MAGIC The `tool_calls` table is your procedural memory. Module 6 theory called this "the receipt your agent leaves for every action it took." Now you have receipts to inspect.

# COMMAND ----------

# Cell 11 — query the audit trail across both demo sessions
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT s.id::text AS session_id,
               t.tool_name, t.duration_ms, t.ok, t.ts
        FROM tool_calls t
        JOIN sessions s ON t.session_id = s.id
        WHERE s.user_id = :u
        ORDER BY t.ts
    """), dict(u=user_email)).fetchall()

print(f"Tool calls for {user_email}: {len(rows)} rows")
print(f"{'SESSION':<10} {'TOOL':<14} {'DURATION':<10} {'OK':<4} TS")
print("-" * 75)
for r in rows:
    print(f"{r.session_id[:8]:<10} {r.tool_name:<14} "
          f"{r.duration_ms:>5} ms  {'✅' if r.ok else '❌':<4} {r.ts}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — Final verification & cleanup
# MAGIC
# MAGIC A pass/fail summary of everything you built. Cleanup is *optional* — Module 7 (Databricks Apps) builds directly on this exact agent code.

# COMMAND ----------

# Cell 12 — final checklist
checks = []

# 1. Four memory tables exist
try:
    with engine.connect() as conn:
        n = conn.execute(text("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_name IN ('sessions','messages','episodes','tool_calls')
        """)).scalar()
    checks.append(("4 memory tables exist", f"{n}/4 found", n == 4))
except Exception as e:
    checks.append(("4 memory tables exist", str(e)[:30], False))

# 2. AgentMemory contract works
try:
    sid_test = mem.start_session("checklist@test.com")
    mem.add_message(sid_test, "user", "ping")
    h = mem.recent_messages(sid_test)
    mem.end_session(sid_test)
    ok = len(h) == 1 and h[0].role == "user"
    checks.append(("AgentMemory round-trip", "INSERT+SELECT OK" if ok else "fail", ok))
except Exception as e:
    checks.append(("AgentMemory round-trip", str(e)[:30], False))

# 3. HNSW on episodes is being used (not seq scan)
try:
    qv = embed(["preferences"])[0]
    with engine.connect() as conn:
        plan = conn.execute(text("""
            EXPLAIN SELECT id FROM episodes
            ORDER BY embedding <=> CAST(:qv AS vector) LIMIT 3
        """), {"qv": str(qv)}).fetchall()
    plan_text = "\n".join(p[0] for p in plan)
    using_hnsw = "idx_epi_hnsw" in plan_text
    checks.append(("HNSW index on episodes",
                   "Index Scan" if using_hnsw else "Seq Scan", using_hnsw))
except Exception as e:
    checks.append(("HNSW index on episodes", str(e)[:30], False))

# 4. Episode was distilled from session 1
try:
    with engine.connect() as conn:
        ep = conn.execute(text("""
            SELECT summary, importance FROM episodes
            WHERE user_id = :u
            ORDER BY last_seen DESC LIMIT 1
        """), dict(u=user_email)).first()
    has_pref = ep is not None and (
        "email" in (ep.summary or "").lower() or "concise" in (ep.summary or "").lower()
    )
    detail = f"imp={ep.importance:.2f}" if ep else "no episode"
    checks.append(("Episode captured preference", detail, has_pref))
except Exception as e:
    checks.append(("Episode captured preference", str(e)[:30], False))

# 5. Cross-session recall returns the preference
try:
    eps = mem.recall(user_email, "what does this user prefer?", k=3)
    found = any("email" in e.summary.lower() or "concise" in e.summary.lower()
                for e in eps)
    detail = f"top imp={eps[0].importance:.2f}" if eps else "no recall"
    checks.append(("Cross-session recall works", detail, found))
except Exception as e:
    checks.append(("Cross-session recall works", str(e)[:30], False))

# 6. tool_calls audit trail is populated
try:
    with engine.connect() as conn:
        n = conn.execute(text("""
            SELECT count(*) FROM tool_calls t JOIN sessions s ON t.session_id=s.id
            WHERE s.user_id = :u
        """), dict(u=user_email)).scalar()
    checks.append(("tool_calls audit trail", f"{n} rows", n >= 1))
except Exception as e:
    checks.append(("tool_calls audit trail", str(e)[:30], False))

# Print
print("=" * 70)
print(f"{'CHECK':<32} {'DETAIL':<24} STATUS")
print("-" * 70)
for name, detail, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"{name:<32} {str(detail)[:24]:<24} {icon}")
print("=" * 70)
passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\n🎯 ALL {total} CHECKS PASSED — Module 6 complete.")
else:
    print(f"\n⚠ {passed}/{total} passed. Resolve the ❌ rows before Module 7.")

# COMMAND ----------

# Cell 13 — cleanup
# Module 7 (Databricks Apps) reuses these tables — most readers leave CLEANUP=False.
CLEANUP_MEMORY = False

if CLEANUP_MEMORY:
    with engine.begin() as conn:
        # Order matters — drop children before parents
        conn.execute(text("DROP TABLE IF EXISTS tool_calls"))
        conn.execute(text("DROP TABLE IF EXISTS messages"))
        conn.execute(text("DROP TABLE IF EXISTS episodes"))
        conn.execute(text("DROP TABLE IF EXISTS sessions"))
    print("✅ Dropped 4 memory tables. (documents and Module 3 schema preserved.)")
else:
    print("⏸ Cleanup skipped. Module 7 reuses these tables for the Databricks App.")
    print(f"   Project: {TUTORIAL['project_name']}")
    print(f"   Tables:  sessions · messages · episodes · tool_calls")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your final cell printed `🎯 ALL 6 CHECKS PASSED`, you have done — for real, not in slides:
# MAGIC
# MAGIC - ✅ Built the four-table memory schema (sessions, messages, episodes, tool_calls) with HNSW + composite indexes
# MAGIC - ✅ Implemented the `AgentMemory` Python contract — six methods, all parameterized, fully testable
# MAGIC - ✅ Defined three tools with explicit JSON schemas (`retrieve`, `get_orders`, `recall`)
# MAGIC - ✅ Composed the five-step agent loop where short-term, long-term, semantic, and procedural memory all enter at the right step
# MAGIC - ✅ Distilled a session into an episode with LLM-scored importance — the same pattern ChatGPT uses
# MAGIC - ✅ Proved cross-session memory works end-to-end — the agent honored "I prefer email" in session 2 without being told twice
# MAGIC - ✅ Inspected the procedural audit trail — every tool call, every duration, every ok/fail, queryable from DBSQL
# MAGIC
# MAGIC **Next up: Module 7** — *Powering Databricks Apps with Lakebase.* You'll wrap `run_turn` in a Streamlit UI, learn the OBO identity model and per-request engine factories, navigate the connection-pooling trap, and ship the memory-aware agent as a real Databricks App.
