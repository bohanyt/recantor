from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from release_manifest import channel_for_version, render_manifest, validate_manifest


def valid_manifest_v1() -> dict:
    api_digest = "sha256:" + "a" * 64
    web_digest = "sha256:" + "b" * 64
    return {
        "format_version": 1,
        "version": "v0.1.0-alpha.2",
        "channel": "alpha",
        "source": {"repository": "https://github.com/bohanyt/recantor", "revision": "c" * 40},
        "images": {
            "api": {"repository": "ghcr.io/bohanyt/recantor-api", "digest": api_digest, "reference": f"ghcr.io/bohanyt/recantor-api@{api_digest}"},
            "web": {"repository": "ghcr.io/bohanyt/recantor-web", "digest": web_digest, "reference": f"ghcr.io/bohanyt/recantor-web@{web_digest}"},
        },
        "compatibility": {"platforms": ["linux/amd64"], "schema_head": "0009", "automatic_database_downgrade": False},
        "compose_file": "infra/compose.release.yaml",
    }


def valid_manifest_v2() -> dict:
    manifest = valid_manifest_v1()
    manifest["format_version"] = 2
    manifest["version"] = "v0.1.0-alpha.3"
    manifest["compatibility"]["application_rollback_safe_to_versions"] = ["v0.1.0-alpha.2"]
    manifest["compatibility"]["backup_required"] = False
    return manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_channel_for_supported_release_versions(self) -> None:
        for version, expected in {
            "v1.2.3": "stable",
            "v1.2.3-alpha.1": "alpha",
            "v1.2.3-beta.2": "beta",
            "v1.2.3-rc.4": "beta",
        }.items():
            with self.subTest(version=version):
                self.assertEqual(channel_for_version(version), expected)

    def test_v1_baseline_remains_valid(self) -> None:
        validate_manifest(valid_manifest_v1())

    def test_v2_requires_explicit_rollback_and_backup_policy(self) -> None:
        manifest = valid_manifest_v2()
        del manifest["compatibility"]["application_rollback_safe_to_versions"]
        with self.assertRaisesRegex(ValueError, "application_rollback_safe_to_versions"):
            validate_manifest(manifest)
        manifest = valid_manifest_v2()
        del manifest["compatibility"]["backup_required"]
        with self.assertRaisesRegex(ValueError, "backup_required"):
            validate_manifest(manifest)

    def test_v2_rejects_invalid_safe_version(self) -> None:
        manifest = valid_manifest_v2()
        manifest["compatibility"]["application_rollback_safe_to_versions"] = ["v0.1.0-preview.1"]
        with self.assertRaisesRegex(ValueError, "unsupported release version"):
            validate_manifest(manifest)

    def test_rejects_version_channel_mismatch_and_mutable_digest(self) -> None:
        manifest = valid_manifest_v2()
        manifest["channel"] = "stable"
        with self.assertRaisesRegex(ValueError, "requires channel="):
            validate_manifest(manifest)
        manifest = valid_manifest_v2()
        manifest["images"]["api"]["digest"] = "latest"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_manifest(manifest)

    def test_render_uses_policy_and_format_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({
                "application_rollback_safe_to_versions": ["v0.1.0-alpha.2"],
                "backup_required": False,
            }), encoding="utf-8")
            rendered = render_manifest(Namespace(
                version="v0.1.0-alpha.3",
                channel="alpha",
                source_revision="d" * 40,
                api_digest="sha256:" + "a" * 64,
                web_digest="sha256:" + "b" * 64,
                schema_head="0009",
                platform=["linux/amd64"],
                policy=policy,
            ))
            self.assertEqual(rendered["format_version"], 2)
            self.assertEqual(rendered["compatibility"]["application_rollback_safe_to_versions"], ["v0.1.0-alpha.2"])
            self.assertFalse(rendered["compatibility"]["backup_required"])


if __name__ == "__main__":
    unittest.main()
