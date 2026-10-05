#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
sys.path.insert(0, str(_API_SRC))

from recantor.meeting_intelligence import (  # noqa: E402
    CodexSubscriptionMeetingIntelligenceProvider,
    MeetingIntelligenceError,
    MeetingIntelligenceRequest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Development-only host bridge from transcript text to a locally "
            "ChatGPT-authenticated Codex CLI."
        )
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="required opt-in; without it the bridge will not invoke Codex",
    )
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high", "xhigh"),
        default="high",
    )
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "check-auth",
        help="verify that saved Codex auth reports ChatGPT mode; does not call a model",
    )
    subparsers.add_parser(
        "analyze",
        help='read {"transcript_text": "..."} from stdin and emit derived JSON',
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


async def _run(args: argparse.Namespace) -> int:
    if not args.enable:
        _emit(
            {
                "status": "disabled",
                "detail": "pass --enable to opt in to the development-only Codex bridge",
            }
        )
        return 2

    provider = CodexSubscriptionMeetingIntelligenceProvider(
        codex_binary=args.codex_binary,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout_seconds,
    )

    try:
        if args.command == "check-auth":
            await provider.check_chatgpt_auth()
            _emit(
                {
                    "status": "available",
                    "auth_mode": "chatgpt",
                    "requested_model": args.model,
                    "requested_reasoning_effort": args.reasoning_effort,
                    "model_availability": "unprobed",
                }
            )
            return 0

        try:
            request_payload = json.load(sys.stdin)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _emit({"status": "invalid_input", "detail": "stdin must contain one JSON object"})
            return 2

        if not isinstance(request_payload, dict) or set(request_payload) != {"transcript_text"}:
            _emit(
                {
                    "status": "invalid_input",
                    "detail": "stdin JSON must contain only transcript_text",
                }
            )
            return 2
        transcript_text = request_payload.get("transcript_text")
        if not isinstance(transcript_text, str):
            _emit({"status": "invalid_input", "detail": "transcript_text must be a string"})
            return 2

        result = await provider.derive(
            MeetingIntelligenceRequest(transcript_text=transcript_text)
        )
        _emit(
            {
                "status": "ok",
                "requested_model": result.requested_model,
                "requested_reasoning_effort": result.requested_reasoning_effort,
                "model_availability": (
                    "requested invocation succeeded"
                    if result.requested_model is not None
                    else "default model not identified by this bridge"
                ),
                "intelligence": {
                    "key_points": list(result.key_points),
                    "decisions": list(result.decisions),
                    "action_items": list(result.action_items),
                    "open_questions": list(result.open_questions),
                },
            }
        )
        return 0
    except MeetingIntelligenceError as exc:
        _emit(
            {
                "status": exc.category.value,
                "detail": str(exc),
                "requested_model": args.model,
                "requested_reasoning_effort": args.reasoning_effort,
            }
        )
        return 3


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
