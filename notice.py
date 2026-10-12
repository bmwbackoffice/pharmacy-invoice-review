"""
Set or clear the sitewide maintenance banner shown in the review web app.

STANDARD PROCEDURE before a disruptive operation — a review_app.py restart for a code deploy,
or a BULK reprocess/recovery that touches EXISTING live records (recover_errored_email.py in
bulk, a backlog recovery, etc.):
  1. python notice.py set "<what's happening and why>" --minutes 5
  2. wait out the grace period (5 min is a reasonable default — raise it if the change needs longer)
  3. do the restart / run the job
  4. python notice.py clear

A routine classify run on BRAND-NEW emails only (not reprocessing anything existing) does NOT
need this — there's nothing live for anyone to lose.

    python notice.py set "Reprocessing MMS payments — please finish any entry in progress" --minutes 5
    python notice.py set "Deploying an update, ~2 min" --minutes 2
    python notice.py set "Maintenance in progress" --minutes 0   # 0 = until explicitly cleared
    python notice.py clear
"""

import argparse

import database

p = argparse.ArgumentParser(description="Set or clear the sitewide maintenance banner.")
p.add_argument("action", choices=["set", "clear"])
p.add_argument("message", nargs="?", help="banner text (required for 'set')")
p.add_argument("--minutes", type=int, default=5,
               help="auto-expire after this many minutes; 0 = until explicitly cleared (default 5)")
args = p.parse_args()

if args.action == "set":
    if not (args.message or "").strip():
        p.error("'set' needs a message, e.g.  python notice.py set \"Maintenance\" --minutes 5")
    minutes = args.minutes if args.minutes and args.minutes > 0 else None
    database.set_notice(args.message, minutes, "system")
    when = f"for {minutes} min" if minutes else "until cleared"
    print(f"Notice set ({when}): {args.message}")
else:
    database.clear_notice("system")
    print("Notice cleared.")
