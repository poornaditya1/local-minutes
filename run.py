from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# Load project-local environment variables when a .env file is present.
load_dotenv(ROOT / ".env")

from local_minutes.server import main

if __name__ == "__main__":
    main()
