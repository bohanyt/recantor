from __future__ import annotations

import unittest

from release_manifest import channel_for_version, validate_manifest


def valid_manifest() -> dict:
    api_digest = "sha256:" + "a" * 64
    web_digest = "sha256:" + "b" * 64
    return {
        "format_version": 1,
        "version": "v0.2.0-alpha.1",
        "channel": "alpha",
        "source": {
            "repository": "https://github.com/bohanyt/recantor",
            "revision": "c" * 40,
        },
        "images": {
            "api": {
                "repository": "ghcr.io/bohanyt/recantor-api",
                "digest": api_digest,
                "reference": f"ghcr.io/bohanyt/recantor-api@{api_digest}",
            },
            "web": {
                "repository": "ghcr.io/bohanyt/recantor-web",
                "digest": web_digest,
                "reference": f"ghcr.io/bohanyt/recantor-web@{web_digest}",
            },
        },
        "compatibility": {
            "platforms": ["linux/amd64"],
            "schema_head": "0009",
            "automatic_database_downgrade": False,
        },
        "compose_file": "infra/compose.release.yaml",
    }


class ReleaseManifestTests(unittest.TestCase):
    def test_channel_for_supported_release_versions(self) -> None:
        cases = {
            "v1.2.3": "stable",
            "v1.2.3-alpha": "alpha",
            "v1.2.3-alpha.1": "alpha",
            "v1.2.3-alpha.dev.abcdef123456": "alpha",
            "v1.2.3-beta": "beta",
            "v1.2.3-beta.2": "beta",
            "v1.2.3-rc": "beta",
            "v1.2.3-rc.4": "beta",
        }
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(channel_for_version(version), expected)

    def test_rejects_unknown_prerelease_identifier(self) -> None:
        for version in ("v0.2.0-preview.1", "v0.2.0-dev.1", "v0.2.0-nightly.1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "unsupported release version"):
                    channel_for_version(version)

    def test_manifest_accepts_coherent_supported_version_channel_pairs(self) -> None:
        cases = (
            ("v2.0.0", "stable"),
            ("v2.0.0-alpha.1", "alpha"),
            ("v2.0.0-beta.1", "beta"),
            ("v2.0.0-rc.1", "beta"),
        )
        for version, channel in cases:
            with self.subTest(version=version, channel=channel):
                manifest = valid_manifest()
                manifest["version"] = version
                manifest["channel"] = channel
                validate_manifest(manifest)

    def test_rejects_version_channel_mismatch(self) -> None:
        cases = (
            ("v2.0.0-alpha.1", "stable"),
            ("v2.0.0-alpha.1", "beta"),
            ("v2.0.0-beta.1", "stable"),
            ("v2.0.0-rc.1", "stable"),
            ("v2.0.0", "alpha"),
        )
        for version, channel in cases:
            with self.subTest(version=version, channel=channel):
                manifest = valid_manifest()
                manifest["version"] = version
                manifest["channel"] = channel
                with self.assertRaisesRegex(ValueError, "requires channel="):
                    validate_manifest(manifest)

    def test_manifest_rejects_unknown_prerelease_even_with_known_channel(self) -> None:
        manifest = valid_manifest()
        manifest["version"] = "v0.2.0-preview.1"
        manifest["channel"] = "stable"
        with self.assertRaisesRegex(ValueError, "unsupported release version"):
            validate_manifest(manifest)

    def test_rejects_mutable_or_missing_digest(self) -> None:
        manifest = valid_manifest()
        manifest["images"]["api"]["digest"] = "latest"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_manifest(manifest)

    def test_rejects_automatic_database_downgrade(self) -> None:
        manifest = valid_manifest()
        manifest["compatibility"]["automatic_database_downgrade"] = True
        with self.assertRaisesRegex(ValueError, "automatic_database_downgrade"):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
