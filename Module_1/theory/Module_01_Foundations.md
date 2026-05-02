# Module 01 — Foundations & the Lakebase Mental Model

> **Level:** Beginner · **Duration:** ~2 hours · **Format:** Theory primer
> **Coverage:** Topics 1.1, 1.2, 1.3 — the conceptual scaffolding for everything that follows.

---

## What this module gives you

By the end of these three topics, you should be able to walk into any customer conversation and answer three questions without hesitation:

1. **What is Lakebase, and why did Databricks build it?** (1.1)
2. **What are the moving parts I'd point to on a whiteboard?** (1.2)
3. **Autoscaling or Provisioned — which one am I recommending today?** (1.3)

Everything in Modules 2 through 9 — architecture deep dives, hands-on labs, the capstone, the RSA toolkit — assumes you have the mental model built here. Don't skim it.

---

## 1.1 — Why Operational Databases Belong in the Lakehouse

### The historical split

For roughly thirty years, the database world has been divided into two tribes that almost never talk to each other:

| Dimension | OLTP (Online Transaction Processing) | OLAP (Online Analytical Processing) |
|---|---|---|
| **Workload shape** | Many small reads/writes per second | Few large scans per minute |
| **Latency target** | Single-digit milliseconds | Seconds to minutes |
| **Concurrency** | Thousands of users hitting one row each | Tens of analysts scanning whole tables |
| **Data layout** | Row-oriented | Columnar |
| **Typical engines** | Postgres, MySQL, Aurora, RDS, DynamoDB | Snowflake, BigQuery, Redshift, **Delta Lake** |
| **What it powers** | Apps, agents, transactions, user state | Dashboards, ML training, reporting |

The Databricks Lakehouse — Delta + Spark + Photon + Unity Catalog — is exceptional at OLAP. It is, by design, not an OLTP engine. So every customer building a real product on Databricks ended up with the same story: "We do all our analytics here, but our app/agent/feature-store actually lives over there in Aurora."

### The seam tax

That "over there" is not free. Once you have two databases, you pay what we call the **seam tax**:

- **Reverse-ETL pipelines** — Fivetran, Hightouch, Census, or hand-written Spark jobs to push data from Delta into Postgres so the app can read it
- **Separate IAM** — workspace OAuth on one side, AWS IAM or Postgres roles on the other
- **Separate audit logs** — compliance and security teams have to correlate events across two systems
- **Separate billing** — Databricks DBUs plus an Aurora invoice plus a Fivetran subscription
- **Separate SLAs** — when something breaks, you triage two stacks
- **Drift** — the schema in Postgres slowly diverges from the Delta source it was supposed to mirror

In our customer reviews, this seam was almost always the largest source of operational pain — and the largest hidden cost line on the data platform bill.

### What Lakebase actually replaces

Lakebase is a **fully managed, cloud-native PostgreSQL 17 database that lives natively inside the Databricks Data Intelligence Platform**. Read the sentence twice. The two important phrases are:

- **"PostgreSQL"** — not a Postgres-flavored API, not a wire-compatible reimplementation. Real Postgres. Existing drivers, ORMs, extensions (from a curated list), and Postgres knowledge transfer 1:1.
- **"natively inside Databricks"** — same governance plane (Unity Catalog), same identity (workspace OAuth), same data plane (synced tables, Lakehouse Sync, federation), same bill.

The thing it eliminates is not Postgres. It's the seam.

When a customer adopts Lakebase, what disappears from their architecture diagram is roughly:

- The reverse-ETL tool (Hightouch / Census / custom)
- The sidecar Aurora/RDS instance for app data (in many cases)
- The third-party vector database (Pinecone, Weaviate) for sub-10M corpora
- The bespoke agent state store (Redis, DynamoDB) for memory layers
- The "let's stand up a small Postgres on EKS for this app" recurring decision

What replaces all of those is one Lakebase project, registered in Unity Catalog.

### The five killer use cases

These are the conversations we have with customers most often. If you only memorize five things from this module, memorize these:

1. **Operational app backends** — internal admin tools, CRUD apps, customer 360 lookups, approval workflows. Backed by Databricks Apps + Lakebase.
2. **Real-time feature serving** — sub-10ms feature lookups for online ML inference, replacing Redis or a sidecar Postgres.
3. **AI agent state and memory** — durable conversation history, tool-call logs, vector-retrievable episodic memories. (Module 6 in depth.)
4. **Vector search at app latency** — pgvector + HNSW for production RAG, without bolting on a third-party vector DB. Best fit under ~10M vectors.
5. **Reverse-ETL replacement** — synced tables push UC Delta data into Postgres for low-latency reads, eliminating Hightouch/Census/Fivetran style pipelines.

> **🎯 Checkpoint for 1.1**
> Can you state, in one sentence, what Lakebase replaces in a typical customer's stack? A passing answer mentions: *a managed Postgres + a reverse-ETL tool, plus often a vector DB and an agent state store.* If you can say that without looking, move on.

---

## 1.2 — The Lakebase Stack: The Full Map

Lakebase is not one thing — it's a small set of objects you'll be referring to constantly. Get the vocabulary right now and every later module reads cleanly.

### The four planes

Think of Lakebase as four stacked layers, each one composed of well-defined objects:

```
┌─────────────────────────────────────────────────────────────┐
│  APPLICATION PLANE                                          │
│  Databricks Apps · Notebooks · Feature Store · AI Agents   │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│  OPERATIONAL PLANE                                          │
│  SQL Editor · Tables Editor · psql/JDBC · PostgREST API    │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│  INTEGRATION PLANE                                          │
│  Unity Catalog · Synced Tables · Federation · Lakehouse Sync│
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│  CORE OBJECTS                                               │
│  Project · Branch · Compute · Database · Synced Table       │
│  · Read Replica                                             │
└─────────────────────────────────────────────────────────────┘
```

Let's walk each one.

### Core objects — the vocabulary

These are the nouns you'll use every day. Every later module assumes you know them.

#### Project

The top-level container. A **project** owns the storage, the branches, the compute, the networking, and the billing scope. When a customer says *"we have one Lakebase database"*, they almost certainly mean *"we have one Lakebase project."* Provisioning a project is the first hands-on step in Module 3.

A project consists of two physically separated layers:

- **Storage layer** — a multi-tenant page server backed by object storage. Durable, versioned, copy-on-write. This is what makes branching and instant restore possible.
- **Compute layer** — one or more Postgres processes that lazily fetch pages from the storage layer and cache them locally.

The fact that storage and compute are separated is the single most important architectural choice in Lakebase. Module 2 dwells on it; for now, internalize that *every magical capability flows from this separation*.

#### Branch

A **branch** is a named, isolated compute endpoint pointing at a copy-on-write snapshot of project storage. Every project starts with a `main` branch. Creating a new branch takes seconds, not minutes — no data is physically copied, only the pointer.

Branches are how you do:

- **PR-based schema migrations** — branch off `main`, run the migration, test it, merge by promotion
- **Ephemeral CI databases** — branch on PR open, drop on PR close
- **Blue-green deploys** — point production at the new branch only after validation
- **Point-in-time recovery** — create a branch from a timestamp 5 minutes ago to recover a corrupted row
- **"What-if" analysis** — let an analyst loose on a writable copy without risking `main`

If you've used Git, the mental model transfers almost perfectly. If you've used Aurora clones or Neon branches, the mental model transfers exactly.

#### Compute

The Postgres process attached to a branch. Compute is **elastic** — it can autoscale up under load, scale to zero when idle (on dev/preview branches), and survive failover with a synchronous standby in another availability zone (on HA-enabled branches).

Compute is *not* where your data lives. Your data lives in the storage layer. Compute is just the engine that reads pages, processes queries, and writes WAL.

#### Database

Inside a branch's Postgres, you have one or more standard PostgreSQL databases. The default is `databricks_postgres`. This is plain Postgres — schemas, tables, roles, GRANTs all work exactly as you'd expect.

Most customers use one database per project. There's no architectural rule against more, but it adds operational complexity for little gain.

#### Synced Table

A **synced table** is a Lakebase table that is automatically populated from a Unity Catalog Delta source. You declare the source (`main.gold.customers`), the primary key, and a scheduling policy (CONTINUOUS or SNAPSHOT), and Databricks runs a managed CDC pipeline that keeps Lakebase in sync within seconds-to-minutes.

Synced tables are how you replace reverse-ETL. They appear as ordinary Postgres tables to your app, with whatever indexes you add — you query them like any other table.

#### Read Replica

An additional compute node sharing the **same storage** as the primary. Because storage is already replicated at the page-server level, read replicas have zero replication lag for committed pages. The pattern: send writes and hot reads to the primary, route analyst/dashboard load to a replica.

### Integration plane — how it talks to the lakehouse

This is the layer that makes Lakebase a *lakehouse* OLTP rather than just another managed Postgres. Three patterns, covered in depth in Module 4:

| Pattern | Direction | Latency | Typical use |
|---|---|---|---|
| **Synced tables** | UC Delta → Lakebase | Seconds to minutes | Serve features, profiles, catalog data to apps |
| **Lakehouse Sync** | Lakebase → UC Delta | Continuous CDC | Run analytics on operational data, history |
| **Federation** | Live read from DBSQL | Per-query | Ad-hoc joins, low-volume reads, exploration |

**Decision rule for now:** if data flows *from* the lakehouse into the app, use synced tables. If it flows *from* the app *back to* the lakehouse for analytics, use Lakehouse Sync. For occasional ad-hoc joins between Postgres and Delta, federation is fine.

### Operational plane — how humans and code touch it

Multiple ways to read and write Lakebase, all of which work at the same time:

- **SQL Editor / Tables Editor** in the Databricks UI — quick lookups, ad-hoc queries
- **Postgres clients** (psql, DataGrip, JDBC, psycopg, SQLAlchemy) — your existing tools work unchanged
- **PostgREST Data API** (Autoscaling only) — auto-generated REST endpoints for tables, useful for lightweight integrations
- **Notebook SDK** — `WorkspaceClient.database` for provisioning, credential generation, and lifecycle management

### Application plane — what gets built on top

The whole point. Anything that needs row-level reads/writes at app latency, in the same governance plane as your analytics:

- **Databricks Apps** — full-stack data products (Module 7)
- **AI agents** — using Lakebase as the memory + tool-call store (Module 6)
- **Feature Store online layer** — sub-10ms lookups for inference
- **Notebooks** — for data scientists who need transactional state inside their work
- **External apps** — connect over public TLS or Inbound Private Link

> **🎯 Checkpoint for 1.2**
> Without looking, list the six core objects: *project, branch, compute, database, synced table, read replica.* Then explain, for each, what would break if it didn't exist. If "compute" feels redundant with "branch", reread — the separation is what makes scale-to-zero and read replicas possible.

---

## 1.3 — Autoscaling vs Provisioned: What & When

### The short version

As of **March 12, 2026**, every newly created Lakebase instance is provisioned as a **Lakebase Autoscaling** project. The older **Lakebase Provisioned** offering still exists for backward compatibility, but it is in maintenance mode — no new feature work is targeted at it.

**For all new customer designs, recommend Autoscaling.** The exceptions are narrow, and we'll cover them at the end of this section.

### Capability comparison

This is the table you'll show in design reviews:

| Capability | Autoscaling (Default) | Provisioned (Legacy) |
|---|---|---|
| **Compute model** | Auto-scaling, scale-to-zero supported | Fixed-size, manually changed |
| **Branching (copy-on-write)** | ✅ Native, ready in seconds | ❌ Not supported |
| **Instant restore (any point in time)** | ✅ Up to 30-day window | ⚠️ Traditional PITR only |
| **Read replicas** | ✅ True replicas, same storage | ⚠️ "Readable secondaries" |
| **Data API (PostgREST)** | ✅ Generally available | ⚠️ Private Preview |
| **HA across AZs** | ✅ | ✅ |
| **Unity Catalog registration** | ✅ | ✅ |
| **Synced tables** | ✅ | ✅ |
| **Lakehouse Sync (reverse)** | ✅ Beta on AWS | ⚠️ Limited |
| **New feature investment** | ✅ Active | ❌ Maintenance mode |

The honest summary: Autoscaling is a **superset** of Provisioned in capabilities. The four distinguishing features — branching, instant restore, true read replicas, the Data API — only exist on Autoscaling. Everything Provisioned does, Autoscaling does too.

### Why this matters: the architectural difference

The capability gap isn't an arbitrary feature-flag decision. It reflects a fundamentally different storage architecture:

- **Provisioned** runs Postgres with locally-attached storage, replicated at the block level. Branching would mean physically copying terabytes; that's why it's not offered.
- **Autoscaling** runs Postgres against a shared, copy-on-write **page server** backed by object storage. Branching is just a new pointer at the same pages — instant and cheap.

When customers ask *"can't you just add branching to Provisioned?"* — no, not without rebuilding it as Autoscaling.

### When to choose each — the decision rule

**Choose Autoscaling when:**

- Starting a new project (this is essentially always)
- You want branching, instant restore, scale-to-zero, the Data API, or active feature development
- You're in a region where Autoscaling is generally available
- You want the architecture Databricks is investing in long-term

**Choose Provisioned only when:**

- You have an existing Provisioned project and aren't ready to migrate (it remains supported — no forced migration)
- Autoscaling is not yet GA in the customer's required region (verify in the docs before the design call)
- A specific compliance certification you need is on Provisioned but not yet on Autoscaling (rare; check current status)

There is no "Provisioned for predictable cost" answer. Autoscaling supports a fixed-baseline compute mode if you want predictability — and you get branching and instant restore for free.

### Region availability — verify, don't assume

Autoscaling is rolling out region by region. Before you commit to a customer architecture, run:

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
list(w.database.list_database_projects())   # if this works, the region supports it
```

If the customer is in a non-US region, **check region availability in the docs before the design call.** Walking back a recommendation in front of the customer is far worse than discovering the limit privately first. (Module 9 lists this as the #1 thing RSAs forget to verify upfront.)

### Upgrade path

Existing Provisioned instances continue to be supported. There is **no forced migration** to Autoscaling. Customers can:

1. Leave existing Provisioned projects in place
2. Provision new projects as Autoscaling
3. Migrate Provisioned → Autoscaling on their own schedule using a logical-replication / dump-and-load approach (the standard Postgres migration toolkit)

In the field, the most common pattern is *coexistence*: keep the legacy Provisioned project running, build new applications on Autoscaling, migrate the old one only when there's a compelling reason. Don't push migration-for-migration's-sake.

> **🎯 Checkpoint for 1.3**
> Quick decision drill — for each scenario, pick Autoscaling or Provisioned without thinking longer than five seconds:
>
> 1. New customer, brand new use case, US-East region → **Autoscaling**
> 2. Customer has a 2-year-old Provisioned project running fine → **leave it on Provisioned, no forced move**
> 3. Customer wants branching for PR-based migrations → **Autoscaling** (only option)
> 4. Customer in a region where Autoscaling isn't GA yet → **Provisioned** (interim) and flag the limitation
> 5. Customer says "I want predictable monthly cost" → **Autoscaling** with fixed-baseline compute (still gets branching/restore)

---

## Module 1 wrap-up

You should now be able to handle the first three minutes of any Lakebase customer conversation:

- Explain in one sentence what Lakebase is and what it replaces (1.1)
- Whiteboard the four planes and six core objects without notes (1.2)
- Recommend Autoscaling vs Provisioned with a clear, honest rationale (1.3)

The remaining hands-on lab in topic 1.4 (workspace prerequisites and sanity check) is what gets your laptop ready to do every other lab in this roadmap. Run it next, then continue to Module 2 for the architecture deep dive.
