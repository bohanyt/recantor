"""Namespace fence and logical metrics input.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15

Downgrade projects multiple input-fingerprint rows that share one old key
``(session_id, source_fingerprint, config_fingerprint)`` into a single row.
Counters and stage durations are summed. The earliest queue time and first
useful time are kept. The latest final time is kept. Event keys are the union.
Tokens stay null because a merged input is not an observed token total.
Canonical transcript, job, and evidence tables are not modified. The collapsed
row's separate input identities are not reconstructed on the way back up;
upgrade fills ``input_fingerprint`` from ``source_fingerprint``.
"""

import json
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column("holder_namespace", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "mi_summary_metrics",
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE mi_summary_metrics SET input_fingerprint = source_fingerprint "
        "WHERE input_fingerprint IS NULL"
    )
    op.alter_column(
        "mi_summary_metrics",
        "input_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text("''"),
    )
    op.drop_constraint("mi_summary_metrics_pkey", "mi_summary_metrics", type_="primary")
    op.create_primary_key(
        "mi_summary_metrics_pkey",
        "mi_summary_metrics",
        ["session_id", "input_fingerprint", "config_fingerprint"],
    )
    # 1 was a placeholder, not an intelligence artifact version.
    op.execute(
        "UPDATE mi_summary_session_states SET frozen_transcript_version = NULL "
        "WHERE frozen_transcript_version = 1"
    )


def downgrade() -> None:
    _aggregate_metrics_to_source_key()
    op.drop_constraint("mi_summary_metrics_pkey", "mi_summary_metrics", type_="primary")
    op.create_primary_key(
        "mi_summary_metrics_pkey",
        "mi_summary_metrics",
        ["session_id", "source_fingerprint", "config_fingerprint"],
    )
    op.drop_column("mi_summary_metrics", "input_fingerprint")
    op.drop_column("mi_host_invocation_lease", "holder_namespace")


def _aggregate_metrics_to_source_key() -> None:
    """Collapse input rows onto the 0015 primary key. Telemetry only."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT session_id, input_fingerprint, config_fingerprint, source_fingerprint, "
            "original_queued_at, first_useful_at, final_result_at, provider_calls, retries, "
            "cache_hits, failures, quantum_count, observed_event_keys, extract_ms, "
            "reconcile_ms, check_ms, render_ms "
            "FROM mi_summary_metrics"
        )
    ).mappings()
    grouped: dict[tuple[object, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = (row["session_id"], str(row["source_fingerprint"]), str(row["config_fingerprint"]))
        grouped[key].append(dict(row))
    for items in grouped.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: str(item["input_fingerprint"]))
        keeper = items[0]
        summed = {
            "provider_calls": 0,
            "retries": 0,
            "cache_hits": 0,
            "failures": 0,
            "quantum_count": 0,
            "extract_ms": 0.0,
            "reconcile_ms": 0.0,
            "check_ms": 0.0,
            "render_ms": 0.0,
        }
        queued = None
        first_useful = None
        final_at = None
        events: set[str] = set()
        for item in items:
            for name in (
                "provider_calls",
                "retries",
                "cache_hits",
                "failures",
                "quantum_count",
            ):
                summed[name] = int(summed[name]) + int(item[name] or 0)
            for name in ("extract_ms", "reconcile_ms", "check_ms", "render_ms"):
                summed[name] = float(summed[name]) + float(item[name] or 0.0)
            queued_at = item["original_queued_at"]
            if queued is None or (queued_at is not None and queued_at < queued):
                queued = queued_at
            useful = item["first_useful_at"]
            if useful is not None and (first_useful is None or useful < first_useful):
                first_useful = useful
            finished = item["final_result_at"]
            if finished is not None and (final_at is None or finished > final_at):
                final_at = finished
            events.update(_event_keys(item["observed_event_keys"]))
        connection.execute(
            sa.text(
                "UPDATE mi_summary_metrics SET provider_calls = :provider_calls, "
                "retries = :retries, cache_hits = :cache_hits, failures = :failures, "
                "quantum_count = :quantum_count, extract_ms = :extract_ms, "
                "reconcile_ms = :reconcile_ms, check_ms = :check_ms, render_ms = :render_ms, "
                "original_queued_at = :original_queued_at, first_useful_at = :first_useful_at, "
                "final_result_at = :final_result_at, observed_tokens = NULL, "
                "observed_event_keys = :observed_event_keys "
                "WHERE session_id = :session_id AND input_fingerprint = :input_fingerprint "
                "AND config_fingerprint = :config_fingerprint"
            ),
            {
                **summed,
                "original_queued_at": queued,
                "first_useful_at": first_useful,
                "final_result_at": final_at,
                "observed_event_keys": json.dumps(sorted(events)),
                "session_id": keeper["session_id"],
                "input_fingerprint": keeper["input_fingerprint"],
                "config_fingerprint": keeper["config_fingerprint"],
            },
        )
        for extra in items[1:]:
            connection.execute(
                sa.text(
                    "DELETE FROM mi_summary_metrics WHERE session_id = :session_id "
                    "AND input_fingerprint = :input_fingerprint "
                    "AND config_fingerprint = :config_fingerprint"
                ),
                {
                    "session_id": extra["session_id"],
                    "input_fingerprint": extra["input_fingerprint"],
                    "config_fingerprint": extra["config_fingerprint"],
                },
            )


def _event_keys(payload: object) -> list[str]:
    if not isinstance(payload, str) or not payload:
        return []
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]
