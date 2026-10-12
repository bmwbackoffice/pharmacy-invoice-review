"""
ONE-TIME import of the vendor/account standardization CSVs into the DB. Run by hand once (and
again whenever Joel sends updated CSVs — it's idempotent, upserting by the unique key):

    python import_vendor_standardization.py

Put vendor_name_map.csv, account_name_map.csv, and vendor_account_assignment.csv in the project
root (next to this file) first. NOT called by classify_emails.py or review_app.py. DB mode only.
"""
import csv
import os
import sys

import database


def upsert(table, unique_col, cols, path):
    if not os.path.exists(path):
        print(f"{path}: NOT FOUND — skipped (put the CSV in the project root and re-run).")
        return
    conn = database.connect()
    n = 0
    try:
        with conn.cursor() as cur, open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not (row.get(unique_col) or "").strip():
                    continue                       # skip blank-key rows
                vals = [(row.get(c) or None) for c in cols]
                set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != unique_col)
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}, created_by) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}, 'csv-import') "
                    f"ON CONFLICT ({unique_col}) DO UPDATE SET {set_clause}, "
                    f"edited_by='csv-import', edited_at=now()",
                    vals)
                n += 1
        conn.commit()
    finally:
        conn.close()
    print(f"{table}: {n} rows imported/updated")


def main():
    if not os.environ.get("DATABASE_URL"):
        print("import_vendor_standardization needs DATABASE_URL (DB mode). Nothing to do.")
        return
    database.init_db()   # ensure the tables exist before importing
    upsert("vendor_name_map", "standard_vendor_name",
           ["standard_vendor_name", "name_3m", "name_madison", "name_maple", "name_refuah",
            "name_westgate", "name_bmw", "name_lakewood_rx"],
           "vendor_name_map.csv")
    upsert("account_name_map", "standard_account_name",
           ["standard_account_name", "name_3m", "name_madison", "name_maple", "name_refuah",
            "name_westgate", "name_bmw", "name_lakewood_rx"],
           "account_name_map.csv")
    upsert("vendor_account_assignment", "standard_vendor_name",
           ["standard_vendor_name", "standard_account_name"],
           "vendor_account_assignment.csv")


if __name__ == "__main__":
    main()
