"""Correspondence triage, step 0 (2026-10-15).
1) Fills emails.from_address for correspondence emails that don't have one (older fetches), read from
   the stored .eml/.msg -- so 'Always ignore' rules can match them. --apply to write; dry run otherwise.
2) Prints an overview: how many correspondence emails, by month and by top senders/domains, so the
   biggest groups can be decided first on the Correspondence page.
"""
import re
import sys
from collections import Counter
from pathlib import Path
import email, email.policy
import database

APPLY = "--apply" in sys.argv
ROOT = Path(__file__).parent
DIRS = ("processed_emails", "review_pending", "test_emails")

def sender_from_file(fn):
    p = next((ROOT / d / fn for d in DIRS if (ROOT / d / fn).is_file()), None)
    if not p:
        return None
    try:
        if p.suffix.lower() == ".eml":
            m = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
            return str(m.get("From") or "") or None
        if p.suffix.lower() == ".msg":
            import extract_msg
            msg = extract_msg.Message(str(p)); s = msg.sender; msg.close(); return s or None
    except Exception:
        return None
    return None

database.init_db()
conn = database.connect()
try:
    cur = conn.cursor()
    cur.execute("SELECT filename FROM emails WHERE email_type = 'correspondence' "
                "AND coalesce(btrim(from_address), '') = ''")
    missing = [r[0] for r in cur.fetchall()]
    filled = 0
    for fn in missing:
        s = sender_from_file(fn)
        if s:
            filled += 1
            if APPLY:
                cur.execute("UPDATE emails SET from_address = %s WHERE filename = %s", (s, fn))
    print(f"Correspondence emails with no stored sender: {len(missing)}; "
          f"{'filled' if APPLY else 'can fill'} from the stored email: {filled}")
    if APPLY:
        conn.commit()
        n = database.apply_correspondence_rules("overview backfill")
        if n:
            print(f"Existing ignore rules then cleared {n} email(s).")

    cur.execute("SELECT filename, from_address, subject, coalesce(received_at, date_added::timestamptz), "
                "coalesce(corr_status, 'open') FROM emails WHERE email_type = 'correspondence'")
    rows = cur.fetchall()
    print(f"\nCorrespondence emails: {len(rows)}  "
          + "  ".join(f"{k}={v}" for k, v in Counter(r[4] for r in rows).most_common()))
    print("\nBy month received:")
    for k, v in sorted(Counter((r[3].strftime('%Y-%m') if r[3] else 'unknown') for r in rows).items()):
        print(f"   {k}: {v}")
    def addr(s):
        m = re.search(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", s or "")
        return (m.group(0).lower(), "@" + m.group(1).lower()) if m else ("(unknown)", "(unknown)")
    open_rows = [r for r in rows if r[4] == "open"]
    print(f"\nTop senders among the {len(open_rows)} OPEN ones (address: count -- sample subject):")
    by_addr = {}
    for r in open_rows:
        by_addr.setdefault(addr(r[1])[0], []).append(r[2] or "")
    for a, subs in sorted(by_addr.items(), key=lambda kv: -len(kv[1]))[:30]:
        print(f"   {len(subs):>4}  {a:<45} -- {subs[0][:70]}")
    print("\nTop domains among the OPEN ones:")
    for d, n in Counter(addr(r[1])[1] for r in open_rows).most_common(15):
        print(f"   {n:>4}  {d}")
    if not APPLY and filled:
        print("\nDRY RUN -- senders not saved. Re-run with --apply to fill them in.")
finally:
    conn.close()
