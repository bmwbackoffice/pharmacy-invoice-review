"""ONE-OFF (idempotent): adds the two vitamin-department domains as whole-domain store entries in
Setup > Senders (sender_store_map) and Setup > Contacts (contact_store_map):
    @3mvitamins.com    -> 3M Pharmacy
    @thevitamindesk.com -> Maple Pharmacy
Existing rows are left alone (ON CONFLICT DO NOTHING).

    python add_vitamin_domains.py
"""
import database

ROWS = [("@3mvitamins.com", "3M Pharmacy"), ("@thevitamindesk.com", "Maple Pharmacy")]
NOTE = "whole domain -- vitamin department (Joel, 2026-10-16)"

conn = database.connect()
try:
    with conn.cursor() as cur:
        for table, col in (("sender_store_map", "sender_email"), ("contact_store_map", "contact_email")):
            for addr, store in ROWS:
                cur.execute(f"INSERT INTO {table} ({col}, store_matched, note, created_by) "
                            f"VALUES (%s, %s, %s, 'add_vitamin_domains') "
                            f"ON CONFLICT ({col}) DO NOTHING", (addr, store, NOTE))
                print(f"{table}: {addr} -> {store}: {'added' if cur.rowcount else 'already there'}")
    conn.commit()
finally:
    conn.close()
