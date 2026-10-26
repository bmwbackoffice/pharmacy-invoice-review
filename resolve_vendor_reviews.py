"""Answers review-queue items that were flagged ONLY because the vendor wasn't in Vendor names and
now is, so the next run (or "Apply changes now") records them. Items with other reasons keep them.
  python resolve_vendor_reviews.py          -> dry run (lists what would change)
  python resolve_vendor_reviews.py --commit -> do it"""
import sys
import classify_emails as ce

commit = "--commit" in sys.argv
r = ce.resolve_vendor_review_items(actor="joel (resolve_vendor_reviews.py)", dry_run=not commit)
print(("DONE" if commit else "DRY RUN") + f": {len(r['answered'])} item(s) now resolvable {r['answered']}")
print(f"  {len(r['updated'])} item(s) still flagged for something else (vendor sentence removed) {r['updated']}")
if commit and r["answered"]:
    print('Now click "Apply changes now" on the Review queue to record them.')
