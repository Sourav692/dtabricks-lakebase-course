# Module 07 — Powering Databricks Apps with Lakebase: Concepts Behind the Lab

> **Level:** Advanced · **Duration:** ~75 min (theory) + ~3.5h (hands-on lab) · **Format:** Conceptual primer
> **Coverage:** Topics 7.1 → 7.4 — the *why* behind every line of app code you'll write in the Module 7 lab.

---

## What this module gives you

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 put a real Lakebase project in your hands. Module 4 wired it to the lakehouse. Modules 5 and 6 layered pgvector and agent memory onto your schema. **Module 7 is where Lakebase becomes a full-stack data product** — bound to a Databricks App, authenticated as the end user, with row-level security enforced for free.

Before you open the notebook, you need a clean mental model of four conceptual jumps:

1. **Why Apps + Lakebase is different** — what the integration actually eliminates from a typical "app + Postgres" stack
2. **The OBO runtime contract** — how `PGHOST`, `PGUSER`, and `DATABRICKS_TOKEN` are injected into your container, scoped to the end user
3. **The connection-pool trap** — why the naive "global SQLAlchemy engine" pattern is the #1 security incident in real-world Apps deployments, and the two correct patterns
4. **Common app patterns in the field** — the four shapes most Lakebase-backed apps fall into

This module covers the theory. The Module 7 lab notebook walks you through running it — writing the `app.yaml` manifest, building the per-request engine factory, deploying the Streamlit app, and verifying that two different users get two different views of the data.

> **Prerequisite check.** You should have completed **Module 3 Lab 3** with a working Lakebase project registered in Unity Catalog, and **ideally Modules 5 and 6** so the schema you'll query has documents and agent-memory tables in it. Module 7 does not provision new infrastructure — it puts the project from Module 3 behind a real app.

---

## 7.1 — Why Apps + Lakebase Is Different from "App + Some Postgres"

### The seam tax, reconsidered

In Module 1 we introduced the **seam tax** — separate IAM, separate audit, separate billing, separate SLAs across OLTP and OLAP. Module 7 is where you see the same idea play out at the application tier. Before Apps + Lakebase, shipping a "data app" on Databricks meant a five-step ritual:

1. Deploy a container on EKS / GKE / AKS
2. Provision a sidecar Postgres (RDS, Cloud SQL, Aurora) and configure VPC peering
3. Set up SSO separately — typically duplicating the workspace identity provider
4. Manage application secrets (DB passwords, API tokens) in a secrets manager
5. Convince security review that the audit trail is consistent across all of the above

Each step is solvable in isolation. Together, they take a quarter of engineering time and produce an app that's *adjacent to* the lakehouse, not part of it. **Databricks Apps + Lakebase collapses all five steps into one command.**

```
databricks apps deploy --source-dir ./app my-orders-app
```

The runtime injects Lakebase credentials at startup. Identity is the workspace user — the same identity that already governs UC catalogs, Delta tables, and serving endpoints. Every interaction shows up in the same audit logs as the rest of your Databricks usage. One bill, one identity plane, one governance plane.

### What you actually get when you bind a Lakebase project to an App

The app manifest declares a `database` resource and a permission. That declaration produces three runtime guarantees:

- **Network path is solved.** No VPC peering. Your container reaches Lakebase over the same fabric the workspace uses.
- **Auth is solved.** No DB password. The runtime issues a workspace OAuth token scoped to the requesting user and injects it into your container's environment.
- **Governance is solved.** The same `GRANT` you wrote in Module 3 (`GRANT SELECT ON TABLE orders TO "alice@company.com"`) applies to the app connection, because the app connects *as alice*, not as a service account.

This last point is the one that matters most. We'll spend topic 7.2 unpacking it.

### The use cases that drive the demand

In customer conversations, four shapes account for almost every "Apps + Lakebase" build:

- **Internal admin tools** — replacing Retool, Hightouch UI, or homegrown Flask dashboards. Already governed by UC, no third-party SaaS bill.
- **AI chat-over-data** — Apps + Lakebase agent memory (Module 6) + Foundation Models = ChatGPT-for-your-data without OpenAI keys.
- **Approval workflows** — orders or expenses sit in Lakebase, the app provides the UI, a Job promotes the row to a Delta silver layer on approval.
- **Customer 360 lookups** — synced tables (Module 4) feed Lakebase from UC, the app gives sub-100ms search across millions of customers.

If your customer's request fits one of these four, the answer is almost always Apps + Lakebase. If it doesn't fit, ask why before reaching for it.

> **🎯 Checkpoint for 7.1**
> Without looking, list the three runtime guarantees you get from binding a Lakebase project to an App. A passing answer touches: network path, auth (OBO), and governance (GRANTs apply per user). If you can also name two of the four common patterns, you're ready for 7.2.

---

## 7.2 — The OBO Runtime Contract: On-Behalf-Of Auth

### Three environment variables and one big idea

When the Apps runtime starts your container with a Lakebase binding, it injects four environment variables. Three of them are the contract:

| Variable | Value | Lifetime |
|---|---|---|
| `PGHOST` | The read-write DNS for the bound project | Stable for the life of the app |
| `PGUSER` | The end user's workspace identity (e.g., `alice@company.com`) | Per request |
| `DATABRICKS_TOKEN` | A short-lived OBO token scoped to the end user | Per request, ~1h TTL |
| `PGDATABASE` | Database name (typically `databricks_postgres`) | Stable |

The fourth, `PGDATABASE`, is just configuration. The first three are the killer detail: **the database connection is opened as the end user, not as the app.** When alice loads your Streamlit page, her HTTP request arrives with credentials that the runtime turns into a Postgres connection authenticated as `alice@company.com`. When bob loads the same page, his connection comes in as `bob@company.com`.

This is **on-behalf-of (OBO)** auth. It's the architectural inversion of every traditional web app, where a service account talks to the DB and the app code itself enforces who can see what.

### Why OBO matters: GRANTs and RLS just work

In a traditional Postgres-backed app, you write code like this:

```python
# Traditional pattern — app enforces authorization
@app.route("/orders")
def my_orders():
    user = get_current_user_from_session()
    rows = db.execute(
        "SELECT * FROM orders WHERE user_email = %s",
        (user.email,)
    )
    return render(rows)
```

Notice the `WHERE user_email = %s` filter. **The app is the only thing standing between alice and bob's orders.** Any bug in `get_current_user_from_session()`, any missed filter, any clever SQL injection, and the wall comes down.

With OBO, the same endpoint looks like this:

```python
# OBO pattern — Postgres enforces authorization
@app.route("/orders")
def my_orders():
    rows = db.execute("SELECT * FROM orders")  # no WHERE clause!
    return render(rows)
```

The `WHERE` clause is gone — but the result is still correct, because the Postgres connection itself is authenticated as alice, and the table has a row-level security policy:

```sql
CREATE POLICY orders_self ON orders
  USING (user_email = current_user);

ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
```

When alice's connection runs `SELECT * FROM orders`, Postgres rewrites the query to add `WHERE user_email = current_user`, where `current_user` resolves to `alice@company.com`. Bob gets *his* rows, alice gets *her* rows, the app never sees a foreign row at all.

This is what people mean by "secure by default." There is no shared service-account secret to leak. There is no per-endpoint authorization filter to forget.

### The lifecycle, end-to-end

When alice loads your app:

1. The browser sends an authenticated request to the Apps runtime
2. The runtime mints an OBO token scoped to alice
3. The runtime sets `PGUSER=alice@company.com` and `DATABRICKS_TOKEN=<obo-token>` in the request handler's environment
4. Your code reads those env vars and opens a Postgres connection
5. Postgres authenticates the connection against the workspace identity provider
6. The connection runs as alice — RLS, GRANTs, and `current_user` all reflect that
7. When the request ends, the connection is closed (or returned to a per-user pool)

Step 7 is where most teams get into trouble. We'll get there in 7.3.

### What the manifest looks like

`app.yaml` is the only place you bind the Lakebase project. There are no secrets in this file — everything is identity-driven:

```yaml
command: ["streamlit", "run", "app.py"]

env:
  - name: PGHOST
    valueFrom: lakebase-tutorial      # name of the bound resource
  - name: PGDATABASE
    value: databricks_postgres

resources:
  - name: lakebase-tutorial
    description: "Operational store"
    database:
      instance_name: lakebase-tutorial
      permission: CAN_USE
```

The `valueFrom: lakebase-tutorial` tells the runtime: "fill `PGHOST` with the read-write DNS of the resource named lakebase-tutorial." The `permission: CAN_USE` is a UC permission — it gates whether the app can be deployed against this project at all. Per-user authorization happens at the row level, in Postgres.

> **🎯 Checkpoint for 7.2**
> If a customer asks "where do we store the database password?" — what's your answer? A passing answer: there is no password. The runtime injects a short-lived OAuth token scoped to the end user; Postgres authenticates against the workspace identity. GRANTs and RLS run against the actual workspace user. If you can also explain why this makes RLS policies "free," you've internalized OBO.

---

## 7.3 — The Connection-Pool Trap (and the Two Correct Patterns)

### The naive pattern — and why it's catastrophic

Here is the code that ships in 80% of first-attempt Lakebase apps. It looks clean. It runs fine in dev. It is a security incident waiting to happen.

```python
# ❌ THE TRAP — global engine, shared across all users
import os, streamlit as st
from sqlalchemy import create_engine

# This runs ONCE when the container starts
engine = create_engine(
    f"postgresql+psycopg://{os.environ['PGUSER']}:"
    f"{os.environ['DATABRICKS_TOKEN']}@{os.environ['PGHOST']}"
    f":5432/{os.environ['PGDATABASE']}?sslmode=require"
)

@st.cache_resource
def get_engine():
    return engine
```

Read it carefully. The engine is created **once, at container startup**, using whatever values of `PGUSER` and `DATABRICKS_TOKEN` were in the environment at that moment — typically the *first user* who hit the app. Every subsequent user reuses that engine. **Every subsequent user's queries run as the first user.**

If alice opens the app first, then bob opens it, bob's queries run as alice. Bob sees alice's orders. Bob can write rows that look like alice wrote them. The audit log shows alice doing things bob did. RLS provides zero protection because RLS is doing exactly what it was told — return rows for alice, because the connection is authenticated as alice.

This is what we mean by "the #1 security incident vector in real-world Apps deployments." The bug is silent in dev (only one developer testing). It surfaces in production the moment a second user logs in. Customers will copy this pattern from a tutorial and ship it. Your job during a code review is to find it.

### Why the trap exists: SQLAlchemy's design assumptions

SQLAlchemy was designed for traditional apps where one service account talks to the DB. A global engine pool is the *correct* pattern there — you pay the connection-creation cost once, reuse forever. In OBO land, that assumption breaks. The credentials are per-user, per-request, with a ~1h TTL. A pool that outlives the credential is a pool that does the wrong thing.

There are two correct patterns. Pick based on traffic profile.

### Pattern 1 — Per-request engine (simple, works for almost everyone)

Build a fresh engine on each request from the request-scoped token:

```python
# ✅ Pattern 1 — per-request engine factory
import os
from sqlalchemy import create_engine

def get_engine():
    """Build a per-request engine from current OBO env vars."""
    user  = os.environ["PGUSER"]
    token = os.environ["DATABRICKS_TOKEN"]
    host  = os.environ["PGHOST"]
    db    = os.environ["PGDATABASE"]
    url = (f"postgresql+psycopg://{user}:{token}@{host}"
           f":5432/{db}?sslmode=require")
    return create_engine(url, pool_pre_ping=True, pool_size=2)

@app.route("/orders")
def my_orders():
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(text("SELECT * FROM orders")).fetchall()
```

The cost: one TLS handshake plus one Postgres auth round-trip per request — typically 30–80 ms on a warm Lakebase project. For most internal admin tools, dashboards, and AI chat apps, this is fine. The connection lifetime is bounded by the request, so the token is always fresh and pool-rollover concerns disappear.

The pattern is dead simple, easy to review, and almost impossible to use incorrectly. **If you don't have a measured reason to use Pattern 2, use Pattern 1.**

### Pattern 2 — Pool keyed by user (high QPS, paid in complexity)

If you've measured per-request engine creation as a real bottleneck — typically only happens when the app is doing thousands of QPS or has very latency-sensitive interactions — you can keep an LRU cache of engines, keyed by user identity:

```python
# ✅ Pattern 2 — LRU pool keyed by (user, token_hash)
from functools import lru_cache
import hashlib, os
from sqlalchemy import create_engine

@lru_cache(maxsize=128)
def _engine_for(user: str, token_hash: str, host: str, db: str):
    # This is called once per (user, token) combination
    token = os.environ["DATABRICKS_TOKEN"]   # current value
    url = (f"postgresql+psycopg://{user}:{token}@{host}"
           f":5432/{db}?sslmode=require")
    return create_engine(url, pool_pre_ping=True, pool_recycle=2700)

def get_engine():
    user  = os.environ["PGUSER"]
    token = os.environ["DATABRICKS_TOKEN"]
    host  = os.environ["PGHOST"]
    db    = os.environ["PGDATABASE"]
    th    = hashlib.sha256(token.encode()).hexdigest()[:16]
    return _engine_for(user, th, host, db)
```

Three details that matter:

- **Key includes the token hash** — when the token rotates (every ~1h), the cache key changes, the old engine ages out of the LRU, and a new engine is created with the new token.
- **`pool_recycle=2700`** — 45 minutes, comfortably under the 1h token TTL. Connections in the pool that were opened with an old token get retired before they can be handed out with stale auth.
- **`pool_pre_ping=True`** — every checkout validates the connection. A connection that was killed by Lakebase (e.g., during a scale event) gets replaced transparently.

The complexity buys you cache hits across requests for the same user, at the cost of a much larger surface area for bugs. **Don't use Pattern 2 until you've benchmarked Pattern 1 and proven it's the bottleneck.**

### A check you can run

After deploying, open the Postgres logs from the workspace UI. Have two teammates log into the app at the same time, run a query each, and look at the connection log. You should see two distinct connections, one with `user=alice@company.com` and one with `user=bob@company.com`. If you see two connections both as alice, you've shipped the trap. Restart the app and re-check after the second user logs in.

> **🎯 Checkpoint for 7.3**
> Without looking, explain why a global SQLAlchemy engine breaks in an OBO app. A passing answer: the engine captures the first user's token at startup; subsequent users reuse the connection and run as the first user; RLS provides no protection because the connection's `current_user` is wrong. If you can also describe when to choose Pattern 2 over Pattern 1, you're set.

---

## 7.4 — Common App Patterns in the Field

### The four shapes

Across customer engagements, almost every Apps + Lakebase build falls into one of four patterns. Recognizing the pattern fast is half the consulting battle — each one has a different rough cost, a different demo path, and a different set of pitfalls.

#### Pattern A — Internal admin tool (the Retool replacement)

A small team needs a UI to look up rows, edit a few fields, kick off a workflow. Today they pay for Retool or build something in Flask. Tomorrow it's a 200-line Streamlit app, governed by UC, billed alongside the rest of their Databricks usage.

- **Stack:** Streamlit + Lakebase + Apps OBO
- **Demo:** Show the ops team looking up a customer, editing a flag, the change appearing in DBSQL within seconds
- **Pitfall:** Customers want this *fast* — don't over-engineer. Pattern 1 (per-request engine) is enough.

#### Pattern B — AI chat-over-data (the ChatGPT alternative)

Apps + Lakebase agent memory (Module 6) + Foundation Models = a chat UI that knows the customer's data, remembers prior conversations, and runs entirely inside Databricks. No OpenAI keys. No third-party SaaS contract.

- **Stack:** Streamlit chat + Module 5 RAG + Module 6 agent memory + Foundation Models API
- **Demo:** Two-day session showing recall across sessions, RAG citations, audit-able tool calls
- **Pitfall:** The temptation to do chunk-and-embed inside the app. Don't — it belongs in a Job. The app reads from `documents`, it doesn't write to it.

#### Pattern C — Approval workflows (the Job-promoted row)

Orders, expenses, content moderation: a row sits in Lakebase, the app provides the approval UI, a Databricks Job promotes the row to `main.silver.approved` once a human signs off.

- **Stack:** Streamlit form + Lakebase write + Lakehouse Sync (Module 4) → Delta + Job for promotion
- **Demo:** Submit form → see row in Lakebase → approver sees it in app → approve → see it appear in DBSQL silver
- **Pitfall:** Forgetting to mark the Lakebase row "in flight" while approval is pending — you'll get duplicate-promotion bugs.

#### Pattern D — Customer 360 lookups (the sub-100ms search)

Synced tables (Module 4) feed Lakebase from UC. The app gives reps a search box that finds the right customer in <100ms across millions of rows.

- **Stack:** UC → Lakebase synced table + pgvector hybrid search (Module 5) + Streamlit
- **Demo:** Search "JS" → 50 candidates returned in 80ms, ranked by recent activity score
- **Pitfall:** Cold-start latency on the first query after idle. Either keep the project warm, or set user expectations on the first interaction.

### How to pick — two questions

The decision tree is short:

1. **Does the data change in response to user actions?** If yes, you're writing to Lakebase — Pattern A, B (memory), or C. If no, you're reading from Lakebase only — Pattern D.
2. **Does the app generate or use embeddings?** If yes, you're in B or D (with vectors). If no, you're in A or C.

That's it. Two questions, four buckets. Use the buckets to seed the architecture conversation; let the customer's specifics fill in the details.

### One thing all four share

Every one of these patterns relies on **OBO doing exactly what it says on the tin** — alice's queries run as alice, bob's run as bob, and Postgres GRANTs do the work the app would otherwise do in `WHERE` clauses. If your code review finds a global engine, none of the four patterns work safely. Make Pattern 1 vs. Pattern 2 from topic 7.3 the first thing you check on any Lakebase app PR.

> **🎯 Checkpoint for 7.4**
> Pick a customer scenario you've heard recently. In under 30 seconds, decide which of the four patterns it is, and name one pitfall for that pattern. If you can't decide in 30 seconds, the customer's request is probably under-specified — go ask one more question.

---

## Module 7 — theory complete

You should now be able to handle the architecture-review portion of any "let's put a Databricks App on Lakebase" customer conversation:

- Explain why Apps + Lakebase eliminates the seam tax at the application tier — one network path, one auth model, one governance plane (7.1)
- Describe the OBO runtime contract — what `PGHOST`, `PGUSER`, `DATABRICKS_TOKEN` mean, why GRANTs and RLS just work, and what `app.yaml` looks like (7.2)
- Spot the global-engine trap on sight, recommend Pattern 1 by default, and know when Pattern 2 is justified (7.3)
- Map a customer's verbal request to one of four common patterns, and name the right demo and pitfall for each (7.4)

The Module 7 lab notebook puts every one of these in your hands. You'll write a real `app.yaml` against the project from Module 3, build a per-request engine factory, deploy a Streamlit app, and verify with a teammate that two different identities produce two different views of the same data.

### Common pitfalls to remember

- **Global engine in `@st.cache_resource`.** The single most damaging pattern. Always per-request, or LRU-keyed by user.
- **Using `PGUSER` from somewhere other than the env var.** The runtime resets it per request — capturing it once at startup is the same bug as the global engine.
- **Forgetting `pool_recycle` in Pattern 2.** Connections opened at minute 0 with token T1 must not be handed out at minute 65 when token T2 is in effect.
- **Putting secrets in `app.yaml`.** There is nothing to put there. If you find yourself reaching for a `password:` field, you've drifted off the OBO path.
- **Skipping the two-user verification step.** Always log in as a teammate after deploy. The trap is silent until a second identity hits the app.

---

## What's next

**Module 8** is the capstone — you build a complete agent that ties Modules 3 (project), 4 (synced tables), 5 (vectors), 6 (memory), and 7 (apps) together into a single Streamlit-deployed customer-support agent. The per-request engine pattern from this module is the substrate the entire capstone is built on. **Save this notebook somewhere you can find it again.**
