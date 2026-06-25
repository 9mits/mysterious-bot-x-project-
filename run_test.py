"""
run_test.py — Launch ONLY the test-token bot, locally, for staging.

This is the staging entry point for the "test on your PC first" workflow. It
loads `.env.test` (which sets the test token and TEST_MODE=1) and runs a single
bot instance. It deliberately does NOT touch `.env.bot1` / `.env.bot2`, so it
can never accidentally start the live bots (which run on the host) a second time
and collide with them.

Usage (from the project root, in the local virtualenv):
    .venv/Scripts/python.exe run_test.py        # Windows
    .venv/bin/python run_test.py                # macOS/Linux

Do not run `start.py` locally — that launches every .env.* it finds, including
the live tokens. Use this file for local testing instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env.test")


def main() -> None:
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE.name} not found next to run_test.py — cannot start the test bot.")

    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit("python-dotenv is not installed. Run: pip install -r requirements.txt")

    # override=True so .env.test wins over anything already in the environment.
    load_dotenv(ENV_FILE, override=True)

    from core.bot import run
    run()


if __name__ == "__main__":
    main()
