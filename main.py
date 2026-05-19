try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.bot import run

if __name__ == "__main__":
    run()
