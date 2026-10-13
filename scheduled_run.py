"""Unattended fetch + classify, for Windows Task Scheduler (set up by setup_scheduled_run.ps1).

Does what "Fetch New Invoices.bat" does -- gmail_fetcher.py, then classify_emails.py --drive -- but with
no window and no "press any key", and with time limits so a stuck step can't block later runs (the
Gmail step opens a browser and waits if its saved sign-in ever expires). Everything is appended to
logs/scheduled_runs.log, one block per run, with a one-line result at the end:

    OK                     the run finished
    SKIPPED                another classify run (scheduled or manual) was going; nothing changed
    FAILED (exit N)        classify stopped with an error -- read the lines above it
    TIMED OUT              a step ran past its limit and was stopped

Run by hand to try it:   python scheduled_run.py
A run with no new email makes no Claude API calls, so frequent runs cost nothing extra when it's quiet.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
LOG = LOG_DIR / "scheduled_runs.log"
LOG_MAX_BYTES = 5 * 1024 * 1024            # roll to scheduled_runs.log.1 past this
FETCH_TIMEOUT = 10 * 60                     # seconds
CLASSIFY_TIMEOUT = 90 * 60
SKIPPED_EXIT = 3                            # classify_emails.py: another run holds the lock

# python.exe, even when this script runs under pythonw.exe (no console): the steps print a lot.
PY = str(Path(sys.executable).with_name("python.exe")) if Path(sys.executable).name.lower() == "pythonw.exe" \
    else sys.executable
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env():
    env = dict(os.environ)
    env.setdefault("STATEMENT_GOLIVE_DATE", "2026-08-07")   # as in the .bat launchers
    env["PYTHONUTF8"] = "1"                 # the log is a file: without this, a printed arrow or emoji
    env["PYTHONIOENCODING"] = "utf-8"       # in a vendor name would crash the run (Windows cp1252)
    env["PYTHONUNBUFFERED"] = "1"           # write each line as it happens, so the log shows progress mid-run
    return env


def _roll_log():
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
            old = LOG.with_suffix(".log.1")
            if old.exists():
                old.unlink()
            LOG.rename(old)
    except OSError:
        pass


def _step(log, script, args, timeout):
    """Run one step with its output going to the log. Returns its exit code, or None on timeout."""
    log.write(f"--- {script} {' '.join(args)}\n")
    log.flush()
    try:
        return subprocess.run([PY, str(BASE / script), *args], cwd=BASE, env=_env(), stdout=log,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout,
                              creationflags=NO_WINDOW).returncode
    except subprocess.TimeoutExpired:
        log.write(f"\n!!! {script} ran longer than {timeout // 60} minutes and was stopped.\n")
        if script == "gmail_fetcher.py":
            log.write("!!! Gmail's sign-in probably needs renewing: run \"Fetch New Invoices.bat\" once by hand "
                      "and sign in when the browser opens.\n")
        return None


def main():
    LOG_DIR.mkdir(exist_ok=True)
    _roll_log()
    started = datetime.now()
    # "--by NAME": started from the web app's "Apply changes now" button (2026-10-13).
    # "--classify-only": skip the Gmail fetch -- just process answers, Quick fixes, uploads and anything
    # already fetched, so a reviewer's changes take effect without waiting for the next scheduled run.
    by = sys.argv[sys.argv.index("--by") + 1] if "--by" in sys.argv[1:-1] else ""
    classify_only = "--classify-only" in sys.argv[1:]
    with open(LOG, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n===== Scheduled run {started:%Y-%m-%d %H:%M:%S} =====\n")
        if by or classify_only:
            log.write(f"({'classifier only' if classify_only else 'fetch + classify'}"
                      f"{', started by hand by ' + by + ' from the web app' if by else ''})\n")
        log.flush()
        if classify_only:
            fetch = 0
        else:
            fetch = _step(log, "gmail_fetcher.py", [], FETCH_TIMEOUT)
        if fetch not in (0, None):
            log.write("(gmail_fetcher.py did not finish cleanly -- classifying whatever is already in test_emails/)\n")
        rc = _step(log, "classify_emails.py", ["--drive"], CLASSIFY_TIMEOUT)
        if rc is None or fetch is None:
            result = "TIMED OUT"
        elif rc == 0:
            result = "OK"
        elif rc == SKIPPED_EXIT:
            result = "SKIPPED (another run was in progress)"
        else:
            result = f"FAILED (exit {rc})"
        mins = (datetime.now() - started).total_seconds() / 60
        log.write(f"===== Result: {result} -- {mins:.1f} min =====\n")
    return 0 if result.startswith(("OK", "SKIPPED")) else 1


if __name__ == "__main__":
    sys.exit(main())
