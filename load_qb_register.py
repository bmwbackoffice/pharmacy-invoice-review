"""ONE-OFF: fill the QuickBooks register from the export copies already saved in qb_imports\\ (uploads
made before 10/20/2026 weren't kept in the register), oldest first, so "In QB twice?" and the Duplicates
page's in-QuickBooks-once/twice settling work without waiting for a new upload. Idempotent: each file
replaces its stores' bills for the dates it covers, same as an upload. Then settles duplicate sets.

    python load_qb_register.py
"""
import glob
import os
import re
from datetime import datetime

import psycopg2.extras

import classify_emails as ce
import database
import dup_settle
import qb_match as qm

files = sorted(glob.glob(os.path.join(qm.QB_IMPORTS_DIR, "batch_*")),
               key=lambda p: int(re.search(r"batch_(\d+)", os.path.basename(p)).group(1)))
if not files:
    raise SystemExit("No saved QuickBooks exports in qb_imports\\ -- upload one on QB reconcile instead.")
std = ce.load_vendor_standardization()
ce.VENDOR_STD = std
conn = database.connect()
conn.autocommit = False
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for path in files:
            batch_id = int(re.search(r"batch_(\d+)", os.path.basename(path)).group(1))
            cur.execute("SELECT uploaded_at FROM qb_import_batches WHERE id = %s", (batch_id,))
            row = cur.fetchone()
            uploaded_at = row["uploaded_at"] if row else datetime.fromtimestamp(os.path.getmtime(path))
            with open(path, "rb") as f:
                rows = qm._parse_rows(f.read())
            register = []
            for r in rows:
                t = str(r["type"] or "").strip().lower()
                if t not in ("bill", "credit"):
                    continue
                amt = qm._num(r["amount"])
                if t == "bill" and amt is not None:
                    amt = abs(amt)
                register.append((r["store"], r["type"], qm._parse_date(r["date"]), qm._num_str(r["num"]) or None,
                                 r["name"], ce.resolve_standard_vendor(r["name"], std_tables=std), amt))
            spans = qm.write_register(cur, batch_id, register, uploaded_at)
            print(f"{os.path.basename(path)}: {len(register)} bill/credit rows")
            for st, (lo, hi) in sorted(spans.items()):
                print(f"   {st}: {lo:%m/%d/%Y} - {hi:%m/%d/%Y}")
    conn.commit()
finally:
    conn.close()

print("\nSettling duplicate sets against the register...")
res = dup_settle.settle_duplicate_sets("auto: same bill, in QuickBooks once (register backfill)")
print(f"Resolved {len(res['resolved'])}, entered twice in QuickBooks {len(res['twice'])}.")
print("Now open QB entry > In QB twice?")
