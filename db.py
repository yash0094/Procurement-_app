"""
SQLite schema and connection handling for TenderKart Mini.

Stdlib only. The database is a single file (data/tenderkart.db) created on
first run. Every table here maps to something a real tender platform needs:
tenders, the historical award/bid record that the pricing engine learns from,
company profiles for eligibility matching, and a bid pipeline.
"""

import os
import sqlite3
import json
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "tenderkart.db")

_local = threading.local()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- The company profile drives eligibility matching and the cost/capital model.
CREATE TABLE IF NOT EXISTS profiles (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    udyam_no            TEXT,
    msme_class          TEXT,     -- Micro | Small | Medium
    bidder_class        TEXT,     -- Class I | Class II | Non-local
    turnover_cr         REAL,     -- average annual turnover, INR crore
    experience_years    REAL,
    max_similar_work_cr REAL,     -- largest single similar work executed
    certifications      TEXT,     -- JSON list
    states              TEXT,     -- JSON list of states they operate in
    categories          TEXT,     -- JSON list of work categories
    working_capital_cr  REAL,     -- capital available to lock in EMDs
    overhead_pct        REAL,     -- overhead as % of direct cost
    target_margin_pct   REAL
);

CREATE TABLE IF NOT EXISTS tenders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no            TEXT UNIQUE NOT NULL,
    title             TEXT NOT NULL,
    buyer             TEXT NOT NULL,
    buyer_state       TEXT NOT NULL,
    category          TEXT NOT NULL,
    portal            TEXT NOT NULL,     -- CPPP | GeM | State eProc
    estimated_value   REAL NOT NULL,     -- INR
    emd               REAL NOT NULL,     -- INR
    tender_fee        REAL NOT NULL,
    published_at      TEXT NOT NULL,
    closes_at         TEXT NOT NULL,
    completion_months INTEGER NOT NULL,
    description       TEXT NOT NULL,
    raw_eligibility   TEXT NOT NULL,     -- unstructured clause text (parser input)
    eligibility_json  TEXT NOT NULL,     -- structured criteria extracted from the above
    expected_bidders  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenders_cat   ON tenders(category);
CREATE INDEX IF NOT EXISTS idx_tenders_buyer ON tenders(buyer);
CREATE INDEX IF NOT EXISTS idx_tenders_close ON tenders(closes_at);

-- Historical results. This is the training data for the pricing engine.
CREATE TABLE IF NOT EXISTS awards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no          TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    buyer           TEXT NOT NULL,
    buyer_state     TEXT NOT NULL,
    category        TEXT NOT NULL,
    estimated_value REAL NOT NULL,
    awarded_at      TEXT NOT NULL,
    n_bidders       INTEGER NOT NULL,
    winner          TEXT NOT NULL,
    winning_bid     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_awards_bucket ON awards(buyer, category);

CREATE TABLE IF NOT EXISTS bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    award_id    INTEGER NOT NULL REFERENCES awards(id) ON DELETE CASCADE,
    bidder      TEXT NOT NULL,
    amount      REAL NOT NULL,
    rank        INTEGER NOT NULL      -- 1 = L1 (lowest, wins)
);

CREATE INDEX IF NOT EXISTS idx_bids_award  ON bids(award_id);
CREATE INDEX IF NOT EXISTS idx_bids_bidder ON bids(bidder);

-- A user's bid pipeline: watchlist -> preparing -> submitted -> won/lost.
CREATE TABLE IF NOT EXISTS pipeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tender_id   INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,     -- watching|preparing|submitted|won|lost|dropped
    our_bid     REAL,
    cost_est    REAL,
    emd_paid    REAL NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    UNIQUE(user_id, tender_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tender_id   INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,     -- match|deadline|result|screen
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, read);

-- Saved search that powers the "new matching tender" alerts.
CREATE TABLE IF NOT EXISTS saved_searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    params      TEXT NOT NULL,     -- JSON of the search filters
    created_at  TEXT NOT NULL
);
"""


def connect():
    """One connection per thread; http.server uses a thread per request."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def query(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql, params=()):
    conn = connect()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def executemany(sql, seq):
    conn = connect()
    cur = conn.executemany(sql, seq)
    conn.commit()
    return cur.rowcount


def is_seeded():
    try:
        row = connect().execute("SELECT COUNT(*) AS c FROM tenders").fetchone()
        return row["c"] > 0
    except sqlite3.OperationalError:
        return False


def jloads(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (ValueError, TypeError):
        return default if default is not None else []
