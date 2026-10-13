from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptIdentity:
    name: str
    version: str
    asset_path: str


@dataclass(frozen=True)
class PackageIdentities:
    package_id: str
    schema_identity: str
    algorithm_identity: str
    prompts: tuple[PromptIdentity, ...]


PACKAGE_IDENTITIES = PackageIdentities(
    package_id="recantor-meeting-essence-v1",
    schema_identity="meeting-essence.contracts/2026-10-13.3",
    algorithm_identity="meeting-essence.pipeline/serial-v1",
    prompts=(
        PromptIdentity("extract", "1.0.0", "prompts/extract_v1.txt"),
        PromptIdentity("reconcile", "1.0.0", "prompts/reconcile_v1.txt"),
        PromptIdentity("check", "1.0.0", "prompts/check_v1.txt"),
        PromptIdentity("render", "1.0.0", "prompts/render_v1.txt"),
    ),
)

SUPPORTED_PROMPT_BUNDLE_IDS: frozenset[str] = frozenset({PACKAGE_IDENTITIES.package_id})

SUPPORTED_SCHEMA_IDENTITIES: frozenset[str] = frozenset({PACKAGE_IDENTITIES.schema_identity})

SUPPORTED_ALGORITHM_IDENTITIES: frozenset[str] = frozenset({PACKAGE_IDENTITIES.algorithm_identity})

SUPPORTED_PROMPT_VERSIONS: frozenset[str] = frozenset(
    {f"{item.name}@{item.version}" for item in PACKAGE_IDENTITIES.prompts}
)


def assert_known_schema_identity(value: str) -> str:
    if value not in SUPPORTED_SCHEMA_IDENTITIES:
        msg = f"unsupported schema_identity {value!r}; known: {sorted(SUPPORTED_SCHEMA_IDENTITIES)}"
        raise ValueError(msg)
    return value


def assert_known_algorithm_identity(value: str) -> str:
    if value not in SUPPORTED_ALGORITHM_IDENTITIES:
        msg = (
            f"unsupported algorithm_identity {value!r}; "
            f"known: {sorted(SUPPORTED_ALGORITHM_IDENTITIES)}"
        )
        raise ValueError(msg)
    return value


def assert_known_prompt_bundle_id(value: str) -> str:
    if value not in SUPPORTED_PROMPT_BUNDLE_IDS:
        msg = (
            f"unsupported prompt_bundle_id {value!r}; known: {sorted(SUPPORTED_PROMPT_BUNDLE_IDS)}"
        )
        raise ValueError(msg)
    return value
