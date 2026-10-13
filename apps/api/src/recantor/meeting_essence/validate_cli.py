from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recantor.meeting_essence.contracts import MeetingIntelligenceExampleBundle
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.validation import validate_example_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate meeting essence example bundles.")
    parser.add_argument(
        "bundle_path", type=Path, help="JSON file with manifest + result (+ records)"
    )
    args = parser.parse_args(argv)

    payload = json.loads(args.bundle_path.read_text(encoding="utf-8"))
    bundle = MeetingIntelligenceExampleBundle.model_validate(payload)
    report = validate_example_bundle(bundle)
    prompt_bundle = load_prompt_bundle()

    print(f"schema_identity={prompt_bundle.schema_identity}")
    print(f"algorithm_identity={prompt_bundle.algorithm_identity}")
    print(f"prompt_cache_key={prompt_bundle.cache_key}")
    for item in prompt_bundle.prompts:
        print(f"prompt:{item.identity.name}@{item.identity.version} sha256={item.sha256}")

    if report.ok:
        print("validation=OK")
        return 0

    print("validation=FAILED")
    for issue in report.issues:
        location = f" ({issue.path})" if issue.path else ""
        print(f"- [{issue.code}]{location} {issue.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
