# Module 04 — Sync, Federate & Unify: Concepts Behind the Lab

> **Level:** Intermediate · **Duration:** ~75 min (theory) + ~2.5h (hands-on lab) · **Format:** Conceptual primer
> **Coverage:** Topics 4.1 → 4.5 — the *why* behind every integration pattern you will build in the Module 4 lab.

---

## What this module gives you

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 made it real — you now have a live Lakebase project, registered in Unity Catalog, with a working OAuth pattern.

**Module 4 is where Lakebase stops being "managed Postgres" and starts being a *lakehouse-native* OLTP store.** The single capability that separates Lakebase from RDS is its three-way conversation with Delta and Unity Catalog. Before you open the notebook, you need a clean mental model of these five conceptual jumps:

1. **The three integration patterns** — when to use synced tables, Lakehouse Sync, or federation
2. **Synced tables** — how UC Delta replicates *into* Lakebase via managed CDC
3. **Lakehouse Sync** — how Lakebase changes flow *back to* UC Delta for analytics
4. **Federation** — how DBSQL can join Postgres rows live with Delta tables
5. **Cost & latency trade-offs** — the decision rule that prevents anti-patterns in production

This module covers the theory. The Module 4 lab notebook walks you through running each pattern against the project you provisioned in Module 3.

> **Prerequisite check.** You should have completed the Module 3 lab with all 6 ✅ checks. If `CLEANUP=False` was set at the end (the default), your `lakebase-tutorial` project and the `lakebase_tutorial` UC catalog are still live and ready. If you cleaned up, re-run Module 3 Steps 1–4 before continuing.

---

## 4.1 — The Three Integration Patterns: When To Use Each

### The deceptively simple question

A customer asks: *"How do I get my user profile data — currently sitting in a Delta table — into my application?"*

There are three valid answers. Each one corresponds to a different data direction, freshness need, and cost profile. Picking the wrong one is the most common Lakebase design mistake an RSA can make.

### The three patterns at a glance

| Pattern | Direction | Latency | Typical Use |
|---|---|---|---|
| **Synced tables** | UC Delta → Lakebase | Seconds to minutes | Serve features, profiles, catalog data to apps |
| **Lakehouse Sync** | Lakebase → UC Delta | Continuous CDC (~tens of seconds) | Run analytics on operational data, history |
| **Federation** | Live read from DBSQL | Per-query (network RTT) | Ad-hoc joins, low-volume reads, exploration |

### The decision rule

Three questions, in order:

1. **What direction does the data flow?**
   - *Lakehouse → app:* synced tables.
   - *App → lakehouse:* Lakehouse Sync.
   - *Live cross-store query from analyst:* federation.

2. **How fresh does it need to be at the destination?**
   - *Sub-second:* synced tables with `CONTINUOUS` scheduling.
   - *Few minutes is fine:* synced tables with `SNAPSHOT` scheduling (cheaper).
   - *Few seconds, append-only history:* Lakehouse Sync.

3. **How often will the query run?**
   - *Many times per second from an app:* synced tables (never federation).
   - *A few times a day from a dashboard:* federation is fine.
   - *Continuously, for analytics:* Lakehouse Sync.

### The anti-pattern to memorize

**Federation for high-volume reads.** Every federated query travels across the network and pushes load onto your OLTP primary. Use it once a week and you'll never notice. Use it from an app endpoint and you'll degrade both your dashboards and your app — *at the same time*. The cost is paid in p99 latency.

> **🎯 Checkpoint for 4.1**
> Three customer scenarios — pick the pattern for each, with no peeking:
> 1. *"Our recommendation model writes to Delta hourly. The app needs profiles in <10ms."*
> 2. *"Our orders table in Lakebase needs to be analyzed for fraud trends in our daily Delta pipeline."*
> 3. *"The auditor wants a one-off join between our Postgres customers and our Delta marketing events."*
>
> Answers: synced tables (CONTINUOUS or SNAPSHOT), Lakehouse Sync, federation.

---

## 4.2 — Synced Tables: UC Delta → Lakebase, Managed CDC

### The reverse-ETL replacement

For a decade, "reverse-ETL" tools (Hightouch, Census, Reverse-ETL pipelines built in-house) have existed for one reason: to push curated lakehouse data into operational stores so apps can read it at app latency. Synced tables make those tools obsolete *for any Lakebase customer*.

The promise is concrete: you declare a Delta table as a source, declare a primary key, declare a refresh policy. Databricks runs a managed CDC pipeline that keeps a Lakebase mirror within seconds-to-minutes of the source. Your app queries Lakebase like any Postgres table — with indexes, joins, and 1ms latency.

### The SDK call

```python
from databricks.sdk.service.database import (
    SyncedDatabaseTable, SyncedTableSpec, SyncedTableSchedulingPolicy
)

st = w.database.create_synced_database_table(
    synced_table=SyncedDatabaseTable(
        name="lakebase_tutorial.public.user_profile_synced",
        spec=SyncedTableSpec(
            source_table_full_name="main.features.user_profile",
            primary_key_columns=["user_id"],
            scheduling_policy=SyncedTableSchedulingPolicy.CONTINUOUS,
            timeseries_key="updated_at",
        ),
        database_instance_name="lakebase-tutorial",
    )
)
```

Five fields, each consequential:

- **`name`** — the *destination* fully-qualified UC name (catalog.schema.table). The catalog must be your Lakebase UC registration from Module 3.4.
- **`source_table_full_name`** — the *source* Delta table in UC. Must already exist.
- **`primary_key_columns`** — required. The CDC pipeline uses this to upsert correctly. No PK = no synced table.
- **`scheduling_policy`** — `CONTINUOUS` or `SNAPSHOT`. Covered next.
- **`timeseries_key`** — optional but recommended. A monotonic timestamp column that lets the pipeline detect ordering and avoid out-of-order writes.

### CONTINUOUS vs SNAPSHOT — the trade-off

| Scheduling | Latency | Cost | When to use |
|---|---|---|---|
| **`CONTINUOUS`** | Seconds (streaming) | Higher (pipeline always-on) | Real-time features, fraud signals, live dashboards |
| **`SNAPSHOT`** | Minutes (scheduled) | Lower (compute only at refresh) | Daily user profiles, slowly-changing dimensions |

**RSA heuristic:** start with `SNAPSHOT`. Only upgrade to `CONTINUOUS` when latency requirements are proven by a real workload. "We might need it real-time someday" is not proof.

### What the synced table looks like in Postgres

After creation, `lakebase_tutorial.public.user_profile_synced` exists as a real Postgres table. You can:

- `SELECT` from it like any table
- `CREATE INDEX` on any column the app needs
- `JOIN` it with native Postgres tables in the same database
- Read it from your app driver in <10ms

You **cannot** write to it. The platform owns the table — direct `INSERT/UPDATE/DELETE` from Postgres will be rejected. Writes belong upstream in Delta.

### The CDC pipeline you don't see

Behind the scenes, Databricks runs a managed pipeline that:

1. Reads the source Delta table's change data feed (CDF).
2. Transforms the changes (insert/update/delete) into upserts/deletes against Lakebase.
3. Applies them in primary-key order, respecting `timeseries_key` if provided.
4. Tracks pipeline state in UC so failures resume rather than restart.

You don't manage this pipeline directly. You can monitor it (status, last sync time, lag) via the SDK or the UC catalog UI.

### Common pitfalls

- **No primary key on Delta source** → synced table creation fails. Add a deterministic PK to the Delta table first.
- **Schema drift in Delta** (column added/removed) → synced table goes into `FAILED` until you re-create it. Synced tables are not schema-evolution-tolerant by default.
- **Synced table query is slower than expected** → you forgot to add a Postgres-side index. Synced tables ship with the PK index only.

---

## 4.3 — Lakehouse Sync: Lakebase → UC Delta (Continuous CDC)

### The reverse direction

Synced tables push data *into* Lakebase. Lakehouse Sync does the opposite: it captures every change in a Lakebase database and replicates it as a Delta table in UC, **continuously**. This is the pattern you reach for when:

- The app writes operational events (orders, sessions, signups) into Lakebase
- Your data team wants those events available in Delta for analytics, ML training, downstream pipelines
- You don't want to write your own Debezium / Kafka / Spark Structured Streaming pipeline

> **Beta status (as of 2026.04).** Lakehouse Sync is currently Beta on AWS and being progressively rolled out. Always confirm regional availability before designing for production.

### How it works under the hood

Lakebase's storage layer already keeps every page version (that's how branching and PIT restore work). Lakehouse Sync taps into the same versioned-page stream, decodes it into row-level CDC events (insert, update, delete), and writes them to a managed Delta table in UC. The Delta table maintains the same primary key as the source, with `_change_type` and `_commit_timestamp` columns for downstream MERGE patterns.

### The SQL call

```sql
CREATE LAKEHOUSE_SYNC main.operational.orders_history
FROM DATABASE_INSTANCE 'lakebase-tutorial'
SOURCE 'public.orders'
WITH (continuous = true);
```

Three things to notice:

- The destination is a **fully-qualified UC table** — `main.operational.orders_history` — created and managed by the platform.
- The source is `database_instance.schema.table`, not a UC name. Lakehouse Sync reads from the Lakebase storage layer directly.
- `continuous = true` makes this a streaming pipeline. Set `false` for periodic snapshot mode.

### What you do with the output

The output Delta table is append-only by default — every insert/update/delete in Lakebase becomes a new row. Two patterns dominate:

**Pattern A — Time-travel analytics (no MERGE).** Query the change feed directly. Useful for fraud, audit, "what did this row look like at 3:42 yesterday?" analyses.

**Pattern B — Materialized current state (MERGE downstream).** Run a periodic MERGE that folds the change feed into a current-state table. Standard CDC pattern:

```sql
MERGE INTO main.gold.orders_current t
USING main.operational.orders_history c
ON t.order_id = c.order_id
WHEN MATCHED AND c._change_type = 'delete' THEN DELETE
WHEN MATCHED AND c._change_type IN ('update', 'insert') THEN UPDATE SET *
WHEN NOT MATCHED AND c._change_type <> 'delete' THEN INSERT *;
```

### Cost & latency profile

- **Latency:** typically tens of seconds end-to-end (Lakebase commit → Delta visible). Acceptable for nearly all analytics; **not** for real-time app reads.
- **Cost:** continuous CDC pipeline + Delta storage. Cheap once steady-state because the source is already producing the change stream — Lakehouse Sync is mostly forwarding it.

### When Lakehouse Sync is the wrong answer

- **You need sub-second analytics** → query Lakebase directly (federation) or restructure the data flow.
- **You only need a daily snapshot** → a scheduled `INSERT INTO ... SELECT * FROM federated_table` is simpler and cheaper.
- **The data leaves the lakehouse boundary** → Lakehouse Sync targets UC; for external sinks, use a different tool.

---

## 4.4 — Federation: Live Reads From DBSQL

### What gets unlocked by UC registration

Recall Module 3.4: the moment you ran `CREATE CATALOG ... USING DATABASE_INSTANCE`, your Lakebase tables became visible to the Databricks SQL engine *without copying data*. That is federation — DBSQL queries Lakebase **live**, every time, over the network.

### What a federated query looks like

```sql
-- DBSQL, joining Postgres rows with Delta in one query
SELECT
  o.order_id,
  o.customer_id,
  o.order_total,
  p.product_name,
  p.category
FROM lakebase_tutorial.public.orders o      -- Postgres, live
JOIN main.gold.products p                   -- Delta
  ON o.product_id = p.product_id
WHERE o.created_at >= current_date - 7;
```

The query planner sees `lakebase_tutorial.public.orders` is a federated source. It pushes down what it can (predicates on `created_at`, projections of just the listed columns) and fetches matching rows over the wire. The Delta join happens in DBSQL.

### What pushes down, what doesn't

| Operation | Pushed to Postgres? | Notes |
|---|---|---|
| Simple WHERE predicates (`=`, `<`, `IN`) | Yes | Native Postgres planner uses indexes |
| Column projections | Yes | Only requested columns travel the wire |
| LIMIT (small) | Often | Postgres returns early |
| Aggregations (`SUM`, `COUNT`) | Sometimes | Depends on query complexity |
| Window functions | No | Rows pulled to DBSQL, computed there |
| Joins between Postgres tables | Yes | Postgres handles intra-DB joins |
| Joins between Postgres and Delta | No | DBSQL joins after pulling rows |

The takeaway: federation is best when your Postgres-side filter is selective. If your `WHERE` clause matches 10 rows, federation is fast. If it matches 10 million, the wire is the bottleneck.

### When federation is the right answer

- **One-off auditor reports** — quarterly, run once, no ongoing cost
- **Ad-hoc data science exploration** — analyst wants to peek at app data
- **Low-volume reference joins** — a daily dashboard with a small filter
- **Sanity checks** — comparing Lakebase row counts with synced table outputs

### When federation is the wrong answer

- **App-facing reads** — federation cannot beat in-Postgres latency
- **High-volume scheduled jobs** — every run hammers the OLTP primary
- **Anything more than weekly** — build a synced table or Lakehouse Sync instead

> **RSA heuristic.** If you'd run the query more than once a week, don't federate it — replicate it.

---

## 4.5 — Cost & Latency: The Decision Rule

### The three patterns priced

| Pattern | Recurring cost | Per-query cost | Latency tier |
|---|---|---|---|
| **Synced (CONTINUOUS)** | Sync compute always-on + LB storage | None | Seconds |
| **Synced (SNAPSHOT)** | Sync compute only at refresh + LB storage | None | Minutes |
| **Lakehouse Sync** | CDC pipeline + Delta storage | None | Tens of seconds |
| **Federation** | Zero | Network RTT + Postgres CPU | Per-query |

### The cost intuition

- **Synced tables** charge you to *keep data fresh* — even when no app is reading. Worth it when reads are frequent.
- **Lakehouse Sync** charges you to *forward changes* — cheap because the change stream already exists.
- **Federation** charges you per query — fine for occasional reads, brutal at high volume because every read taxes Postgres.

### The latency intuition

```
   <10 ms  ────  Postgres native (synced table is "just a table" once landed)
   ~1 sec  ────  Federation network round-trip + Postgres scan
   ~10s    ────  Synced CONTINUOUS lag (Delta commit → Lakebase visible)
   ~mins   ────  Synced SNAPSHOT refresh interval
   ~mins   ────  Lakehouse Sync end-to-end (Lakebase commit → Delta visible)
```

Notice that *synced table reads* are sub-10ms because by the time the app queries, the data already lives in Postgres. The "seconds-to-minutes" latency you sometimes hear about synced tables refers to **freshness lag**, not query latency. Don't confuse the two.

### The decision tree

```
                        ┌─ Need to write back to Lakehouse?
                        │      │
                        │      ├─ Yes  →  Lakehouse Sync
                        │      │
                        │      └─ No  ──┐
                        │               │
   Where does           │               ├─ Reads from app at app latency?
   data live now?  ─────┤               │      │
                        │               │      ├─ Yes  →  Synced table
                        │               │      │
                        │               │      └─ No  ──┐
                        │               │               │
                        │               │               ├─ Read frequency?
                        │               │               │      │
                        │               │               │      ├─ Often  →  Synced (SNAPSHOT)
                        │               │               │      │
                        │               │               │      └─ Rare   →  Federation
                        └───────────────┘               │
                                                        │
   Lakehouse  ──→  ──→  ──→  ──→  ──→  ──→  ──→  ──→ ──┘
```

### The single most important sentence

> **Federation is for queries that run rarely. Synced tables are for queries that run often. Lakehouse Sync is for changes that flow back to analytics.**

Internalize this and you'll never pick the wrong pattern again.

---

## Module 4 Wrap-Up & Common Pitfalls

### What the Module 4 lab actually proves

By the end of Lab 4 (the hands-on notebook), you will have:

1. **Created a synced table** from a Delta source into the Lakebase project from Module 3, with `CONTINUOUS` scheduling — proving sub-second-fresh app reads against the lakehouse.
2. **Built a Lakehouse Sync** from a Lakebase table back into UC Delta — proving the reverse-ETL path that competing OLTP databases require third-party tooling for.
3. **Run a federated join** in DBSQL between Postgres rows and Delta tables — proving the live cross-store path for ad-hoc work.
4. **Compared cost and latency** of all three with measured numbers — not slides, not promises, your own numbers.

These four capabilities are what every customer integration story rests on. **Module 5** layers pgvector on top of the synced-table pattern (vector embeddings landing from Delta into Lakebase). **Module 6** uses the Lakehouse Sync pattern to record agent traces. **Module 7** ties all three together inside a Databricks App.

### Common pitfalls — the things RSAs forget

| Pitfall | Why it happens | Fix |
|---|---|---|
| Synced table create fails: "no PK on source" | Delta table has no enforced PK | Add deterministic PK column + identifier in Delta |
| Synced table works but is "missing rows" | Source PK has duplicates | Ensure PK is truly unique on source side |
| `CONTINUOUS` synced table costs more than expected | Always-on pipeline | Switch to `SNAPSHOT` if minute-level freshness is fine |
| Lakehouse Sync not available in target region | Beta region rollout in progress | Check `databricks.feature_availability` before designing |
| Federated query slow on a small Postgres table | DBSQL pulled all rows due to non-pushdown predicate | Inspect `EXPLAIN`; add Postgres-side index; rewrite predicate |
| Federation on hourly job degrades app latency | Every run hits OLTP primary | Replace with synced table; federation is for rare reads |
| Synced table missing index, query slow | Only PK index ships by default | `CREATE INDEX` on Postgres after sync — query like any table |
| Schema drift breaks synced table | Delta column added | Re-create synced table with new schema |

### Module 4 — what's next

Once you've completed the four labs (4.1 → 4.4) in the Module 4 hands-on notebook, you have working examples of every Lakebase-Lakehouse integration pattern, with measured cost and latency profiles. You can pick the right pattern for any customer data flow — and articulate the trade-offs.

**Up next: Module 5** — *Lakebase as a Vector Store.* You'll enable `pgvector` in your project, ingest embeddings via a synced table, and build a low-latency RAG retrieval path — without bolting on a third-party vector DB.
