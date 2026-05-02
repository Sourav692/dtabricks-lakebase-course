# Module 09 — RSA Toolkit: Comparison, Best Practices & Limitations

> **Level:** Expert · **Duration:** ~3 hours (theory) · **Format:** Field manual
> **Coverage:** Topics 9.1 → 9.7 — the conversations customers actually have, with the talk tracks, decision trees, and limits you will reference forever.

---

## What this module gives you

Modules 1–7 taught you how Lakebase works. Module 8 had you build the capstone with your own hands. **Module 9 is the field manual** — the artifact you reach for when a customer asks "should I migrate?" or "how does this compare to Aurora?" or "what are the gotchas?"

By the end of this module, you should be able to:

1. **Compare** Lakebase against Aurora, Neon, Supabase, RDS, and Cloud SQL on every dimension that matters (9.1)
2. **Recommend** schema, indexing, and connection patterns that hold up under production load (9.2)
3. **Map** customers to supported regions and warn early when they aren't (9.3)
4. **Flag** every published limit *before* the customer hits it in design review (9.4)
5. **Decide** with the strong-yes / mixed / honest-no decision tree (9.5)
6. **Pitch** Lakebase in 30 seconds with the five-reason answer (9.6)
7. **Refuse politely** the anti-patterns that turn into incidents three months later (9.7)

This is a theory module — no notebook. The deliverable is a **Design Review Template** you'll save to your team's wiki and reuse on every Lakebase conversation forever.

> **Prerequisite check.** You should have completed the **Module 8 capstone**. The toolkit is dramatically more useful when you've actually shipped the architecture you're about to recommend to others. If you skipped the capstone, go back — every section below assumes you've felt the moving parts personally.

---

## 9.1 — Lakebase vs. The PostgreSQL Competitive Set

### The deceptively simple question

A customer says: *"We're already on AWS. Why not just use Aurora? It's Postgres too."*

There is a good answer to this question, and there is a wrong answer. The wrong answer is "Lakebase is better" — because for many workloads it isn't. The right answer is **a different product class** — Lakebase isn't trying to outscale Aurora on raw OLTP; it's trying to be the operational database that lives natively inside the lakehouse.

Get the framing right and the rest of the comparison flows naturally.

### The dimensions that matter

| Dimension | Lakebase Autoscaling | Aurora PG | Neon | Supabase | RDS / Cloud SQL |
|---|---|---|---|---|---|
| **Compute model** | Autoscale + S2Z | Serverless v2 | Autoscale + S2Z | Fixed | Fixed |
| **Branching (CoW)** | ✓ Native, instant | Aurora clones (slower) | ✓ Native | — | — |
| **Lakehouse integration** | ✓ Sync + Federation + UC | Manual ETL | Manual ETL | Manual ETL | Manual ETL |
| **Identity / Auth** | ✓ Workspace OAuth, OBO | IAM auth (custom) | PG roles only | Supabase Auth | IAM / native |
| **Vector search** | ✓ pgvector + UC + Mosaic | pgvector | pgvector | pgvector | pgvector |
| **Governance** | ✓ Unity Catalog | IAM + tags | — | RLS only | IAM + tags |
| **Single bill** | ✓ Databricks | AWS | Neon | Supabase | Cloud provider |
| **Max OLTP scale** | Multi-TB, ~50k QPS | Petabyte, 200k+ QPS | Multi-TB | TB-scale | Multi-TB |

### The honest one-liner per competitor

- **vs. AWS Aurora PG.** Aurora has Aurora Serverless v2 + cloning, but no UC integration, no OBO, no Databricks Apps. **Aurora wins on raw OLTP scale.** Lakebase wins on lakehouse integration and governance.

- **vs. Neon.** Both have branching and scale-to-zero. **Neon wins on pure-Postgres dev workflow** (better psql ergonomics, broader extension set today). **Lakebase wins on lakehouse integration and governance** — which Neon doesn't try to provide.

- **vs. Supabase.** Supabase has a richer mobile/JAMstack story (Auth, Edge Functions, Realtime). **Lakebase wins for analytical/AI-heavy workloads on Databricks** — Supabase doesn't try to be a lakehouse-native DB.

- **vs. RDS / Cloud SQL.** Legacy lift-and-shift Postgres. Manual scaling, no branching, no UC. **Choose for migration simplicity, not capability.** Many large Postgres footprints will live on RDS for years; that's fine.

### The honest framing — say this out loud

> **Lakebase is a different product class.** It's a Postgres governed and synchronized as part of a lakehouse. If those three properties — *governance via UC, zero-ETL sync to Delta, identity via workspace OAuth* — don't matter to the customer, recommend the alternative without hesitation. The trust you keep is worth more than the deal you force.

```
                    ┌──────────────────────────────┐
                    │  Are governance + zero-ETL   │
                    │   + workspace identity       │
                    │   the customer's top 3?      │
                    └──────┬───────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
            YES                        NO
        Lakebase wins            Recommend best fit
                                 (Aurora / Neon / etc.)
```

---

## 9.2 — Best Practices: Schema, Indexing, Connections

These are the recommendations you will repeat in every design review. Internalize them.

### Schema

- **Use BIGSERIAL / BIGINT primary keys, not UUIDs.** Index locality matters more on remote-paged storage than on local SSD. UUIDs scatter writes across the B-tree; BIGSERIAL keeps them clustered.
- **Add an `updated_at TIMESTAMPTZ` column on every table.** It's the seam Lakehouse Sync uses for incremental CDC, and it costs nothing to maintain (cheap trigger, indexed).
- **Use `JSONB`, not `JSON`.** JSON stores raw text and re-parses on every read; JSONB stores the parsed binary and supports GIN indexes.
- **Constraints early.** `NOT NULL`, `CHECK`, and foreign keys catch bugs at insert time. The cost is negligible; the bugs they catch are not.

### Indexing

- **Prefer BRIN over B-tree** for monotonically-increasing columns (timestamps, sequences). Tens of bytes per page instead of one entry per row. Range scans stay fast; storage drops 1000×.
- **Add GIN indexes on JSONB filters.** `WHERE metadata->>'tenant' = 'X'` without GIN = full scan. With GIN = index lookup.
- **HNSW for vectors, with `m=16` and `ef_construction=64` defaults.** Tune up only if recall benchmarks demand it. Module 5 covered the why.
- **One index per access pattern, not per column.** Composite indexes covering `(tenant_id, created_at)` are usually right for multi-tenant apps.

### Connections

- **Always use `pool_pre_ping=True` in SQLAlchemy.** OAuth tokens expire after ~1 hour; without pre-ping you get a `connection failed` mid-request when the token rotates.
- **Per-request engine factories for OBO apps.** A single global engine breaks On-Behalf-Of identity — rows of user A appear for user B. Module 7 covered the trap; commit it to memory.
- **Pool size = 2× expected concurrent users, not "as many as possible".** Postgres connections are heavy. Lakebase Autoscaling handles bursts at the compute layer; you don't need to buffer them at the pool.
- **Set `statement_timeout` per workload class.** App reads = 5s, batch jobs = 10min, ad-hoc analyst queries = 30min. A runaway query without a timeout will pin a connection until the next deploy.

### The cheat sheet

| Concern | Default to | Reach for | Avoid |
|---|---|---|---|
| Primary key | `BIGSERIAL` | `UUID` only with `uuid_generate_v7` | `UUID v4` (random — kills locality) |
| Time-series index | `BRIN` | `B-tree` for selective queries | `B-tree` on `created_at` for full scans |
| JSON column | `JSONB` + GIN | Generated columns for hot paths | `JSON` (text mode) |
| Vector index | `HNSW` (m=16) | `HNSW` (m=32) only on benchmarks | `IVFFlat` as default |
| Connection pool | SQLAlchemy + `pool_pre_ping` | psycopg-direct for batch | Hardcoded password strings |

---

## 9.3 — The Region Matrix

Lakebase Autoscaling is not yet GA in every Databricks region. The single most common reason a Module 1 lab fails is *"DATABASE_CREATE & region"* — the user has the entitlement but is in a workspace where Autoscaling isn't available.

### The discipline

**Check the region before the design call**, not during it.

- Run `w.serving_endpoints.list()` and `w.database.list_database_projects()` in the customer's workspace yourself if possible.
- If unsupported: offer **Provisioned** if it's available, or recommend a different workspace, or be honest that Lakebase isn't yet an option there.
- Region availability changes monthly. The matrix in your team wiki is probably stale; the docs are the source of truth.

### Four cases, four answers

| Customer state | Recommendation |
|---|---|
| In a region with Autoscaling GA | Strong yes — proceed with the standard talk track |
| In a region with Provisioned only | Yes for steady workloads; flag that branching/instant-restore aren't available yet |
| In a region with neither | Honest no — recommend Aurora / Neon and revisit when GA |
| Multi-region customer with one region supported | Pilot in supported region; plan multi-region as Lakebase regions expand |

### The conversation

> *"Before we go any further, let me check that Lakebase Autoscaling is available in your region. The feature list I'm about to walk you through assumes it is — if it's not, we still have options, but the talk track is different."*

Saying this in the first five minutes saves an hour of architecture discussion that ends in *"oh, but we can't actually use this yet."*

---

## 9.4 — Hard Limits to Flag in Design Reviews

Every customer hits a limit eventually. Your job is to flag them **before** they design around an assumption that doesn't hold.

### The limit table

| Limit | What breaks | Mitigation |
|---|---|---|
| **OAuth token TTL ≈ 1 hour** | Long batch jobs / app pools fail mid-flight | Use Databricks driver token refresh; chunk batch < 1h or refresh in loop |
| **Region availability is not universal** | Customer in unsupported region cannot use Autoscaling | Check region matrix before the design call |
| **Cold starts on scale-to-zero (~3–8s)** | p99 spikes on first request after idle | Disable S2Z on prod primary; keep on dev/preview only |
| **Synced table latency is seconds–minutes** | Not suitable for "instant" UC → Lakebase consistency | Use direct writes for write-once-read-fast; sync = read-only mirror |
| **Lakehouse Sync (reverse) is Beta on AWS** | SLA / GA status varies by region | For mission-critical CDC to Delta, validate region GA; have fallback plan |
| **Not a billion-row vector store** | HNSW build & memory grow with N | Switch to Mosaic AI Vector Search above ~10M vectors |
| **No cross-region active-active** | Global apps need additional architecture | HA across AZs only; for multi-region replicate via UC + Lakehouse Sync |
| **Logical replication slot limits** | External CDC tools may exhaust slots | Use Lakehouse Sync for CDC to UC; plan slot count carefully if external |

### How to use this in a design review

For every customer architecture diagram, **walk the limit table top-to-bottom** and put one of three labels next to each row:

- **Discussed** — customer is aware, no blocker
- **Blocker** — workload exceeds the limit; needs alternative
- **N/A** — limit doesn't apply to this workload

The Design Review Template in 9.8 codifies this. Don't skip it; this is the single artifact that prevents 90% of post-deploy escalations.

### The conversation

> *"There are eight limits I want to walk through with you before we agree on this design. Most won't apply, but for the ones that do, we should decide together whether they're a blocker or just something to manage."*

This sentence transforms you from a vendor pitching a product into a trusted advisor managing risk.

---

## 9.5 — When To Use Lakebase: The Decision Tree

Run **every** Lakebase customer conversation through this filter before pitching.

### Strong YES

The customer is on Databricks and:

- Building an app or agent that needs **operational state** (user profiles, session data, transaction history)
- Or needs **vector retrieval** for RAG / semantic search (corpus < 10M)
- Or needs **low-latency feature serving** for ML
- Wants single bill, single governance, single auth
- Postgres compatibility is acceptable

This is the canonical Lakebase customer. The whole roadmap was built for them.

### Mixed

The customer has a **100M-row OLTP on Aurora today**:

- Migration cost is real — schema + data + app code + runbooks
- Lakebase doesn't make the existing app faster
- **Recommend Lakebase for *new* apps in the same workspace** — coexist, don't migrate-for-migration's-sake
- Plan a 6–12 month transition, not a forklift

This is where most enterprise customers sit. Don't try to flip them; help them coexist.

### Honest NO

The customer needs:

- **MySQL / SQL Server / Oracle compatibility** — Lakebase is Postgres only
- **Workload exceeds limits** — multi-TB hot working set, > 50k QPS, billion-vector index
- **Region not supported** — and they can't move workspaces
- **Mobile/edge replication** — Supabase or Firebase fit better

**Don't force-fit. Recommend the right tool. Retain the trust.** The customer who hears "this isn't the right product for you" once will believe you forever.

### The decision flow

```
                  Customer is on Databricks?
                         │
                ┌────────┴────────┐
               YES               NO
                │                 │
   Workload fits Postgres        Honest NO
   + within limits                (recommend best fit)
                │
       ┌────────┴────────┐
      YES               NO
       │                 │
  Existing big          Honest NO
  Aurora footprint?     (limits exceeded)
       │
   ┌───┴───┐
  YES     NO
   │       │
 MIXED   STRONG YES
 (coexist)  (canonical fit)
```

---

## 9.6 — The 30-Second Customer Answer

When the customer asks *"Why Lakebase instead of Aurora?"* — you have 30 seconds before their attention drifts. Here is the answer, in order:

### The five reasons, ranked

1. **Governance.** Lakebase tables register in Unity Catalog. Same RBAC, same tags, same audit logs as your Delta tables. One governance plane, not two.

2. **Zero ETL.** Synced tables eliminate reverse-ETL pipelines from Delta to your app DB. No Fivetran. No Hightouch. No hand-written Spark jobs that break every quarter.

3. **Identity.** Apps connect on behalf of the end user — RLS works against workspace identity for free. No mapping layer between AWS IAM and Postgres roles.

4. **Branching.** Schema changes get tested on copy-on-write branches in seconds, not on a staging RDS that was last refreshed six weeks ago. Every PR gets its own database.

5. **One bill, one platform.** Compute, storage, vectors, models, apps — all on Databricks. One vendor relationship, one support contract, one capacity plan.

### The closer

> *"If none of those five matter to your team, Aurora is the right answer. Tell me which of them matter, and I'll show you the architecture."*

This is not a sales pitch. It is a diagnostic. The customer's answer tells you whether you're talking to the right buyer.

### The visual

Picture five pillars holding up the customer's app:

```
   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │Governance│ │ Zero-ETL │ │ Identity │ │Branching │ │ Single   │
   │   (UC)   │ │  (Sync)  │ │  (OBO)   │ │  (CoW)   │ │  Bill    │
   └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
        └────────────┴────────────┴────────────┴────────────┘
                              │
                       ┌──────┴──────┐
                       │  Customer   │
                       │  Outcome    │
                       └─────────────┘
```

If three or more pillars matter, Lakebase is a fit. If one or zero, recommend the alternative.

---

## 9.7 — Anti-Patterns to Refuse Politely

These are the requests that show up in customer conversations that you should **not** agree to. Each one feels reasonable in the moment and turns into an incident three months later.

### The seven sins

- ❌ **"Use Lakebase as our 50TB Aurora replacement next quarter."**
  *Too aggressive.* Pilot a single new app first. Forklift migrations of legacy OLTP fail for reasons unrelated to Lakebase quality (app coupling, untested edge cases, multi-team coordination).

- ❌ **"Run analytics dashboards directly off Lakebase to skip Delta."**
  *Postgres is OLTP.* Push aggregations to Delta + DBSQL; serve hot reads from Lakebase. Running a 100-million-row scan against your operational DB is how you take down both the dashboard and the app.

- ❌ **"Store 1 billion document vectors here."**
  *Wrong tool.* Use Mosaic AI Vector Search. Lakebase + pgvector is for < 10M vectors. Above that, HNSW build time and memory dominate.

- ❌ **"Avoid UC registration to keep things simple."**
  *Without UC you lose governance, federation, AI/BI access, and downstream sync.* Always register. The "simplicity" of skipping UC compounds into an irreversible mess.

- ❌ **"Hardcode the Postgres password in our app config."**
  *Tokens expire after ~1 hour.* Use the SDK's credential provider every time. The first time you redeploy and the app dies on a token rotation, you'll wish you hadn't.

- ❌ **"One global SQLAlchemy engine for the whole App."**
  *Breaks OBO* — rows of user A appear for user B. Per-request engine factory is the only correct pattern. Module 7 covered the trap; this is your reminder.

- ❌ **"Use Lakebase for our IoT firehose at 100k events/sec."**
  *Wrong tool.* OLTP databases are not high-volume ingest engines. Use Kafka → Delta → maybe sync hot rollups back to Lakebase for app reads.

### How to refuse politely

> *"I hear what you're trying to do, and I want to help you get there — but the architecture you're describing will fail in production for a reason I can name now. Let me show you the pattern that does work."*

Naming the failure mode up front is what separates a trusted advisor from an order-taker.

---

## 9.8 — Lab: Build Your Own Design Review Template

Module 9 has no notebook. Instead, the deliverable is a **1-page Design Review Template** you'll save to your Confluence / Notion / team wiki and reuse forever.

### The template structure

The template has five sections. Fill it out for **every** Lakebase customer conversation.

#### Section 1 — Customer scenario (3 sentences max)

- What's the workload? (app / agent / feature store / vector search)
- What's the scale? (rows, QPS, vector count)
- What's the latency target? (p99 in ms)

#### Section 2 — Fit score (1–5 on each pillar)

| Pillar | Score | Rationale |
|---|---|---|
| Governance (UC) | / 5 | |
| Zero-ETL (Sync) | / 5 | |
| Identity (OBO) | / 5 | |
| Branching (CoW) | / 5 | |
| Single platform | / 5 | |

**Sum the scores.** ≥ 18: strong fit. 12–17: mixed. < 12: honest no.

#### Section 3 — Limit checklist (from 9.4)

For each limit, mark **discussed / blocker / N/A**:

- [ ] OAuth token TTL ≈ 1 hour
- [ ] Region availability
- [ ] Cold starts on scale-to-zero
- [ ] Synced table latency seconds–minutes
- [ ] Lakehouse Sync reverse is Beta on AWS
- [ ] Not a billion-row vector store
- [ ] No cross-region active-active
- [ ] Logical replication slot limits

#### Section 4 — Recommendation

**Strong-yes / Mixed / Honest-no** — with a one-paragraph rationale that references specific scores and limits from sections 2 and 3.

#### Section 5 — Migration timeline (if applicable)

- **Day 0–30:** Pilot one new app on Lakebase in the customer's workspace
- **Day 30–90:** Validate the five pillars with real workload metrics
- **Day 90–180:** Plan coexistence with existing OLTP — what stays, what moves, what's net-new
- **Day 180+:** Expand based on what worked, not what was promised

### Save it

Drop the filled-in template into your team's wiki under a stable URL. Reference it in every customer conversation summary. **Three months from now, you'll search for it and be glad it exists.**

---

## Module 9 — wrap-up

You should now be able to handle **any** Lakebase customer conversation — not just the first three minutes.

- Compare Lakebase against the full Postgres competitive set with the right framing (9.1)
- Recommend schema, indexing, and connection patterns from memory (9.2)
- Map customers to supported regions before the design call (9.3)
- Walk every published limit through a design review (9.4)
- Decide strong-yes / mixed / honest-no with a clear rationale (9.5)
- Pitch Lakebase in 30 seconds with the five-reason answer (9.6)
- Refuse the seven anti-patterns politely and constructively (9.7)
- Run every conversation through your Design Review Template (9.8)

### Common pitfalls to remember

- **Pitching Lakebase as "better Aurora".** It isn't, on raw OLTP. Pitch the *different product class* framing.
- **Skipping the region check.** The single most common reason a customer's first lab fails. Always check first.
- **Treating limits as marketing pushback.** They're risk management. Every customer hits one eventually; flag them early.
- **Forcing the deal when the customer is an honest no.** The trust you keep is worth more than the deal you force.
- **Not actually filling out the Design Review Template.** "I'll do it later" means "I won't do it." Fill it out during the call.

---

## Roadmap — complete

Module 1 gave you the vocabulary. Module 2 gave you the architecture. Module 3 put a real Lakebase project in your hands. Module 4 wired it to the lakehouse. Module 5 turned it into a vector store. Module 6 turned it into agent memory. Module 7 wrapped it in a Databricks App. Module 8 made you build the whole thing end-to-end. **Module 9 turned you into a trusted advisor who can lead any Lakebase customer through their architecture with full conviction.**

The roadmap is complete. The next conversation you have with a customer is the real test — and you're ready for it.
