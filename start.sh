#!/bin/bash
# Launches both bot instances from the same codebase.
# Each reads its own .env file for token, brand name, and database path.

set -a; source .env.bot1; set +a
python3 main.py &

set -a; source .env.bot2; set +a
python3 main.py
