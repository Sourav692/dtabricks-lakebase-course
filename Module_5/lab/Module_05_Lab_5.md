# Module 05 · Lab 5 — Lakebase as a Vector Store, End-to-End

> **Type:** Hands-on · **Duration:** ~90 minutes · **Format:** Databricks notebook
> **Purpose:** Layer pgvector onto the Lakebase project you built in Module 3, build a real `documents` table with HNSW + GIN indexes, generate embeddings via Foundation Models, run hybrid retrieval with Reciprocal Rank Fusion, and wrap the whole thing in a working RAG function. By the end of this lab you will have done — for real, not in slides — every step of a production-grade RAG pipeline on Lakebase.

---

## Why this lab exists

Module 5 theory taught you *when* to use pgvector, *what* indexes to put on a vector schema, and *why* hybrid retrieval beats pure vector search. This lab makes you **execute every line** so you can demo it on a customer call without reaching for slides.

This lab walks the seven steps in order:

1. **Setup** — reuse your Module 3 project, install pgvector deps, open an engine
2. **Enable** the `vector` extension and verify
3. **Build** the `documents` schema with HNSW + GIN indexes
4. **Embed & load** a small but realistic corpus via the Foundation Models API
5. **Pure vector** retrieval — confirm HNSW is being used via `EXPLAIN`
6. **Hybrid retrieval** with Reciprocal Rank Fusion — prove the SKU corner case
7. **End-to-end RAG** — a 40-line `rag()` function answering questions from the corpus
8. **Final verification & cleanup**

Total runtime ~90 minutes. About 10 minutes of that is your hands on the keyboard; the rest is embedding generation and waiting for HNSW to build.

> **Run mode.** Do this lab in a Databricks notebook attached to a serverless cluster (or DBR 14+). All cells are Python except where SQL is explicitly called out via `%sql`. Copy each cell into your own notebook as you go.

---

## Prerequisites before you start

You need:

- ✅ **Module 3 Lab 3 passed** with all 6 checks green and `CLEANUP=False` — this lab layers onto that project
- ✅ A Databricks workspace with **Foundation Models API** enabled in the region (default for most workspaces)
- An attached compute cluster (serverless is fine, recommended)
- The same `TUTORIAL` config dict you used in Module 3 — re-run that cell in a new tab if needed

You do **not** need:

- A new Lakebase project (you reuse `lakebase-tutorial` from Module 3)
- Any external embedding service (Foundation Models is built-in)
- Any third-party vector database
- Module 4 (synced tables are independent of vector search)

> **⚠ Billing note.** Foundation Models embedding calls are billed per token. The corpus in this lab is ~50 KB of text producing ~5,000 embedding tokens — well under one cent. The bigger cost is the Lakebase compute already running from Module 3, which Step 8b leaves untouched (so Module 6 can reuse it).

---

## Step 1 — Setup: reuse the Module 3 project

You're not provisioning a new project. You're picking up the same `lakebase-tutorial` project from Module 3 and opening a fresh engine on it. The Cell 2 logic is idempotent — if the project somehow doesn't exist, you'll get a clear error and a pointer back to Module 3.

```python
# Cell 1 — install dependencies (re-run if the kernel restarted)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()
```

```python
# Cell 2 — reload the TUTORIAL config and re-open the engine on the Module 3 project
import uuid, time, json
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

w = WorkspaceClient()

TUTORIAL = {
    "catalog":      "main",
    "schema":       "lakebase_tutorial",
    "project_name": "lakebase-tutorial",
    "embed_model":  "databricks-bge-large-en",
    "chat_model":   "databricks-meta-llama-3-3-70b-instruct",
    "embed_dim":    1024,                     # bge-large-en is 1024-dim
    "user":         w.current_user.me().user_name,
    "host":         w.config.host,
}

# Confirm the project from Module 3 exists.
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

with engine.connect() as conn:
    pg_version = conn.execute(text("SELECT version()")).scalar()
print(f"✅ Reconnected to '{TUTORIAL['project_name']}'")
print(f"   {pg_version.split(',')[0]}")
print(f"   Endpoint: {project.read_write_dns}")
```

**Expected output:**

```
✅ Reconnected to 'lakebase-tutorial'
   PostgreSQL 17.x on aarch64-unknown-linux-gnu
   Endpoint: instance-a3f9e1c4-rw.cloud.databricks.com
```

**What this proves:** the Module 3 project is alive, your OAuth still works, and you have a writable engine. Nothing about pgvector yet — that's Step 2.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `Project 'lakebase-tutorial' not found` | Module 3 cleanup ran, or a different workspace | Run Module 3 Lab 3 again with `CLEANUP=False` |
| `Project state is DELETING` | Cleanup is in progress | Wait for `delete_database_project` to finish, then re-run Module 3 |
| `password authentication failed` | Token expired in a forgotten kernel | Re-run Cell 2; `generate_database_credential` issues a fresh token |
| `psycopg.OperationalError: connection refused` | Project went idle | Wait 10–15s; Lakebase auto-resumes on first connection |

---

## Step 2 — Enable the `vector` extension

`pgvector` ships pre-installed on Lakebase Autoscaling, but the extension must be enabled per-database. This is a single command, idempotent (`IF NOT EXISTS`), and finishes in milliseconds.

```python
# Cell 3 — enable pgvector and verify
with engine.begin() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

with engine.connect() as conn:
    ext = conn.execute(text(
        "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
    )).first()

if ext is None:
    raise RuntimeError("pgvector extension is not registered after CREATE EXTENSION")
print(f"✅ pgvector enabled — version {ext.extversion}")
```

**Expected output:**

```
✅ pgvector enabled — version 0.7.x
```

**What this proves:** the `vector` type, the `<=>` `<->` `<#>` operators, and the `hnsw` / `ivfflat` access methods are now available in this database. Nothing about your existing `users` and `orders` tables changed — pgvector is purely additive.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `extension "vector" is not available` | Working against a non-Lakebase Postgres | Confirm `read_write_dns` ends in a Databricks domain |
| `permission denied to create extension` | Connected as a non-owner role | Confirm Cell 2 used the same workspace identity that created the project in Module 3 |
| Extension version `< 0.5` | Very old extension build | Open a support ticket — all current Lakebase regions ship `>=0.7` |

---

## Step 3 — Build the `documents` schema

This is the schema from Module 5 theory 5.2, with all three indexes the lab section called out: HNSW for similarity, GIN on the generated tsvector for BM25, and GIN on the JSONB metadata for filter pushdown.

```python
# Cell 4 — documents schema with HNSW + GIN indexes
DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT,
    title       TEXT,
    content     TEXT,
    metadata    JSONB,
    embedding   vector(1024),
    tsv         tsvector GENERATED ALWAYS AS
                (to_tsvector('english',
                    coalesce(title,'') || ' ' || coalesce(content,''))) STORED,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- HNSW for vector similarity (cosine distance to match bge-large-en)
CREATE INDEX IF NOT EXISTS idx_doc_hnsw  ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH  (m = 16, ef_construction = 64);

-- GIN on tsvector for BM25 / keyword search
CREATE INDEX IF NOT EXISTS idx_doc_tsv   ON documents USING GIN (tsv);

-- GIN on JSONB metadata for filter pushdown (tenant, lang, source, ...)
CREATE INDEX IF NOT EXISTS idx_doc_meta  ON documents USING GIN (metadata);
"""

with engine.begin() as conn:
    for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
        conn.execute(text(stmt))

# Confirm
with engine.connect() as conn:
    cols = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'documents'
        ORDER BY ordinal_position
    """)).fetchall()
    idx = conn.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE tablename = 'documents' ORDER BY indexname
    """)).fetchall()

print("✅ Table 'documents' columns:")
for c in cols:
    print(f"   · {c.column_name:<12} {c.data_type}")
print()
print("✅ Indexes on 'documents':")
for i in idx:
    print(f"   · {i.indexname}")
```

**Expected output:**

```
✅ Table 'documents' columns:
   · id           bigint
   · source       text
   · title        text
   · content      text
   · metadata     jsonb
   · embedding    USER-DEFINED      ← this is vector(1024)
   · tsv          tsvector
   · created_at   timestamp with time zone

✅ Indexes on 'documents':
   · documents_pkey
   · idx_doc_hnsw
   · idx_doc_meta
   · idx_doc_tsv
```

**What this proves:** every theory choice from Module 5.2 is now real DDL on disk — `vector(1024)` matches the bge-large-en embedding dimension, `vector_cosine_ops` matches the `<=>` operator you'll query with, the `tsv` column is `GENERATED ALWAYS AS … STORED` so Postgres maintains it for you, and you have GIN indexes on both `tsv` and `metadata`.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `type "vector" does not exist` | Skipped Step 2 | Run Cell 3 to enable the extension first |
| `relation "documents" already exists` | Re-running the lab | Acceptable — the DDL is `IF NOT EXISTS` and idempotent |
| `column "embedding" cannot have a vector dimension > 16000` | Typo'd the dimension | Confirm `vector(1024)`, not `vector(10240)` |
| HNSW index build hangs | Unusually large `maintenance_work_mem` request | Restart the cell; the empty-table build should complete in <1s |

---

## Step 4 — Embed and bulk-load the corpus

We use the Foundation Models API via the SDK's OpenAI-compatible client. The corpus is a small set of fictional electronics-store policy documents — small enough that the lab finishes in a minute, large enough to demonstrate hybrid retrieval (in particular, one document contains the literal SKU `LB-9047X`, which Step 6 uses to prove vector-only retrieval misses exact matches).

### Step 4a — Define the corpus

```python
# Cell 5 — the corpus (10 docs covering policies, FAQs, and one SKU-specific doc)
CORPUS = [
    ("policy/returns.md",        "Return Window Policy",
     "Standard returns are accepted within 30 days of delivery for most items. "
     "Opened electronics have a reduced 14-day return window. Restocking fees may apply."),
    ("policy/rma.md",             "RMA Process for Defective Devices",
     "If a device arrives defective, request a Return Merchandise Authorization "
     "within 7 days. We will dispatch a prepaid label and replace or refund."),
    ("policy/warranty.md",        "Warranty Terms",
     "All electronics carry a one-year manufacturer warranty covering defects in "
     "materials and workmanship. Damage from misuse is not covered."),
    ("faq/shipping.md",           "Shipping Frequently Asked Questions",
     "Standard ground shipping arrives in 3-5 business days. Expedited two-day "
     "shipping is available at checkout for an additional fee."),
    ("faq/electronics-list.md",   "Electronics Categories We Carry",
     "We stock laptops, tablets, smart-home devices, audio equipment, and "
     "mobile accessories. Product availability varies by region."),
    ("policy/refunds.md",         "Refund Method & Timing",
     "Refunds are issued to the original payment method within 5-10 business "
     "days of receiving the returned item at our warehouse."),
    ("sku/LB-9047X.md",           "Product Detail: SKU LB-9047X Wireless Headphones",
     "The LB-9047X is our flagship over-ear wireless headphone. Active noise "
     "cancellation, 40 hours of battery, and a two-year extended warranty option."),
    ("policy/damaged.md",         "Damaged on Arrival",
     "If your package arrives damaged, photograph it before opening and contact "
     "support within 48 hours. We will arrange a replacement at no charge."),
    ("faq/payment.md",            "Accepted Payment Methods",
     "We accept major credit cards, ACH bank transfers, and store credit. "
     "Buy-now-pay-later options are available for orders over $200."),
    ("policy/exchange.md",        "Item Exchanges",
     "Exchanges for size or color are processed as a return + new order. "
     "Original shipping fees are refunded if the exchange is due to our error."),
]
print(f"✅ Corpus loaded: {len(CORPUS)} documents")
```

### Step 4b — Generate embeddings via Foundation Models API

```python
# Cell 6 — embed and insert
client = w.serving_endpoints.get_open_ai_client()

def embed(texts: list[str]) -> list[list[float]]:
    """Call the Foundation Models embedding endpoint. Batches of 16-32 are fine."""
    r = client.embeddings.create(
        model=TUTORIAL["embed_model"],
        input=texts,
    )
    return [d.embedding for d in r.data]

# Embed everything in one batch (10 docs is well under any rate limit)
texts = [content for _, _, content in CORPUS]
print(f"Embedding {len(texts)} documents with {TUTORIAL['embed_model']}...")
vectors = embed(texts)

# Sanity check the vectors
assert len(vectors) == len(CORPUS), "embedding count mismatch"
assert len(vectors[0]) == TUTORIAL["embed_dim"], (
    f"expected {TUTORIAL['embed_dim']}-dim vector, got {len(vectors[0])}"
)
print(f"✅ Got {len(vectors)} vectors of dimension {len(vectors[0])}")

# Insert in a single transaction (fine for 10 docs; for >1k use COPY)
with engine.begin() as conn:
    # Idempotency: clear the table first so re-runs don't double-insert
    conn.execute(text("TRUNCATE TABLE documents RESTART IDENTITY"))
    
    for (source, title, content), vec in zip(CORPUS, vectors):
        conn.execute(
            text("""
                INSERT INTO documents (source, title, content, embedding, metadata)
                VALUES (:s, :t, :c, :e, :m)
            """),
            {
                "s": source,
                "t": title,
                "c": content,
                "e": str(vec),                          # pgvector accepts the str repr
                "m": json.dumps({"lang": "en", "kind": source.split("/")[0]}),
            },
        )

with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM documents")).scalar()
print(f"✅ Inserted {n} documents with embeddings")
```

**Expected output:**

```
✅ Corpus loaded: 10 documents
Embedding 10 documents with databricks-bge-large-en...
✅ Got 10 vectors of dimension 1024
✅ Inserted 10 documents with embeddings
```

**What this proves:** the Foundation Models API call succeeded with your workspace identity (no API keys), the embedding dimension matches the schema, and `pgvector` accepted the inserts. The HNSW graph index is now populated incrementally — for 10 docs that's instant, for 100k docs you'd want to bulk-insert first and `REINDEX` once at the end.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `Endpoint 'databricks-bge-large-en' not found` | Foundation Models API not enabled in this region | Confirm via Workspace UI → Serving → Foundation Model API. If unavailable, use Mosaic AI Vector Search (covered in Module 5 theory 5.1) |
| `dimension mismatch ... got 1024 expected 384` | Wrong embedding model in `TUTORIAL` | Set `embed_model` to `databricks-bge-large-en` and `embed_dim=1024` |
| `vector dimensions 1024 do not match column type 768` | Schema in Cell 4 used `vector(768)` | Drop and recreate the table with `vector(1024)` |
| Insert hangs or rate-limit error | Too-large batch | The lab uses 10 docs; for production batch by 32 |

---

## Step 5 — Pure vector retrieval (and verify HNSW is being used)

Before we layer on BM25, prove the simple case works: top-k by cosine similarity, with the HNSW index actually serving the query (not a sequential scan). The `EXPLAIN` output is the receipt — if you don't see `Index Scan using idx_doc_hnsw`, your operator and opclass are mismatched (the silent trap from theory 5.2).

```python
# Cell 7 — pure vector top-k + EXPLAIN
QUESTION = "What is the warranty on opened electronics?"

# 1. Embed the question
qvec = embed([QUESTION])[0]

# 2. Pure vector top-3
print(f"Question: {QUESTION}\n")
print("─── Pure vector top-3 ───")
with engine.connect() as conn:
    rows = conn.execute(
        text("""
            SELECT id, title, embedding <=> CAST(:qv AS vector) AS distance
            FROM documents
            ORDER BY embedding <=> CAST(:qv AS vector)
            LIMIT 3
        """),
        {"qv": str(qvec)},
    ).fetchall()
for r in rows:
    print(f"   {r.distance:0.4f}  {r.title}")

# 3. EXPLAIN — confirm we're hitting the HNSW index, not seq-scanning
print("\n─── EXPLAIN ───")
with engine.connect() as conn:
    plan = conn.execute(
        text("""
            EXPLAIN (FORMAT TEXT, VERBOSE FALSE)
            SELECT id FROM documents
            ORDER BY embedding <=> CAST(:qv AS vector)
            LIMIT 3
        """),
        {"qv": str(qvec)},
    ).fetchall()
for line in plan:
    print(f"   {line[0]}")
```

**Expected output:**

```
Question: What is the warranty on opened electronics?

─── Pure vector top-3 ───
   0.1923  Warranty Terms
   0.2014  Return Window Policy
   0.2487  RMA Process for Defective Devices

─── EXPLAIN ───
   Limit  (cost=... rows=3 ...)
     ->  Index Scan using idx_doc_hnsw on documents  (cost=... rows=10 ...)
           Order By: (embedding <=> '[...]'::vector)
```

**What this proves:** the planner used `idx_doc_hnsw` (not `Seq Scan`), which means your `vector_cosine_ops` opclass and the `<=>` operator agree. If the EXPLAIN output shows `Seq Scan on documents`, **stop and fix it before continuing** — the rest of the lab will give correct results but at sequential-scan speed, which masks the performance lessons.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `EXPLAIN` shows `Seq Scan on documents` | Opclass mismatch | Drop and recreate the index with `vector_cosine_ops` (matches `<=>`) |
| `EXPLAIN` shows `Bitmap Heap Scan` | Filter or LIMIT preventing index use | Confirm there is no `WHERE` clause in the EXPLAIN query, only `ORDER BY` |
| Distances all `1.0` | Query vector is null or all-zeros | Re-run Cell 6 to regenerate; check `vectors[0]` is non-zero |
| `operator does not exist: vector <=> text` | Missing the `CAST(:qv AS vector)` | Keep the cast; SQLAlchemy passes parameters as text by default |

---

## Step 6 — Hybrid retrieval with Reciprocal Rank Fusion

This is the headline result of Module 5. We'll run a query where pure vector misses an obvious answer (a SKU lookup), then show RRF surfaces it correctly. The query is the exact pattern from Module 5 theory 5.3 — two CTEs, one for vector, one for BM25, fused by `1/(60+rank)`.

### Step 6a — The deliberately-bad-for-vector query

```python
# Cell 8 — pure vector misses the SKU
SKU_QUESTION = "Tell me about the LB-9047X product"
qv = embed([SKU_QUESTION])[0]

print(f"Question: {SKU_QUESTION}\n")
print("─── Pure vector top-3 ───")
with engine.connect() as conn:
    rows = conn.execute(
        text("""
            SELECT title, embedding <=> CAST(:qv AS vector) AS d
            FROM documents
            ORDER BY embedding <=> CAST(:qv AS vector)
            LIMIT 3
        """),
        {"qv": str(qv)},
    ).fetchall()
for r in rows:
    print(f"   {r.d:0.4f}  {r.title}")
```

**Expected output (the failure mode):**

```
Question: Tell me about the LB-9047X product

─── Pure vector top-3 ───
   0.34xx  Electronics Categories We Carry
   0.36xx  Product Detail: SKU LB-9047X Wireless Headphones
   0.39xx  Warranty Terms
```

The right answer (`Product Detail: SKU LB-9047X Wireless Headphones`) is *somewhere* in the top-3, but it isn't #1, even though the user gave us the exact identifier. The embedding model treats `LB-9047X` as a low-information token. **This is the failure mode RRF fixes.**

> **Note.** Because this is a tiny 10-doc corpus, pure vector may rank the right doc first by luck. On a production corpus with thousands of similar-looking docs, the SKU answer would routinely fall to #5 or worse. Either way, RRF gives a more robust ranking.

### Step 6b — Hybrid retrieval with RRF

```python
# Cell 9 — RRF combining vector and BM25
print("─── Hybrid (RRF) top-3 ───")
with engine.connect() as conn:
    rows = conn.execute(
        text("""
            WITH v AS (
              SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:qv AS vector)) AS rk
              FROM documents
              ORDER BY embedding <=> CAST(:qv AS vector)
              LIMIT 50
            ),
            k AS (
              SELECT id, ROW_NUMBER() OVER
                (ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC) AS rk
              FROM documents
              WHERE tsv @@ plainto_tsquery('english', :q)
              LIMIT 50
            )
            SELECT d.title,
                   COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + k.rk), 0) AS rrf,
                   v.rk AS vec_rank, k.rk AS bm25_rank
            FROM documents d
            LEFT JOIN v USING(id)
            LEFT JOIN k USING(id)
            WHERE v.id IS NOT NULL OR k.id IS NOT NULL
            ORDER BY rrf DESC
            LIMIT 3
        """),
        {"qv": str(qv), "q": SKU_QUESTION},
    ).fetchall()

print(f"   {'RRF':<8} {'VEC':<5} {'BM25':<5} TITLE")
for r in rows:
    vec = str(r.vec_rank) if r.vec_rank else "—"
    bm = str(r.bm25_rank) if r.bm25_rank else "—"
    print(f"   {r.rrf:0.5f}  {vec:<5} {bm:<5} {r.title}")
```

**Expected output:**

```
─── Hybrid (RRF) top-3 ───
   RRF       VEC   BM25  TITLE
   0.03252   2     1     Product Detail: SKU LB-9047X Wireless Headphones
   0.01613   1     —     Electronics Categories We Carry
   0.01587   3     —     Warranty Terms
```

**What this proves:** the SKU document is now ranked #1 because BM25 found the exact token `LB-9047X` (rank 1) and vector also had it in the top-5 (rank 2). The fused score `1/(60+2) + 1/(60+1) = 0.0325` beats the pure-vector winner. Two retrievers, opposite failure modes, fused into a more robust ranking.

### Step 6c — Filter pushdown — the pgvector advantage

The reason you'd choose pgvector over a dedicated vector service: **arbitrary SQL predicates that filter rows before HNSW runs**. Let's prove it.

```python
# Cell 10 — same query, but only return policy docs (uses the GIN index on metadata)
print("─── RRF restricted to metadata.kind='policy' ───")
with engine.connect() as conn:
    rows = conn.execute(
        text("""
            WITH v AS (
              SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:qv AS vector)) AS rk
              FROM documents
              WHERE metadata->>'kind' = 'policy'
              ORDER BY embedding <=> CAST(:qv AS vector)
              LIMIT 50
            ),
            k AS (
              SELECT id, ROW_NUMBER() OVER
                (ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC) AS rk
              FROM documents
              WHERE metadata->>'kind' = 'policy'
                AND tsv @@ plainto_tsquery('english', :q)
              LIMIT 50
            )
            SELECT d.title, d.source,
                   COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + k.rk), 0) AS rrf
            FROM documents d
            LEFT JOIN v USING(id)
            LEFT JOIN k USING(id)
            WHERE v.id IS NOT NULL OR k.id IS NOT NULL
            ORDER BY rrf DESC
            LIMIT 3
        """),
        {"qv": str(qv), "q": SKU_QUESTION},
    ).fetchall()
for r in rows:
    print(f"   {r.rrf:0.5f}  [{r.source}] {r.title}")
```

**Expected output:**

```
─── RRF restricted to metadata.kind='policy' ───
   0.01613  [policy/warranty.md]   Warranty Terms
   0.01587  [policy/returns.md]    Return Window Policy
   0.01563  [policy/rma.md]        RMA Process for Defective Devices
```

The SKU document is gone — it was tagged `kind='sku'`, not `kind='policy'`. The filter ran via the **GIN index on metadata** before HNSW even saw the rows. In a multi-tenant production system, this is exactly how you scope vector search to one tenant: a single `WHERE metadata->>'tenant_id' = :tid` clause.

**What this proves:** pgvector hybrid retrieval is just SQL. You compose vector similarity, BM25, and arbitrary SQL filters in a single plan, single transaction, single set of GRANTs. The dedicated vector service alternatives can't do arbitrary JOINs against your other Postgres tables.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| RRF ranking matches pure vector exactly | All BM25 ranks are NULL | Confirm `tsv` column is populated: `SELECT count(*) FROM documents WHERE tsv IS NOT NULL` |
| `function ts_rank does not exist` | Missing the `tsvector` arg | The lab uses `ts_rank(tsv, plainto_tsquery(...))` — check the column name |
| Cell 10 returns the SKU doc | Metadata not tagged as expected | `SELECT metadata FROM documents` — confirm Cell 6 inserted `{"kind":"sku"}` for the SKU row |

---

## Step 7 — End-to-end RAG function

Wrap everything into one Python function: take a question, embed it, run hybrid retrieval, build a context block, call the chat model with low temperature, return a grounded answer. About 40 lines.

```python
# Cell 11 — the rag() function
def rag(question: str, k: int = 3, temperature: float = 0.1) -> dict:
    """
    Retrieve top-k passages with hybrid RRF, then ask the chat model
    to answer ONLY from those passages. Returns dict with answer + citations.
    """
    qv = embed([question])[0]

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                WITH v AS (
                  SELECT id, ROW_NUMBER() OVER
                      (ORDER BY embedding <=> CAST(:qv AS vector)) AS rk
                  FROM documents
                  ORDER BY embedding <=> CAST(:qv AS vector)
                  LIMIT 50
                ),
                k AS (
                  SELECT id, ROW_NUMBER() OVER
                      (ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC) AS rk
                  FROM documents
                  WHERE tsv @@ plainto_tsquery('english', :q)
                  LIMIT 50
                )
                SELECT d.id, d.source, d.title, d.content,
                       COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + k.rk), 0) AS rrf
                FROM documents d
                LEFT JOIN v USING(id)
                LEFT JOIN k USING(id)
                WHERE v.id IS NOT NULL OR k.id IS NOT NULL
                ORDER BY rrf DESC
                LIMIT :k
            """),
            {"qv": str(qv), "q": question, "k": k},
        ).fetchall()

    # Build the context block from the top-k passages
    context = "\n\n".join(
        f"[Source: {r.source}]\n# {r.title}\n{r.content}" for r in rows
    )

    chat = client.chat.completions.create(
        model=TUTORIAL["chat_model"],
        temperature=temperature,
        messages=[
            {"role": "system",
             "content": (
                "You are a customer-support assistant. Answer the question using "
                "ONLY the context below. If the context does not contain the answer, "
                "reply 'I don't know based on the available documents.' "
                "Cite the source filename in square brackets when you use it."
             )},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ],
    )

    return {
        "question": question,
        "answer": chat.choices[0].message.content,
        "citations": [r.source for r in rows],
    }


# Smoke test — three questions hitting different parts of the corpus
for q in [
    "What is the return window for opened electronics?",
    "Tell me about the LB-9047X.",
    "How do I exchange an item for a different size?",
]:
    out = rag(q)
    print(f"\n▸ Q: {out['question']}")
    print(f"  A: {out['answer']}")
    print(f"  cited: {', '.join(out['citations'])}")
```

**Expected output:**

```
▸ Q: What is the return window for opened electronics?
  A: Opened electronics have a 14-day return window, while standard items
     can be returned within 30 days [policy/returns.md].
  cited: policy/returns.md, policy/warranty.md, policy/rma.md

▸ Q: Tell me about the LB-9047X.
  A: The LB-9047X is the flagship over-ear wireless headphone with active
     noise cancellation, 40 hours of battery life, and a two-year extended
     warranty option [sku/LB-9047X.md].
  cited: sku/LB-9047X.md, faq/electronics-list.md, policy/warranty.md

▸ Q: How do I exchange an item for a different size?
  A: Exchanges are processed as a return plus a new order. Original shipping
     fees are refunded if the exchange is due to our error [policy/exchange.md].
  cited: policy/exchange.md, policy/returns.md, policy/refunds.md
```

**What this proves:** you have a working, grounded, citation-aware RAG pipeline running entirely on Lakebase + Foundation Models. No Pinecone, no Weaviate, no separate retrieval service. The whole pipeline is one Postgres query and one chat call. You can demo this on a customer call.

**If it fails:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `Endpoint 'databricks-meta-llama-3-3-70b-instruct' not found` | Chat model not enabled in region | Use a different available model (e.g. `databricks-dbrx-instruct`); update `TUTORIAL["chat_model"]` |
| Answer says "I don't know" for the LB-9047X question | Step 6's RRF didn't surface the SKU doc | Re-run Cell 9; if RRF still misses, lower the `LIMIT` to 1 in the SKU CTE for testing |
| Answer hallucinates facts not in the context | Temperature too high | Drop `temperature` to 0.0; keep the system prompt strict |
| Citations look wrong | Top-k too small | Bump `k` to 5; the model sees more context but may dilute the answer |

---

## Step 8 — Final verification & cleanup

A pass/fail summary of everything you built. Cleanup is *optional* — Module 6 (agent memory) builds directly on this `documents` table.

### Step 8a — The summary checklist

```python
# Cell 12 — final checklist
checks = []

# 1. pgvector extension is installed
try:
    with engine.connect() as conn:
        v = conn.execute(text(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        )).scalar()
    checks.append(("pgvector extension", f"v{v}", v is not None))
except Exception as e:
    checks.append(("pgvector extension", str(e)[:30], False))

# 2. documents table + 3 indexes
try:
    with engine.connect() as conn:
        idx_count = conn.execute(text("""
            SELECT count(*) FROM pg_indexes
            WHERE tablename='documents'
              AND indexname IN ('idx_doc_hnsw','idx_doc_tsv','idx_doc_meta')
        """)).scalar()
    checks.append(("Schema + 3 indexes", f"{idx_count}/3 expected", idx_count == 3))
except Exception as e:
    checks.append(("Schema + 3 indexes", str(e)[:30], False))

# 3. corpus is loaded
try:
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM documents")).scalar()
        non_null = conn.execute(text(
            "SELECT count(*) FROM documents WHERE embedding IS NOT NULL"
        )).scalar()
    checks.append(("Corpus loaded + embedded", f"{non_null}/{n} embedded",
                   n >= 10 and non_null == n))
except Exception as e:
    checks.append(("Corpus loaded + embedded", str(e)[:30], False))

# 4. HNSW index is being used (not seq scan)
try:
    qv = embed(["test query"])[0]
    with engine.connect() as conn:
        plan = conn.execute(
            text("""EXPLAIN SELECT id FROM documents
                    ORDER BY embedding <=> CAST(:qv AS vector) LIMIT 3"""),
            {"qv": str(qv)},
        ).fetchall()
    plan_text = "\n".join(p[0] for p in plan)
    using_hnsw = "idx_doc_hnsw" in plan_text
    checks.append(("HNSW index is used", "Index Scan" if using_hnsw else "Seq Scan",
                   using_hnsw))
except Exception as e:
    checks.append(("HNSW index is used", str(e)[:30], False))

# 5. Hybrid RRF surfaces the SKU on the SKU question
try:
    qv = embed(["Tell me about the LB-9047X"])[0]
    with engine.connect() as conn:
        top = conn.execute(
            text("""
                WITH v AS (
                  SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:qv AS vector)) AS rk
                  FROM documents ORDER BY embedding <=> CAST(:qv AS vector) LIMIT 50
                ),
                k AS (
                  SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC) AS rk
                  FROM documents WHERE tsv @@ plainto_tsquery('english', :q) LIMIT 50
                )
                SELECT d.title FROM documents d
                LEFT JOIN v USING(id) LEFT JOIN k USING(id)
                WHERE v.id IS NOT NULL OR k.id IS NOT NULL
                ORDER BY COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + k.rk), 0) DESC
                LIMIT 1
            """),
            {"qv": str(qv), "q": "Tell me about the LB-9047X"},
        ).scalar()
    checks.append(("Hybrid RRF works", (top or "")[:35], "LB-9047X" in (top or "")))
except Exception as e:
    checks.append(("Hybrid RRF works", str(e)[:30], False))

# 6. RAG function returns a grounded answer
try:
    out = rag("What is the return window for opened electronics?")
    grounded = ("14" in out["answer"] or "fourteen" in out["answer"].lower())
    checks.append(("RAG grounded answer", "14-day" if grounded else "ungrounded", grounded))
except Exception as e:
    checks.append(("RAG grounded answer", str(e)[:30], False))

# Print
print("=" * 70)
print(f"{'CHECK':<28} {'DETAIL':<28} STATUS")
print("-" * 70)
for name, detail, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"{name:<28} {str(detail)[:28]:<28} {icon}")
print("=" * 70)
passed = sum(1 for _, _, ok in checks if ok)
total = len(checks)
if passed == total:
    print(f"\n🎯 ALL {total} CHECKS PASSED — Module 5 complete.")
else:
    print(f"\n⚠ {passed}/{total} passed. Resolve the ❌ rows before Module 6.")
```

**Expected output (the success case):**

```
======================================================================
CHECK                        DETAIL                       STATUS
----------------------------------------------------------------------
pgvector extension           v0.7.x                       ✅
Schema + 3 indexes           3/3 expected                 ✅
Corpus loaded + embedded     10/10 embedded               ✅
HNSW index is used           Index Scan                   ✅
Hybrid RRF works             Product Detail: SKU LB-9047 ✅
RAG grounded answer          14-day                       ✅
======================================================================

🎯 ALL 6 CHECKS PASSED — Module 5 complete.
```

### Step 8b — Cleanup (optional)

```python
# Cell 13 — cleanup
# Module 6 builds on the documents table — most readers leave CLEANUP=False.
CLEANUP = False

if CLEANUP:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS documents"))
    print("✅ Dropped 'documents' table. (pgvector extension and the project remain.)")
else:
    print("⏸ Cleanup skipped. Module 6 reuses 'documents' for agent memory.")
    print(f"   Project: {TUTORIAL['project_name']}")
    print(f"   Table:   documents (10 rows, HNSW + 2 GIN indexes)")
```

---

## Troubleshooting reference

If your final checklist has any ❌ rows, find the row in the table below and apply the fix. Don't proceed to Module 6 with failing checks — the agent-memory layer in Module 6 reuses this table.

| ❌ Row | Most common cause | Resolution path |
|---|---|---|
| **pgvector extension** | Step 2 was skipped or rolled back | Re-run Cell 3 |
| **Schema + 3 indexes** | DDL in Cell 4 had a syntax error or partial commit | Re-run Cell 4; check `SELECT * FROM pg_indexes WHERE tablename='documents'` |
| **Corpus loaded + embedded** | Cell 6 errored mid-insert | Re-run Cell 6 — `TRUNCATE` makes it idempotent |
| **HNSW index is used** | Wrong opclass or wrong operator | Drop the index, rebuild with `vector_cosine_ops`, query with `<=>` |
| **Hybrid RRF works** | `tsv` column isn't populated for the SKU row | Confirm Cell 4's DDL set `GENERATED ALWAYS AS … STORED`. If you removed STORED accidentally, drop and rebuild |
| **RAG grounded answer** | Chat model unavailable or temperature too high | Update `TUTORIAL["chat_model"]` to an available model; set temperature=0.0 |

---

## What you've accomplished

If your final cell printed `🎯 ALL 6 CHECKS PASSED`, you have done — for real, not in slides:

- ✅ Enabled `pgvector` on the Module 3 project (no new infrastructure)
- ✅ Built a `documents` schema with HNSW + 2× GIN indexes — every theory choice from 5.2 made real
- ✅ Generated 1024-dim embeddings via the Foundation Models API and bulk-inserted them
- ✅ Verified via `EXPLAIN` that HNSW (not sequential scan) is serving your queries
- ✅ Watched pure vector miss the SKU lookup, and watched RRF fix it
- ✅ Composed vector + BM25 + JSONB filter in one query plan — the pgvector advantage made concrete
- ✅ Wrapped it all in a `rag()` function answering grounded, cited questions

These six capabilities cover the entire RAG pipeline most customers ask about. The same `documents` table powers Module 6's agent memory layer, which adds episodic memory, tool-call logs, and short/long-term memory tiers on top of what you just built.

---

## Module 5 — complete

You finished three theory topics (5.1–5.3) and the hands-on lab. You now have:

- A live pgvector schema on the Module 3 Lakebase project, registered in UC, with HNSW + GIN indexes
- A working RRF hybrid retrieval query that you can paste into any customer demo
- A `rag()` function returning grounded, cited answers from a real corpus
- First-hand evidence that the HNSW index is being used (the `EXPLAIN` output)
- The intuition for *why* hybrid retrieval beats pure vector (the SKU corner case)

**Next up: Module 6** — *Lakebase as Memory Store for AI Agents.* You'll layer episodic memory, tool-call logs, and a four-tier memory model (working → short → long → semantic) onto the `documents` table you just built. Then you'll wire it into a real agent loop and watch it remember things across sessions.
