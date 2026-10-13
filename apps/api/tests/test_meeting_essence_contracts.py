from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from meeting_intelligence_fixtures import (
    build_informational_bundle,
    build_s01_s06_bundle,
    build_s01_s06_manifest,
)

from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    AtomicRecord,
    CheckStatus,
    DeadlineNormalizationBasis,
    EvidenceReference,
    LayeredMeetingResult,
    ManifestSegmentEntry,
    MeetingIntelligenceExampleBundle,
    MeetingIntelligenceJobMetadata,
    OverviewBullet,
    ProcessingJobStatus,
    RecordContentStatus,
    RecordKind,
    RecordRelation,
    RecordRelationType,
    ResultCoverageReport,
    SourceCoverageFlags,
    SourceSnapshotManifest,
    TopicDetail,
    TopicDetailPoint,
    TranscriptGapMarker,
    UnicodeTextSpan,
)
from recantor.meeting_essence.identities import PACKAGE_IDENTITIES
from recantor.meeting_essence.identity import (
    compute_snapshot_content_hash,
    finalize_snapshot_manifest,
    segment_content_hash,
)
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.validation import (
    validate_coverage_report,
    validate_example_bundle,
    validate_manifest_integrity,
    validate_result_against_manifest,
    validate_structural_references,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "meeting_intelligence"
API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[5]
LOCAL_UV = REPO_ROOT / ".recantor" / "tooling" / "uv" / "uv.exe"


def resolve_uv_executable() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    if LOCAL_UV.is_file():
        return str(LOCAL_UV)
    return None


def test_prompt_loader_uses_installed_assets() -> None:
    bundle = load_prompt_bundle()
    assert bundle.package_id == "recantor-meeting-essence-v1"
    assert len(bundle.prompts) == 4
    assert all(len(item.sha256) == 64 for item in bundle.prompts)
    assert len(bundle.cache_key) == 64
    assert "Do not output chain-of-thought" in bundle.prompts[0].text


def test_package_identities_are_known() -> None:
    assert PACKAGE_IDENTITIES.schema_identity.startswith("meeting-essence.contracts/")
    assert PACKAGE_IDENTITIES.algorithm_identity.startswith("meeting-essence.pipeline/")


def test_s01_s06_fixture_semantics() -> None:
    bundle = build_s01_s06_bundle()
    report = validate_example_bundle(bundle)
    assert report.ok, report.issues

    release_record = next(record for record in bundle.records if record.kind == RecordKind.DECISION)
    assert release_record.condition_text is not None
    assert "tes pembayaran" in release_record.condition_text
    assert release_record.content_status == RecordContentStatus.DECISION
    assert any(
        ref.segment_id == UUID("11111111-1111-4111-8111-111111111102")
        for ref in release_record.evidence
    )
    assert any(
        ref.segment_id == UUID("11111111-1111-4111-8111-111111111103")
        for ref in release_record.evidence
    )

    release_topic = next(item for item in bundle.result.topics if item.topic == "Keputusan rilis")
    assert "Selasa" in release_topic.points[0].text
    assert "Jumat" in release_topic.points[0].text

    dini_action = next(item for item in bundle.result.actions if item.assignee == "Dini")
    assert dini_action.content_status == RecordContentStatus.ACCEPTANCE
    assert dini_action.deadline_raw == "besok pukul 12.00"
    assert dini_action.deadline_normalized is None
    assert any(
        ref.segment_id == UUID("11111111-1111-4111-8111-111111111104")
        for ref in dini_action.evidence
    )
    assert any(
        ref.segment_id == UUID("11111111-1111-4111-8111-111111111105")
        for ref in dini_action.evidence
    )

    invoice_action = next(
        item for item in bundle.result.actions if item.assignee_explicitly_undetermined
    )
    assert invoice_action.deadline_unmentioned is True
    assert invoice_action.assignee is None

    assert bundle.result.coverage.structural_check_status == CheckStatus.COMPLETED
    assert bundle.result.coverage.forward_semantic_check_status == CheckStatus.NOT_STARTED
    assert bundle.result.coverage.processing_job_status == ProcessingJobStatus.SUCCEEDED


def test_informational_fixture_has_no_actions() -> None:
    bundle = build_informational_bundle()
    report = validate_example_bundle(bundle)
    assert report.ok
    assert bundle.result.actions == []
    assert bundle.result.atomic_records == []
    assert len(bundle.result.overview_bullets) >= 2
    assert any("12%" in bullet.text for bullet in bundle.result.overview_bullets)
    assert bundle.result.coverage.forward_semantic_check_status == CheckStatus.NOT_STARTED


def test_unicode_emoji_offsets_in_bounds() -> None:
    text = "Catatan 👋 penting"
    segment = ManifestSegmentEntry(
        id=UUID("33333333-3333-4333-8333-333333333301"),
        sequence=1,
        start_ms=0,
        end_ms=1_000,
        text=text,
        content_hash=segment_content_hash(text),
    )
    manifest = finalize_snapshot_manifest(
        SourceSnapshotManifest(
            session_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            snapshot_id="snap_tmp0",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=1,
            segments=[segment],
        )
    )
    snapshot_id = manifest.snapshot_id
    emoji_start = text.index("👋")
    ref = EvidenceReference(
        segment_id=segment.id,
        start_ms=0,
        end_ms=1_000,
        text_span=UnicodeTextSpan(start_cp=emoji_start, end_cp=emoji_start + 1),
    )
    result = LayeredMeetingResult(
        snapshot_id=snapshot_id,
        overview_bullets=[
            OverviewBullet(
                text="Emoji span uji.",
                evidence=[ref],
            )
        ],
        topics=[
            TopicDetail(
                topic="Emoji",
                points=[TopicDetailPoint(text="Sorot emoji", evidence=[ref])],
            )
        ],
        coverage=ResultCoverageReport(
            snapshot_id=snapshot_id,
            source_segments_enumerated=1,
            source_segments_referenced=1,
            structural_check_status=CheckStatus.COMPLETED,
        ),
    )
    report = validate_result_against_manifest(manifest, result)
    assert report.ok


def test_narrower_evidence_timestamps_rejected() -> None:
    manifest = build_s01_s06_manifest()
    segment = manifest.segments[2]
    bad_ref = EvidenceReference(
        segment_id=segment.id,
        start_ms=segment.start_ms + 100,
        end_ms=segment.end_ms - 100,
    )
    result = build_s01_s06_bundle().result
    broken = result.model_copy(
        update={
            "topics": [
                result.topics[0].model_copy(
                    update={
                        "points": [
                            result.topics[0].points[0].model_copy(update={"evidence": [bad_ref]})
                        ]
                    }
                )
            ]
        }
    )
    report = validate_result_against_manifest(manifest, broken)
    assert not report.ok
    assert any(issue.code == "evidence_time_not_exact" for issue in report.issues)


def test_unicode_offset_out_of_bounds_fails() -> None:
    manifest = build_s01_s06_manifest()
    result = build_s01_s06_bundle().result
    bad_ref = EvidenceReference(
        segment_id=manifest.segments[0].id,
        start_ms=manifest.segments[0].start_ms,
        end_ms=manifest.segments[0].end_ms,
        text_span=UnicodeTextSpan(start_cp=0, end_cp=999),
    )
    broken = result.model_copy(
        update={
            "topics": [
                result.topics[0].model_copy(
                    update={
                        "points": [
                            result.topics[0].points[0].model_copy(update={"evidence": [bad_ref]})
                        ]
                    }
                )
            ]
        }
    )
    report = validate_result_against_manifest(manifest, broken)
    assert not report.ok
    assert any(issue.code == "offset_out_of_bounds" for issue in report.issues)


def test_missing_segment_reference_fails() -> None:
    manifest = build_s01_s06_manifest()
    result = build_s01_s06_bundle().result
    bad_ref = EvidenceReference(
        segment_id=UUID("99999999-9999-4999-8999-999999999999"),
        start_ms=0,
        end_ms=1,
    )
    broken = result.model_copy(
        update={
            "topics": [
                result.topics[0].model_copy(
                    update={
                        "points": [
                            result.topics[0].points[0].model_copy(update={"evidence": [bad_ref]})
                        ]
                    }
                )
            ]
        }
    )
    report = validate_result_against_manifest(manifest, broken)
    assert not report.ok
    assert any(issue.code == "missing_segment" for issue in report.issues)


def test_malformed_content_status_for_kind() -> None:
    with pytest.raises(ValueError, match="content_status"):
        AtomicRecord(
            record_id="rec_bad",
            topic="x",
            kind=RecordKind.FINDING,
            claim_text="y",
            content_status=RecordContentStatus.ACCEPTANCE,
            evidence=[
                EvidenceReference(
                    segment_id=UUID("11111111-1111-4111-8111-111111111101"),
                    start_ms=12_000,
                    end_ms=15_000,
                )
            ],
        )


def test_extra_fields_forbidden_on_atomic_record() -> None:
    with pytest.raises(ValueError):
        AtomicRecord.model_validate(
            {
                "record_id": "rec_x",
                "topic": "t",
                "kind": "finding",
                "claim_text": "c",
                "content_status": "finding",
                "evidence": [
                    {
                        "segment_id": "11111111-1111-4111-8111-111111111101",
                        "start_ms": 12000,
                        "end_ms": 15000,
                    }
                ],
                "speaker": "invented",
            }
        )


def test_unknown_relation_identity_fails() -> None:
    manifest = build_s01_s06_manifest()
    records = build_s01_s06_bundle().records
    relations = [
        RecordRelation(
            relation_type=RecordRelationType.SUPPORTS,
            from_record_id="rec_missing",
            to_record_id=records[0].record_id,
            evidence=[records[0].evidence[0]],
        )
    ]
    report = validate_structural_references(manifest, records, relations)
    assert not report.ok


def test_committed_json_fixtures_validate() -> None:
    for name in ("s01_s06_example.json", "informational_example.json"):
        path = FIXTURE_DIR / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        bundle = MeetingIntelligenceExampleBundle.model_validate(payload)
        assert validate_example_bundle(bundle).ok


def test_manifest_hash_tamper_detected() -> None:
    manifest = build_s01_s06_manifest()
    tampered = manifest.model_copy(update={"snapshot_content_hash": "0" * 64})
    report = validate_manifest_integrity(tampered)
    assert not report.ok
    assert any(issue.code == "snapshot_hash_mismatch" for issue in report.issues)


def test_segment_text_change_without_hash_fails_model() -> None:
    manifest = build_s01_s06_manifest()
    segment = manifest.segments[0]
    payload = segment.model_dump()
    payload["text"] = segment.text + " tampered"
    with pytest.raises(ValueError, match="content_hash"):
        ManifestSegmentEntry.model_validate(payload)


def test_false_coverage_counts_rejected() -> None:
    bundle = build_s01_s06_bundle()
    broken = bundle.result.model_copy(
        update={
            "coverage": bundle.result.coverage.model_copy(
                update={"source_segments_referenced": 999}
            )
        }
    )
    report = validate_coverage_report(bundle.manifest, broken)
    assert not report.ok
    assert any(issue.code == "coverage_referenced_mismatch" for issue in report.issues)


def test_semantic_completed_requires_method() -> None:
    with pytest.raises(ValueError, match="semantic_check_method"):
        ResultCoverageReport(
            snapshot_id="snap_x",
            source_segments_enumerated=1,
            source_segments_referenced=1,
            forward_semantic_check_status=CheckStatus.COMPLETED,
        )


def test_check_pending_does_not_imply_undecided_in_fixture() -> None:
    bundle = build_s01_s06_bundle()
    pending = bundle.result.model_copy(
        update={
            "coverage": bundle.result.coverage.model_copy(
                update={"forward_semantic_check_status": CheckStatus.IN_PROGRESS}
            )
        }
    )
    assert any("Selasa" in bullet.text for bullet in pending.overview_bullets)
    assert any(record.content_status == RecordContentStatus.DECISION for record in bundle.records)


def test_source_pending_empty_snapshot_representable() -> None:
    manifest = finalize_snapshot_manifest(
        SourceSnapshotManifest(
            session_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            snapshot_id="snap_tmp0",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=0,
            segments=[],
            coverage=SourceCoverageFlags(source_pending=True),
        )
    )
    assert validate_manifest_integrity(manifest).ok


def test_meeting_intelligence_module_coexists_with_meeting_essence_package() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        (root / "recantor").mkdir()
        (root / "recantor" / "__init__.py").write_text("", encoding="utf-8")
        (root / "recantor" / "meeting_intelligence.py").write_text(
            "class CodexSubscriptionMeetingIntelligenceProvider:\n    pass\n",
            encoding="utf-8",
        )
        (root / "recantor" / "meeting_essence").mkdir()
        (root / "recantor" / "meeting_essence" / "__init__.py").write_text(
            '"""essence stub"""',
            encoding="utf-8",
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib, sys; "
                f"sys.path.insert(0, r'{root}'); "
                "legacy = importlib.import_module('recantor.meeting_intelligence'); "
                "essence = importlib.import_module('recantor.meeting_essence'); "
                "assert hasattr(legacy, 'CodexSubscriptionMeetingIntelligenceProvider'); "
                "assert essence.__doc__ == 'essence stub'",
            ],
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr


def test_wheel_install_exposes_prompt_assets() -> None:
    uv = resolve_uv_executable()
    if uv is None:
        pytest.skip("uv executable not available")

    with tempfile.TemporaryDirectory() as workspace:
        work = Path(workspace)
        wheel_dir = work / "wheels"
        target_dir = work / "site-packages"
        wheel_dir.mkdir()
        target_dir.mkdir()
        build = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
            cwd=API_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert build.returncode == 0
        wheel_path = next(wheel_dir.glob("*.whl"))
        install = subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(target_dir),
                str(wheel_path),
            ],
            cwd=API_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert install.returncode == 0
        target_literal = str(target_dir.resolve()).replace("\\", "\\\\")
        probe_script = (
            "import sys; from pathlib import Path; "
            f"target = Path(r'{target_literal}'); "
            f"sys.path.insert(0, str(target)); "
            "from recantor.meeting_essence.prompt_loader import load_prompt_bundle; "
            "bundle = load_prompt_bundle(); "
            "assert bundle.package_id == 'recantor-meeting-essence-v1'; "
            "assert len(bundle.prompts) == 4; "
            "assert all(len(item.sha256) == 64 for item in bundle.prompts); "
            "import recantor.meeting_essence.prompt_loader as pl; "
            "module_path = Path(pl.__file__).resolve(); "
            "assert target in module_path.parents; "
            "texts = [p.text for p in bundle.prompts]; "
            "assert all(len(t) > 20 for t in texts); "
            "assert any('chain-of-thought' in t.lower() for t in texts)"
        )
        probe = subprocess.run(
            [sys.executable, "-c", probe_script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert probe.returncode == 0, probe.stderr or probe.stdout


def test_meeting_metadata_in_snapshot_hash_not_captured_at() -> None:
    manifest = build_s01_s06_manifest()
    original_hash = manifest.snapshot_content_hash
    shifted_capture = manifest.model_copy(
        update={"captured_at": datetime(2031, 6, 1, tzinfo=UTC)},
    )
    assert compute_snapshot_content_hash(shifted_capture) == original_hash

    metadata_changed = manifest.model_copy(
        update={
            "meeting_metadata": {"meeting_date": "2030-01-01", "timezone": "Asia/Bangkok"},
        },
    )
    assert compute_snapshot_content_hash(metadata_changed) != original_hash


def test_segment_whitespace_and_crlf_hash_preservation() -> None:
    canonical = "Catatan penting"
    padded = f"  {canonical}  "
    crlf = f"{canonical}\r\n"
    assert segment_content_hash(padded) != segment_content_hash(canonical)
    assert segment_content_hash(crlf) != segment_content_hash(f"{canonical}\n")
    ManifestSegmentEntry(
        id=UUID("44444444-4444-4444-8444-444444444401"),
        sequence=1,
        start_ms=0,
        end_ms=1000,
        text=padded,
        content_hash=segment_content_hash(padded),
    )


def test_deadline_normalized_without_anchors_rejected() -> None:
    bundle = build_s01_s06_bundle()
    normalized = datetime(2030, 1, 1, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    broken_action = bundle.result.actions[0].model_copy(
        update={
            "deadline_normalized": normalized,
            "deadline_normalization_basis": DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE,
        }
    )
    broken = bundle.result.model_copy(
        update={"actions": [broken_action, *bundle.result.actions[1:]]}
    )
    report = validate_result_against_manifest(bundle.manifest, broken)
    assert not report.ok
    assert any(issue.code == "deadline_anchor_missing" for issue in report.issues)


def test_deadline_normalized_with_manifest_anchors_validates() -> None:
    manifest = build_s01_s06_manifest().model_copy(
        update={
            "meeting_metadata": {"meeting_date": "2030-01-01", "timezone": "Asia/Bangkok"},
        },
    )
    manifest = finalize_snapshot_manifest(manifest)
    bundle = build_s01_s06_bundle()
    normalized = datetime(2030, 1, 1, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    action = ActionFollowUpItem(
        task="Kabari pelanggan soal perubahan jadwal",
        assignee="Dini",
        deadline_raw="besok pukul 12.00",
        deadline_normalized=normalized,
        deadline_normalization_basis=DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE,
        content_status=RecordContentStatus.ACCEPTANCE,
        evidence=bundle.result.actions[0].evidence,
    )
    result = bundle.result.model_copy(
        update={
            "snapshot_id": manifest.snapshot_id,
            "actions": [action, bundle.result.actions[1]],
            "coverage": bundle.result.coverage.model_copy(
                update={"snapshot_id": manifest.snapshot_id}
            ),
        }
    )
    report = validate_result_against_manifest(manifest, result)
    assert report.ok, report.issues


def test_ledger_relation_evidence_mismatch_rejected() -> None:
    bundle = build_s01_s06_bundle()
    relation = bundle.relations[0]
    tampered_relation = relation.model_copy(
        update={
            "evidence": [
                EvidenceReference(
                    segment_id=UUID("11111111-1111-4111-8111-111111111101"),
                    start_ms=12_000,
                    end_ms=15_000,
                )
            ]
        }
    )
    broken = bundle.model_copy(update={"relations": [tampered_relation]})
    report = validate_example_bundle(broken)
    assert not report.ok
    assert any(issue.code == "ledger_relations_inconsistent" for issue in report.issues)


def test_overview_unknown_segment_reference_rejected() -> None:
    manifest = build_s01_s06_manifest()
    bundle = build_s01_s06_bundle()
    bad_bullet = OverviewBullet(
        text="Klaim tanpa sumber valid.",
        evidence=[
            EvidenceReference(
                segment_id=UUID("99999999-9999-4999-8999-999999999999"),
                start_ms=0,
                end_ms=1,
            )
        ],
    )
    broken = bundle.result.model_copy(
        update={"overview_bullets": [bad_bullet, *bundle.result.overview_bullets[1:]]}
    )
    report = validate_result_against_manifest(manifest, broken)
    assert not report.ok
    assert any(issue.code == "missing_segment" for issue in report.issues)


def test_complete_publication_boundary_ahead_of_segments_rejected() -> None:
    segments = build_s01_s06_manifest().segments[:3]
    with pytest.raises(ValueError, match="complete publication missing coverage"):
        SourceSnapshotManifest(
            session_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            snapshot_id="snap_tmp",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=6,
            segments=segments,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
        )


def test_sparse_segments_allowed_when_publication_not_complete() -> None:
    segments = build_s01_s06_manifest().segments[:3]
    manifest = finalize_snapshot_manifest(
        SourceSnapshotManifest(
            session_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
            snapshot_id="snap_tmp",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=6,
            segments=segments,
            coverage=SourceCoverageFlags(transcript_publication_complete=False),
        )
    )
    assert validate_manifest_integrity(manifest).ok


def test_explicit_gap_allows_complete_publication_with_missing_sequences() -> None:
    segments = build_s01_s06_manifest().segments[:3]
    manifest = finalize_snapshot_manifest(
        SourceSnapshotManifest(
            session_id=UUID("12121212-1212-4121-8121-121212121212"),
            snapshot_id="snap_tmp",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=6,
            segments=segments,
            coverage=SourceCoverageFlags(
                transcript_publication_complete=True,
                explicit_gaps=[
                    TranscriptGapMarker(sequence_start=4, sequence_end=6, reason="stt backlog"),
                ],
            ),
        )
    )
    assert validate_manifest_integrity(manifest).ok


def test_unknown_job_schema_identity_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported schema_identity"):
        MeetingIntelligenceJobMetadata(
            job_id=UUID("13131313-1313-4131-8131-131313131313"),
            session_id=UUID("14141414-1414-4141-8141-141414141414"),
            snapshot_id="snap_test",
            prompt_bundle_id=PACKAGE_IDENTITIES.package_id,
            schema_identity="meeting-essence.contracts/unknown",
            algorithm_identity=PACKAGE_IDENTITIES.algorithm_identity,
            input_fingerprint="a" * 64,
        )


def test_strict_int_rejects_bool_sequence_coercion() -> None:
    with pytest.raises(ValueError):
        ManifestSegmentEntry.model_validate(
            {
                "id": "33333333-3333-4333-8333-333333333301",
                "sequence": True,
                "start_ms": 0,
                "end_ms": 1000,
                "text": "x",
                "content_hash": segment_content_hash("x"),
            }
        )
