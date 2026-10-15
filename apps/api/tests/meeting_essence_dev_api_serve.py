"""Development API entry for Windows async psycopg (selector event loop).

``uvicorn`` CLI creates a Proactor loop on Windows and ignores
``WindowsSelectorEventLoopPolicy``. Use this module for the #74 fixture path:

``uv run python tests/meeting_essence_dev_api_serve.py --host 127.0.0.1 --port 8000``
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve recantor.main:app with a selector loop.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config = uvicorn.Config("recantor.main:app", host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
