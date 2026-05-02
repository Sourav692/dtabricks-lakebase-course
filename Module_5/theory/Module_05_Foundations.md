# Module 05 — Lakebase as a Vector Store: Concepts Behind the Lab

> **Level:** Intermediate · **Duration:** ~75 min (theory) + ~3h (hands-on lab) · **Format:** Conceptual primer
> **Coverage:** Topics 5.1 → 5.3 — the *why* behind every line of pgvector code you'll write in the Module 5 lab.

---

## What this module gives you

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 put a real Lakebase project in your hands. Module 4 wired it to the lakehouse. **Module 5 turns that same Postgres into a production-grade vector store** — for RAG, semantic search, and embedding-driven features — without bolting on a third-party service.

Before you open the notebook, you need a clean mental model of three conceptual jumps:

1. **The pgvector vs. Mosaic AI Vector Search decision** — when each is the right tool, and the boundary between them
2. **Schema design for embeddings** — why HNSW beats IVFFlat, what `m` and `ef_construction` actually do, and which auxiliary indexes you need
3. **Hybrid retrieval theory** — why pure vector search misses obvious matches, and how Reciprocal Rank Fusion fixes it

This module covers the theory. The Module 5 lab notebook walks you through running it — building the schema, generating embeddings via the Foundation Models API, running RRF queries, and wrapping it all in a 40-line RAG function.

> **Prerequisite check.** You should have completed Module 3 Lab 3 with a working Lakebase project registered in Unity Catalog, and Module 4 Lab 4 with at least one synced table demonstrating UC → Lakebase data flow. Module 5 layers `pgvector` onto the schema you already built — it does not provision a new project.

---

## 5.1 — When to Use Lakebase + pgvector vs. Mosaic AI Vector Search

### Two products, one platform, no contradiction

Databricks ships **two** first-party answers for "I need to do vector search." This confuses customers more often than it helps them. The first thing to internalize is that this is not a competition. They solve overlapping problems at **different scales**, and the right answer depends on three numbers and two architecture facts.

- **Lakebase + pgvector** — the `vector` extension running inside your Postgres compute. Embeddings are columns in a table. Search is a SQL query. Hybrid filters, joins, and row-level security come for free because it's just Postgres.
- **Mosaic AI Vector Search** — a dedicated, governed retrieval service in Databricks that indexes a Delta table and serves nearest-neighbor queries via a REST endpoint. Tuned for very large corpora and high recall.

Both are real products you'd recommend with a straight face. The skill is knowing which one fits the customer's actual constraints.

### The decision rule (memorize this)

| Corpus size | Recommendation |
|---|---|
| **< 5 million vectors** | Lakebase + pgvector |
| **5 – 10 million vectors** | Benchmark both — query patterns matter more than corpus size |
| **> 10 million vectors** | Mosaic AI Vector Search |

Corpus size is the headline number, but it is rarely the only number that matters. Three other dimensions usually pull the decision one way or the other:

- **Hybrid SQL filters.** If a query looks like *"find the 10 most similar product descriptions, but only for tenant=42, in stock, from last 30 days, and where the user has access via RLS"* — that is a SQL workload. pgvector wins because the filter and the similarity search happen in the same plan, on the same data, with the same transaction.
- **Transactional consistency.** If embeddings must be in the **same row** as the source document — committed in the same transaction, dropped in the same delete, isolated by the same MVCC snapshot — pgvector is the only option. Mosaic AI Vector Search has eventual consistency between the Delta source and the index.
- **Co-location.** If your app already has a Lakebase connection open for users, sessions, and orders, adding a `documents` table with an `embedding` column is **zero new infrastructure**. Adding Mosaic AI Vector Search is a new service, a new endpoint, a new client, and a new auth path.

### When pgvector is the wrong call

Be honest with the customer. Recommend Mosaic AI Vector Search when:

- The corpus is **already in Delta** and is updated by Spark jobs — let the index live where the data lives.
- You need **billion-scale vectors** with sub-50ms p99 latency. Postgres is not the right substrate at that scale.
- Governance is the primary driver — Mosaic AI Vector Search inherits Unity Catalog ACLs natively, so a single GRANT on the source table covers the index.
- The team explicitly does **not** want to manage HNSW parameters, vacuum, and Postgres tuning.

> **🎯 Checkpoint for 5.1**
> Without looking, list three questions you would ask a customer before recommending pgvector vs. Mosaic AI Vector Search. A good answer touches: corpus size, whether they need transactional consistency, and whether they already have a Lakebase project in the architecture.

---

## 5.2 — Schema Design for Embeddings: Indexes That Matter

### What pgvector adds to Postgres

`pgvector` is a Postgres extension that adds:

- A new column type — `vector(N)` — storing a fixed-dimension float array
- Distance operators — `<=>` (cosine), `<->` (L2), `<#>` (negative inner product)
- Two index types — **HNSW** and **IVFFlat** — that approximate nearest-neighbor search

Everything else in your `documents` table is just standard Postgres. That's the whole point of Module 5: vector search is a column, not a service.

### HNSW vs. IVFFlat — the only choice that matters

You will see both index types in pgvector documentation. For app workloads, **always reach for HNSW first**. The trade-off:

| Dimension | HNSW | IVFFlat |
|---|---|---|
| **Build time** | Higher — minutes to hours for millions of rows | Lower — usually faster to build |
| **Build memory** | Higher — graph fits in `maintenance_work_mem` | Lower |
| **Recall at app latency** | **Excellent** | Mediocre — degrades fast as `lists` is wrong-sized |
| **Tuning at query time** | `ef_search` — easy, per-query | None — index-level only |
| **Stable under inserts** | **Yes** — graph adapts incrementally | No — quality drifts; needs rebuild |

The summary: **IVFFlat was the sensible default in 2022; HNSW is the sensible default in 2026.** Spend the build time once. Reap the recall and latency forever.

### HNSW parameters — what `m` and `ef_construction` actually do

```sql
CREATE INDEX idx_doc_hnsw ON documents
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

These two numbers control the shape of the navigable graph that HNSW builds. You don't need to derive the algorithm to pick good values — you need a mental model:

- **`m`** = number of edges per node in the graph. Higher `m` means more connections, higher recall, more memory, slower build. **`m = 16` is the right starting value** for most app workloads. Bump to 32 only if recall benchmarks demand it; almost no one needs higher.
- **`ef_construction`** = the size of the dynamic candidate list during graph construction. Higher means a better-quality graph at build time, at the cost of build duration. **`ef_construction = 64` is sane**; 128 is acceptable for a one-time bulk index on a corpus that won't change often.
- **`ef_search`** = the candidate list size **at query time**. This is the recall/latency dial you turn per query — not at index creation. Default is 40. Bump to 100 for higher recall, drop to 20 for faster but lower-recall search. Set with `SET LOCAL hnsw.ef_search = 100;` inside a transaction.

The mental model that helps: **`ef_construction` shapes the graph once. `ef_search` is how aggressively you walk it on each query.** You will tune `ef_search` weekly. You will tune `m` once per project, if ever.

### Distance operators — pick one and stick with it

pgvector exposes three. The choice is determined by your **embedding model**, not by preference:

| Operator | Distance | Index opclass | When to use |
|---|---|---|---|
| `<=>` | Cosine | `vector_cosine_ops` | **Default for text embeddings** (BGE, OpenAI, etc.) |
| `<->` | Euclidean (L2) | `vector_l2_ops` | When the model is normalized for L2 |
| `<#>` | Negative inner product | `vector_ip_ops` | When you've already L2-normalized vectors |

For `databricks-bge-large-en` and effectively every text embedding model you'll meet on the Foundation Models API, **use cosine** (`<=>` and `vector_cosine_ops`). The index opclass must match the operator you use in queries — picking the wrong opclass means the index is silently bypassed.

### Why dimension matters: 1024 is not a magic number

The `vector(N)` declaration must match your embedding model exactly:

| Model | Dimension | Notes |
|---|---|---|
| `databricks-bge-large-en` | **1024** | Default Databricks Foundation Models API embedder |
| `databricks-gte-large-en` | 1024 | Alternative; similar quality |
| OpenAI `text-embedding-3-small` | 1536 | If using external OpenAI |
| OpenAI `text-embedding-3-large` | 3072 | High-quality, larger storage cost |

A 1024-dim float vector is 4 KB per row at full precision. A million rows = ~4 GB just for embeddings, before any Postgres overhead. Memory and storage planning matters at scale; a casual `vector(3072)` decision multiplies your storage line item by 3×.

### The auxiliary indexes you forget at your peril

A vector-only schema will work, but it will not be the schema you wish you had once the corpus is real. The Module 5 lab has you create three indexes for a reason:

```sql
CREATE INDEX idx_doc_hnsw ON documents
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_doc_tsv  ON documents USING GIN (tsv);
CREATE INDEX idx_doc_meta ON documents USING GIN (metadata);
```

- **HNSW** on `embedding` — for vector similarity. Already covered.
- **GIN** on `tsv` (a generated `tsvector` column) — for keyword/BM25 search. This is the second leg of hybrid retrieval (covered in 5.3). Without this index, the BM25 half of every query becomes a table scan.
- **GIN** on `metadata` (JSONB) — for filter pushdown. When the query is *"top-10 by similarity, but only where metadata->>'tenant' = '42'"*, you want this index pruning rows before HNSW runs. Without it, you scan the whole table and filter post-hoc.

### The generated tsvector pattern

The schema in the lab uses a **generated column** for the tsvector:

```sql
tsv tsvector GENERATED ALWAYS AS
    (to_tsvector('english',
       coalesce(title,'') || ' ' || coalesce(content,''))) STORED,
```

Three things to notice:

1. **`GENERATED ALWAYS AS … STORED`** means Postgres maintains the tsvector for you on every insert and update. You never compute it in application code.
2. **`coalesce(...,'')`** prevents NULL fields from poisoning the concatenation. A NULL anywhere in `||` would produce a NULL tsvector, which silently disables BM25 for that row.
3. **`'english'`** sets the text-search configuration — controls stemming and stop-words. For multilingual corpora you store the language in `metadata` and switch configurations per query.

> **🎯 Checkpoint for 5.2**
> Without looking: which HNSW parameter do you tune at index creation, and which do you tune per query? A passing answer says: `m` and `ef_construction` are creation-time; `ef_search` is per-query.

---

## 5.3 — Hybrid Retrieval and Reciprocal Rank Fusion

### Why pure vector search isn't enough

Embeddings capture *meaning*. They are excellent at "find me documents about returning damaged electronics" matching a doc titled "RMA process for defective devices" — even though they share no words. They are also famously bad at exact-match queries: a search for the SKU `LB-9047X` will not necessarily rank the document that contains exactly `LB-9047X` first, because the embedding model has no special intuition about identifier strings.

The classic answer is BM25 — keyword search with TF-IDF weighting, which Postgres implements via `tsvector` + `ts_rank`. BM25 is excellent at exact tokens and acronyms, mediocre at synonyms and paraphrase. **Vector and BM25 fail in opposite directions.** The fix is to run both and combine the rankings.

### Reciprocal Rank Fusion (RRF) in one paragraph

RRF combines two (or more) ranked lists by ignoring the underlying scores entirely and using only the **rank position** of each document in each list. The formula is:

```
score(doc) = Σ over lists L  of  1 / (k + rank_L(doc))
```

Where `k` is a smoothing constant (60 is the convention from the original paper). The intuition: a document that appears in the top 3 of *both* lists scores higher than a document that appears at rank 1 of only one list. RRF is robust because it doesn't try to normalize the cosine distance against the BM25 score — those quantities are incomparable. It only compares ranks, which are already on the same scale.

### What hybrid retrieval looks like in SQL

The Module 5 lab walks you through this query. Read it now so the lab makes sense later:

```sql
WITH v AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> :qv) AS rk
  FROM documents
  ORDER BY embedding <=> :qv
  LIMIT 50
),
k AS (
  SELECT id, ROW_NUMBER() OVER
    (ORDER BY ts_rank(tsv, plainto_tsquery(:q)) DESC) AS rk
  FROM documents
  WHERE tsv @@ plainto_tsquery(:q)
  LIMIT 50
)
SELECT d.id, d.title,
       1.0/(60 + v.rk) + 1.0/(60 + k.rk) AS rrf
FROM documents d
LEFT JOIN v USING(id)
LEFT JOIN k USING(id)
WHERE v.id IS NOT NULL OR k.id IS NOT NULL
ORDER BY rrf DESC
LIMIT 10;
```

Three things to notice:

1. **Two CTEs, both top-50.** You retrieve 50 from each leg, not the final 10, so RRF has enough overlap to fuse meaningfully. Going to 50/50 → 10 is a typical fanout.
2. **`LEFT JOIN` both ways.** A document that ranks high on vector but doesn't appear in BM25 at all should still surface — RRF handles this gracefully because the missing rank just contributes 0 to the score.
3. **`ORDER BY rrf DESC`.** The fused ranking is the final ranking. There is no further re-rank step (though you can add a cross-encoder re-rank on the top 10 if recall isn't enough — that's a Module 6 advanced topic).

### Filters, tenancy, and RLS — the pgvector advantage made concrete

The reason you'd choose pgvector over a dedicated vector service often comes down to **arbitrary SQL predicates**. Both CTEs above can take a `WHERE` clause that restricts to a tenant, language, recency window, or row-level security policy:

```sql
WITH v AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> :qv) AS rk
  FROM documents
  WHERE metadata->>'tenant_id' = :tenant
    AND metadata->>'lang' = 'en'
    AND created_at > now() - interval '90 days'
  ORDER BY embedding <=> :qv
  LIMIT 50
),
...
```

The Postgres planner uses your **GIN index on `metadata`** to prune rows before HNSW runs. This is the single most important reason to keep that index in the schema — without it, the filter happens after similarity, which is both slower and gives wrong top-K (because the most similar 50 might all be from another tenant).

### Tuning ef_search per query

You can dial recall vs. latency at the query level by setting `hnsw.ef_search`:

```sql
BEGIN;
SET LOCAL hnsw.ef_search = 100;   -- raise from default 40 for higher recall
WITH v AS ( ... ) ...
COMMIT;
```

Use `SET LOCAL` so the change applies only inside the transaction. A typical pattern:

- **Latency-sensitive lookup** (autocomplete, instant search): `ef_search = 20–40`
- **App-tier RAG** (ChatGPT-style Q&A): `ef_search = 60–100`
- **Offline / batch / re-ranking pipeline**: `ef_search = 200+` or just bump to exact search

> **🎯 Checkpoint for 5.3**
> Without looking, write the RRF formula. A passing answer is `1 / (k + rank)` summed over lists, with k commonly = 60. If you can also explain *why* RRF doesn't try to normalize the underlying scores, you've got it.

---

## Module 5 wrap-up

You should now be able to handle the first ten minutes of any "should we use pgvector?" customer conversation:

- Recommend pgvector vs. Mosaic AI Vector Search using corpus size, hybrid-filter needs, transactional-consistency needs, and existing architecture (5.1)
- Design a vector schema with HNSW + GIN indexes, pick `m` and `ef_construction` defensibly, and avoid the `vector_l2_ops` / `<=>` mismatch trap (5.2)
- Explain why hybrid retrieval beats pure vector, and write an RRF query that mixes vector and BM25 with arbitrary SQL filters (5.3)

The Module 5 lab notebook (5.4–5.6 in the roadmap) puts every one of these in your hands. You'll create the `vector` extension, build the `documents` table on top of the Module 3 schema, generate embeddings via the Foundation Models API, run hybrid retrieval against real data, and wrap the whole thing in a `rag()` function that takes a question and returns a grounded answer.

### Common pitfalls to remember

- **Wrong opclass on the HNSW index.** Querying with `<=>` (cosine) but the index uses `vector_l2_ops` — the planner silently bypasses the index and you get a slow sequential scan. Check `EXPLAIN` output if your latency suddenly drops.
- **Forgetting the GIN index on metadata.** Filters on `metadata->>'tenant'` without the index = full scan. The HNSW index alone does not help here.
- **Using IVFFlat by default.** It "works" on small datasets and looks like a reasonable choice, then quietly degrades as data shape changes. Reach for HNSW first.
- **Hardcoding `ef_search` in your DDL.** It's a query-time GUC, not an index parameter. Tune it per query class, not globally.
- **Computing the tsvector in application code.** Use `GENERATED ALWAYS AS … STORED` so Postgres maintains it. Hand-maintained tsvectors drift the moment a developer forgets.

**Next up: the Module 5 lab.** You'll build the schema on top of your Module 3 project, embed and bulk-load documents via Foundation Models, run an end-to-end RAG query, and verify hybrid retrieval beats pure vector on a deliberately-constructed corner case.
