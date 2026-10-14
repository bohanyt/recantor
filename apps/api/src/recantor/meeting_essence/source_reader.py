from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from recantor.meeting_essence.contracts import (
    ManifestSegmentEntry,
    SourceCoverageFlags,
    SourceSnapshotManifest,
    TranscriptGapMarker,
    UnicodeTextSpan,
)
from recantor.meeting_essence.identity import finalize_snapshot_manifest, segment_content_hash
from recantor.meeting_essence.wire_payload import SourceDescriptor, WireSegmentFragment


@dataclass(frozen=True)
class CanonicalSegmentInput:
    id: UUID
    sequence: int
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class FrozenSourceExpectation:
    publication_boundary_sequence: int
    segment_ids: tuple[UUID, ...]
    sequences: tuple[int, ...]
    content_hashes: tuple[str, ...]
    start_ms: tuple[int, ...] = ()
    end_ms: tuple[int, ...] = ()


@dataclass
class SourceReadPage:
    page_index: int
    segments: list[ManifestSegmentEntry]
    has_more: bool


@dataclass
class SourceCoverageReport:
    ok: bool
    missing_segment_ids: list[UUID] = field(default_factory=list)
    unexpected_segment_ids: list[UUID] = field(default_factory=list)
    missing_sequences: list[int] = field(default_factory=list)
    uncovered_spans: list[tuple[int, int]] = field(default_factory=list)
    message: str = ""


class CanonicalSourceReader(Protocol):
    def freeze_publication_boundary(self, boundary_sequence: int) -> FrozenSourceExpectation: ...

    def frozen_expectation(self) -> FrozenSourceExpectation: ...

    def read_segments_page(self, *, page_index: int, page_size: int) -> SourceReadPage: ...


@dataclass
class InMemoryCanonicalSourceReader:
    """Read-model seam: freeze once, paginate bounded reads, ignore post-freeze growth."""

    segments: list[CanonicalSegmentInput]
    _post_freeze_injections: list[CanonicalSegmentInput] = field(default_factory=list)
    _frozen: FrozenSourceExpectation | None = None
    _frozen_rows: list[ManifestSegmentEntry] = field(default_factory=list)
    _live_segments: list[CanonicalSegmentInput] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._live_segments = list(self.segments)

    def freeze_publication_boundary(self, boundary_sequence: int) -> FrozenSourceExpectation:
        if boundary_sequence < 0:
            msg = "publication boundary must be non-negative"
            raise ValueError(msg)
        eligible = [
            segment
            for segment in sorted(self._live_segments, key=lambda item: item.sequence)
            if segment.sequence <= boundary_sequence
        ]
        rows = [
            ManifestSegmentEntry(
                id=segment.id,
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                language=segment.language,
                content_hash=segment_content_hash(segment.text),
                created_at=segment.created_at,
            )
            for segment in eligible
        ]
        self._frozen_rows = rows
        frozen = FrozenSourceExpectation(
            publication_boundary_sequence=boundary_sequence,
            segment_ids=tuple(row.id for row in rows),
            sequences=tuple(row.sequence for row in rows),
            content_hashes=tuple(row.content_hash for row in rows),
            start_ms=tuple(row.start_ms for row in rows),
            end_ms=tuple(row.end_ms for row in rows),
        )
        self._frozen = frozen
        return frozen

    def frozen_expectation(self) -> FrozenSourceExpectation:
        if self._frozen is None:
            msg = "source reader is not frozen"
            raise RuntimeError(msg)
        return self._frozen

    def inject_post_freeze_segment(self, segment: CanonicalSegmentInput) -> None:
        """Test hook: simulate concurrent publisher growth after freeze."""
        self._post_freeze_injections.append(segment)
        self._live_segments.append(segment)

    def read_segments_page(self, *, page_index: int, page_size: int) -> SourceReadPage:
        if self._frozen is None:
            msg = "cannot read pages before freeze"
            raise RuntimeError(msg)
        if page_size < 1:
            msg = "page_size must be >= 1"
            raise ValueError(msg)
        ordered = sorted(self._frozen_rows, key=lambda item: item.sequence)
        start = page_index * page_size
        end = start + page_size
        page_rows = ordered[start:end]
        return SourceReadPage(
            page_index=page_index,
            segments=page_rows,
            has_more=end < len(ordered),
        )


def uncovered_sequence_spans(
    boundary: int,
    present_sequences: set[int],
    gap_intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Holes in 1..boundary without allocating the integer range."""
    if boundary < 1:
        return []
    intervals: list[tuple[int, int]] = []
    for sequence in present_sequences:
        if 1 <= sequence <= boundary:
            intervals.append((sequence, sequence))
    for start, end in gap_intervals:
        lo = max(1, start)
        hi = min(boundary, end)
        if lo <= hi:
            intervals.append((lo, hi))
    intervals.sort()
    merged: list[list[int]] = []
    for lo, hi in intervals:
        if not merged or lo > merged[-1][1] + 1:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    holes: list[tuple[int, int]] = []
    cursor = 1
    for lo, hi in merged:
        if lo > cursor:
            holes.append((cursor, lo - 1))
        cursor = hi + 1
    if cursor <= boundary:
        holes.append((cursor, boundary))
    return holes


def _bounded_missing_sequences(holes: list[tuple[int, int]]) -> list[int]:
    missing: list[int] = []
    for lo, hi in holes:
        length = hi - lo + 1
        if length <= 32 and len(missing) + length <= 64:
            missing.extend(range(lo, hi + 1))
        else:
            missing.append(lo)
    return missing


def _pending_manifest(
    *,
    session_id: UUID,
    meeting_metadata: dict[str, str] | None,
    captured_at: datetime | None,
) -> SourceSnapshotManifest:
    manifest = SourceSnapshotManifest(
        session_id=session_id,
        snapshot_id="snap_pending",
        snapshot_content_hash="0" * 64,
        publication_boundary_sequence=0,
        captured_at=captured_at,
        meeting_metadata=meeting_metadata or {},
        segments=[],
        coverage=SourceCoverageFlags(source_pending=True),
    )
    return finalize_snapshot_manifest(manifest)


def materialize_manifest_from_reader(
    reader: CanonicalSourceReader,
    *,
    session_id: UUID,
    coverage: SourceCoverageFlags,
    meeting_metadata: dict[str, str] | None = None,
    captured_at: datetime | None = None,
    page_size: int = 32,
    max_pages: int = 10_000,
) -> tuple[SourceSnapshotManifest, SourceCoverageReport]:
    """Enumerate manifest pages independently of downstream chunk success."""
    try:
        expectation = reader.frozen_expectation()
    except Exception:  # noqa: BLE001 - malformed readers become source partial
        report = SourceCoverageReport(ok=False, message="source reader failed before enumeration")
        return (
            _pending_manifest(
                session_id=session_id,
                meeting_metadata=meeting_metadata,
                captured_at=captured_at,
            ),
            report,
        )
    collected: list[ManifestSegmentEntry] = []
    page_index = 0
    seen_page_fingerprints: set[tuple[str, ...]] = set()
    read_failed = False
    nonprogress = False
    last_sequence = 0
    while True:
        if page_index >= max_pages:
            nonprogress = True
            break
        try:
            page = reader.read_segments_page(page_index=page_index, page_size=page_size)
        except Exception:  # noqa: BLE001 - malformed readers become source partial
            read_failed = True
            break
        fingerprint = tuple(str(segment.id) for segment in page.segments)
        if fingerprint in seen_page_fingerprints and (page.has_more or not page.segments):
            nonprogress = True
            break
        seen_page_fingerprints.add(fingerprint)
        if page.segments and page.segments[0].sequence < last_sequence:
            nonprogress = True
            break
        if page.segments:
            last_sequence = page.segments[-1].sequence
        collected.extend(page.segments)
        if not page.has_more:
            break
        if not page.segments:
            nonprogress = True
            break
        page_index += 1

    identity_mismatches: list[UUID] = []
    expected_by_id: dict[UUID, tuple[str, int, int, int]] = {}
    starts = expectation.start_ms or tuple(0 for _ in expectation.segment_ids)
    ends = expectation.end_ms or tuple(0 for _ in expectation.segment_ids)
    for segment_id, content_hash, sequence, start_ms, end_ms in zip(
        expectation.segment_ids,
        expectation.content_hashes,
        expectation.sequences,
        starts,
        ends,
        strict=False,
    ):
        expected_by_id[segment_id] = (content_hash, sequence, start_ms, end_ms)
    for segment in collected:
        expected = expected_by_id.get(segment.id)
        if expected is None:
            continue
        content_hash, sequence, start_ms, end_ms = expected
        times_known = bool(expectation.start_ms)
        time_ok = (not times_known) or (segment.start_ms == start_ms and segment.end_ms == end_ms)
        if segment.content_hash != content_hash or segment.sequence != sequence or not time_ok:
            identity_mismatches.append(segment.id)

    duplicate_ids = len(collected) != len({segment.id for segment in collected})
    collected_ids = {segment.id for segment in collected}
    expected_ids = set(expectation.segment_ids)
    missing = sorted(expected_ids - collected_ids, key=str)
    unexpected = sorted(collected_ids - expected_ids, key=str)
    present_sequences = {segment.sequence for segment in collected}
    holes: list[tuple[int, int]] = []
    if coverage.transcript_publication_complete and expectation.publication_boundary_sequence > 0:
        gap_intervals = [
            (gap.sequence_start, gap.sequence_end)
            for gap in coverage.explicit_gaps
            if gap.sequence_start is not None and gap.sequence_end is not None
        ]
        holes = uncovered_sequence_spans(
            expectation.publication_boundary_sequence,
            present_sequences,
            gap_intervals,
        )
    missing_sequences = _bounded_missing_sequences(holes)
    ok = not (
        missing
        or unexpected
        or missing_sequences
        or identity_mismatches
        or duplicate_ids
        or read_failed
        or nonprogress
    )
    message = "coverage ok"
    if read_failed:
        message = "source reader failed during bounded pagination"
    elif nonprogress:
        message = "source pagination made no progress"
    elif identity_mismatches:
        message = "manifest segment identity mismatch vs frozen expectation"
    elif duplicate_ids:
        message = "source page returned duplicate segment ids"
    elif not ok:
        if holes and (holes[0][1] - holes[0][0]) > 32:
            message = f"uncovered sequence span {holes[0][0]}..{holes[0][1]}"
        else:
            message = "manifest enumeration mismatch vs frozen expectation"
    report = SourceCoverageReport(
        ok=ok,
        missing_segment_ids=missing,
        unexpected_segment_ids=unexpected,
        missing_sequences=missing_sequences,
        uncovered_spans=holes,
        message=message,
    )
    if not collected:
        return (
            _pending_manifest(
                session_id=session_id,
                meeting_metadata=meeting_metadata,
                captured_at=captured_at,
            ),
            report,
        )
    coverage_for_manifest = coverage
    if (missing_sequences or holes) and coverage.transcript_publication_complete:
        coverage_for_manifest = coverage.model_copy(
            update={"transcript_publication_complete": False},
        )
    manifest = SourceSnapshotManifest(
        session_id=session_id,
        snapshot_id="snap_pending",
        snapshot_content_hash="0" * 64,
        publication_boundary_sequence=expectation.publication_boundary_sequence,
        captured_at=captured_at,
        meeting_metadata=meeting_metadata or {},
        segments=sorted(collected, key=lambda item: item.sequence),
        coverage=coverage_for_manifest,
    )
    try:
        finalized = finalize_snapshot_manifest(manifest)
    except Exception:  # noqa: BLE001 - malformed rows become source partial
        report.ok = False
        report.message = "source reader failed during bounded pagination"
        return (
            _pending_manifest(
                session_id=session_id,
                meeting_metadata=meeting_metadata,
                captured_at=captured_at,
            ),
            report,
        )
    return finalized, report


@dataclass(frozen=True)
class SourceWirePlan:
    pages: list[SourceDescriptor]
    omitted_metadata_keys: list[str]


def split_gap_exact(gap: TranscriptGapMarker, max_span: int) -> list[TranscriptGapMarker]:
    if gap.sequence_start is None or gap.sequence_end is None:
        return [gap]
    if gap.sequence_end < gap.sequence_start:
        return [gap]
    slices: list[TranscriptGapMarker] = []
    cursor = gap.sequence_start
    while cursor <= gap.sequence_end:
        slice_end = min(gap.sequence_end, cursor + max_span)
        slices.append(gap.model_copy(update={"sequence_start": cursor, "sequence_end": slice_end}))
        if slice_end >= gap.sequence_end:
            break
        cursor = slice_end + 1
    return slices


def plan_source_wire(
    manifest: SourceSnapshotManifest,
    *,
    max_explicit_gaps: int,
    max_gap_sequence_span: int,
    max_meeting_metadata_keys: int,
    max_metadata_value_utf8_bytes: int,
) -> SourceWirePlan:
    """Page metadata and gaps without dropping a tail or shortening a span."""
    omitted: list[str] = []
    kept: dict[str, str] = {}
    for key in sorted(manifest.meeting_metadata):
        value = manifest.meeting_metadata[key]
        if len(value.encode("utf-8")) > max_metadata_value_utf8_bytes:
            omitted.append(key)
            continue
        kept[key] = value
    keys = list(kept)
    if not keys:
        meta_pages: list[dict[str, str]] = [{}]
    else:
        meta_pages = [
            {key: kept[key] for key in keys[index : index + max_meeting_metadata_keys]}
            for index in range(0, len(keys), max_meeting_metadata_keys)
        ]
    gap_slices: list[TranscriptGapMarker] = []
    for gap in manifest.coverage.explicit_gaps:
        gap_slices.extend(split_gap_exact(gap, max_gap_sequence_span))
    if not gap_slices:
        gap_pages: list[list[TranscriptGapMarker]] = [[]]
    else:
        gap_pages = [
            gap_slices[index : index + max_explicit_gaps]
            for index in range(0, len(gap_slices), max_explicit_gaps)
        ]
    page_count = max(len(meta_pages), len(gap_pages))
    pages: list[SourceDescriptor] = []
    for index in range(page_count):
        metadata = meta_pages[index] if index < len(meta_pages) else {}
        gaps = gap_pages[index] if index < len(gap_pages) else []
        pages.append(
            SourceDescriptor(
                session_id=manifest.session_id,
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                publication_boundary_sequence=manifest.publication_boundary_sequence,
                enumerated_segment_count=len(manifest.segments),
                meeting_metadata=metadata,
                coverage=manifest.coverage.model_copy(update={"explicit_gaps": gaps}),
            )
        )
    return SourceWirePlan(pages=pages, omitted_metadata_keys=omitted)


def manifest_to_source_descriptor(
    manifest: SourceSnapshotManifest,
    *,
    max_explicit_gaps: int = 32,
    max_gap_sequence_span: int = 10_000,
    max_meeting_metadata_keys: int = 64,
    max_metadata_value_utf8_bytes: int = 4_000,
) -> SourceDescriptor:
    plan = plan_source_wire(
        manifest,
        max_explicit_gaps=max_explicit_gaps,
        max_gap_sequence_span=max_gap_sequence_span,
        max_meeting_metadata_keys=max_meeting_metadata_keys,
        max_metadata_value_utf8_bytes=max_metadata_value_utf8_bytes,
    )
    if plan.omitted_metadata_keys or len(plan.pages) != 1:
        msg = "source does not fit one descriptor page"
        raise ValueError(msg)
    return plan.pages[0]


def split_segment_fragments(
    segment: ManifestSegmentEntry,
    *,
    max_chars: int,
) -> list[WireSegmentFragment]:
    if max_chars < 1:
        msg = "max_chars must be >= 1"
        raise ValueError(msg)
    text = segment.text
    if len(text) <= max_chars:
        return [
            WireSegmentFragment(
                segment_id=segment.id,
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=text,
                content_hash=segment.content_hash,
            )
        ]
    fragments: list[WireSegmentFragment] = []
    start_cp = 0
    length = len(text)
    parts: list[tuple[int, int]] = []
    while start_cp < length:
        end_cp = min(start_cp + max_chars, length)
        parts.append((start_cp, end_cp))
        start_cp = end_cp
    total = len(parts)
    for index, (span_start, span_end) in enumerate(parts):
        excerpt = text[span_start:span_end]
        fragments.append(
            WireSegmentFragment(
                segment_id=segment.id,
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=excerpt,
                content_hash=segment.content_hash,
                fragment_index=index,
                fragment_count=total,
                text_span_start_cp=span_start,
                text_span_end_cp=span_end,
            )
        )
    return fragments


def explicit_gaps_from_sequences(
    missing_sequences: list[int], *, reason: str
) -> list[TranscriptGapMarker]:
    if not missing_sequences:
        return []
    markers: list[TranscriptGapMarker] = []
    start = missing_sequences[0]
    prev = start
    for sequence in missing_sequences[1:]:
        if sequence == prev + 1:
            prev = sequence
            continue
        markers.append(TranscriptGapMarker(sequence_start=start, sequence_end=prev, reason=reason))
        start = sequence
        prev = sequence
    markers.append(TranscriptGapMarker(sequence_start=start, sequence_end=prev, reason=reason))
    return markers


def unicode_span_from_fragment(fragment: WireSegmentFragment) -> UnicodeTextSpan | None:
    if fragment.text_span_start_cp is None or fragment.text_span_end_cp is None:
        return None
    return UnicodeTextSpan(
        start_cp=fragment.text_span_start_cp,
        end_cp=fragment.text_span_end_cp,
    )
