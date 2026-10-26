"""ONE-OFF (idempotent): emails already on the Correspondence page that are automatic replies (by their
headers or subject -- classify_emails._is_auto_reply) get request type 'Automatic reply / out of office'
and lose any NEEDS REPLY flag, then the correspondence rules are applied (so an auto-reply ignore rule
clears them). Only emails still Open are touched.

    python mark_auto_replies.py            # report only
    python mark_auto_replies.py --commit
"""
import sys
from pathlib import Path

import classify_emails as ce
import database

commit = "--commit" in sys.argv[1:]
base = Path(__file__).parent
conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, subject, from_address, needs_response FROM emails "
                    "WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                    "AND coalesce(corr_status, 'open') = 'open' AND coalesce(topic, '') <> 'auto_reply'")
        hits = []
        for fn, subj, frm, needs in cur.fetchall():
            path = next((base / d / fn for d in ("processed_emails", "review_pending", "test_emails")
                         if (base / d / fn).is_file()), base / "processed_emails" / fn)
            auto = ce._is_auto_reply(path, f"Subject: {subj or ''}")
            if auto == "header" or (auto == "subject" and not needs):   # same test as the classifier
                hits.append(fn)
                print(f"  {frm}: {subj}")
        print(f"\n{len(hits)} open automatic repl{'y' if len(hits) == 1 else 'ies'}.")
        if commit and hits:
            cur.execute("UPDATE emails SET topic = 'auto_reply', needs_response = FALSE, response_request = NULL, "
                        "response_requested_by = NULL WHERE filename = ANY(%s)", (hits,))
            n = database.apply_correspondence_rules("mark_auto_replies", cur=cur)
            print(f"Marked them; correspondence rules then cleared {n} email(s).")
    conn.commit() if commit else conn.rollback()
finally:
    conn.close()
if not commit:
    print("DRY-RUN -- re-run with --commit.")
