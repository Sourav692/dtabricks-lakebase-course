# Module 08 — Capstone: Build "AskMyOrders" End-to-End
## The Concepts Behind the Build

> **Module Type:** Capstone · Advanced · ~4 hours total (5 phases × ~45 min)
> **Format:** Theoretical walkthrough + end-to-end build
> **Modules Exercised:** All of 1 → 7

---

## What this module gives you

Modules 1 through 7 each delivered one capability. Module 8 is where they **stop being seven separate ideas and start being one product**. You will build "AskMyOrders" — an internal AI-powered customer support app for a fictional retailer, *Northwind Goods* — that exercises every concept from the roadmap in exactly one place.

By the end of this module, you can walk a customer through the complete reference architecture for a Lakebase-backed AI app, point at every component, and say **"I built this. It works. Here's what it cost."** That conviction is what closes design reviews.

> **▸ Prerequisite Check**
> You should have completed **Modules 1 through 7** end-to-end. Specifically: a working Lakebase project from Module 3, a synced table from Module 4, a `pgvector` schema from Module 5, an agent memory layer from Module 6, and the OBO connection pattern from Module 7. The capstone reuses every one of those artefacts.

---

## On this page

1. The Capstone Scenario — *Northwind Goods*
2. The System Architecture — Four Layers, One Stack
3. The Five Phases — How the Build Decomposes
4. The Agent Loop — The One Piece Worth Seeing in Full
5. Definition of Done — The 9 Checkpoints
6. Stretch Goals & Module 8 Wrap-Up

---

## 8.1 — The Capstone Scenario

### The customer in the story

**You are the lead engineer at "Northwind Goods,"** a fictional mid-market retailer running on Databricks. Customer support agents currently jump between five tools every shift:

- A ticketing system
- A customer database
- A returns portal
- A knowledge wiki
- Slack

Leadership wants **one app**: an AI assistant that answers customer questions using internal docs, knows the customer's order history, remembers prior conversations, and lets the agent record resolutions back to the data platform.

### Why this scenario, specifically

This is — almost word-for-word — the scenario customers describe in real RSA conversations. Build it once for yourself; reuse the patterns forever. Every architectural choice maps to a decision you will help a real customer make in the next 90 days.

### What you will produce

By the end of the capstone you will have, in your own workspace:

- A **working Databricks App** at a real URL, signed in via workspace OAuth
- A **Lakebase project** with `main` and `dev` branches
- **Synced UC tables** carrying customer/order data into the app
- A **RAG knowledge base** with HNSW + GIN indexes over support documents
- An **agent loop with memory** demonstrating cross-session recall
- **Lakehouse Sync** streaming app-side resolutions back to Delta
- A **DBSQL dashboard** on top of the Delta resolution stream

---

## 8.2 — The System Architecture

### Four layers, one platform

Every Lakebase-backed AI app you will ever architect with a customer fits this four-layer model. Memorize it. The capstone is one concrete instantiation.

```
┌─────────────────────────────────────────────────────────┐
│  USER LAYER          Support Agent                      │
│                      Workspace OAuth identity           │
└─────────────────────────────────────────────────────────┘
                              │ HTTPS · OBO token
                              ▼
┌─────────────────────────────────────────────────────────┐
│  APP LAYER           Streamlit on Databricks Apps       │
│                      ┌──────────┐  ┌──────────────┐     │
│                      │ Chat UI  │  │ Customer     │     │
│                      │ + tools  │  │ context pane │     │
│                      └──────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────┘
                    │              │              │
              psycopg │ pgvector │ FM API
                    ▼              ▼              ▼
┌─────────────────────────────────────────────────────────┐
│  DATA LAYER          Lakebase (askmyorders project)     │
│                      ┌──────────┐ ┌──────────┐          │
│                      │customers │ │ orders   │ synced   │
│                      │  synced  │ │  synced  │          │
│                      ├──────────┤ ├──────────┤          │
│                      │ kb_docs  │ │ episodes │ pgvector │
│                      ├──────────┤ ├──────────┤          │
│                      │ messages │ │ sessions │          │
│                      ├──────────┴─┴──────────┤          │
│                      │      resolutions      │ writes   │
│                      └───────────────────────┘          │
└─────────────────────────────────────────────────────────┘
                    ▲                            │
            Synced Tables                Lakehouse Sync
            (Module 4)                     (Module 4)
                    │                            ▼
┌─────────────────────────────────────────────────────────┐
│  LAKEHOUSE LAYER     Unity Catalog · Delta              │
│                      ┌─────────────┐  ┌──────────────┐  │
│                      │ main.gold.* │  │ main.silver. │  │
│                      │ customers,  │  │ support_     │  │
│                      │ orders      │  │ resolutions  │  │
│                      └─────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Concept-to-layer map

| Layer | Concept | Where it came from |
|---|---|---|
| User | Workspace OAuth, OBO identity | Module 2.5, Module 7.2 |
| App | Streamlit, per-request engine factory | Module 7.3 |
| App | Agent loop, tool calls | Module 6 |
| Data | Lakebase project, branches | Module 1, Module 3 |
| Data | UC catalog registration | Module 3.4 |
| Data | Synced tables (Delta → Lakebase) | Module 4.2 |
| Data | pgvector + HNSW + GIN | Module 5.2 |
| Data | Memory layers (sessions, episodes) | Module 6 |
| Lakehouse | Lakehouse Sync (Lakebase → Delta) | Module 4.3 |

> **▸ Why this matters**
> Every concept appears in *exactly one place*. That is the test of a good architecture: no duplicated responsibilities, no overlapping stores. If a customer ever asks "where does X live?" — you can answer in one sentence.

---

## 8.3 — The Five Phases

The build decomposes into five phases of roughly 45 minutes each. The phases are deliberately layered: each one assumes the previous is working. **Do not skip ahead** — phase 4's agent will not work without phase 3's RAG, which will not work without phase 2's data, which will not work without phase 1's project.

### Phase progression

```
PHASE 01           PHASE 02           PHASE 03           PHASE 04           PHASE 05
~30 min            ~45 min            ~60 min            ~60 min            ~45 min
─────────          ─────────          ─────────          ─────────          ─────────
Foundation         Hydrate from       Build the          Agent with         Ship as App
& Provisioning     the Lakehouse      Knowledge Base     Memory             Close the loop
                                      (RAG)
─────────          ─────────          ─────────          ─────────          ─────────
M1·M2·M3           M4                 M5                 M6 + M5            M7 + M4-reverse
```

### Phase 1 — Foundation & Provisioning *(~30 min · M1, M2, M3)*

Stand up the project, create branches, register in Unity Catalog, seed the operational schema.

- Create Lakebase project `askmyorders` (CU_1, 7-day retention, HA off for now)
- Branch `dev` off `main` for schema iteration
- Apply DDL: `customers`, `orders`, `returns`, `resolutions`
- Create the `vector` extension and KB/memory tables: `kb_documents`, `sessions`, `messages`, `episodes`, `tool_calls`
- Register Lakebase as catalog `askmyorders_db` in Unity Catalog
- Verify connection from a notebook using OAuth credential generation

> **What this proves:** you can provision a production-shaped Lakebase project from scratch in under 30 minutes with full UC governance — the gate for everything else.

### Phase 2 — Hydrate from the Lakehouse *(~45 min · M4)*

Customers and orders already live in `main.gold.*` Delta tables. Don't duplicate the pipeline — sync them into Lakebase so the app reads at app latency without reverse-ETL.

- Create synced table `customers_synced` from `main.gold.customers` (CONTINUOUS, PK = `customer_id`)
- Create synced table `orders_synced` from `main.gold.orders` (CONTINUOUS, PK = `order_id`, timeseries = `placed_at`)
- Verify a write to the source Delta appears in Lakebase within seconds
- Add B-tree index on `orders_synced(customer_id)` and BRIN on `placed_at`
- Test row-level latency: a SELECT for one customer's last 10 orders should return in under 30 ms

> **What this proves:** Lakebase reads at app latency from data the lakehouse already owns — the killer pattern of the lakehouse-native OLTP.

### Phase 3 — Build the Knowledge Base (RAG) *(~60 min · M5)*

Load the company's support docs (returns policy, shipping FAQ, warranty terms) into `kb_documents` with embeddings. Build hybrid retrieval and a thin `retrieve()` tool function.

- Chunk source docs into ~500-token segments with 50-token overlap
- Generate embeddings with `databricks-bge-large-en` via Foundation Models API
- Bulk-load into `kb_documents` (one transaction per 1000 rows)
- Create HNSW index `(m=16, ef_construction=64)` on the embedding column
- Create GIN index on the generated `tsvector` for hybrid retrieval
- Implement `retrieve(query, k=5)` using Reciprocal Rank Fusion (vector + BM25)
- Smoke test: *"What's the return window for opened electronics?"* should retrieve the right policy chunk

> **What this proves:** Lakebase is a real vector store, not a toy — hybrid retrieval, governed indexes, callable as a tool.

### Phase 4 — The Agent with Memory *(~60 min · M6 + M5)*

Wire the four-layer memory model into a working agent loop with three tools.

- Implement `AgentMemory` against the project schema (Module 6 class)
- Define tool functions with explicit JSON schemas: `retrieve`, `get_orders`, `recall`
- Build the agent loop: system prompt → recall episodes → retrieve docs → LLM → tool calls → response → log message
- On `end_session`: ask the LLM for a 3-sentence summary, embed, write as an episode
- Cross-session memory test: set "I prefer email replies" in session 1 → verify recall in session 2

> **What this proves:** durable cross-session memory, tool use, and grounded retrieval — all backed by one Postgres.

### Phase 5 — Ship as App + Close the Loop *(~45 min · M7 + M4-reverse)*

Wrap the agent in a Streamlit UI, deploy as a Databricks App, and configure Lakehouse Sync for analytics.

- Streamlit UI: chat panel (left) + customer context panel (right, live from synced tables)
- Use the per-request OBO engine pattern from Module 7
- "Save resolution" form writes to the `resolutions` table
- `app.yaml` binds the `askmyorders` Lakebase project
- Deploy: `databricks apps deploy` — verify identity flows correctly (try as two different users)
- Configure Lakehouse Sync: `resolutions` → `main.silver.support_resolutions`
- Build a DBSQL dashboard: count by day, top resolution categories, average resolution time

> **What this proves:** end-to-end loop closed — operational data flows in, app-generated data flows out, analytics see both.

### Reference: project directory layout

```
askmyorders/
├── notebooks/
│   ├── 01_provision.py        # Phase 1: project + branches + DDL
│   ├── 02_sync_tables.py      # Phase 2: synced tables + indexes
│   ├── 03_build_kb.py         # Phase 3: chunk, embed, index
│   └── 04_agent_smoke_test.py # Phase 4: agent loop validation
├── app/
│   ├── app.py                 # Phase 5: Streamlit entrypoint
│   ├── agent.py               # agent loop + tool definitions
│   ├── memory.py              # AgentMemory class
│   ├── retrieve.py            # hybrid RAG function
│   ├── db.py                  # per-request OBO engine factory
│   ├── requirements.txt
│   └── app.yaml               # Databricks App manifest
├── sql/
│   ├── schema.sql             # operational + memory + KB DDL
│   └── lakehouse_sync.sql     # resolutions → Delta
└── README.md
```

---

## 8.4 — The Agent Loop

The five phases mostly reuse code patterns from earlier modules. The one piece worth seeing in full is the agent loop — because it is where memory, retrieval, tool use, and the LLM compose into a single function.

### What `run_turn` does, conceptually

For every user message, the agent runs through five steps in order:

1. **Recall** — pull the top-3 long-term episodes for this user, semantically related to their question.
2. **Build context** — system prompt + recalled episodes + last 10 messages of this session + the new user message.
3. **Loop** — call the LLM. If it returns tool calls, execute them, append the results, and call again. Cap at 4 iterations.
4. **Persist** — log the user message, the assistant message, and every tool call into the memory schema.
5. **Return** — final assistant response to the UI.

### The skeleton (the part you write)

```python
def run_turn(engine, embed_fn, llm, sid, user_id, user_msg):
    mem = AgentMemory(engine, embed_fn)

    # 1. Recall long-term episodes
    episodes = mem.recall(user_id, user_msg, k=3)
    epi = "\n".join(f"- {e.summary}" for e in episodes)

    # 2. Build messages from history + system prompt
    history = mem.recent_messages(sid, n=10)
    messages = [
        {"role": "system",
         "content": f"You are a Northwind support assistant.\nKnown:\n{epi}"},
        *[{"role": m.role, "content": m.content} for m in history],
        {"role": "user", "content": user_msg},
    ]
    mem.add_message(sid, "user", user_msg)

    # 3. Tool-using loop (max 4 iterations)
    for _ in range(4):
        out = llm.chat.completions.create(
            model="databricks-meta-llama-3-3-70b-instruct",
            messages=messages, tools=TOOLS, temperature=0.1,
        )
        msg = out.choices[0].message
        if not msg.tool_calls:
            mem.add_message(sid, "assistant", msg.content)
            return msg.content
        # ... handle tool calls, log, append to messages ...
```

### The three tools, declared as JSON schemas

| Tool name | Backed by | Purpose |
|---|---|---|
| `retrieve` | pgvector + GIN (Phase 3) | Search the support knowledge base |
| `get_orders` | `orders_synced` (Phase 2) | Look up a customer's recent orders |
| `recall` | `episodes` table (Phase 4) | Search long-term agent memory |

> **Key insight:** all three tools hit *the same Postgres*. Customer would otherwise have stitched together a vector DB, an OLTP, and a memory store from three vendors. Lakebase collapses that into one connection string.

---

## 8.5 — Definition of Done

The capstone is complete when **all nine** of the following are true. Treat this as your acceptance test — you can demo to a customer once every box is ticked.

| # | Checkpoint |
|---|---|
| 1 | ✓ Project provisioned with both `main` and at least one feature branch demonstrated |
| 2 | ✓ UC catalog registered — Lakebase tables visible from DBSQL and Catalog Explorer |
| 3 | ✓ Two synced tables live — writing to Delta source propagates within ~10s |
| 4 | ✓ Knowledge base loaded — at least 200 chunks indexed; hybrid retrieval works on 3 test queries |
| 5 | ✓ Agent demonstrates cross-session memory — preference set in session 1 recalled in session 2 |
| 6 | ✓ Agent uses both tools — at least one turn invokes `retrieve` AND `get_orders` |
| 7 | ✓ App deployed — accessible via App URL; two different users see different data via OBO |
| 8 | ✓ Lakehouse Sync running — resolutions appear in `main.silver.support_resolutions` |
| 9 | ✓ DBSQL dashboard built with at least three tiles on the Delta resolutions table |

> **▸ The 9-checkpoint test is non-negotiable**
> Don't claim to a customer you've built the reference architecture if any of these is missing. Each one validates a different module's promise — gaps will surface in the customer's PoC, not yours.

---

## 8.6 — Stretch Goals

For the truly committed. Each one extends the build with a real-world hardening step you will see customers ask for.

- **HA on production primary** — simulate a failover and confirm the app reconnects transparently
- **Read replica** — route customer-context panel reads to the replica; writes stay on primary
- **Point-in-time restore** — corrupt a row deliberately, branch from 5 minutes ago, verify recovery
- **Inbound Private Link** — if your workspace is in a VPC, measure latency vs. public endpoint
- **MLflow Tracing** — wire spans around `run_turn` to capture every agent trajectory for offline evaluation
- **Row-level security** — add RLS on `orders_synced` so agents only see customers in their assigned region

---

## Module 8 wrap-up — what changes for you after this

You now have:

- **A working AI-powered support app**, deployed on Databricks Apps, backed by Lakebase as the operational + memory + vector store, with Delta sources and analytics targets.
- **Hands-on conviction.** You can lead a customer through the same architecture without bluffing — you've personally exercised every capability: branching, sync, RAG, memory, OBO, Lakehouse Sync.
- **A reusable reference build.** Every customer scenario that looks remotely like "an AI app over our data" now starts with you saying *"I built one of these last week, here's the repo."*

### Common pitfalls to watch for

The capstone is also the place where the pitfalls from earlier modules compound. Be alert for:

- **Forgetting `CLEANUP=False`** at the end of earlier labs — you'll have re-provisioned three times by Phase 3.
- **A global SQLAlchemy engine in the App** — the connection-pooling trap from Module 7. Two users, one identity, security incident.
- **Synced table without a primary key** on the source — the pipeline will refuse to start. (Module 4.2.)
- **HNSW index built before bulk insert** — slow. Build the index *after* the load, with `SET maintenance_work_mem` raised. (Module 5.2.)
- **Skipping the cross-session memory test** — without it, you can't prove memory persists, and "memory" was the headline feature.

> **▸ Module 8 — complete**
> You finished all five phases and validated against the 9-checkpoint Definition of Done. **Next up: Module 9** — the RSA Toolkit, where you turn the build into the conversation.
