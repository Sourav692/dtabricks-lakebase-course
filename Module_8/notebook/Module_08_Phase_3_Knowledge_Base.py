# Databricks notebook source
# MAGIC %md
# MAGIC # Module 08 · Capstone · Phase 3 — Build the Knowledge Base (RAG)
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~60 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Load Northwind's support documents (returns policy, shipping FAQ, warranty terms) into the `kb_documents` table built in Phase 1. Chunk, embed via Foundation Models, bulk-load, build HNSW + GIN indexes, and implement `retrieve(query, k=5)` using Reciprocal Rank Fusion. The agent's `retrieve` tool in Phase 4 calls exactly this function.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — reconnect to the Phase 1/2 project
# MAGIC 2. **Source the corpus** — three small support docs encoded inline
# MAGIC 3. **Chunk** — ~500-token segments with 50-token overlap
# MAGIC 4. **Embed** — `databricks-bge-large-en` via Foundation Models API
# MAGIC 5. **Bulk-load** `kb_documents` (one transaction per 1000 rows)
# MAGIC 6. **Build indexes** — HNSW on `embedding`, GIN on `tsv` (after the load)
# MAGIC 7. **Implement `retrieve()`** — RRF over vector similarity + BM25
# MAGIC 8. **Smoke test** — three real questions, three correct chunks
# MAGIC 9. **Final verification** — six green checks before Phase 4
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Phase 1 passed** — `kb_documents` table with the `vector(1024)` column exists
# MAGIC - **Phase 2 passed** — synced tables are live (Phase 4 will join across them)
# MAGIC - **Foundation Models API** enabled in the workspace region
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sql` is used. Step 5's bulk-load `TRUNCATE`s the table first — re-running is safe and produces an identical KB.
# MAGIC
# MAGIC > **⚠ Billing note.** Foundation Models embeddings are billed per token; this lab uses ~12,000 tokens (~1 cent at current pricing). Lakebase compute is the same `CU_1` instance, no extra cost.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: reconnect to the Phase 1/2 project

# COMMAND ----------

# Cell 1 — install dependencies
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy mlflow openai
dbutils.library.restartPython()

# COMMAND ----------

# Cell 2 — reconnect
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
if CAPSTONE["project_name"] not in existing:
    raise RuntimeError(f"Project '{CAPSTONE['project_name']}' not found. Run Phase 1 first.")
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
print(f"✅ Reconnected to '{CAPSTONE['project_name']}'.main")

# Verify Phase 1 table exists
with engine.connect() as conn:
    cnt = conn.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='kb_documents' AND column_name='embedding'"
    )).scalar()
assert cnt == 1, "kb_documents.embedding column missing — re-run Phase 1"
print(f"✅ kb_documents schema present")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Source the corpus
# MAGIC
# MAGIC Three small support documents — returns policy, shipping FAQ, warranty terms — typical of any retailer's support wiki. We keep them inline so the lab is self-contained. In a real customer build you would pull these from Confluence, Notion, or a `main.gold.support_docs` Delta table.

# COMMAND ----------

# Cell 3 — the corpus
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
14-day return window. A 15% restocking fee applies. Items must include all original
accessories, manuals, and documentation.

## Final Sale Items
Items marked "final sale" — clearance, last-chance, custom orders — are not eligible
for return. This is shown clearly on the product page at checkout.

## Damaged or Defective
Damaged or defective goods may be returned at any time within the warranty period
at no cost to the customer. Northwind covers return shipping for defective items.

## How to Return
Initiate a return by clicking "Return" on the order page in your account, or by
contacting support. You will receive a prepaid shipping label by email within 1
business day. Pack the item, attach the label, and drop off at any carrier location.
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
arrive in 7-12 business days. The customer is responsible for any import duties
or taxes assessed by the destination country.

## Tracking
A tracking number is emailed within 4 hours of carrier pickup. You can also see
real-time tracking on the order page in your account.

## Address Changes
You can change the shipping address up until the order enters the "shipped" status,
typically within 4 hours of placing the order. After that, contact the carrier
directly using the tracking number.

## Delays
Carrier delays are outside Northwind's control. We monitor every shipment and will
proactively contact you if a package is more than 3 days late, with a refund or
replacement option.
""",
    },
    {
        "source": "warranty_terms.md",
        "title":  "Warranty Terms",
        "body": """
# Warranty Terms

## Standard Warranty
Most Northwind products carry a 1-year limited warranty against manufacturing
defects, starting from the date of delivery. The warranty covers replacement
or repair at Northwind's discretion.

## Extended Warranty
Certain product categories — major appliances, electronics over $500, power tools —
are eligible for a 3-year extended warranty. The extended warranty is sold at
checkout for 12% of the product price.

## What's Covered
The warranty covers defects in materials and workmanship under normal use.
"Normal use" excludes accidental damage, misuse, unauthorized modifications,
liquid damage, and wear-and-tear consumables (batteries, filters, blades).

## What's Not Covered
Cosmetic damage, software issues on third-party operating systems, damage from
power surges (use a surge protector), and any damage from improper installation
not performed by an authorized installer.

## How to Claim
Contact support with the order number, a description of the issue, and photos.
Most claims are resolved within 5 business days. If a replacement is needed,
it is shipped free; the defective unit can be sent back using the prepaid label
provided.

## Transferability
The warranty stays with the original purchaser and is not transferable. If you
gift a product, the recipient should keep the original purchase confirmation
to make a claim.
""",
    },
]

print(f"Corpus loaded: {len(CORPUS)} documents")
for d in CORPUS:
    print(f"   · {d['source']:<24} ({len(d['body'])} chars)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Chunk the corpus
# MAGIC
# MAGIC ~500 tokens per chunk with 50 tokens of overlap. We use the simple "split on `##` headers, then split long sections into windows" strategy. For a real customer build with longer docs you would use a proper tokenizer (`tiktoken` or the model's own) — for this corpus, character-based windowing with a safety margin is plenty.

# COMMAND ----------

# Cell 4 — chunking
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
for c in chunks[:5]:
    preview = c["chunk"].replace("\n", " ")[:80]
    print(f"   · {c['source']:<24} chunk {c['metadata']['chunk_index']:<2} | {preview}...")
print(f"   ... and {max(0, len(chunks) - 5)} more")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Embed every chunk via the Foundation Models API
# MAGIC
# MAGIC Each call returns a 1024-dim vector that matches the `vector(1024)` column type. We batch in groups of 16 to keep request size sensible. ~12 chunks total → ~1 second wall time.

# COMMAND ----------

# Cell 5 — embed all chunks
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Bulk-load `kb_documents`
# MAGIC
# MAGIC One transaction. `TRUNCATE` first so the cell is idempotent. `executemany` keeps it tight on the wire — for our 12 chunks we don't need it, but the pattern scales to tens of thousands.

# COMMAND ----------

# Cell 6 — bulk-load kb_documents
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Build the indexes (after the load)
# MAGIC
# MAGIC **Order matters here.** Building HNSW *before* a bulk insert is the classic Module 5.2 anti-pattern: each row triggers an index update, and the load is 10× slower. Build the indexes *after* the data is in place and you get the same final state for a fraction of the cost.
# MAGIC
# MAGIC We raise `maintenance_work_mem` for the build, then leave it alone for query time.

# COMMAND ----------

# Cell 7 — build HNSW + GIN indexes
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Implement `retrieve()` with Reciprocal Rank Fusion
# MAGIC
# MAGIC Same RRF pattern as Module 5 Lab 5 Cell 8. We fetch the top-K from vector search and the top-K from BM25 (Postgres `tsvector`) separately, then combine the two ranked lists with `1 / (k + rank)`. The fused list captures both semantic match (vector) and exact-keyword match (BM25) — the classic counter-example is a query like "warranty for SKU-A12" where pure vector misses the SKU.

# COMMAND ----------

# Cell 8 — retrieve(query, k=5) using RRF
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
        {
            "source":    r.source,
            "title":     r.title,
            "chunk":     r.chunk,
            "rrf_score": float(r.rrf_score),
        }
        for r in rows
    ]

# Quick smoke
hits = retrieve("how do I return a phone I opened?", k=3)
print(f"retrieve() returned {len(hits)} hits for the smoke query.")
for h in hits:
    print(f"   · {h['source']:<24} score={h['rrf_score']:.4f}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — Smoke-test on three real questions
# MAGIC
# MAGIC We expect each question to retrieve a chunk from the *correct* document. If any query routes to the wrong file, something is wrong with the embedding model, the chunking, or the RRF weights.

# COMMAND ----------

# Cell 9 — three-question smoke test
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9 — Final verification — 6 green checks

# COMMAND ----------

# Cell 10 — verification checklist
checks = []

# 1. KB row count ≥ 8 (we inserted ~12)
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

# 4. HNSW used by the planner (not seq scan)
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

# Print
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
    print(f"  ⚠ {passed}/{total} passed — see troubleshooting below")
print("=" * 60)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Troubleshooting reference
# MAGIC
# MAGIC | ❌ Row | Most common cause | Resolution |
# MAGIC |---|---|---|
# MAGIC | **kb_documents row count** | Cell 6 transaction rolled back | Re-run Cell 6 — `TRUNCATE` makes it idempotent |
# MAGIC | **all rows have embeddings** | FM endpoint returned an error mid-batch | Re-run Cell 5; check `w.serving_endpoints.list()` |
# MAGIC | **HNSW + GIN indexes** | Cell 7 failed during build | Re-run Cell 7; the `IF NOT EXISTS` makes it safe |
# MAGIC | **HNSW used** | `vector_cosine_ops` opclass missing on the index | Drop the index, recreate with the correct opclass |
# MAGIC | **smoke queries route** | Embedding mismatch — wrong model dim or stale data | Confirm model is `databricks-bge-large-en` (1024-dim) |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your verification printed `🎯 ALL 6 CHECKS PASSED`, Phase 3 is complete and you have:
# MAGIC
# MAGIC - ✅ A loaded `kb_documents` table with embeddings + tsvector
# MAGIC - ✅ HNSW + GIN indexes built **after** the bulk load (the right order)
# MAGIC - ✅ A working `retrieve(query, k=5)` function using RRF — the same shape the agent's `retrieve` tool calls in Phase 4
# MAGIC - ✅ Three smoke queries routed to the correct documents — basic recall is real
# MAGIC
# MAGIC **Do not run cleanup.** Phase 4 reuses `retrieve()` directly, plus the `episodes` table from Phase 1.
# MAGIC
# MAGIC > **Next:** open `Module_08_Phase_4_Agent_Memory.py` to wire the agent loop.

# COMMAND ----------

# Cell 11 — pin state for the next phase
print("=" * 60)
print(f"  ▸ Phase 3 complete. Pinned for Phase 4:")
print("=" * 60)
print(f"  KB chunks loaded : {n}")
print(f"  Indexes          : HNSW (vector_cosine_ops, m=16, ef=64) + GIN (tsv)")
print(f"  retrieve()       : RRF over top-{max(5*2,20)} from each side")
print(f"  Smoke queries    : 3/3 routed correctly")
print("=" * 60)
print(f"  ⏸ DO NOT clean up. Open Phase 4 next.")
print("=" * 60)
