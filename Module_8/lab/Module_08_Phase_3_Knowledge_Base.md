# Module 08 · Capstone · Phase 3 — Build the Knowledge Base (RAG)

> **Type:** Hands-on · **Duration:** ~60 minutes · **Format:** Databricks notebook
> **Purpose:** Load Northwind's support documents (returns policy, shipping FAQ, warranty terms) into the `kb_documents` table built in Phase 1. Chunk, embed via Foundation Models, bulk-load, build HNSW + GIN indexes, and implement `retrieve(query, k=5)` using Reciprocal Rank Fusion. The agent's `retrieve` tool in Phase 4 calls exactly this function.

---

## Why this phase exists

A support agent without a knowledge base is just an order-lookup screen. The interesting questions — *"can a customer return an opened laptop after 18 days?"*, *"does the warranty cover liquid damage?"* — are policy questions, not data questions. They live in the support wiki, not the orders table.

This phase turns Lakebase into the vector store for those documents:

- Chunk a small corpus of three policy docs into ~500-token segments
- Embed each chunk with `databricks-bge-large-en` (1024-dim) — same model as Module 5
- Bulk-load `kb_documents` (the table with the `vector(1024)` column from Phase 1)
- Build HNSW + GIN indexes **after** the load (avoiding the Module 5.2 anti-pattern)
- Implement `retrieve()` with Reciprocal Rank Fusion across vector + BM25
- Smoke-test that three real questions route to the correct documents

This phase walks nine steps:

1. **Setup** — reconnect to the Phase 1/2 project
2. **Source the corpus** — three small support docs encoded inline
3. **Chunk** — ~500-token segments with 50-token overlap
4. **Embed** — `databricks-bge-large-en` via Foundation Models API
5. **Bulk-load** `kb_documents`
6. **Build indexes** — HNSW + GIN, after the load
7. **Implement `retrieve()`** — RRF over vector + BM25
8. **Smoke-test** — three real questions, three correct chunks
9. **Final verification** — six green checks before Phase 4

> **Run mode.** Top-to-bottom. Cells are Python except where `%sql` is used. Step 5's bulk-load `TRUNCATE`s the table first — re-running is safe and produces an identical KB.

---

## Prerequisites before you start

You need:

- ✅ **Phase 1 passed** — `kb_documents` table with the `vector(1024)` column exists
- ✅ **Phase 2 passed** — synced tables are live (Phase 4 will join across them)
- ✅ **Foundation Models API** enabled in the workspace region
- An attached compute cluster (serverless is fine, recommended)

> **⚠ Billing note.** Foundation Models embeddings are billed per token; this lab uses ~12,000 tokens (~1 cent at current pricing). Lakebase compute is the same `CU_1` instance, no extra cost.

---

## Step 1 — Setup: reconnect to the Phase 1/2 project

### Cell 1 — install dependencies

```python
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy mlflow openai
dbutils.library.restartPython()
```

### Cell 2 — reconnect

```python
import os, uuid, time
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text

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

engine = make_engine()

# Verify Phase 1 schema
with engine.connect() as conn:
    cnt = conn.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='kb_documents' AND column_name='embedding'"
    )).scalar()
assert cnt == 1, "kb_documents.embedding column missing — re-run Phase 1"
print(f"✅ kb_documents schema present")
```

---

## Step 2 — Source the corpus

Three small support documents — returns policy, shipping FAQ, warranty terms — typical of any retailer's support wiki. We keep them inline so the lab is self-contained. In a real customer build you would pull these from Confluence, Notion, or a `main.gold.support_docs` Delta table.

### Cell 3 — the corpus

```python
CORPUS = [
    {
        "source": "returns_policy_v2.md",
        "title":  "Northwind Returns Policy",
        "body": """
# Northwind Returns Policy

## Standard Returns
Customers may return most items within 30 days of delivery for a full refund.
The item must be unopened, in original packaging, and accompanied by proof of purchase.
Refunds are issued to the original payment method within 5 business days of receipt.

## Opened Electronics
Opened consumer electronics — phones, tablets, laptops, headphones — have a reduced
14-day return window. A 15% restocking fee applies.

## Final Sale Items
Items marked "final sale" — clearance, last-chance, custom orders — are not eligible
for return. This is shown clearly on the product page at checkout.

## Damaged or Defective
Damaged or defective goods may be returned at any time within the warranty period
at no cost to the customer. Northwind covers return shipping for defective items.

## How to Return
Initiate a return by clicking "Return" on the order page in your account.
""",
    },
    {
        "source": "shipping_faq.md",
        "title":  "Shipping & Delivery FAQ",
        "body": """
# Shipping & Delivery — FAQ

## Standard Shipping
Free standard shipping is available on all orders over $50 within the continental US.
Standard delivery is 3-5 business days from order confirmation.

## Express Shipping
Express shipping (1-2 business days) is available at checkout for $14.99 flat rate.
Orders placed before 2 PM ET ship same-day on business days.

## International Shipping
We ship to Canada, the UK, the EU, and Australia. International orders typically
arrive in 7-12 business days.

## Tracking
A tracking number is emailed within 4 hours of carrier pickup.

## Address Changes
You can change the shipping address up until the order enters the "shipped" status.

## Delays
Carrier delays are outside Northwind's control. We monitor every shipment and will
proactively contact you if a package is more than 3 days late.
""",
    },
    {
        "source": "warranty_terms.md",
        "title":  "Warranty Terms",
        "body": """
# Warranty Terms

## Standard Warranty
Most Northwind products carry a 1-year limited warranty against manufacturing
defects, starting from the date of delivery.

## Extended Warranty
Certain product categories — major appliances, electronics over $500, power tools —
are eligible for a 3-year extended warranty sold at checkout for 12% of price.

## What's Covered
Defects in materials and workmanship under normal use. "Normal use" excludes
accidental damage, misuse, unauthorized modifications, liquid damage, and
wear-and-tear consumables.

## What's Not Covered
Cosmetic damage, software issues on third-party operating systems, damage from
power surges, and any damage from improper installation.

## How to Claim
Contact support with the order number, a description of the issue, and photos.
Most claims are resolved within 5 business days.

## Transferability
The warranty stays with the original purchaser and is not transferable.
""",
    },
]

print(f"Corpus loaded: {len(CORPUS)} documents")
for d in CORPUS:
    print(f"   · {d['source']:<24} ({len(d['body'])} chars)")
```

---

## Step 3 — Chunk the corpus

~500 tokens per chunk with 50 tokens of overlap. We use the simple "split on `##` headers, then split long sections into windows" strategy. For a real customer build with longer docs you would use a proper tokenizer (`tiktoken` or the model's own) — for this corpus, character-based windowing with a safety margin is plenty.

### Cell 4 — chunking

```python
def chunk_text(body: str, max_chars: int = 1800, overlap: int = 200):
    """Split on ## headers; if a section is still long, window it with overlap."""
    sections, cur = [], []
    for line in body.strip().splitlines():
        if line.startswith("## ") and cur:
            sections.append("\n".join(cur).strip())
            cur = []
        cur.append(line)
    if cur:
        sections.append("\n".join(cur).strip())

    out = []
    for sec in sections:
        if len(sec) <= max_chars:
            out.append(sec)
        else:
            i = 0
            while i < len(sec):
                out.append(sec[i:i + max_chars])
                i += max_chars - overlap
    return [c for c in out if c.strip()]

chunks = []
for doc in CORPUS:
    for i, ch in enumerate(chunk_text(doc["body"])):
        chunks.append({
            "source": doc["source"],
            "title":  doc["title"],
            "chunk":  ch,
            "metadata": {"chunk_index": i, "n_chars": len(ch)},
        })

print(f"Chunked corpus: {len(chunks)} chunks")
```

---

## Step 4 — Embed every chunk via the Foundation Models API

Each call returns a 1024-dim vector that matches the `vector(1024)` column type. We batch in groups of 16 to keep request size sensible. ~12 chunks total → ~1 second wall time.

### Cell 5 — embed all chunks

```python
from openai import OpenAI

# The Foundation Models API exposes an OpenAI-compatible endpoint
fm_client = OpenAI(
    api_key=w.config.token,
    base_url=f"{w.config.host}/serving-endpoints",
)

def embed_batch(texts):
    out = fm_client.embeddings.create(
        model=CAPSTONE["embed_model"],
        input=texts,
    )
    return [d.embedding for d in out.data]

batch_size = 16
embedded = []
t0 = time.time()
for i in range(0, len(chunks), batch_size):
    batch = chunks[i:i + batch_size]
    vecs = embed_batch([c["chunk"] for c in batch])
    for c, v in zip(batch, vecs):
        c["embedding"] = v
        embedded.append(c)

print(f"✅ Embedded {len(embedded)} chunks in {time.time()-t0:.1f}s")
print(f"   Vector dim: {len(embedded[0]['embedding'])}  (expected 1024)")
assert len(embedded[0]["embedding"]) == 1024
```

### If it fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `model not found` | FM endpoint not enabled in this region | `w.serving_endpoints.list()` to confirm; switch model |
| 429 rate limit | Too-large batch | Reduce `batch_size` to 8 |
| Vector dim ≠ 1024 | Wrong model | Use `databricks-bge-large-en` (1024-dim); column is fixed |

---

## Step 5 — Bulk-load `kb_documents`

One transaction. `TRUNCATE` first so the cell is idempotent. `executemany` keeps it tight on the wire — for our 12 chunks we don't need it, but the pattern scales to tens of thousands.

### Cell 6 — bulk-load

```python
import json

INSERT = text("""
    INSERT INTO kb_documents (source, title, chunk, embedding, metadata)
    VALUES (:source, :title, :chunk, CAST(:embedding AS vector), CAST(:metadata AS jsonb))
""")

with engine.begin() as conn:
    conn.execute(text("TRUNCATE kb_documents"))
    for c in embedded:
        conn.execute(INSERT, {
            "source":    c["source"],
            "title":     c["title"],
            "chunk":     c["chunk"],
            "embedding": str(c["embedding"]),
            "metadata":  json.dumps(c["metadata"]),
        })

with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM kb_documents")).scalar()
print(f"✅ Loaded {n} chunks into kb_documents")
```

---

## Step 6 — Build the indexes (after the load)

**Order matters here.** Building HNSW *before* a bulk insert is the classic Module 5.2 anti-pattern: each row triggers an index update, and the load is 10× slower. Build the indexes *after* the data is in place and you get the same final state for a fraction of the cost.

We raise `maintenance_work_mem` for the build, then leave it alone for query time.

### Cell 7 — build HNSW + GIN

```python
INDEX_DDL = """
SET maintenance_work_mem = '256MB';

CREATE INDEX IF NOT EXISTS kb_documents_embedding_hnsw
    ON kb_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS kb_documents_tsv_gin
    ON kb_documents USING GIN (tsv);

RESET maintenance_work_mem;
"""
with engine.begin() as conn:
    for stmt in INDEX_DDL.split(";"):
        if stmt.strip():
            conn.execute(text(stmt))

with engine.connect() as conn:
    idx = conn.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE tablename='kb_documents'
        ORDER BY indexname
    """)).scalars().all()

print("Indexes on kb_documents:")
for i in idx:
    print(f"   · {i}")
```

---

## Step 7 — Implement `retrieve()` with Reciprocal Rank Fusion

Same RRF pattern as Module 5 Lab 5 Cell 8. We fetch the top-K from vector search and the top-K from BM25 (Postgres `tsvector`) separately, then combine the two ranked lists with `1 / (k + rank)`. The fused list captures both semantic match (vector) and exact-keyword match (BM25) — the classic counter-example is a query like *"warranty for SKU-A12"* where pure vector misses the SKU.

### Cell 8 — retrieve(query, k=5) using RRF

```python
RRF_K = 60  # standard RRF constant; not the same as `k` (top-K)

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
    FROM (
        SELECT doc_id, source, title, chunk, rank FROM vec
        UNION ALL
        SELECT doc_id, source, title, chunk, rank FROM bm25
    ) u
    GROUP BY doc_id, source, title, chunk
)
SELECT source, title, chunk, rrf_score
FROM fused
ORDER BY rrf_score DESC
LIMIT :k
""")

def retrieve(query: str, k: int = 5):
    """Hybrid (vector + BM25) RRF retrieval. Returns top-k chunks."""
    qv = embed_batch([query])[0]
    with engine.connect() as conn:
        rows = conn.execute(RETRIEVE_SQL, {
            "emb":    str(qv),
            "q":      query,
            "k":      k,
            "k_each": max(k * 2, 20),
            "rrf_k":  RRF_K,
        }).fetchall()
    return [
        {"source": r.source, "title": r.title,
         "chunk":  r.chunk,  "rrf_score": float(r.rrf_score)}
        for r in rows
    ]

# Quick smoke
hits = retrieve("how do I return a phone I opened?", k=3)
print(f"retrieve() returned {len(hits)} hits for the smoke query.")
for h in hits:
    print(f"   · {h['source']:<24} score={h['rrf_score']:.4f}")
```

---

## Step 8 — Smoke-test on three real questions

We expect each question to retrieve a chunk from the *correct* document. If any query routes to the wrong file, something is wrong with the embedding model, the chunking, or the RRF weights.

### Cell 9 — three-question smoke test

```python
SMOKE_QUERIES = [
    ("What is the return window for opened electronics?", "returns_policy_v2.md"),
    ("How long does international shipping take?",        "shipping_faq.md"),
    ("Does the warranty cover liquid damage?",            "warranty_terms.md"),
]

print(f"{'Query':<55} {'Top hit':<26} {'OK?'}")
print("-" * 95)
all_ok = True
for q, expected in SMOKE_QUERIES:
    hits = retrieve(q, k=1)
    top  = hits[0]["source"] if hits else "(no hits)"
    ok   = (top == expected)
    if not ok:
        all_ok = False
    print(f"{q[:54]:<55} {top:<26} {'✅' if ok else '❌'}")

print("-" * 95)
print(f"\n{'✅ ALL SMOKE QUERIES ROUTED CORRECTLY' if all_ok else '⚠ At least one query mis-routed'}")
```

---

## Step 9 — Final verification — six green checks

### Cell 10 — verification checklist

```python
checks = []

# 1. KB row count ≥ 8
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM kb_documents")).scalar()
checks.append((f"kb_documents row count ({n})", n >= 8))

# 2. Embeddings populated
with engine.connect() as conn:
    null_emb = conn.execute(text(
        "SELECT count(*) FROM kb_documents WHERE embedding IS NULL"
    )).scalar()
checks.append((f"all rows have embeddings (nulls={null_emb})", null_emb == 0))

# 3. Both indexes present
with engine.connect() as conn:
    idx = set(conn.execute(text("""
        SELECT indexname FROM pg_indexes WHERE tablename='kb_documents'
    """)).scalars().all())
need = {"kb_documents_embedding_hnsw", "kb_documents_tsv_gin"}
checks.append((f"HNSW + GIN indexes ({len(need & idx)}/{len(need)})", need.issubset(idx)))

# 4. HNSW used by the planner
with engine.connect() as conn:
    qv = embed_batch(["sample query for explain"])[0]
    plan = conn.execute(text(f"""
        EXPLAIN
        SELECT doc_id FROM kb_documents
        ORDER BY embedding <=> CAST('{qv}' AS vector)
        LIMIT 5
    """)).scalars().all()
plan_text = "\n".join(plan).lower()
checks.append((f"HNSW used (not seq scan)", "hnsw" in plan_text or "index" in plan_text))

# 5. retrieve() returns hits
hits = retrieve("how do returns work?", k=3)
checks.append((f"retrieve() returns ≥1 hit ({len(hits)})", len(hits) >= 1))

# 6. All 3 smoke queries route correctly
checks.append(("3/3 smoke queries route correctly", all_ok))

print("=" * 60)
print(f"  PHASE 3 VERIFICATION — knowledge base + RRF")
print("=" * 60)
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")

passed = sum(1 for _, ok in checks if ok)
total  = len(checks)
print("=" * 60)
if passed == total:
    print(f"  🎯 ALL {total} CHECKS PASSED — proceed to Phase 4")
else:
    print(f"  ⚠ {passed}/{total} passed")
print("=" * 60)
```

### Troubleshooting reference

| ❌ Row | Most common cause | Resolution |
|---|---|---|
| kb_documents row count | Cell 6 transaction rolled back | Re-run Cell 6 — `TRUNCATE` makes it idempotent |
| All rows have embeddings | FM endpoint returned an error mid-batch | Re-run Cell 5; check `w.serving_endpoints.list()` |
| HNSW + GIN indexes | Cell 7 failed during build | Re-run Cell 7; the `IF NOT EXISTS` makes it safe |
| HNSW used | `vector_cosine_ops` opclass missing | Drop the index, recreate with the correct opclass |
| Smoke queries route | Embedding mismatch — wrong model dim | Confirm `databricks-bge-large-en` (1024-dim) |

---

## What you've accomplished

If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 3 is complete and you have:

- ✅ A loaded `kb_documents` table with embeddings + tsvector
- ✅ HNSW + GIN indexes built **after** the bulk load (the right order)
- ✅ A working `retrieve(query, k=5)` function using RRF — the same shape the agent's `retrieve` tool calls in Phase 4
- ✅ Three smoke queries routed to the correct documents — basic recall is real

**Do not run cleanup.** Phase 4 reuses `retrieve()` directly, plus the `episodes` table from Phase 1.

> **Next:** open `Module_08_Phase_4_Agent_Memory.py` to wire the agent loop.
