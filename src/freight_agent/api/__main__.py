"""`uv run python -m freight_agent.api`: serve the platform API with uvicorn (port 8000)."""

import argparse
from collections.abc import Sequence

import uvicorn

from freight_agent.api.app import create_app

# An open event stream never ends on its own: give it this long to close on Ctrl+C.
GRACEFUL_SHUTDOWN_S = 3


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m freight_agent.api", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


if __name__ == "__main__":
    main()
