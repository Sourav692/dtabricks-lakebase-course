# Module 03 — Your First Lakebase Project: Concepts Behind the Lab

> **Level:** Beginner → Intermediate · **Duration:** \~75 min (theory) + \~3h (hands-on lab) · **Format:** Conceptual primer **Coverage:** Topics 3.1 → 3.6 — the *why* behind every step you'll execute in the Module 3 lab.

---

## What this module gives you

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 is where you **actually touch Lakebase** — provision it, connect to it, write SQL, register it in Unity Catalog, branch it, and recover from a mistake.

Before you open the notebook, you need a clean mental model of the six conceptual jumps you're about to make:

1. **Provisioning** — what happens between `create_database_project()` returning and the database being usable
2. **Authentication** — why the OAuth token is your password, and what changes when it isn't
3. **Schema design** — why "just standard Postgres" is the most important sentence in the docs
4. **Unity Catalog registration** — why this single command is the difference between a sidecar and a lakehouse-native database
5. **Branching** — why a copy-on-write branch is structurally identical to a Git commit, and what this unlocks
6. **Point-in-time restore** — why you'll never need pg_dump again

This module covers the theory. The Module 3 lab notebook walks you through running it.

> **Prerequisite check.** You should have completed Module 1 Lab 1.4 with all 9 ✅ checks. If `DATABASE_CREATE & region` failed there, this module's lab will fail at Step 1 — fix the entitlement first.

---

## 3.1 — Provisioning: What Happens in Those 60 Seconds

### The deceptively simple call

The first cell of the lab is one SDK call:

```python
project = w.database.create_database_project(
    project=DatabaseProject(
        name="lakebase-tutorial",
        capacity="CU_1",
        retention_window_in_days=7,
    )
)
```

This single call kicks off five distinct things on the Databricks control plane. Understanding what they are makes the difference between "the SDK is broken" and "I know exactly what's pending."

### The provisioning lifecycle

```
   Your SDK call                     PROVISIONING (~60–90s)              READY
        │                                    │                              │
        ▼                                    ▼                              ▼
   ┌────────────┐    ┌─────────────┐   ┌──────────────┐    ┌──────────────────┐
   │ API accept │ →  │ Allocate    │ → │ Spin up      │ →  │ DNS publishes    │
   │ (instant)  │    │ page server │   │ Postgres     │    │ read_write_dns   │
   └────────────┘    │ (storage)   │   │ compute (CU) │    │ accepts traffic  │
                     └─────────────┘   └──────────────┘    └──────────────────┘
                            │                  │                    │
                            ▼                  ▼                    ▼
                   Multi-tenant page   Primary compute       OAuth tokens
                   server is allocated attached to a fresh   accepted; main
                   for your project   `main` branch          branch is live
```

The five things, in order:

1. **API accepts the request** — the control plane validates your entitlement, capacity choice, and name uniqueness within the workspace. Returns a `uid` immediately. State: `PROVISIONING`.
2. **Storage allocation** — a slice of the multi-tenant page server is reserved for your project. Page versions for the entire retention window will live here. This is *not* a per-customer S3 bucket; it's a logical tenant on a shared, hardened page server.
3. **Compute spin-up** — a Postgres process at your chosen capacity (`CU_1` = 1 compute unit) is launched and attached to the storage tenant.
4. `main` **branch creation** — every new project gets a `main` branch automatically. The branch is just a pointer record; no data movement.
5. **DNS publishing** — `read_write_dns` becomes resolvable and the connection endpoint accepts TLS handshakes. State transitions to `READY`.

Total wall-clock: typically 60–90 seconds. The SDK call itself returns in under a second; the project object you receive has `uid` populated but you cannot connect until DNS resolves.

### Why CU_1 is the right starting capacity

`CU_1` (one compute unit) is the smallest non-zero capacity. You should default to it for tutorials, demos, dev branches, and most internal apps. Lakebase Autoscaling's job is to scale you up when load demands it; starting small and scaling up is always cheaper than starting big and waiting for usage. The `capacity` parameter on the project sets the **minimum** compute size for the `main` branch, not a fixed allocation.

### Why retention_window_in_days matters now (not later)

`retention_window_in_days=7` says: "keep every page version produced by every transaction for 7 days." That window is what makes Lab 3.6 (point-in-time restore) possible. If you set it to 0 — which is allowed — you cannot branch from a timestamp in the past, and Module 3.6 silently becomes Module 3-broken.

Production projects typically run with 14–30 day windows. The cost is roughly proportional to the window for active databases. For tutorials, 7 days is plenty.

### What "PROVISIONING → READY" failure looks like

If the project transitions to `FAILED` instead of `READY`, the most common causes are: region not yet GA for Lakebase Autoscaling (Module 1 Lab 1.4 should have caught this), workspace at capacity quota for the region, or a control-plane incident. The SDK exposes a `state_message` field — read it before retrying.

---

## 3.2 — Authentication: Your OAuth Token Is Your Password

### The trap Module 2 warned you about

In Module 2.5 we said: *Lakebase uses short-lived OAuth tokens (typically 1 hour). Long-running app pools must refresh tokens transparently — the Databricks Python / JDBC drivers do this; raw psycopg2 with a hardcoded password will silently fail after an hour.*

Module 3.2 is where you build the muscle memory to never get bitten by it.

### How Lakebase auth actually works

Lakebase speaks the standard Postgres wire protocol. There's nothing exotic in the handshake. What's different is **what you put in the password slot**:

```
                    Standard Postgres                        Lakebase
                  ────────────────────                ────────────────────
   Username    →  static role (e.g. 'app_user')   →  workspace identity
                                                      (e.g. 'you@example.com')
   Password    →  static password from a secrets  →  short-lived OAuth token
                  manager                            (regenerated on demand)
   TLS         →  optional (often disabled)        →  required (sslmode=require)
```

The Databricks SDK exposes `w.database.generate_database_credential(...)` which returns a fresh token. You hand that token to psycopg / SQLAlchemy as `PGPASSWORD`. The wire connection from there is identical to any Postgres.

### The token TTL trap, drawn out

```
   t=0           t=30min        t=58min        t=60min       t=61min
    │              │               │              │             │
    ▼              ▼               ▼              ▼             ▼
   ┌──────┐      ┌──────┐       ┌──────┐      ┌──────┐      ┌──────┐
   │ Get  │ ───► │ Conn │ ────► │ Conn │ ───► │ Conn │ ───► │ Conn │
   │token │      │ open │       │ open │      │ open │      │FAILS │
   └──────┘      └──────┘       └──────┘      └──────┘      └──────┘
                                                              │
                       Pool kept the connection alive         │
                       past token expiry. Server rejects      │
                       the next query: "password authentication failed"
```

The trap: a connection pool happily holds an open TCP socket. The token authenticated *the handshake*; the database does not refuse mid-session. But the moment the pool tries to **open a new connection** with the now-expired token, every fresh connection fails until you refresh.

### The correct pattern (and why the lab uses it)

```python
# Right at the top of every code path that opens an engine:
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[project.name],
)

url = (f"postgresql+psycopg://{user}:{cred.token}"
       f"@{project.read_write_dns}:5432/databricks_postgres?sslmode=require")

engine = create_engine(url, pool_pre_ping=True)
```

Two things to internalize:

- `pool_pre_ping=True` — before handing a pooled connection to your code, SQLAlchemy sends a cheap `SELECT 1`. If it fails (e.g., token expired between checkouts), SQLAlchemy discards and reopens. Costs \~1ms per checkout, saves you from the trap.
- **Generate fresh credentials per-engine, not per-process** — when an engine is recreated (in long-running apps, on a TTL or after a failure), a fresh token is generated. Never bake a token into a config file.

### What raw psycopg looks like (and why we don't recommend it)

Raw `psycopg.connect(host=..., user=..., password=token)` works for one-shot scripts. It does **not** work for anything that lives more than \~50 minutes. The lab uses SQLAlchemy because every later module's code (vector search, agent memory, app backend) needs pool management anyway.

---

## 3.3 — Schema Design: It's Just Postgres (Really)

### The most underappreciated sentence in the docs

> "Lakebase is PostgreSQL 17. All standard Postgres features work."

Customers, especially ones coming from proprietary databases, hear "managed Postgres on Databricks" and assume there must be Databricks-flavored quirks. There aren't. Your existing schema-migration tools, ORMs, indexes, extensions (from a curated list), constraints, triggers — they all work.

### The lab schema, anatomized

```
   ┌─────────────────────────────────┐
   │ users                           │
   │  id          BIGSERIAL  PK      │ ◄──┐
   │  email       TEXT UNIQUE NN     │    │
   │  full_name   TEXT               │    │ FK reference
   │  created_at  TIMESTAMPTZ now()  │    │
   └─────────────────────────────────┘    │
                                          │
   ┌─────────────────────────────────┐    │
   │ orders                          │    │
   │  id          BIGSERIAL  PK      │    │
   │  user_id     BIGINT     FK ─────┼────┘
   │  sku         TEXT NN            │
   │  qty         INT  CHECK > 0     │
   │  total_cents BIGINT             │
   │  placed_at   TIMESTAMPTZ now()  │
   └─────────────────────────────────┘
        │              │
        ▼              ▼
   idx_orders_user   idx_orders_placed
   (B-tree)          (BRIN — time-series friendly)
```

Three things in this schema deserve highlighting because they pay off in later modules:

- `BIGSERIAL` **for IDs.** Standard auto-incrementing 64-bit integer. Synced tables in Module 4 require a primary key; using `BIGSERIAL` from day one means you don't have to retrofit.
- `TIMESTAMPTZ DEFAULT now()`**.** Always use timezone-aware timestamps in Lakebase. The page server's audit metadata is in UTC; mixing in naive `TIMESTAMP` columns leads to off-by-one debugging at 3am.
- **Two indexes with different access patterns.** B-tree on `user_id` for point lookups ("show me this user's orders"). BRIN on `placed_at` because order time is monotonically increasing — BRIN is dramatically smaller and faster for time-range scans on append-only tables.

### Indexing reminder for Lakebase specifically

There is no Lakebase-specific indexing hint. `CREATE INDEX ... USING <type>` works exactly as in any modern Postgres:

| Index type | Use when |
| --- | --- |
| **B-tree** (default) | Equality, range scans on selective columns |
| **BRIN** | Massive append-only columns where values correlate with insertion order (timestamps, monotonic IDs) |
| **GIN** | JSONB, array containment, full-text search |
| **HNSW** (via `pgvector`) | Vector similarity — you'll use this in Module 5 |

The only Lakebase-specific advice: **don't over-index on the** `main` **branch in dev**. Branches share storage; indexes on `main` cost storage on every branch you ever spin off it. Build indexes on the production branch you'll eventually promote, not on the playground.

---

## 3.4 — Unity Catalog Registration: The Single Most Important Step

### What changes when you register

Up to this point, your Lakebase project is reachable from any Postgres client. Useful, but it's just a managed Postgres — like any RDS. The one SQL command that makes it a *lakehouse-native* database is the one that registers it in Unity Catalog:

```sql
CREATE CATALOG lakebase_tutorial
USING DATABASE lakebase-tutorial.databricks_postgres
OPTIONS (description = 'OLTP catalog for tutorial');
```

The moment you run that, four things become true at once:

```
   Before registration                    After registration
   ──────────────────                    ──────────────────
   ✓ Postgres clients can connect       ✓ All of that, AND
                                         ✓ DBSQL can SELECT from it via federation
                                         ✓ AI/BI dashboards can chart it
                                         ✓ Genie can answer questions on it
                                         ✓ Notebooks see it in the Catalog Explorer
                                         ✓ Synced tables can target it
                                         ✓ UC permissions (GRANT ...) apply
                                         ✓ Audit events flow to system tables
```

The single command flipped seven dependent capabilities from "not available" to "GA." This is the registration decision point — and it's why Lakebase is fundamentally different from a sidecar Postgres. There is no equivalent of this command for RDS.

### The registration model — what UC sees

```
   ┌─────────────────────────────────────────────────────────┐
   │                    UNITY CATALOG                         │
   │                                                          │
   │   ┌───────────┐       ┌───────────┐     ┌───────────┐   │
   │   │ catalog:  │       │ catalog:  │     │ catalog:  │   │
   │   │   main    │       │ samples   │     │ lakebase_ │   │
   │   │  (Delta)  │       │  (Delta)  │     │  tutorial │   │
   │   │           │       │           │     │ (LAKEBASE)│   │
   │   └───────────┘       └───────────┘     └─────┬─────┘   │
   │                                                │         │
   └────────────────────────────────────────────────┼─────────┘
                                                    │
                                                    ▼
                                           ┌────────────────┐
                                           │  Lakebase      │
                                           │  project DNS   │
                                           │  (live PG 17)  │
                                           └────────────────┘
```

The UC catalog object is a **pointer**, not a copy. SELECTs from DBSQL against `lakebase_tutorial.public.users` are answered by querying the live Lakebase Postgres, with the row data flowing back through the federation path. You get one source of truth — UC governs access, Lakebase owns the rows.

### Permissions: GRANT works in both worlds

After registration, you have *two* permission systems to think about, and they compose:

- **UC permissions** (`GRANT SELECT ON CATALOG lakebase_tutorial TO ...`) gate who can see the catalog at all from Databricks surfaces.
- **Postgres GRANTs** (`GRANT SELECT ON TABLE users TO ...`) gate who can query at the row level when connected directly via Postgres clients.

The honest answer when asked "which one wins?" is: **whichever is more restrictive.** If UC says "no", Databricks tools won't show the catalog. If Postgres says "no", direct PG connections fail. Module 7 (apps with on-behalf-of identity) is where this composition becomes powerful.

---

## 3.5 — Branching: Git for Your Database

### The mental model

If you've ever run `git checkout -b feature/new-thing`, you already understand Lakebase branching. Replace "commit graph" with "page versions" and the model is identical.

```
   main branch                  dev branch (created from main at t=now)
   ─────────                    ──────────────────────────────────────
   storage:  pages [v1, v2, v3, v4]   storage: SAME pages [v1, v2, v3, v4]
   compute:  PG process A             compute: PG process B
   DNS:      ...rw-main.cloud         DNS:     ...rw-dev.cloud

                Both branches point at
                the same physical pages
                          │
                          ▼
              When dev writes, ONLY dev's
              compute creates new page
              versions. main is untouched.
```

### What "copy-on-write" actually means here

Three properties, in plain terms:

- **Branch creation is constant-time, not data-size proportional.** A new branch on a 1 GB database takes the same wall-clock time as a new branch on a 10 TB database. You're creating a pointer, not copying bytes.
- **Reads on the new branch hit shared pages.** No cost duplication for unchanged data.
- **Writes diverge.** The first time `dev` writes a page that exists on `main`, the storage layer creates a new version *for dev only*. `main` keeps the old one. From that moment, the two branches' histories diverge. They will never converge unless you explicitly merge (which Lakebase does at the SQL level — there's no merge command, you `INSERT ... SELECT` between branches).

### What branches unlock — the four production patterns

```
   1) PR-based migrations              2) Ephemeral CI databases
   ────────────────────                ────────────────────────
   main ──────────────────────►        main ──────────────────────►
        │                                   │
        └─► dev ──► test migration         └─► ci-pr-1234 ──► test suite
            │      pass? promote               │              pass? merge PR
            │                                  │              fail? drop branch
            ▼                                  ▼

   3) Blue-green deploys               4) "What if?" analyses
   ──────────────────────              ──────────────────────
   main ──► branch ──► validate        main ──► analyst-sandbox
                       │                       │
                       └─► swap DNS            └─► run destructive aggregations
                            (instant)              never touches prod
```

In Lab 3.5 you'll build pattern #4: spin up a `dev` branch, run a destructive `DELETE FROM users`, verify `main` is intact, then drop the branch. It takes 90 seconds and demonstrates more value than any whitepaper can convey.

### What branches do NOT do

- **They are not full multi-region replication.** Branches share storage; if the storage region is down, every branch is down.
- **They do not auto-merge.** No equivalent of `git merge`. You do data movement explicitly with `INSERT ... SELECT FROM other_branch.table`.
- **They are not free of conflicts at the schema level.** If `main` and `dev` both add a column named `email`, when you reconcile you'll resolve it manually.

---

## 3.6 — Point-in-Time Restore: Branching, Across Time

### The big idea

Because the storage layer keeps every page version for the duration of `retention_window_in_days`, "restore to 5 minutes ago" is just **branching from a timestamp instead of from a branch tip**.

```
                 Time axis ────────────────────────────────►
   t=-7d ─────── t=-5min ───────── t=-1min ─────────── now
     │              │                  │                  │
     │              │                  │                  │
     ▼              ▼                  ▼                  ▼
   page versions retained on storage (any of them is restorable)
                    │
                    │  CREATE BRANCH recovery
                    │  FROM TIMESTAMP (now() - 5 min)
                    ▼
              ┌──────────────┐
              │  recovery    │   ◄─── Compute attached to storage state at t=-5min
              │   branch     │        Read-write. Just like main.
              └──────────────┘
```

### The lab pattern (and why it beats pg_dump)

```
   Step 1   On main:   UPDATE users SET email='oops' WHERE id=1   (the "outage")
   Step 2   Wait 60s   (so timestamp t=-60s exists in retention)
   Step 3   Branch    CREATE BRANCH recovery FROM TIMESTAMP (now() - '5 min'::interval)
   Step 4   Verify    SELECT email FROM recovery.users WHERE id=1   → original value
   Step 5   Restore   On main: UPDATE users SET email = (
                          SELECT email FROM recovery.users WHERE id=1
                      ) WHERE id=1
   Step 6   Cleanup   DROP BRANCH recovery
```

Compare with the pg_dump path: hourly snapshots (so you might lose up to an hour), restore the entire database to a separate instance (slow, costs full storage), then `pg_dump` the affected rows back. With Lakebase, the resolution is sub-second on time, the recovery branch costs only the diverged pages, and the cleanup is one command.

### When PIT restore is the wrong answer

- **Soft-delete needs.** If your application logically deletes rows (`deleted_at IS NOT NULL`), don't reach for PIT restore — just reset the column.
- **Cross-table consistency at app level.** If the corruption was caused by a multi-step business process (e.g., order placed but inventory not decremented), PIT restore restores both tables to a consistent point but may discard valid concurrent work. Use it carefully or use it with a maintenance window.
- **Audit / compliance.** Sometimes you legally cannot "make the bad data go away." PIT restore is operational, not regulatory.

---

## Putting it all together — what the Module 3 lab actually proves

By the end of Lab 3 (the hands-on notebook), you will have:

1. **Provisioned a Lakebase project programmatically** via SDK — reproducible across workspaces.
2. **Connected with auto-refreshing OAuth credentials** — the only pattern safe for long-running code.
3. **Built a real schema** with constraints, indexes, and reasonable types — not toy `CREATE TABLE foo (x INT)`.
4. **Registered the database in Unity Catalog** — the single command that promotes Lakebase from "managed Postgres" to "lakehouse-native OLTP."
5. **Branched, mutated, and verified isolation** — confirmed `main` is untouched when `dev` is destroyed.
6. **Recovered from a deliberate corruption** via point-in-time branching — the operational story you'll tell every customer.

These six capabilities are what every later module assumes you have working hands-on. Module 4 builds synced tables on top of #1 and #4. Modules 5 and 6 layer pgvector and agent memory onto #3. Module 7 uses #2 in production. The Module 3 lab is the load-bearing notebook for the rest of the roadmap.

---

## Common pitfalls — the things RSAs forget

| Pitfall | Why it happens | Fix |
| --- | --- | --- |
| `read_write_dns` is `None` immediately after create | DNS publishes \~5–10s after `READY` | Poll project state until `READY`, then read `read_write_dns` |
| Connection works at first, fails after an hour | Hardcoded token in connection string | Always use `pool_pre_ping=True` and regenerate creds via SDK |
| `CREATE INDEX` is slow on a small table | Table not actually small — old page versions counted | Confirm with `pg_total_relation_size`; consider `VACUUM FULL` on dev branches |
| UC catalog created but DBSQL can't see tables | Permissions not granted in UC | `GRANT USE CATALOG, BROWSE ON CATALOG ...` to your user/group |
| Branch creation hangs &gt; 60s | First branch in a fresh project warming caches | Normal for the first one; subsequent branches are seconds |
| PIT restore returns "timestamp out of retention window" | `retention_window_in_days` set too low at provision time | Cannot fix retroactively; recreate project with larger window for prod |

---

## Module 3 — what's next

Once you've completed the six labs (3.1 → 3.6) in the Module 3 hands-on notebook, you have a fully working Lakebase project, real experience with branching and point-in-time restore, and Unity Catalog registration done — the two features that most differentiate Lakebase from RDS.

**Up next: Module 4** — *Sync, Federate & Unify with Unity Catalog.* You'll learn the three integration patterns (UC → PG synced tables, PG → UC Lakehouse Sync, and live federation), then build a real reverse-ETL replacement that pushes Delta data into your Lakebase project automatically.