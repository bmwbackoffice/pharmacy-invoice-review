"""
Run the whole local pipeline with one command:

    python run_all.py [--excel] [--drive] [...]

Steps, in order:
  1. gmail_fetcher.py   — pull new invoice emails into test_emails/
  2. classify_emails.py — classify + extract + persist (any extra args are
                          passed straight through, e.g. --excel / --drive)

If the Gmail step fails (e.g. credentials.json not set up yet), you'll be told,
and classification of whatever is already in test_emails/ still runs.
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
PY = sys.executable


def _run(script, args=()):
    print(f"\n=== {script} ===")
    return subprocess.run([PY, str(BASE_DIR / script), *args]).returncode


def main():
    passthrough = sys.argv[1:]  # forwarded to classify_emails.py

    rc = _run("gmail_fetcher.py")
    if rc != 0:
        print("gmail_fetcher.py did not complete cleanly — continuing with whatever "
              "is already in test_emails/.")

    rc = _run("classify_emails.py", passthrough)
    sys.exit(rc)


if __name__ == "__main__":
    main()
