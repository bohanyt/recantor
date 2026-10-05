from __future__ import annotations

import json
from dataclasses import asdict, dataclass

NORMALIZATION_SPEC_ID = "upload-pcm16k-mono-s16le-v1"
SEGMENTATION_SPEC_ID = "upload-energy-vad-180s-v1"
UPLOAD_SAMPLE_RATE = 16_000
UPLOAD_CHANNELS = 1
UPLOAD_SAMPLE_WIDTH_BYTES = 2
UPLOAD_FRAME_MS = 20
UPLOAD_FRAME_SAMPLES = 320


@dataclass(frozen=True)
class UploadSegmentationSpec:
    sample_rate: int = UPLOAD_SAMPLE_RATE
    frame_ms: int = UPLOAD_FRAME_MS
    frame_samples: int = UPLOAD_FRAME_SAMPLES
    pre_roll_ms: int = 200
    min_voiced_ms: int = 160
    trailing_silence_ms: int = 600
    hard_max_ms: int = 180_000
    absolute_threshold_dbfs: float = -50.0
    noise_margin_db: float = 12.0
    initial_noise_dbfs: float = -65.0
    noise_alpha: float = 0.95


UPLOAD_SEGMENTATION_SPEC = UploadSegmentationSpec()
SEGMENTATION_PARAMS_JSON = json.dumps(
    asdict(UPLOAD_SEGMENTATION_SPEC),
    sort_keys=True,
    separators=(",", ":"),
)
