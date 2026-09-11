from pathlib import Path

path = Path("docs/CURRENT.md")
text = path.read_text()
old = """DRAFT PR #40 adds PostgreSQL-authoritative `STTJob` scheduling state, migration/backfill, claim leases with token fencing, session-fair reconciliation, Celery/Redis wake-up hints, bounded provider retries, safe diagnostics/replay, deterministic fake-provider tests, and Compose worker/reconciler wiring. Durable utterance commit does not call Redis, Celery, or the provider; queue/provider availability therefore cannot roll back archive or utterance durability.
"""
new = """DRAFT PR #40 adds PostgreSQL-authoritative `STTJob` scheduling state, migration/backfill, claim leases with token fencing, bounded outstanding admission across reconciliation passes, expiring PostgreSQL delivery hints, least-recently-admitted session rotation, Celery/Redis wake-up hints, bounded provider retries, safe diagnostics/replay, deterministic fake-provider tests, and Compose worker/reconciler wiring. Canonical convergence filters for authoritative transcript evidence before applying its batch bound, so a large non-canonical terminal prefix cannot starve recovery. Production claim/retry eligibility samples PostgreSQL `clock_timestamp()` after the relevant row lock, and scheduler configuration rejects non-positive/pathological values. Durable utterance commit does not call Redis, Celery, or the provider; queue/provider availability therefore cannot roll back archive or utterance durability.
"""
if old not in text:
    raise SystemExit("Phase 2E candidate paragraph not found")
text = text.replace(old, new, 1)
old = "- a large backlog from one session must not starve another session;\n"
new = "- broker admission is bounded globally and per session across reconciliation passes; expiring delivery hints keep PostgreSQL as backlog truth, and session rotation prevents fixed-order starvation when active sessions exceed one batch;\n"
if old not in text:
    raise SystemExit("fairness semantics bullet not found")
path.write_text(text.replace(old, new, 1))
