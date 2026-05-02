# Module 02 — Architecture Deep Dive: Compute, Storage, Branches

> **Level:** Beginner–Intermediate · **Duration:** ~2.5 hours · **Format:** Theory + diagrams
> **Coverage:** Topics 2.1, 2.2, 2.3, 2.4, 2.5 — the architectural choices that make Lakebase feel different from RDS.

---

## What this module gives you

Module 1 told you *what* Lakebase is. Module 2 tells you *how it works under the hood* — and **why every "magical" capability flows from one architectural decision**: separating compute from storage.

By the end of these five topics you should be able to:

1. **Whiteboard the storage/compute split** and explain what each layer owns (2.1)
2. **Reason about branches** the same way you reason about Git (2.2)
3. **Predict cold-start behavior** and know when to disable scale-to-zero (2.3)
4. **Design HA and read-replica topologies** for production workloads (2.4)
5. **Avoid the OAuth token TTL trap** that breaks long-running app pools (2.5)

Module 3 is hands-on — you'll provision a real project, connect to it, and see all of these capabilities in action. Module 2 makes that lab make sense.

---

## 2.1 — Separated Compute & Storage: The Defining Move

### The architectural decision

Classical Postgres — including managed offerings like RDS Postgres — runs Postgres on a VM with **locally attached block storage**. The data lives next to the compute. Replication is at the block level. Scaling means resizing the VM. Cloning means copying terabytes.

Lakebase Autoscaling rebuilds this stack. Storage is moved into a **multi-tenant page server** backed by object storage. Compute is a Postgres process that **lazily fetches pages** from the page server and caches them locally. The two layers communicate over a network.

This single decision — separating compute from storage — is what makes every distinguishing Lakebase capability possible.

### The two layers

```
┌────────────────────────────────────────────────────────────┐
│  COMPUTE LAYER (stateless, elastic)                        │
│  Postgres process · query plan · local page cache · WAL    │
│  Can scale up/down · Can scale to zero · Can fail over     │
└────────────────────────────────────────────────────────────┘
                       ↕ pages ↑   ↓ WAL
┌────────────────────────────────────────────────────────────┐
│  STORAGE LAYER (durable, versioned, copy-on-write)         │
│  Page server · safekeepers · object storage backing        │
│  Multi-tenant · always replicated · per-page versioning    │
└────────────────────────────────────────────────────────────┘
```

The interface between them is simple: **compute requests pages by `(rel, blk, lsn)`; storage returns the page version at that LSN**. WAL flows the other way — compute writes WAL records, the storage layer durably accepts them and applies them to update page versions.

### What each layer owns

| Concern | Compute | Storage |
|---|---|---|
| **Query parsing & planning** | ✅ | — |
| **Buffer cache (hot pages)** | ✅ | — |
| **WAL generation** | ✅ | — |
| **Page durability** | — | ✅ |
| **Per-page version history** | — | ✅ |
| **Replication / redundancy** | — | ✅ (always on) |
| **Scaling up/down** | ✅ (elastic) | ✅ (independent) |
| **Survives restart of itself** | — (stateless) | ✅ (durable) |

The asymmetry is the point. **Compute is stateless** — it can be killed, restarted, suspended, or replaced without touching durable data. **Storage is durable and versioned** — every page version is retained for the configured retention window (up to 30 days).

### Why this enables everything

Because compute and storage are decoupled, Lakebase can do things classical Postgres physically cannot:

- **Scale-to-zero** — kill the compute when no queries run; storage keeps the data alive. Cost drops to storage-only. Restart on next connection. (Topic 2.3.)
- **Branching** — a new branch is just a new compute process pointing at the same storage with a copy-on-write snapshot pointer. No data is copied. Ready in seconds. (Topic 2.2.)
- **Instant restore** — every page version is already retained; restoring is a metadata operation, not a backup replay.
- **Read replicas with zero replication lag** — additional compute nodes share the same page-server storage. They see committed pages immediately. (Topic 2.4.)
- **Resize without downtime** — change the compute size; storage doesn't move because it was never local.

Provisioned Lakebase doesn't have these capabilities precisely because its storage is locally attached and replicated at the block level. *Branching would mean physically copying terabytes.* That's why these capabilities are Autoscaling-exclusive.

### The trade-off you must internalize

Remote storage is not free. A page that isn't in the local buffer cache must be fetched over the network from the page server. Cold pages cost network latency.

This shifts what good index design looks like:

- **Index locality matters more.** A well-indexed lookup hits a few pages; a sequential scan touches many. On a single-node Postgres, a seq scan of a 100MB table on a fast SSD is cheap. On Lakebase, fetching 12,500 cold 8KB pages over the network is *not* cheap.
- **`SELECT *` is more expensive than you remember.** The page server returns whole rows; projecting columns matters.
- **Tiny commits inflate WAL.** Batch writes in transactions of 1k–10k rows.

We'll cover the concrete best practices in Module 9. For now, the takeaway: **think about pages, not just rows.**

### Mental model

If you've used **Aurora**, the architecture is similar in spirit (compute pods over a shared distributed storage layer) but Aurora's storage isn't copy-on-write versioned the same way.

If you've used **Neon**, the architecture is essentially identical — separated compute, page-server storage, copy-on-write branches. Lakebase Autoscaling is built on the same architectural lineage, with deep Databricks integration (Unity Catalog, OAuth, synced tables) layered on top.

> **🎯 Checkpoint for 2.1**
> Can you state, in two sentences: *(1) what storage owns and what compute owns*, and *(2) which capability is impossible if you put them back together?* If yes, move on.

---

## 2.2 — Branches: Git-Like Workflows for Data

### What a branch actually is

A **branch** in Lakebase is a named, isolated compute endpoint pointing at a copy-on-write snapshot of project storage at a specific point in time.

That sentence is doing a lot of work. Read it slowly:

- **Named** — you give it a string like `pr-1487-add-feature-flags`.
- **Isolated** — writes on the branch don't touch the parent.
- **Compute endpoint** — it has its own Postgres process and its own connection string.
- **Copy-on-write snapshot** — no pages are physically copied at creation time. The branch *points at* the parent's pages until it diverges.
- **At a specific point in time** — you can branch from `now()`, from 5 minutes ago, or from any LSN inside the retention window.

### Branch lifecycle

```
                    ┌─────────────────────┐
   create from  →   │  Branch is born     │   No data copied.
   parent at LSN    │  Points at parent   │   Just a pointer.
                    └──────────┬──────────┘
                               │
                       reads / writes
                               │
                    ┌──────────▼──────────┐
                    │  Branch diverges    │   New writes create
                    │  Writes go to new   │   new page versions
                    │  page versions      │   on the branch only.
                    └──────────┬──────────┘
                               │
                  ┌────────────┴────────────┐
                  │                         │
        promote to parent              drop branch
        (merge by promotion)           (CI cleanup)
                  │                         │
        new pages become parent     compute released, deltas GC'd
```

A branch starts as a pure pointer — zero copied data. As writes occur on the branch, **new page versions are written to the storage layer attributed to the branch**. The parent's pages are unchanged. This is exactly how copy-on-write filesystems and Git work.

When you're done, you either:

- **Promote** the branch — its page versions become the new "main" view (e.g., a successful blue-green deploy).
- **Drop** the branch — the branch's compute is released and the branch-only deltas are garbage collected. The parent is unaffected.

### What branches are for

These are the patterns we see customers use repeatedly:

- **PR-based schema migrations** — open a PR, branch off `main`, run the migration on the branch, run integration tests, review the diff, then merge by promotion when CI is green.
- **Ephemeral CI databases** — every test run gets its own branch. Created when the PR opens, dropped when it closes. No more "shared dev DB" contention.
- **Blue-green deployments** — deploy your app against a fresh branch, validate it, then atomically swap traffic by promoting the branch.
- **Point-in-time recovery** — somebody dropped the wrong table at 14:32. Branch from `2026-04-30 14:31:59` and you have a writable copy of the world before the mistake. Cherry-pick the row out and back into main, or promote the branch.
- **"What-if" analysis on writable data** — let an analyst run destructive `UPDATE` statements on a branch without touching `main`. Drop the branch when done.

### The Git mental model

If you know Git, the model transfers almost perfectly:

| Git | Lakebase |
|---|---|
| `git branch feature-x` | Create branch `feature-x` from `main` |
| Commits diverge per branch | Page versions diverge per branch |
| `git merge` (fast-forward) | Promote a branch |
| `git branch -d feature-x` | Drop the branch |
| Branches share history until they diverge | Branches share pages until they diverge |
| Storage cost ≈ size of unique commits | Storage cost ≈ size of unique page deltas |

The one place the analogy is *not* perfect: Lakebase doesn't have a built-in three-way merge. "Merging" a branch means promoting it to take over from the parent, or running a SQL-level reconciliation. This matches how schema migrations actually work in practice (a deploy is a swap, not a textual merge).

### Cost model for branches

You pay for two things on a branch:

1. **Compute** — the Postgres process attached to the branch. If the branch sits idle and scale-to-zero is enabled, this approaches zero.
2. **Storage deltas** — the page versions that exist *only* on this branch (because writes diverged it from the parent). You do **not** pay for the parent's pages a second time.

This means an idle ephemeral CI branch with scale-to-zero costs roughly **the size of its writes**. A read-only branch that does no writes costs essentially nothing. This is what makes "one branch per PR" economically viable.

### Anti-patterns

- **Long-lived diverged branches.** If a branch has been writing for weeks, its delta footprint grows. Branches are designed for short cycles (minutes to days). For long-lived environments, use full projects.
- **Branching to escape main's load.** A read replica is the right answer for analytical load on production data — branches diverge over time, replicas don't.
- **Treating a branch as a backup.** Backups should outlive branches. The retention window covers point-in-time recovery; branches are working copies.

> **🎯 Checkpoint for 2.2**
> Be able to explain in one breath: *a branch is a new compute pointing at a copy-on-write snapshot of storage; nothing is copied at creation; writes diverge as new page versions; cost is compute + branch-unique deltas.*

---

## 2.3 — Scale-to-Zero & Cold-Start Mechanics

### What scale-to-zero actually does

When a Lakebase Autoscaling branch sees no queries for roughly **5 minutes**, the compute process **suspends**. The Postgres process is gone. The storage stays exactly where it is — durable, versioned, untouched.

Cost behavior during suspension:

- **Compute billing → $0**
- **Storage billing → unchanged** (you're paying for the data either way)

When a new connection arrives, the compute boots, attaches to storage, and begins serving. From the client's perspective, the connection looks like a slightly slow first query.

### The cold-start sequence

```
Client sends first query    →  pgbouncer holds the connection
                                          ↓
                            Lakebase notices: branch is suspended
                                          ↓
                            Boot Postgres process (~1–2s)
                                          ↓
                            Attach to page server (sub-second)
                                          ↓
                            Recover any in-flight WAL (sub-second)
                                          ↓
                            Open connection accepted
                                          ↓
                            First query runs — pages fetched cold
                                          ↓
                            Local cache warms over next ~minute
                                          ↓
                            Steady-state latency restored
```

### Cold-start latency budget

Typical cold-start for a small DB is **3–8 seconds end-to-end** before the first query returns. The breakdown:

- **Compute boot + attach**: ~1–3 seconds (deterministic).
- **First query page fetch**: variable — depends on how many pages the query needs and whether they're in the page server's hot tier.
- **Cache warming**: the *first minute* of queries will see elevated p99 as cold pages are pulled into the local buffer cache. This isn't part of the cold-start itself, but it's part of the post-cold-start performance profile users will feel.

The pgbouncer connection pool **holds the client connection during boot** — the client doesn't see a connection error, just elevated latency on the first query.

### When to use scale-to-zero (and when NOT to)

| Branch type | Scale-to-zero? | Reasoning |
|---|---|---|
| **Production primary** | ❌ Disable | A 3–8s tail on the first request after idle = p99 SLA violation. Real users will hit it. |
| **Production read replica** | ❌ Usually disable | Same logic — if it serves user traffic. |
| **Dev / preview branches** | ✅ Enable | Idle 90% of the time. Scale-to-zero is the whole point. |
| **CI / ephemeral branches** | ✅ Enable | Created on PR open, used briefly, dropped. Perfect fit. |
| **Internal admin tools** | ✅ Often enable | If users tolerate a 5s "wake up" on the first morning click, you'll save real money. |

### The latency-sensitive API anti-pattern

If you point a customer-facing real-time API (recommendation, fraud, search) at a scale-to-zero branch, here's what happens:

1. Traffic is steady during the day → compute stays warm → looks fine.
2. Traffic dips to near-zero overnight → compute suspends.
3. First request next morning → 3–8 second cold start.
4. **Your p99 chart shows a daily spike** that correlates exactly to the suspension window.

The fix is straightforward: **disable scale-to-zero on the production primary**. You're paying for compute 24/7, but your p99 is honest. Module 9 covers this in the "Best Practices" topic.

### Page warming after cold start

Even after the Postgres process is up, the **local buffer cache is empty**. The first queries pull pages from the page server. Hot pages typically sit in the page server's own memory tier and arrive in single-digit ms; truly cold pages (rarely accessed) come from object storage and take longer.

In practice this means: even after the cold-start completes, the *first minute* of queries on a freshly-woken branch is slower than steady-state. After that, the working set is cached locally and you're back to normal Postgres latency profiles.

> **🎯 Checkpoint for 2.3**
> Two things to remember verbatim: *(1) cold start ≈ 3–8s for a small DB; (2) disable scale-to-zero on the production primary, enable it on dev/preview/CI.* Both come up in customer design reviews.

---

## 2.4 — High Availability & Read Replicas

### The HA topology

Lakebase HA pairs a **primary compute in AZ-A** with a **synchronous standby in AZ-B**. If the primary fails — VM crash, AZ outage, hardware fault — the standby takes over.

```
       ┌─────────────────────────────────┐
       │      Application / pgbouncer    │
       │      (stable connection string) │
       └──────────────┬──────────────────┘
                      │
            ┌─────────┴─────────┐
            ▼                   ▼
      ╔═══════════╗       ╔═══════════╗
      ║ PRIMARY   ║◄─────►║ STANDBY   ║   sync replication
      ║   AZ-A    ║       ║   AZ-B    ║   on commit
      ╚═════╦═════╝       ╚═════╦═════╝
            │                   │
            └────────┬──────────┘
                     ▼
            ┌────────────────┐
            │  Page server   │   already replicated
            │  (durable)     │   inside the storage layer
            └────────────────┘
```

Two important properties to internalize:

- **Storage is already replicated.** The page server replicates internally — that's what makes it durable. **HA is only protecting the compute tier.** If the page server itself had a fault, HA wouldn't be the mechanism that recovers — the page server's own redundancy would.
- **Connection strings are stable across failover.** The DNS name your app uses doesn't change when the standby is promoted. pgbouncer reroutes; the app sees a brief disconnect-and-reconnect, not a config update.

### Failover behavior

- Typical failover time: **<30 seconds** end-to-end.
- The application sees existing connections drop. It must reconnect — any production-grade Postgres client does this with retry logic (psycopg's `connect_retry`, JDBC's `tcpKeepAlive` + connection pool retry, etc.).
- In-flight transactions are rolled back. Committed transactions (synchronous replication) survive.
- For **RPO=0** workloads, enable HA on the primary branch. Branches that don't enable HA have no standby — they go down with their AZ.

### HA inheritance

HA settings are **per-branch**, not per-project. If you enable HA on `main`, child branches **inherit the choice independently** — they don't automatically get HA. This usually matches what you want: prod gets HA, ephemeral CI branches don't (paying for HA on a branch that lives 20 minutes is silly).

### Read replicas

A **read replica** is an additional compute node sharing the **same page-server storage** as the primary.

The classic Postgres mental model — "read replicas have replication lag because they're catching up via streaming WAL" — *does not apply here*. Because the storage is shared at the page-server level, **committed pages are visible to the replica with zero replication lag**.

```
┌──────────────┐    writes   ┌──────────────┐    reads   ┌──────────────┐
│ App / writer ├────────────►│   PRIMARY    │            │ READ REPLICA │
└──────────────┘             │   compute    │            │   compute    │
                             └──────┬───────┘            └──────┬───────┘
                                    │                           │
                                    └──────────┬────────────────┘
                                               ▼
                                      ┌────────────────┐
                                      │  Page server   │   ← shared storage
                                      │   (one copy)   │     zero replication lag
                                      └────────────────┘                 for committed pages
```

### When to add a read replica

- **Analyst / dashboard load** that you don't want competing with app writes for buffer cache and CPU.
- **Heavy report queries** that scan large ranges and would otherwise pollute the primary's cache.
- **Geographic locality** (in regions where the feature exists) — closer compute to readers.

Standard pattern: **send writes and hot reads to the primary, route analyst/dashboard load to a replica.** Connection strings differ — your app reads from `<project>-replica.lakebase...`, writes to `<project>-rw.lakebase...`.

### Provisioned vs Autoscaling read replicas

This is a real difference, not just a naming thing:

- **Autoscaling read replicas** share the page server. **Zero replication lag** for committed pages.
- **Provisioned "readable secondaries"** use classical streaming replication. There's a small but nonzero lag — typically tens of milliseconds, sometimes more under load. Read-after-write consistency is *not* guaranteed.

For workloads where read-your-own-write matters (e.g., a UI that writes a row then immediately reads it back to show the user), Autoscaling read replicas Just Work. Provisioned readable secondaries can produce stale reads.

> **🎯 Checkpoint for 2.4**
> Know two facts: *(1) HA protects only the compute tier — storage is already replicated; (2) Autoscaling read replicas share storage and have zero replication lag, unlike classical Postgres replicas.*

---

## 2.5 — Networking, Auth & the OAuth Token TTL Trap

### The three connection paths

Lakebase exposes three ways to reach the database. Pick the one that matches your workload's network and security posture.

| Path | When to use | Latency | Auth |
|---|---|---|---|
| **Public endpoint over TLS** | Default. Notebooks, ad-hoc tools, test apps | Best | OAuth |
| **Inbound Private Link** | VPC-to-VPC traffic without traversing the public internet | Same-region, intra-AWS | OAuth |
| **Performance-intensive Private Link** | High-QPS production workloads needing dedicated bandwidth | Lowest | OAuth |

The performance-intensive path is **Autoscaling-only** and is what you reach for when an app pool is opening hundreds-to-thousands of connections per second and you need a dedicated network path.

For most internal RSA labs and demos, the public endpoint over TLS is fine. For production designs with VPC-only postures (financial services, healthcare), Inbound Private Link is the correct default.

### Auth — workspace OAuth as Postgres password

This is the part that surprises Postgres-native engineers. There is no "database password" you set when creating the project. The mechanism is:

1. **Identity** = your workspace user (or a service principal). The same identity that logs into the Databricks UI.
2. **The Databricks SDK / driver issues a short-lived OAuth token** representing that identity.
3. **The OAuth token is used as the Postgres password** when opening the JDBC/psycopg connection.
4. **Postgres GRANTs** are mapped to workspace identities — so you `GRANT SELECT ON orders TO "alice@company.com"` and it works against the actual user.

This is what makes **on-behalf-of (OBO)** auth work: when a Databricks App connects to Lakebase, the connection is opened *as the end user*, not as the app. RLS policies and `current_user`-based row filters work against the workspace identity for free.

### The OAuth token TTL trap

Here is the single most-painful production pitfall in Lakebase, and the one this module is named after.

**OAuth tokens expire. The default TTL is approximately 1 hour.**

If your application opens a connection at 09:00 with an OAuth token, holds the connection in a long-lived pool, and the underlying token expires at 10:00 — at 10:01 the connection silently breaks. The app sees:

```
psycopg.OperationalError: connection to server ... lost
SSL connection has been closed unexpectedly
```

…with no obvious cause. The connection was fine, then it wasn't. Restart the app and it works again — for an hour.

This is the trap. The fix has two layers:

#### 1. Use the Databricks credential providers, not raw passwords

The Databricks Python SDK and the Databricks JDBC driver **automatically refresh OAuth tokens** before they expire. As long as the connection is opened through them (not through `psycopg2.connect(password="...")` with a hardcoded token string), token rotation is invisible to your application code.

Concretely:

```python
# ❌ WRONG — hardcoded token, will fail at the 60-minute mark
token = w.config.token  # captures one token
conn = psycopg.connect(host=..., user=..., password=token)

# ✅ RIGHT — token provider supplies fresh tokens on each connect
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
def get_token(): return w.config.authenticate()["Authorization"].split(" ")[1]
# pass get_token() into your pool's password callback, or use the
# Databricks-provided SQLAlchemy/psycopg helpers that do this for you
```

(Module 3 has the full working pattern. The code above is illustrative.)

#### 2. Use connection pools that expire connections

Even with token refresh, **idle pooled connections that were opened with an old token won't auto-renew their underlying authentication**. Configure your pool with:

- `max_lifetime` (or equivalent) of less than the token TTL — typically 50 minutes.
- `pre_ping` / connection validation on checkout.
- Token refresh on connect, not on pool startup.

This combination gives you a pool that looks stable but quietly rolls over connections within the token window, so no individual connection ever sees an expired token.

### Why this trap is so common

- Postgres-native developers expect passwords to be permanent. The OAuth token model is unfamiliar.
- Local development against a Lakebase project usually works fine — the dev session ends before the token expires, so the bug doesn't manifest.
- **Long-lived production app pools** (Streamlit on Databricks Apps, FastAPI workers, etc.) are exactly the workloads where the bug *does* manifest, often hours into deployment, often on a Friday afternoon.
- The error message doesn't say "OAuth token expired." It looks like a generic connection drop.

If you take one practical takeaway from Module 2 and put it on a sticky note, make it this one: **never hardcode a token as a password. Always use a credential provider.**

### Compliance posture

For customers with regulatory requirements, Lakebase Autoscaling supports the standard Databricks compliance security profiles:

- **HIPAA** (US healthcare)
- **C5** (German cloud security)
- **TISAX** (automotive)
- **FedRAMP** (US public sector — verify region GA)

These are workspace-level configurations, not Lakebase-specific knobs. Lakebase inherits the workspace's profile automatically.

> **🎯 Checkpoint for 2.5**
> If a customer asks "how does auth work?" you should say: *workspace OAuth tokens are used as the Postgres password; tokens have a ~1-hour TTL; the Databricks drivers refresh them automatically; long-lived pools must use credential providers, never hardcoded tokens.*

---

## Module 2 wrap-up

You should now be able to walk a customer through the architecture in front of a whiteboard:

- **Storage and compute are separated.** Storage is durable, versioned, multi-tenant. Compute is stateless, elastic, lazily fetches pages. (2.1)
- **Branches are Git for data.** A branch is a new compute pointing at a copy-on-write snapshot. No data copied. Cost ≈ compute + branch-unique deltas. (2.2)
- **Scale-to-zero is real but not free.** Cold start ≈ 3–8s. Disable on production primary. Enable on dev/preview/CI. (2.3)
- **HA protects compute, not storage.** Sync standby in another AZ. <30s failover. Connection strings stable. Read replicas share storage = zero replication lag. (2.4)
- **OAuth tokens are the password.** ~1h TTL. Credential providers refresh automatically. Hardcoded tokens silently break after an hour. (2.5)

Module 3 puts every one of these in your hands. You'll provision a project, connect with a credential provider (no token trap), explore branching with `CREATE TABLE` on a child branch that doesn't affect main, and verify storage/compute separation by suspending and resuming compute while watching storage size stay flat.

**Next up: Module 3** — your first hands-on Lakebase project, end-to-end in one notebook.
