"""Mark sets of invoices as reviewed "not duplicates" -- the same thing the "Not dup" button leaves behind
(one shared group id, no duplicate flag), so the duplicate checks never flag them together again.

  python mark_not_duplicates.py 5953,5957 5954,5958        -> each comma list is one set
  python mark_not_duplicates.py --sushi                     -> the 8 That Sushi Spot receipts (4 store pairs)"""
import sys
import secrets
import database

SUSHI = ["5953,5957", "5954,5958", "5955,5959", "5956,5960"]
args = SUSHI if "--sushi" in sys.argv else [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    print(__doc__)
    sys.exit(1)
conn = database.connect()
conn.autocommit = False
with conn.cursor() as cur:
    for a in args:
        ids = [int(x) for x in a.split(",") if x.strip()]
        gid = secrets.token_hex(4).upper()
        cur.execute("UPDATE invoices SET duplicate_group_id=%s, duplicate_suspect=FALSE, duplicate_reason=NULL "
                    "WHERE id = ANY(%s) AND duplicate_ignored IS NOT TRUE", (gid, ids))
        for i in ids:
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('invoices',%s,'duplicate_group_id',NULL,%s,'joel (mark_not_duplicates.py: not a duplicate)')",
                        (i, gid))
        print(f"set {ids}: reviewed as not duplicates (group {gid})")
conn.commit()
conn.close()
