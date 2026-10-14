"""Durable meeting-essence ledger (#72).

This package must not be named ``recantor.meeting_intelligence``. That module name
is reserved for the #68 provider bridge.
"""

from recantor.meeting_essence.persistence.fingerprints import compute_run_fingerprint
from recantor.meeting_essence.persistence.models import (
    MIArtifactVersion,
    MIAtomicRecordRow,
    MIAttemptCheckpoint,
    MIIntelligenceJob,
    MIJobAttempt,
    MIRecordRelationRow,
    MISessionSourceFence,
    MISourceSnapshot,
    MIValidatedExtraction,
)

__all__ = [
    "MIArtifactVersion",
    "MIAtomicRecordRow",
    "MIAttemptCheckpoint",
    "MIIntelligenceJob",
    "MIJobAttempt",
    "MIRecordRelationRow",
    "MISessionSourceFence",
    "MISourceSnapshot",
    "MIValidatedExtraction",
    "compute_run_fingerprint",
]
