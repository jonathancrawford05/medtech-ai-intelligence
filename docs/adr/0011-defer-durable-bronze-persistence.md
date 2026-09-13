# 0011 — Defer durable bronze persistence; Azure ADLS Gen2 is the target

**Status:** Accepted · **Date:** 2026-09-13
**Relates to:** [ADR 0001](0001-local-spark-delta-substrate.md), [ADR 0004](0004-config-driven-table-resolution.md), [finding 0006](../../findings/0006-phase-1-acceptance-met.md)

## Context

Phase 1 acceptance is met: the ingest runs full-sized both locally and on a clean
CI runner (finding 0006). That raises the question `docs/scheduled-ingest.md` left
open — **where bronze actually lives.**

Today there are two copies and neither is durable shared state:

- The developer's local `./lakehouse`, which *does* accumulate snapshots across
  runs but exists on one laptop and is git-ignored.
- The CI runner's `./lakehouse`, which starts empty every run, so the table never
  holds more than one snapshot. `check_bronze_rowcount.py` says as much in its own
  docstring.

The second point matters more than it first appears. Bronze is append-only and
stamped with `ingested_at` + `source_snapshot_id` precisely so history accumulates,
and roadmap Issue 3 (change monitoring — the registry's actual business value)
works by diffing consecutive snapshots. On CI as configured, that history is
discarded every run.

**And bronze history is not reconstructible.** The FDA list is a mutable web
resource: you can only ever fetch *now*. Today's list is re-fetchable forever;
what the list said last month is not. Every week that passes uncaptured is
permanently gone — so deferring persistence has an irreversible cost, not merely
an inconvenient one.

The project is a prototype that has to be sold internally before any cloud spend
is committed. When it does land, it will be Azure.

## Decision

**Defer durable cloud persistence. Name Azure ADLS Gen2 as the target now, and
capture weekly snapshots in the meantime so the intervening history is not lost.**

Concretely:

1. **The local `./lakehouse` is the working store** for the prototype — a
   deliberate choice, not an accident — and the developer refreshes it on a
   regular cadence (`make ingest`, weekly, matching the workflow's schedule).
   That cadence is what makes it a *fallback persistence layer* rather than
   whatever happens to be left from the last debugging session: it accumulates
   real snapshot history, which is what Issue 3 needs to be developed and demoed.

   Two consequences follow, and both are the developer's to own:

   - **A missed week is a permanently missing snapshot.** The cadence is the
     mechanism, so lapsing in it silently costs history that cannot be refetched.
   - **Back the directory up.** `lakehouse/` is git-ignored and is now the only
     *queryable* copy of the accumulated history. Any ordinary backup (Time
     Machine, a periodic `tar` to cloud storage) is sufficient; the point is that
     a lost laptop should not be a lost registry. The 90-day CI archive below is
     the off-machine copy of each individual pull, but it is not a queryable
     table.
2. **CI artifact retention goes 30 → 90 days.** Each weekly run already uploads a
   complete snapshot zip; at 90 days that is a free, immutable weekly archive
   requiring no new infrastructure. This is *capture*, not a working store — see
   "Alternatives" on why the difference matters.
3. **Azure ADLS Gen2 (`abfss://…`) is the recorded target.** No code changes when
   we get there: `settings.lakehouse_root` with `storage_mode=path` already
   addresses it, which is ADR 0004 working as designed. Later, a Unity Catalog
   external table over the same container is `storage_mode=catalog`.

### What triggers revisiting this

Any one of these should close the deferral:

- A business Azure tenancy exists (the trigger we expect).
- Someone other than the original developer needs to query bronze.
- Issue 3 moves from development to something stakeholders rely on.
- Snapshot history becomes valuable enough that a lost laptop would hurt.

## Consequences

- **Issue 2 (bronze→silver) is unblocked** and needs none of this. It reads
  bronze by logical name; where bronze lives is `settings`' problem.
- **The developer's laptop is the system of record** for queryable history. The
  regular local refresh is what keeps it accumulating, and backing the directory
  up is what keeps it surviving; the 90-day artifact archive covers individual
  pulls off-machine but is not a queryable table.
- **The 90-day ceiling is a real deadline.** Snapshots older than 90 days are
  deleted by GitHub. If the Azure decision slips past roughly a quarter, either
  download the archives or accept losing the earliest weeks.
- **The weekly workflow keeps earning its place** even without persistence: it
  proves the pipeline still runs full-sized and detects source drift.
- When Azure arrives, the archived snapshot zips can be replayed into the real
  table in `ingested_at` order, so the history gap is recoverable rather than
  permanent.

## Alternatives considered

**Azure ADLS Gen2 now.** The strongest alternative, and genuinely zero
migration impact since it *is* the target — `abfss://` works today. Rejected for
timing, not merit: the account would be a personal one that the business tenancy
later replaces, so the setup is done twice while only the code path is reused, and
it means carrying cloud credentials in CI for an unsold prototype. Cost was never
the objection — a pull is 83 KB, so this is cents per year at any tier.

**Commit the Delta files to the repo.** Free and immediate, and the worst to live
with. Delta is a directory of parquet plus a `_delta_log` of JSON; weekly appends
add files forever and git keeps every version permanently, so clones slow down
for good and the space cannot be reclaimed without rewriting history. Git's merge
semantics and Delta's transaction log also do not mix — two branches both
ingesting produce a conflict git cannot meaningfully resolve. It is also
orthogonal to Azure, so all of it is discarded at migration.

**Artifacts as the working store** — download the previous artifact, append, and
re-upload. Rejected: a fragile restore step whose silent failure looks exactly
like a fresh table, which is the worst failure mode an append-only store can
have. Using artifacts purely for *capture* (decision 2 above) is a different
proposition: nothing reads them back, so nothing can silently misread them.

## For whoever implements the Azure move

- Add `hadoop-azure` (and its transitive `azure-storage` deps) to the Spark
  classpath, staged the same way `scripts/warm_delta_jars.py` stages Delta.
- Credentials via GitHub secrets → `REGISTRY_`-prefixed env, never in `.env`.
- Set `REGISTRY_LAKEHOUSE_ROOT=abfss://<container>@<account>.dfs.core.windows.net/lakehouse`.
- **Single-writer assumption:** Delta's default LogStore is safe for one writer.
  ADLS Gen2 provides atomic rename, so it is better behaved here than S3 (which
  would need `S3DynamoDBLogStore`). Still, if a local `make ingest` can overlap
  the weekly schedule, that assumption needs revisiting rather than inheriting.
