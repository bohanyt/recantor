from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    EvidenceReference,
    LayeredMeetingResult,
    ManifestSegmentEntry,
    MeetingIntelligenceExampleBundle,
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
)
from recantor.meeting_essence.identity import (
    compute_record_identity,
    finalize_snapshot_manifest,
    segment_content_hash,
)

FIXTURE_SESSION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

SEGMENT_SPECS: list[tuple[str, int, int, int, str]] = [
    ("11111111-1111-4111-8111-111111111101", 1, 12_000, 15_000, "Rencana awal kita rilis Jumat."),
    (
        "11111111-1111-4111-8111-111111111102",
        2,
        48_000,
        52_000,
        "Jangan rilis sebelum tes pembayaran lulus.",
    ),
    (
        "11111111-1111-4111-8111-111111111103",
        3,
        96_000,
        102_000,
        "Tes pembayaran belum lulus. Kita putuskan rilis Selasa; "
        "tetap hanya kalau tes pembayaran sudah lulus.",
    ),
    (
        "11111111-1111-4111-8111-111111111104",
        4,
        97_000,
        98_000,
        "Dini, tolong kabari pelanggan soal perubahan jadwal.",
    ),
    (
        "11111111-1111-4111-8111-111111111105",
        5,
        98_000,
        99_000,
        "Dini menyetujui tugas mengabari pelanggan dan menyatakan akan "
        "melakukannya besok pukul 12.00.",
    ),
    (
        "11111111-1111-4111-8111-111111111106",
        6,
        100_000,
        101_000,
        "Invoice vendor perlu diperiksa. Belum ditentukan siapa yang menangani.",
    ),
]


def _evidence(segment_id: str, start_ms: int, end_ms: int) -> EvidenceReference:
    return EvidenceReference(
        segment_id=UUID(segment_id),
        start_ms=start_ms,
        end_ms=end_ms,
    )


def build_s01_s06_manifest() -> SourceSnapshotManifest:
    segments = [
        ManifestSegmentEntry(
            id=UUID(segment_id),
            sequence=sequence,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            content_hash=segment_content_hash(text),
        )
        for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
    ]
    manifest = SourceSnapshotManifest(
        session_id=FIXTURE_SESSION_ID,
        snapshot_id="snap_tmp0",
        snapshot_content_hash="0" * 64,
        publication_boundary_sequence=6,
        captured_at=datetime(2026, 10, 13, 4, 0, tzinfo=UTC),
        segments=segments,
        coverage=SourceCoverageFlags(
            transcript_publication_complete=True, stt_backlog_pending=False
        ),
    )
    return finalize_snapshot_manifest(manifest)


def build_s01_s06_bundle() -> MeetingIntelligenceExampleBundle:
    manifest = build_s01_s06_manifest()
    release_decision_id = compute_record_identity(
        "Rilis produk",
        RecordKind.DECISION.value,
        "11111111-1111-4111-8111-111111111103",
        "Rilis Selasa dengan syarat tes pembayaran lulus",
    )
    friday_proposal_id = compute_record_identity(
        "Rilis produk",
        RecordKind.PROPOSAL.value,
        "11111111-1111-4111-8111-111111111101",
        "Rencana rilis Jumat",
    )
    dini_task_id = compute_record_identity(
        "Komunikasi pelanggan",
        RecordKind.ACTION.value,
        "11111111-1111-4111-8111-111111111105",
        "Kabari pelanggan soal perubahan jadwal",
    )
    invoice_id = compute_record_identity(
        "Invoice vendor",
        RecordKind.ACTION.value,
        "11111111-1111-4111-8111-111111111106",
        "Invoice vendor perlu diperiksa",
    )

    records = [
        AtomicRecord(
            record_id=friday_proposal_id,
            topic="Rilis produk",
            kind=RecordKind.PROPOSAL,
            claim_text="Rencana awal rilis Jumat.",
            content_status=RecordContentStatus.PROPOSAL,
            evidence=[_evidence("11111111-1111-4111-8111-111111111101", 12_000, 15_000)],
        ),
        AtomicRecord(
            record_id=release_decision_id,
            topic="Rilis produk",
            kind=RecordKind.DECISION,
            claim_text="Rilis dipindah ke Selasa dan tetap mensyaratkan tes pembayaran lulus.",
            content_status=RecordContentStatus.DECISION,
            condition_text="Hanya jika tes pembayaran sudah lulus.",
            evidence=[
                _evidence("11111111-1111-4111-8111-111111111102", 48_000, 52_000),
                _evidence("11111111-1111-4111-8111-111111111103", 96_000, 102_000),
            ],
        ),
        AtomicRecord(
            record_id=dini_task_id,
            topic="Komunikasi pelanggan",
            kind=RecordKind.ACTION,
            claim_text="Dini menerima tugas mengabari pelanggan tentang perubahan jadwal.",
            content_status=RecordContentStatus.ACCEPTANCE,
            assignee="Dini",
            deadline_raw="besok pukul 12.00",
            deadline_normalized=None,
            evidence=[
                _evidence("11111111-1111-4111-8111-111111111104", 97_000, 98_000),
                _evidence("11111111-1111-4111-8111-111111111105", 98_000, 99_000),
            ],
        ),
        AtomicRecord(
            record_id=invoice_id,
            topic="Invoice vendor",
            kind=RecordKind.ACTION,
            claim_text="Invoice vendor perlu diperiksa; PIC belum ditentukan.",
            content_status=RecordContentStatus.REQUEST,
            assignee=None,
            assignee_explicitly_undetermined=True,
            deadline_unmentioned=True,
            evidence=[_evidence("11111111-1111-4111-8111-111111111106", 100_000, 101_000)],
        ),
    ]
    relations = [
        RecordRelation(
            relation_type=RecordRelationType.SUPERSEDES,
            from_record_id=release_decision_id,
            to_record_id=friday_proposal_id,
            evidence=[_evidence("11111111-1111-4111-8111-111111111103", 96_000, 102_000)],
        )
    ]

    result = LayeredMeetingResult(
        snapshot_id=manifest.snapshot_id,
        overview_bullets=[
            OverviewBullet(
                text=(
                    "Rilis dipindah dari Jumat ke Selasa karena tes pembayaran belum lulus, "
                    "dengan syarat tes tersebut lulus sebelum rilis."
                ),
                evidence=[
                    _evidence("11111111-1111-4111-8111-111111111101", 12_000, 15_000),
                    _evidence("11111111-1111-4111-8111-111111111103", 96_000, 102_000),
                ],
            ),
            OverviewBullet(
                text=(
                    "Dini menerima tugas mengabari pelanggan tentang perubahan jadwal; "
                    "tenggat relatif 'besok pukul 12.00' disimpan mentah "
                    "tanpa normalisasi kalender."
                ),
                evidence=[
                    _evidence("11111111-1111-4111-8111-111111111104", 97_000, 98_000),
                    _evidence("11111111-1111-4111-8111-111111111105", 98_000, 99_000),
                ],
            ),
            OverviewBullet(
                text=(
                    "Pemeriksaan invoice vendor masih perlu dilakukan; PIC belum ditentukan "
                    "dan tenggat belum disebutkan dalam sumber."
                ),
                evidence=[_evidence("11111111-1111-4111-8111-111111111106", 100_000, 101_000)],
            ),
        ],
        topics=[
            TopicDetail(
                topic="Keputusan rilis",
                points=[
                    TopicDetailPoint(
                        text=(
                            "Rencana awal rilis Jumat digantikan keputusan rilis Selasa "
                            "dengan syarat tes pembayaran lulus."
                        ),
                        evidence=[
                            _evidence("11111111-1111-4111-8111-111111111101", 12_000, 15_000),
                            _evidence("11111111-1111-4111-8111-111111111103", 96_000, 102_000),
                        ],
                    )
                ],
            ),
            TopicDetail(
                topic="Komunikasi pelanggan",
                points=[
                    TopicDetailPoint(
                        text=(
                            "Dini menerima tugas mengabari pelanggan; pekerjaan diterima "
                            "tetapi belum dilaporkan selesai."
                        ),
                        evidence=[
                            _evidence("11111111-1111-4111-8111-111111111104", 97_000, 98_000),
                            _evidence("11111111-1111-4111-8111-111111111105", 98_000, 99_000),
                        ],
                    )
                ],
            ),
            TopicDetail(
                topic="Invoice vendor",
                points=[
                    TopicDetailPoint(
                        text="Invoice vendor perlu diperiksa tanpa PIC yang ditetapkan.",
                        evidence=[
                            _evidence("11111111-1111-4111-8111-111111111106", 100_000, 101_000)
                        ],
                    )
                ],
            ),
        ],
        actions=[
            ActionFollowUpItem(
                task="Kabari pelanggan soal perubahan jadwal",
                assignee="Dini",
                deadline_raw="besok pukul 12.00",
                deadline_normalized=None,
                content_status=RecordContentStatus.ACCEPTANCE,
                evidence=[
                    _evidence("11111111-1111-4111-8111-111111111104", 97_000, 98_000),
                    _evidence("11111111-1111-4111-8111-111111111105", 98_000, 99_000),
                ],
            ),
            ActionFollowUpItem(
                task="Periksa invoice vendor",
                assignee=None,
                assignee_explicitly_undetermined=True,
                deadline_unmentioned=True,
                content_status=RecordContentStatus.REQUEST,
                evidence=[_evidence("11111111-1111-4111-8111-111111111106", 100_000, 101_000)],
            ),
        ],
        open_items=[],
        atomic_records=records,
        relations=relations,
        coverage=ResultCoverageReport(
            snapshot_id=manifest.snapshot_id,
            source_segments_enumerated=6,
            source_segments_referenced=6,
            processing_job_status=ProcessingJobStatus.SUCCEEDED,
            structural_check_status=CheckStatus.COMPLETED,
            forward_semantic_check_status=CheckStatus.NOT_STARTED,
            backward_semantic_check_status=CheckStatus.NOT_STARTED,
            limitations=[],
        ),
    )
    return MeetingIntelligenceExampleBundle(
        manifest=manifest,
        result=result,
        records=records,
        relations=relations,
    )


def build_informational_bundle() -> MeetingIntelligenceExampleBundle:
    text_a = (
        "Tim riset mempresentasikan temuan adopsi fitur onboarding naik 12% "
        "setelah tutorial interaktif."
    )
    text_b = (
        "Diskusi menilai opsi A (hemat biaya) dan opsi B (lebih cepat); belum ada keputusan akhir."
    )
    segments = [
        ManifestSegmentEntry(
            id=UUID("22222222-2222-4222-8222-222222222201"),
            sequence=1,
            start_ms=5_000,
            end_ms=20_000,
            text=text_a,
            content_hash=segment_content_hash(text_a),
        ),
        ManifestSegmentEntry(
            id=UUID("22222222-2222-4222-8222-222222222202"),
            sequence=2,
            start_ms=25_000,
            end_ms=45_000,
            text=text_b,
            content_hash=segment_content_hash(text_b),
        ),
    ]
    manifest = finalize_snapshot_manifest(
        SourceSnapshotManifest(
            session_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            snapshot_id="snap_tmp0",
            snapshot_content_hash="0" * 64,
            publication_boundary_sequence=2,
            segments=segments,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
        )
    )
    snapshot_id = manifest.snapshot_id
    result = LayeredMeetingResult(
        snapshot_id=snapshot_id,
        overview_bullets=[
            OverviewBullet(
                text=(
                    "Tim riset melaporkan adopsi fitur onboarding naik 12% "
                    "setelah tutorial interaktif."
                ),
                evidence=[_evidence("22222222-2222-4222-8222-222222222201", 5_000, 20_000)],
            ),
            OverviewBullet(
                text=(
                    "Peserta membandingkan opsi A (hemat biaya) dan opsi B (lebih cepat) "
                    "tanpa keputusan final."
                ),
                evidence=[_evidence("22222222-2222-4222-8222-222222222202", 25_000, 45_000)],
            ),
        ],
        topics=[
            TopicDetail(
                topic="Temuan riset",
                points=[
                    TopicDetailPoint(
                        text="Adopsi onboarding naik 12% setelah tutorial interaktif.",
                        evidence=[
                            _evidence("22222222-2222-4222-8222-222222222201", 5_000, 20_000),
                        ],
                    )
                ],
            ),
            TopicDetail(
                topic="Opsi lanjutan",
                points=[
                    TopicDetailPoint(
                        text=(
                            "Opsi A menekankan efisiensi biaya; "
                            "opsi B menekankan kecepatan implementasi."
                        ),
                        evidence=[
                            _evidence("22222222-2222-4222-8222-222222222202", 25_000, 45_000),
                        ],
                    )
                ],
            ),
        ],
        actions=[],
        open_items=[],
        coverage=ResultCoverageReport(
            snapshot_id=snapshot_id,
            source_segments_enumerated=2,
            source_segments_referenced=2,
            processing_job_status=ProcessingJobStatus.SUCCEEDED,
            structural_check_status=CheckStatus.COMPLETED,
            forward_semantic_check_status=CheckStatus.NOT_STARTED,
            backward_semantic_check_status=CheckStatus.NOT_STARTED,
            limitations=[
                CoverageLimitation(
                    code="informational_no_actions",
                    message=(
                        "Tidak ada tindak lanjut atau keputusan yang diekstrak "
                        "karena sumber hanya berisi temuan dan opsi."
                    ),
                )
            ],
        ),
    )
    return MeetingIntelligenceExampleBundle(manifest=manifest, result=result)
