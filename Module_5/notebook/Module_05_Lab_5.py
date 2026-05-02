# Databricks notebook source
# MAGIC %md
# MAGIC # Module 05 · Lab 5 — Lakebase as a Vector Store, End-to-End
# MAGIC
# MAGIC > **Type:** Hands-on · **Duration:** ~90 minutes · **Format:** Databricks notebook
# MAGIC > **Purpose:** Layer pgvector onto the Lakebase project you built in Module 3, build a real `documents` table with HNSW + GIN indexes, generate embeddings via Foundation Models, run hybrid retrieval with Reciprocal Rank Fusion, and wrap the whole thing in a working RAG function.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### What this notebook does
# MAGIC
# MAGIC 1. **Setup** — reuse the Module 3 project, install pgvector deps, open an engine
# MAGIC 2. **Enable** the `vector` extension and verify
# MAGIC 3. **Build** the `documents` schema with HNSW + GIN indexes
# MAGIC 4. **Embed & load** a corpus via the Foundation Models API
# MAGIC 5. **Pure vector** retrieval — confirm HNSW via `EXPLAIN`
# MAGIC 6. **Hybrid retrieval** with Reciprocal Rank Fusion
# MAGIC 7. **End-to-end RAG** — a 40-line `rag()` function
# MAGIC 8. **Final verification & cleanup**
# MAGIC
# MAGIC ### Prerequisites
# MAGIC
# MAGIC - **Module 3 Lab 3 passed** with all 6 checks green and `CLEANUP=False`
# MAGIC - **Foundation Models API** enabled in the workspace region
# MAGIC - An attached compute cluster (serverless recommended; DBR 14+)
# MAGIC
# MAGIC ### Run mode
# MAGIC
# MAGIC Top-to-bottom. Cells are Python except where `%sql` is used. Re-running individual cells is safe — Steps 2, 3, 4 are written to be idempotent.
# MAGIC
# MAGIC > **⚠ Billing note.** Foundation Models embeddings are billed per token; this lab uses ~5,000 tokens (well under one cent). Lakebase compute is the bigger cost and was already running from Module 3. Step 8b leaves the project alive so Module 6 can reuse it.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Setup: reuse the Module 3 project
# MAGIC
# MAGIC You are not provisioning a new project. You are picking up the same `lakebase-tutorial` project from Module 3 and opening a fresh engine on it.
# MAGIC
# MAGIC | Symptom on failure | Likely cause | Fix |
# MAGIC |---|---|---|
# MAGIC | `Project 'lakebase-tutorial' not found` | Module 3 cleanup ran | Run Module 3 Lab 3 again with `CLEANUP=False` |
# MAGIC | `password authentication failed` | Token expired | Re-run Cell 2; `generate_database_credential` issues a fresh token |
# MAGIC | `connection refused` | Project went idle | Wait 10–15s; Lakebase auto-resumes on first connection |

# COMMAND ----------

# Cell 1 — install dependencies (re-run if the kernel restarted)
%pip install --quiet "databricks-sdk>=0.30.0" "psycopg[binary]" sqlalchemy pgvector
dbutils.library.restartPython()

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Enable the `vector` extension
# MAGIC
# MAGIC `pgvector` ships pre-installed on Lakebase Autoscaling but must be enabled per-database. Single command, idempotent, finishes in milliseconds.

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Build the `documents` schema
# MAGIC
# MAGIC The schema from Module 5 theory 5.2 with all three indexes: HNSW for similarity, GIN on the generated tsvector for BM25, and GIN on JSONB metadata for filter pushdown.

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Embed and bulk-load the corpus
# MAGIC
# MAGIC We use the Foundation Models API via the SDK's OpenAI-compatible client. The corpus is small enough that the lab finishes fast, large enough to demonstrate hybrid retrieval — note the SKU `LB-9047X` document, which Step 6 uses to prove vector-only retrieval misses exact-match queries.

# COMMAND ----------

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

# COMMAND ----------

# Cell 6 — embed and insert
client = w.serving_endpoints.get_open_ai_client()

def embed(texts):
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Pure vector retrieval (and verify HNSW is being used)
# MAGIC
# MAGIC Before layering on BM25, prove the simple case works: top-k by cosine similarity, with the HNSW index actually serving the query (not a sequential scan). The `EXPLAIN` output is the receipt — if you do not see `Index Scan using idx_doc_hnsw`, your operator and opclass are mismatched.

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Hybrid retrieval with Reciprocal Rank Fusion
# MAGIC
# MAGIC The headline result of Module 5. Run a query where pure vector misses an obvious answer (a SKU lookup), then show RRF surfaces it correctly. The query is the dual-CTE pattern from theory 5.3.

# COMMAND ----------

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

# COMMAND ----------

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

# COMMAND ----------

# Cell 10 — same query, but only return policy docs (filter pushdown via GIN on metadata)
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — End-to-end RAG function
# MAGIC
# MAGIC Wrap everything into one Python function: take a question, embed it, run hybrid retrieval, build a context block, call the chat model with low temperature, return a grounded answer. About 40 lines.

# COMMAND ----------

# Cell 11 — the rag() function
def rag(question, k=3, temperature=0.1):
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — Final verification & cleanup
# MAGIC
# MAGIC Pass/fail summary of everything built. Cleanup is *optional* — Module 6 (agent memory) builds directly on this `documents` table.

# COMMAND ----------

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

# COMMAND ----------

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

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC
# MAGIC ## What you've accomplished
# MAGIC
# MAGIC If your final cell printed `🎯 ALL 6 CHECKS PASSED`, you have done — for real, not in slides:
# MAGIC
# MAGIC - ✅ Enabled `pgvector` on the Module 3 project (no new infrastructure)
# MAGIC - ✅ Built a `documents` schema with HNSW + 2× GIN indexes
# MAGIC - ✅ Generated 1024-dim embeddings via the Foundation Models API and bulk-inserted them
# MAGIC - ✅ Verified via `EXPLAIN` that HNSW is serving your queries
# MAGIC - ✅ Watched pure vector miss the SKU lookup, and watched RRF fix it
# MAGIC - ✅ Composed vector + BM25 + JSONB filter in one query plan
# MAGIC - ✅ Wrapped it all in a `rag()` function answering grounded, cited questions
# MAGIC
# MAGIC **Next up: Module 6** — *Lakebase as Memory Store for AI Agents.* Layer episodic memory, tool-call logs, and a four-tier memory model on top of the `documents` table you just built.
