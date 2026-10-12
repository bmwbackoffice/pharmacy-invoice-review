"""Read-only: scope of emails stuck at status='fetched' with no run_id, i.e.
ingested from Gmail but NEVER actually sent through classification at all.
Distinct population from the /reconcile (orphaned_history) backlog, which
only catches emails that got far enough to write a history row."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), min(received_at), max(received_at)
            FROM emails
            WHERE status = 'fetched' AND run_id IS NULL
              AND (legacy_migrated IS NOT TRUE)
        """)
        n, dmin, dmax = cur.fetchone()
        print(f"Total stuck (status=fetched, run_id IS NULL): {n}")
        print(f"Received range: {dmin} .. {dmax}")

        print("\nBy sender (top 20):")
        cur.execute("""
            SELECT from_address, count(*) n, min(received_at), max(received_at)
            FROM emails
            WHERE status = 'fetched' AND run_id IS NULL AND legacy_migrated IS NOT TRUE
            GROUP BY from_address ORDER BY n DESC LIMIT 20
        """)
        for r in cur.fetchall():
            print(f"  {r[0]!r:45s} n={r[1]:4d}  {r[2]} .. {r[3]}")

        print("\nBy received month:")
        cur.execute("""
            SELECT date_trunc('month', received_at)::date, count(*)
            FROM emails
            WHERE status = 'fetched' AND run_id IS NULL AND legacy_migrated IS NOT TRUE
            GROUP BY 1 ORDER BY 1
        """)
        for r in cur.fetchall():
            print(f"  {r[0]}: {r[1]}")

        # Does the source file still exist on disk / in processed_emails (so we know
        # whether a recovery tool could even find it)?
        print("\nSample of 10 filenames (to spot-check on disk):")
        cur.execute("""
            SELECT filename FROM emails
            WHERE status = 'fetched' AND run_id IS NULL AND legacy_migrated IS NOT TRUE
            ORDER BY received_at DESC LIMIT 10
        """)
        for r in cur.fetchall():
            print(f"  {r[0]}")
finally:
    conn.close()
