import subprocess
import sys
import os

def load_env(path):
    env = os.environ.copy()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env

bot1 = subprocess.Popen([sys.executable, "main.py"], env=load_env(".env.bot1"))
bot2 = subprocess.Popen([sys.executable, "main.py"], env=load_env(".env.bot2"))

try:
    bot1.wait()
    bot2.wait()
except KeyboardInterrupt:
    bot1.terminate()
    bot2.terminate()
    bot1.wait()
    bot2.wait()
