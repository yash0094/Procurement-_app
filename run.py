#!/usr/bin/env python3
"""
TenderKart Mini / BidVector -- entry point.

    python3 run.py                 seed if needed, then serve on :8000
    python3 run.py --port 9000     different port
    python3 run.py --reseed        wipe and regenerate the demo data
    python3 run.py --seed-only     build the database and exit

No dependencies beyond the Python standard library (3.9+).
"""

import argparse
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import db, seed as seeder, api            # noqa: E402,F401
from backend.server import serve                       # noqa: E402
from backend.engine import pricing                     # noqa: E402

BANNER = r"""
  ____  _     _ __     __        _
 | __ )(_) __| |\ \   / /__  ___| |_ ___  _ __
 |  _ \| |/ _` | \ \ / / _ \/ __| __/ _ \| '__|
 | |_) | | (_| |  \ V /  __/ (__| || (_) | |
 |____/|_|\__,_|   \_/ \___|\___|\__\___/|_|

 Tender discovery + auction-theoretic bid pricing for Indian public procurement
"""


def main():
    ap = argparse.ArgumentParser(description="Run TenderKart Mini / BidVector")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reseed", action="store_true",
                    help="delete the database and regenerate demo data")
    ap.add_argument("--seed-only", action="store_true")
    ap.add_argument("--open", action="store_true",
                    help="open a browser window once the server is up")
    args = ap.parse_args()

    if args.reseed and os.path.exists(db.DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = db.DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        print("Removed existing database.")

    db.init_db()
    if not db.is_seeded():
        print("Generating demo corpus (this takes a few seconds)...")
        seeder.seed()
    pricing.clear_cache()

    if args.seed_only:
        return

    print(BANNER)
    counts = db.query_one("""
        SELECT (SELECT COUNT(*) FROM tenders) AS tenders,
               (SELECT COUNT(*) FROM awards)  AS awards,
               (SELECT COUNT(*) FROM bids)    AS bids""")
    print(f" {counts['tenders']} live tenders | {counts['awards']} historical "
          f"awards | {counts['bids']} recorded bids")
    print(f"\n  ->  http://{args.host}:{args.port}")
    print("  ->  demo login:  demo@bidvector.in  /  demo1234\n")
    print(" Ctrl-C to stop.\n")

    httpd = serve(args.host, args.port)
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
