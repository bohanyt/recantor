from __future__ import annotations

import unittest

from release_manifest import validate_manifest


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
            "schema_head": "0009_upload_processing",
            "automatic_database_downgrade": False,
        },
        "compose_file": "infra/compose.release.yaml",
    }


class ReleaseManifestTests(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        validate_manifest(valid_manifest())

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
