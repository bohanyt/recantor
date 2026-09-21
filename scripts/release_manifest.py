#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

STABLE_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
ALPHA_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+-alpha(?:\.[0-9A-Za-z-]+)*$")
BETA_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+-beta(?:\.[0-9A-Za-z-]+)*$")
RC_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+-rc(?:\.[0-9A-Za-z-]+)*$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CHANNELS = {"stable", "beta", "alpha"}
IMAGE_NAMES = {
    "api": "ghcr.io/bohanyt/recantor-api",
    "web": "ghcr.io/bohanyt/recantor-web",
}


def channel_for_version(version: str) -> str:
    if STABLE_VERSION_RE.fullmatch(version):
        return "stable"
    if ALPHA_VERSION_RE.fullmatch(version):
        return "alpha"
    if BETA_VERSION_RE.fullmatch(version) or RC_VERSION_RE.fullmatch(version):
        return "beta"
    raise ValueError(
        "unsupported release version; allowed forms are "
        "vX.Y.Z, vX.Y.Z-alpha[.<id>...], vX.Y.Z-beta[.<id>...], "
        "or vX.Y.Z-rc[.<id>...]"
    )


def validate_manifest(data: dict[str, Any]) -> None:
    if data.get("format_version") != 1:
        raise ValueError("format_version must be 1")

    version = data.get("version")
    if not isinstance(version, str):
        raise ValueError("version must be a string")
    expected_channel = channel_for_version(version)

    channel = data.get("channel")
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {sorted(CHANNELS)}")
    if channel != expected_channel:
        raise ValueError(
            f"version {version} requires channel={expected_channel}, got channel={channel}"
        )

    source = data.get("source")
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    if source.get("repository") != "https://github.com/bohanyt/recantor":
        raise ValueError("source.repository must identify the Recantor repository")
    revision = source.get("revision")
    if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
        raise ValueError("source.revision must be an exact 40-character lowercase Git SHA")

    images = data.get("images")
    if not isinstance(images, dict):
        raise ValueError("images must be an object")
    for role, repository in IMAGE_NAMES.items():
        image = images.get(role)
        if not isinstance(image, dict):
            raise ValueError(f"images.{role} must be an object")
        if image.get("repository") != repository:
            raise ValueError(f"images.{role}.repository must be {repository}")
        digest = image.get("digest")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(f"images.{role}.digest must be sha256:<64 lowercase hex>")
        if image.get("reference") != f"{repository}@{digest}":
            raise ValueError(f"images.{role}.reference must be repository@digest")

    compatibility = data.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("compatibility must be an object")
    platforms = compatibility.get("platforms")
    if not isinstance(platforms, list) or not platforms or not all(
        isinstance(item, str) and item for item in platforms
    ):
        raise ValueError("compatibility.platforms must be a non-empty string list")
    schema_head = compatibility.get("schema_head")
    if not isinstance(schema_head, str) or not schema_head.strip():
        raise ValueError("compatibility.schema_head must be non-empty")
    if compatibility.get("automatic_database_downgrade") is not False:
        raise ValueError("automatic_database_downgrade must be false")
    if data.get("compose_file") != "infra/compose.release.yaml":
        raise ValueError("compose_file must identify the supported pull-only Compose file")


def render_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = {
        "format_version": 1,
        "version": args.version,
        "channel": args.channel,
        "source": {
            "repository": "https://github.com/bohanyt/recantor",
            "revision": args.source_revision,
        },
        "images": {
            role: {
                "repository": repository,
                "digest": getattr(args, f"{role}_digest"),
                "reference": f"{repository}@{getattr(args, f'{role}_digest')}",
            }
            for role, repository in IMAGE_NAMES.items()
        },
        "compatibility": {
            "platforms": args.platform,
            "schema_head": args.schema_head,
            "automatic_database_downgrade": False,
        },
        "compose_file": "infra/compose.release.yaml",
    }
    validate_manifest(manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render, classify, or validate a Recantor release manifest")
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("channel")
    classify.add_argument("version")

    render = sub.add_parser("render")
    render.add_argument("--version", required=True)
    render.add_argument("--channel", choices=sorted(CHANNELS), required=True)
    render.add_argument("--source-revision", required=True)
    render.add_argument("--api-digest", required=True)
    render.add_argument("--web-digest", required=True)
    render.add_argument("--schema-head", required=True)
    render.add_argument("--platform", action="append", required=True)
    render.add_argument("--output", type=Path, required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "channel":
        print(channel_for_version(args.version))
        return

    if args.command == "render":
        manifest = render_manifest(args)
        args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return

    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    validate_manifest(data)


if __name__ == "__main__":
    main()
