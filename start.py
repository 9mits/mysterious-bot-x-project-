import subprocess
import sys
import os
from pathlib import Path


def load_env(path: str) -> dict:
    env = os.environ.copy()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


processes = []

for env_file in (".env.bot1", ".env.bot2", ".env.test"):
    if Path(env_file).exists():
        processes.append(subprocess.Popen([sys.executable, "main.py"], env=load_env(env_file)))

if not processes:
    print("No .env.bot1, .env.bot2, or .env.test files found — nothing to launch.")
    sys.exit(1)

try:
    for p in processes:
        p.wait()
except KeyboardInterrupt:
    for p in processes:
        p.terminate()
    for p in processes:
        p.wait()
