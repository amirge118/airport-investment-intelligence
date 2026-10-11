"""CLI: ``python3 -m airport_intel.server [--offline] [--port N]``."""

from __future__ import annotations

import argparse
import os

from .app import run
from .runtime import DATABASE_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Airport Investment Intelligence local demo")
    parser.add_argument("--offline", action="store_true", help="skip startup source refresh and use last-good SQLite rows")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--database", default=os.environ.get("AIRPORT_INTEL_DB", str(DATABASE_PATH)))
    args = parser.parse_args()
    run(host=args.host, port=args.port, offline=args.offline, database_path=args.database)


if __name__ == "__main__":
    main()
