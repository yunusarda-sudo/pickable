#!/usr/bin/env python3
"""Read-only waitlist consistency check.

Compares the persisted waitlist rows against the server-recorded signup
outcome events in the same SQLite file, so a wiped/moved database or a
miscount shows up immediately.

Run on the machine that serves the site (on the VPS, inside the container
where ./data is mounted):

    docker compose exec web python3 scripts/check_waitlist.py
    DB_PATH=/path/to/scan.db python3 scripts/check_waitlist.py [db_path]

Read-only: opens the database in query-only mode and never creates files.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Check waitlist rows vs signup events.")
    parser.add_argument("db_path", nargs="?",
                        default=os.environ.get("DB_PATH", "./data/scan.db"))
    parser.add_argument("--show-emails", action="store_true",
                        help="List email addresses (default: only counts).")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"no database at {args.db_path} (nothing ever signed up here, "
              f"or the server uses another DB_PATH)", flush=True)
        return 1

    conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "waitlist" not in tables:
            print(f"{args.db_path} has no waitlist table yet "
                  f"(server started before the waitlist feature, or wrong file).")
            return 1

        total = conn.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()["n"]
        by_source = conn.execute(
            "SELECT source, COUNT(*) AS n FROM waitlist GROUP BY source").fetchall()
        succeeded = failed = 0
        if "analytics_events" in tables:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_events "
                "WHERE name='waitlist_signup_succeeded'").fetchone()
            succeeded = row["n"] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_events "
                "WHERE name='waitlist_signup_failed'").fetchone()
            failed = row["n"] if row else 0

        print(f"db: {args.db_path}")
        print(f"waitlist rows: {total}")
        for row in by_source:
            print(f"  {row['source']}: {row['n']}")
        print(f"signup_succeeded events: {succeeded}")
        print(f"signup_failed events: {failed}")

        if args.show_emails:
            rows = conn.execute(
                "SELECT email, website, source, created_at FROM waitlist "
                "ORDER BY created_at DESC LIMIT ?", (args.limit,)).fetchall()
            for row in rows:
                print(f"  {row['email']} | {row['website'] or '-'} | "
                      f"{row['source']} | {row['created_at']}")

        # Every stored signup records exactly one succeeded event in the same
        # database (duplicates and honeypot drops never reach the insert), so
        # fewer rows than successes means rows were lost or deleted.
        if succeeded > total:
            print(f"WARNING: {succeeded - total} successful signup(s) have no row "
                  f"(rows deleted, or DB file replaced after signups).")
            return 2
        if total == 0:
            print("note: empty. If signups were made through a --demo server, "
                  "that is expected: demo keeps them in memory only.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
