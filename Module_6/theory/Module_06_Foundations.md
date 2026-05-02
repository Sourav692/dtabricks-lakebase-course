# Module 06 — Lakebase as Memory Store for AI Agents: Concepts Behind the Lab

> **Level:** Intermediate · **Duration:** ~60 min (theory) + ~3h (hands-on lab) · **Format:** Conceptual primer
> **Coverage:** Topics 6.1 → 6.6 — the *why* behind every line of agent-memory code you'll write in the Module 6 lab.

---

## What this module gives you

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 put a real Lakebase project in your hands. Module 4 wired it to the lakehouse. Module 5 turned that same Postgres into a vector store. **Module 6 turns it into the memory substrate of an AI agent** — where conversations, distilled episodes, knowledge, and tool-call audit trails all live in one Postgres, one auth, one bill.

Before you open the notebook, you need a clean mental model of four conceptual jumps:

1. **The four memory layers** — short-term, long-term episodic, semantic, procedural — and why every modern agent framework converges on this same model
2. **Schema design for agent memory** — sessions, messages, episodes, tool_calls — and the foreign-key/index choices that make recall fast
3. **The AgentMemory contract** — wrapping raw SQL behind a clean Python interface so the agent never sees Postgres
4. **The agent loop** — how recall, retrieve, LLM, tools, and message-logging compose into one stateful turn

This module covers the theory. The Module 6 lab notebook walks you through running it — building the agent schema on top of your Module 5 `documents` table, writing the `AgentMemory` class, building a three-tool agent loop, and proving cross-session memory works.

> **Prerequisite check.** You should have completed Module 3 Lab 3 (working Lakebase project), Module 4 Lab 4 (synced tables understood), and Module 5 Lab 5 (the `documents` table is still alive with `pgvector` enabled and HNSW indexes built). Module 6 layers four new tables onto that schema — it does not provision a new project, and it reuses the same embedding pipeline you used in Module 5.

---

## 6.1 — The Four Memory Layers: Conceptual Model

### Why agents need memory at all

A pure LLM call is stateless. You hand it a prompt; it hands you a response; the next call starts from zero. That is fine for a chatbot answering one-off factual questions. It is **catastrophic** for an agent that should:

- Remember what you said three turns ago in the same conversation
- Remember a preference you set last week ("I like concise replies")
- Look things up in a shared knowledge base
- Keep an audit trail of every tool it called and what came back

These four needs are not the same need. They differ in **lifetime, scope, and access pattern**. Modern agent frameworks — LangGraph, AutoGen, Mosaic AI Agent Framework — all converge on the same four-layer answer.

### The four layers — memorize this table

| Layer | Lifetime | Scope | Postgres pattern |
|---|---|---|---|
| **Short-term** (working) | Single conversation | One session | `messages` table keyed by `session_id` |
| **Long-term episodic** | Across sessions | One user | `episodes` table with vector embeddings + importance |
| **Semantic** | Permanent | Cross-user knowledge | `documents` table (the one from Module 5) |
| **Procedural** | Audit / forever | One session, append-only | `tool_calls` log with args, result, duration, ok |

Three things to internalize about this table:

- **All four can live in the same Lakebase database.** One auth, one connection pool, one query plane, one bill. This is the entire pitch — the seam between "app data" and "agent state" disappears.
- **Lifetime increases as you go down.** A short-term message dies with the session. An episode lives until you prune it. A document lives forever (until business says otherwise). A tool call is your audit trail and never dies.
- **Access pattern differs.** Short-term is `WHERE session_id = ? ORDER BY created_at` — pure B-tree. Episodic is vector similarity (`embedding <=> :q`) filtered by user — HNSW. Semantic is hybrid retrieval (vector + BM25) — what you built in Module 5. Procedural is rarely queried at runtime; it's read by humans during incident review.

### Why this is one Postgres, not four services

Customers often arrive with this stack already half-built:

- Short-term in **Redis**
- Long-term in **DynamoDB** or **MongoDB**
- Knowledge in **Pinecone** or **Weaviate**
- Tool logs in **CloudWatch** or **Datadog**

That works, but every cross-layer query is now a fan-out across four services with four auth models, four billing lines, four eventual-consistency windows, and four places to look during a P1. **Lakebase collapses all four into one Postgres.** A query that joins "this user's messages this session" with "their top-3 episodes" with "the documents we cited" with "the tool calls we made" is one SQL statement, one transaction, one ACL.

### Why this isn't "just put everything in one database"

The reason this works *cleanly* on Postgres specifically — and not on, say, MySQL or MongoDB — comes down to four properties Postgres already has:

- **JSONB** for flexible tool-call args and results, with GIN indexing
- **`pgvector`** (Module 5) for embedding-based recall on episodes
- **Foreign keys with `ON DELETE CASCADE`** so deleting a session also drops its messages and tool calls atomically
- **MVCC** so a long-running agent loop reading from `messages` doesn't block another agent writing to it

Without these four, "one database for memory" becomes painful fast. With them, it's just good schema design.

> **🎯 Checkpoint for 6.1**
> Without looking, name the four memory layers with their lifetime and the Postgres table pattern for each. A passing answer: short-term/messages, long-term/episodes, semantic/documents, procedural/tool_calls. If you can also state why all four belong in one database, you're set.

---

## 6.2 — Schema Design: Sessions, Messages, Episodes, Tool Calls

### The full schema, in 40 lines of DDL

```sql
CREATE TABLE sessions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     TEXT NOT NULL,
  agent_name  TEXT,
  created_at  TIMESTAMPTZ DEFAULT now(),
  ended_at    TIMESTAMPTZ
);

CREATE TABLE messages (
  id          BIGSERIAL PRIMARY KEY,
  session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
  role        TEXT CHECK (role IN ('system','user','assistant','tool')),
  content     TEXT,
  tool_call   JSONB,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON messages(session_id, created_at);

CREATE TABLE episodes (
  id          BIGSERIAL PRIMARY KEY,
  user_id     TEXT NOT NULL,
  summary     TEXT,
  embedding   vector(1024),
  importance  REAL DEFAULT 0.5,
  last_seen   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON episodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON episodes(user_id);

CREATE TABLE tool_calls (
  id          BIGSERIAL PRIMARY KEY,
  session_id  UUID REFERENCES sessions(id),
  tool_name   TEXT,
  args        JSONB,
  result      JSONB,
  duration_ms INT,
  ok          BOOL,
  ts          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON tool_calls(session_id, ts);
```

Every line in here earns its place. Walk through it once with intent and you'll never have to look it up again.

### `sessions` — UUID, not BIGSERIAL

The session ID is a **UUID** generated by `gen_random_uuid()`, not a sequential integer. There's a deliberate reason:

- **Clients can mint session IDs themselves** — a Streamlit front-end can `uuid.uuid4()` a session ID and use it in the very first INSERT, eliminating a round-trip to fetch a `RETURNING id`
- **No collision across distributed agents** — three agent workers in three regions can all create sessions without coordinating
- **Privacy** — UUIDs don't leak how many sessions exist (BIGSERIAL would: `id=42` tells you 41 sessions came before)

The `ended_at` column is nullable on purpose. An open session has `ended_at IS NULL`. The nightly distillation job (Topic 6.5) uses this column to find sessions ready to summarize.

### `messages` — the role check and the JSONB tool_call

The `role CHECK` constraint enforces the OpenAI / Anthropic / Mosaic AI message-role contract: `system`, `user`, `assistant`, `tool`. Anything else is an error at insert time, not a silent bug at LLM-call time. Catch the typo when it's cheap to fix.

The `tool_call` column is `JSONB`, not `TEXT`. This matters when an `assistant` message contains a function call: the LLM hands you back structured arguments, and you store them as JSONB so you can later query them (e.g., "show me every time this agent called `get_orders` with `email='alice@example.com'`").

The composite index `(session_id, created_at)` is the workhorse. Every recall of conversation history runs `WHERE session_id = ? ORDER BY created_at DESC LIMIT n` — that index serves it in microseconds even at millions of rows.

### `episodes` — the vector + importance trick

Episodes are the bridge between sessions. After a session ends, you ask the LLM to compress it into a 3-sentence summary, embed the summary with the same model you used in Module 5 (BGE 1024-dim), and write it as a row.

Two columns deserve attention:

- **`embedding vector(1024)`** — same dimension as the Module 5 documents table. Use the same model. Never mix models in the same schema; the cosine distances become meaningless.
- **`importance REAL DEFAULT 0.5`** — a 0.0–1.0 score saying "how important is this memory?" The LLM scores it during distillation. Recall ranks by `(distance) − (importance × 0.1)` — so a slightly less semantically similar but high-importance episode beats a perfect match with low importance.

The HNSW index on `embedding` makes recall fast. The B-tree on `user_id` is the access pattern: every recall query is `WHERE user_id = :u`, never cross-user.

### `tool_calls` — the procedural log

This is your audit trail. Every time the agent invokes a tool — `retrieve`, `get_orders`, `recall`, anything — you write one row. The columns are:

- **`tool_name`** — what was called
- **`args` (JSONB)** — what arguments
- **`result` (JSONB)** — what came back
- **`duration_ms`** — how long it took
- **`ok` (BOOL)** — did it succeed
- **`ts`** — when

This table is **append-only by convention**. You never UPDATE a tool call, you never DELETE one (until a retention job purges them at, say, 90 days). It's the receipt your agent leaves for every action it took.

The `(session_id, ts)` index supports the most common debug query: "show me everything this session did, in order." The next most common, "find all the tool calls that took over 500ms," is just a sequential scan with a filter — fine, this isn't a hot path.

### Why ON DELETE CASCADE on messages but not on tool_calls

Notice the asymmetry:

- `messages.session_id REFERENCES sessions(id) ON DELETE CASCADE`
- `tool_calls.session_id REFERENCES sessions(id)` — **no CASCADE**

The reason is policy, not technology. If a user requests deletion of their conversation history (GDPR, internal retention), you `DELETE FROM sessions WHERE user_id = ?` and Postgres atomically removes the messages too. Tool-call logs are typically retained longer for audit / SRE / billing reasons — so they hang around even after the session row is gone, accessible by `session_id` (which you can resolve via your audit pipeline). Pick the policy that matches your customer's compliance posture.

### Why no `users` table here

The schema uses `user_id TEXT` rather than a foreign key into a `users` table. That's deliberate: in a Databricks Apps deployment, the `user_id` is the workspace OAuth identity (an email or numeric ID) — there's no Lakebase-side user record to join to. Module 7 makes this concrete. If you have a `users` table for an operational reason, add the FK; otherwise keep it as a TEXT identifier and let the auth layer be the source of truth.

> **🎯 Checkpoint for 6.2**
> Without looking, name the four tables, their primary keys, and the indexes on each. A passing answer touches: UUID on sessions, the composite index on messages, HNSW + user_id on episodes, the (session_id, ts) index on tool_calls. If you can also state why messages cascades on session delete but tool_calls doesn't, that's the depth.

---

## 6.3 — The AgentMemory Contract: Clean Python over Raw SQL

### Why wrap SQL behind a class

The agent loop is going to call into memory dozens of times per turn — start a session, recall episodes, fetch recent history, log a message, log a tool call, write an episode at the end. If every one of those is hand-written SQL spread across the agent code, three things go wrong:

- **SQL injection risk** — someone, somewhere, will eventually concatenate a user-controlled string into a query
- **Inconsistency** — six different places that fetch "the last 10 messages" will end up with six slightly different queries
- **Untestable** — you can't mock a Postgres connection in unit tests without standing up a real database

The fix is a thin **memory contract** — one class, six methods, every method takes parameters and emits parameterized SQL. The agent never sees a `text()` call.

### The contract

```python
class AgentMemory:
    def __init__(self, engine, embed_fn):
        self.e, self.embed = engine, embed_fn

    def start_session(self, user_id, agent="assistant") -> str: ...
    def add_message(self, sid, role, content, tool_call=None): ...
    def recent_messages(self, sid, n=20): ...
    def remember(self, user_id, summary, importance=0.5): ...
    def recall(self, user_id, query, k=5): ...
    def log_tool_call(self, sid, name, args, result, duration_ms, ok): ...
```

Six methods cover the entire surface area of agent memory. Nothing else needs raw SQL.

### Method-by-method reasoning

**`__init__(engine, embed_fn)`** — Two dependencies, both injected. The `engine` is a SQLAlchemy engine (so the test suite can pass a SQLite engine pointing at `:memory:`). The `embed_fn` is whatever embeds text — Foundation Models in production, a stub that returns zeros in tests. **No globals, no module-level state.** This is the single most important property of the contract.

**`start_session(user_id, agent)`** — One INSERT, returns the new UUID as a string. Idempotent? No — calling it twice creates two sessions. That's correct: the agent loop should call this exactly once per conversation start.

**`add_message(sid, role, content, tool_call=None)`** — One INSERT into `messages`. The `tool_call` is `json.dumps`'d if present. Notice: parameterized — `:s, :r, :c, :t` — never f-string formatting. This is non-negotiable.

**`recent_messages(sid, n=20)`** — `SELECT role, content FROM messages WHERE session_id=:s ORDER BY created_at DESC LIMIT :n`, then **reverse the result list in Python** (`[::-1]`) so the caller gets oldest-first. Why fetch DESC and reverse instead of ASC with `OFFSET`? Because DESC + LIMIT is O(log n) with the composite index, while ASC + OFFSET on a large session is a scan.

**`remember(user_id, summary, importance=0.5)`** — Embed once, insert once. The vector is passed as a stringified Python list (`str(v)`) — pgvector parses it from the SQL literal. Importance is the LLM-scored salience.

**`recall(user_id, query, k=5)`** — The interesting one. The SQL:

```sql
SELECT summary, importance, embedding <=> :q AS dist
FROM episodes WHERE user_id = :u
ORDER BY (embedding <=> :q) - importance*0.1
LIMIT :k
```

Three things to notice:

1. **Filter by `user_id` first.** This is enforced by Postgres using the B-tree index, before HNSW runs. Episodes are *per-user* — you never recall someone else's memory.
2. **`ORDER BY (distance) − (importance × 0.1)`.** A perfect-match low-importance memory has distance 0.0, score = 0.0 − 0.05 = −0.05. A near-match high-importance memory might have distance 0.15, score = 0.15 − 0.10 = 0.05. The first one still wins, but the second one can beat a slightly better match if its importance is high enough. Tune the `0.1` weight to taste.
3. **`LIMIT :k`** — typically k=3 or k=5. You want a small number; the system prompt has finite room.

**`log_tool_call(...)`** — One INSERT into `tool_calls`. Always inside the `try/finally` of the actual tool execution so the row gets written even when the tool raises. This is the audit trail; it must never be skipped.

### Parameterized SQL is non-negotiable

Every method uses SQLAlchemy's `text(":param")` syntax with a dict. Never `f"...{user_input}..."`. Never `.format()`. Never `%` formatting. The reason isn't style — it's that `summary` and `tool_call` arguments will eventually carry user-controlled content, and the only way to be safe by construction is to never let user input touch the SQL string at all.

### Testability

Because the engine and embed function are injected, you can test the entire `AgentMemory` class against an in-memory SQLite + a stub embedder that returns deterministic vectors. Module 6 lab includes exactly this test in the verification step.

> **🎯 Checkpoint for 6.3**
> Without looking, name the six methods on `AgentMemory` and one reason each exists. A passing answer covers: `start_session` (creates row), `add_message` (logs turn), `recent_messages` (history for prompt), `remember` (writes episode), `recall` (vector + importance ranking), `log_tool_call` (audit). If you can also explain why `recall` ranks by `distance − importance × 0.1`, you've internalized the design.

---

## 6.4 — The Agent Loop: Where Everything Composes

### The control flow, in five steps

The agent loop is the place where short-term, long-term, semantic, and procedural memory all meet. Every turn looks the same:

1. **Recall episodes** — `mem.recall(user_id, user_msg, k=3)` → top-3 long-term memories
2. **Build the message stack** — system prompt (with recalled episodes injected) + recent history + new user message
3. **Call the LLM** — with tools defined; LLM may emit a function call
4. **Execute tools, log them** — for each tool call: run, capture result + duration + ok, write to `tool_calls`, append a `tool` message to `messages`
5. **Write the assistant's reply** — `mem.add_message(sid, 'assistant', reply)`

That's the whole loop. Everything else is plumbing.

### Where each memory layer enters

This is the diagram that makes the four layers click:

- **Short-term** enters at step 2 — `recent_messages(sid, n=10)` is the working memory of the conversation.
- **Long-term episodic** enters at step 1 — recalled summaries are *prepended to the system prompt*, so the LLM treats them as facts it knows.
- **Semantic** enters at step 4 (sometimes step 3) — when the LLM decides to call the `retrieve` tool, that hits the Module 5 `documents` table via the same `rag()` function you already built.
- **Procedural** enters at step 4 — every tool call gets logged, success or failure, with duration.

If you understand where each of those four arrows lands in this loop, you understand the entire architecture of Module 6.

### The three tools the lab agent ships with

The lab gives the agent three tools, each with an explicit JSON schema:

- **`retrieve(query)`** — hybrid search over the `documents` table (Module 5). This is *semantic* memory access.
- **`get_orders(email)`** — operational lookup against the `orders` table (Module 3). This is the operational app data.
- **`recall(query)`** — vector search over `episodes`. The agent can explicitly fetch its own past memories when the LLM decides it needs to.

Three tools is the right size: it's enough to demonstrate composition, small enough that you can read every code path. Real production agents end up with 10–50 tools; the pattern doesn't change, just the registry.

### Why the recalled episodes go in the system prompt, not as a tool

This is a design choice worth understanding. The recalled episodes *could* be exposed via the `recall` tool and the LLM could decide whether to call it. Some frameworks do this. The lab does it differently — episodes are pre-fetched and injected into the system prompt every turn. The reason:

- **Lower latency** — one fewer round-trip per turn
- **More reliable** — the LLM doesn't get to "forget" to recall its own memory
- **Cheaper** — you only embed the user message once, regardless of whether the LLM would have asked

The trade-off: you fetch episodes even on turns that don't need them. At k=3, this is cheap. The `recall` tool still exists as an *escape hatch* — when the user explicitly asks "what do you remember about me?" the LLM can call it directly with a custom query.

### End-of-session distillation

When the session ends — user closes the tab, an idle timeout fires, or the agent itself decides — one final action: ask the LLM to summarize the session in three sentences, score it for importance, embed it, write to `episodes`. That's what makes the *next* session memory-aware.

The summary prompt is short and explicit:

```
Summarize this user's conversation in 3 sentences.
Focus on stated preferences, durable facts, and decisions made.
Skip pleasantries and chitchat.
Then on a new line, write IMPORTANCE: 0.0-1.0
based on how useful this is for future sessions.
```

You parse the response, embed the summary, write the row. Done. That row is now the long-term memory the agent will recall in the user's next session.

> **🎯 Checkpoint for 6.4**
> Without looking, walk through the five steps of the agent loop and name which memory layer enters at each step. A passing answer: step 1 long-term, step 2 short-term, step 4 semantic + procedural. If you can also explain why recalled episodes go in the system prompt rather than behind a tool, you've got the design choice.

---

## 6.5 — Episode Distillation: Nightly Memory Compression

### The problem distillation solves

Without distillation, your `messages` table grows linearly with every turn forever. After a year of one user chatting daily, you have ~10,000 rows of mostly-noise — pleasantries, restated context, abandoned threads. None of that is useful for the *next* conversation. What's useful is the *summary*: "this user prefers concise email replies, works on Snowflake migrations, runs Postgres 17."

Distillation is the nightly compaction job that produces those summaries.

### The pattern

A Databricks Workflow runs on a schedule — typically nightly, can be hourly for high-volume agents. It does this:

1. **Find candidate sessions** — `SELECT id, user_id FROM sessions WHERE ended_at IS NULL AND created_at < now() - interval '24 hours'` (sessions that died without an explicit end). Mark them ended.
2. **For each session** — read all messages, send to LLM with the summarization prompt
3. **Parse summary + importance** — get back 3-sentence summary and a 0.0–1.0 score
4. **Embed the summary** — same Foundation Models call, same 1024-dim model
5. **Write the episode** — INSERT into `episodes`
6. **Optional: prune messages** — for sessions older than, say, 90 days you can DELETE the message rows; the episode preserves the durable signal

That's the entire job. Forty lines of Python in a Workflow. Idempotent — re-running it just produces another episode (or you add a unique constraint on `(user_id, session_id)` to prevent duplicates).

### What "importance" should capture

The LLM's importance score is a soft signal. A good prompt makes the model score high for:

- Stated preferences ("I prefer email", "always use metric units")
- Durable facts about the user's environment ("Postgres 17", "team of 8")
- Decisions made ("we chose pgvector over Pinecone")

And score low for:

- One-off questions with answers that are public knowledge
- Pleasantries
- Resolved confusions

Don't over-engineer this. A simple 0.0–1.0 from a well-prompted LLM is plenty for the recall ranking to work. You can revisit this with a fine-tuned scorer later if recall quality demands it.

### Pruning policy

A sensible default policy:

- Episodes with **importance ≥ 0.7** — keep forever
- Episodes with **importance 0.3–0.7** — keep 180 days
- Episodes with **importance < 0.3** — keep 30 days, then delete
- Optionally: re-embed surviving episodes once a year if you upgrade the embedding model

The numbers matter less than having a policy. Without one, `episodes` grows forever and `recall` slowly degrades as more low-importance noise accumulates near the top of the HNSW graph.

### This is the same pattern ChatGPT uses

The phrase "ChatGPT remembers things across conversations" describes exactly this pattern: a background process distills sessions into summaries, embeds them, retrieves them at the start of new conversations, injects them into the system prompt. LangGraph's checkpoint summarization is structurally identical. You're not inventing this — you're implementing the published pattern on Lakebase.

> **🎯 Checkpoint for 6.5**
> Without looking, describe the inputs, outputs, and frequency of the distillation job. A passing answer: input = ended sessions in the last 24h, output = one episode row per session, frequency = nightly. If you can also explain the importance-based pruning policy, you've got the operational picture.

---

## 6.6 — Wiring into Mosaic AI Agent Framework

### Where Lakebase fits in the framework

Mosaic AI Agent Framework (the Databricks-native agent build/serve/eval stack on top of MLflow) ships with the concept of a **checkpoint store** — a place to persist the state of a multi-step agent so it can survive restarts, scale events, and asynchronous tool calls. The framework supports pluggable persistence; Lakebase becomes that store.

In practical terms:

- **LangGraph state** (the node graph, current node, accumulated tool plans) → persisted as JSONB in a `checkpoints` table in the same Lakebase
- **MLflow tracing** → spans tagged with `session_id` so you can join traces with messages and tool calls
- **Agent versioning** → the `agent_name` and a version number live on the session row, so you can A/B compare two agent revisions on the same memory schema

The benefit of having all of this in Lakebase rather than a separate state store: every dashboard, every debug query, every retention job runs against the same Postgres, queryable from DBSQL with the same OAuth.

### The query patterns this enables

Once everything lives in one place, certain queries become trivial that would otherwise require a multi-system fan-out:

- **"Show me every tool call that returned an error in the last hour"** — `SELECT * FROM tool_calls WHERE NOT ok AND ts > now() - interval '1 hour'`
- **"What was the agent's checkpointed state when this session crashed?"** — join `checkpoints` to `sessions` on `session_id`
- **"Which users have ever set the preference 'concise replies'?"** — full-text search on `episodes.summary`
- **"What did the agent recall, what did it then call, and what was the final answer?"** — left join `messages` ⨝ `tool_calls` on `(session_id, ts)`

Every one of these is a single SQL statement, one transaction, one ACL. No cross-service tracing required.

### MLflow tracing alignment

If you're using MLflow Tracing (which the Mosaic AI Agent Framework ships with), the natural pattern is:

- Span attribute `session_id` = the Lakebase `sessions.id`
- Span attribute `user_id` = same as the row
- Span attribute `tool_call_id` = the row ID in `tool_calls` for cross-correlation

Now an MLflow trace and a Lakebase row are two views of the same fact. Debugging an agent failure becomes: read the trace, jump to the SQL, run a follow-up query against `tool_calls` to find similar failures.

### The "no opaque pickle blob" benefit

A common alternative for agent state is "pickle the LangGraph state object and stuff it in S3." That works until you need to query across sessions, or read state from a tool that doesn't speak Python, or audit what state existed last Tuesday. Storing checkpoints as **JSONB rows in Lakebase** keeps them queryable, indexable, retentionable, and accessible from DBSQL — at the cost of a little serialization care (which the framework handles).

> **🎯 Checkpoint for 6.6**
> Without looking, name two query patterns that get easier when agent state, conversation history, and knowledge all live in one Lakebase. A passing answer: any cross-layer join (e.g., "messages plus their tool calls"), or any audit query (e.g., "every error in the last hour"). If you can also state why JSONB checkpoints beat pickle blobs, that's the production-readiness answer.

---

## Module 6 wrap-up

You should now be able to handle the first thirty minutes of any "how do we do agent memory on Databricks?" customer conversation:

- Name and explain the four memory layers — short-term, long-term episodic, semantic, procedural — and why all four belong in one Postgres (6.1)
- Design the four-table schema with the right indexes, foreign keys, and column types (6.2)
- Write the `AgentMemory` contract and explain why every method is parameterized (6.3)
- Walk the agent loop and identify which memory layer enters at each step (6.4)
- Describe the nightly distillation job and an importance-based pruning policy (6.5)
- Wire the schema into the Mosaic AI Agent Framework as a checkpoint store with MLflow trace alignment (6.6)

The Module 6 lab notebook (the hands-on part) puts every one of these in your hands. You'll add the four tables to your Module 5 project, implement the `AgentMemory` class, write the three tools, build the agent loop, and prove cross-session memory works end-to-end with the "I prefer email replies" demo.

### Common pitfalls to remember

- **Mixing embedding models between `documents` and `episodes`.** Both must use the same model and dimension. Cosine distances are not comparable across models.
- **Forgetting `ON DELETE CASCADE` on `messages`.** Deleting a session leaves orphaned message rows. GDPR auditors will find them.
- **String-formatting SQL.** Every parameter goes through `text(":x")` + dict. No exceptions.
- **Logging tool calls only on success.** The `tool_calls` row must be written in a `try/finally` so failures are recorded too. The audit trail is most useful when something went wrong.
- **Skipping importance scoring.** Without a 0.0–1.0 score, recall ranks purely by cosine distance and high-signal but slightly-off-topic memories get buried.
- **Letting `episodes` grow unbounded.** Pick a pruning policy at week one, not month six.

**Next up: the Module 6 lab.** You'll build the four-table schema on top of your Module 5 project, implement the `AgentMemory` class, define three tools with explicit JSON schemas, run the agent loop, and watch the cross-session memory demo work — set "I prefer email" in session 1, see the agent honor it in session 2.
