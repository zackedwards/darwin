"""SQLite state: what has been seen, decided, approved and quarantined.  Bodies are never stored.

One file, two tables, no ORM.  The database is what makes the tool incremental
(skip messages seen before) and auditable (every action is a row).  It sits on
the tenant's storage next to the archive and contains subjects and addresses,
so it is as sensitive as the archive's index and no more.

Lifecycle of a row in `messages`:

    insert()        first time the key is seen: class, category, decision, reason
    redecide()      every run: decision recomputed from stored class + today's date
    set_approval()  director's CSV edit read back after the pause
    mark_applied()  message copied to quarantine and removed from the mbox

Rows with applied_at set are history; every query here excludes them.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import classify

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  msg_key TEXT PRIMARY KEY,          -- classify.Msg.key: normalized Message-ID or sha256 of bytes
  mbox_key INTEGER,                  -- position in the mbox as of the last run (refreshed every run)
  date TEXT, from_addr TEXT, subject TEXT, size_bytes INTEGER,
  klass TEXT, category TEXT, via TEXT, decision TEXT, reason TEXT, flags TEXT,   -- flags is a JSON list of names
  retain_until TEXT,                 -- ISO date for RETAIN rows with a finite retention; NULL otherwise
  approved_action TEXT, approved_by TEXT, approved_at TEXT,   -- 'delete' | 'retain', who, when
  applied_at TEXT, quarantine_file TEXT                       -- set once the message has left the archive
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, msg_key TEXT, action TEXT, detail TEXT   -- action: classified | sent_to_model | approved | quarantined
);
"""


def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) and return a connection whose rows act like dicts."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)   # idempotent thanks to IF NOT EXISTS
    return con


def seen(con) -> set:
    """All keys ever inserted, including applied ones, so a quarantined message is never re-classified if it reappears."""
    return {r[0] for r in con.execute("SELECT msg_key FROM messages")}


def set_mbox_key(con, key: str, mbox_key: int) -> None:
    """Refresh a known message's mbox position.  Removing messages renumbers an mbox, so positions go stale between runs."""
    con.execute("UPDATE messages SET mbox_key=? WHERE msg_key=?", (mbox_key, key))


def insert(con, msg: classify.Msg, res: dict) -> None:
    """Record a newly classified message.  `res` is classify.classify_one()'s dict.  No body, no matched values."""
    con.execute(
        "INSERT INTO messages (msg_key, mbox_key, date, from_addr, subject, size_bytes, klass, category, via, decision, reason, flags, retain_until)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (msg.key, msg.mbox_key, msg.date.isoformat() if msg.date else None, msg.from_addr, msg.subject[:200], msg.size,
         res["klass"], res["category"], res["via"], res["decision"], res["reason"], json.dumps(res["flags"]), res["retain_until"]),
    )


def audit(con, key: str | None, action: str, detail: str, at: datetime) -> None:
    """Append one audit row.  Nothing ever updates or deletes from this table."""
    con.execute("INSERT INTO audit (at, msg_key, action, detail) VALUES (?,?,?,?)", (at.isoformat(), key, action, detail))


def redecide(con, policy: dict, now: datetime) -> None:
    """Recompute every unapplied row's decision from its stored class and today's date.

    This is how RETAIN_PENDING ages into DELETE_PROPOSED 30 days later without
    re-reading the mbox or asking the model again, and how a changed
    window_days or retention_years in policy.json takes effect on the next run.

    A row the director approved as 'retain' is pinned to RETAIN regardless of
    class, so the same message never comes back as a proposal.
    """
    rows = con.execute("SELECT msg_key, klass, category, date, flags, approved_action FROM messages WHERE applied_at IS NULL").fetchall()
    for r in rows:
        if r["approved_action"] == "retain":  # director pinned it; stays KEEP on every future run
            con.execute("UPDATE messages SET decision='RETAIN' WHERE msg_key=?", (r["msg_key"],))
            continue
        date = datetime.fromisoformat(r["date"]) if r["date"] else None
        series = r["category"] if r["klass"] == "RECORD" else None   # category holds the series id only for records
        decision, until = classify.decide(r["klass"], json.loads(r["flags"]), date, now, policy, series)
        con.execute("UPDATE messages SET decision=?, retain_until=? WHERE msg_key=?", (decision, until, r["msg_key"]))


def counts(con) -> dict:
    """decision -> (count, bytes) over messages still in the archive.  Feeds the bucket totals printed by `run`."""
    return {r["decision"]: (r["n"], r["b"]) for r in con.execute(
        "SELECT decision, COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM messages WHERE applied_at IS NULL GROUP BY decision")}


def pending(con) -> list:
    """Every message still in the archive; the rows the report is built from."""
    return con.execute("SELECT * FROM messages WHERE applied_at IS NULL").fetchall()


def applied_count(con) -> int:
    """How many messages earlier runs have already quarantined; shown in the report header."""
    return con.execute("SELECT COUNT(*) FROM messages WHERE applied_at IS NOT NULL").fetchone()[0]


def set_approval(con, key: str, action: str, by: str, at: datetime) -> bool:
    """Record the director's action for one message.  Returns False if the key is unknown or already applied.

    A 'retain' approval also flips the decision to RETAIN right away so the
    counts and the regenerated report reflect it in this run, not the next.
    """
    cur = con.execute("UPDATE messages SET approved_action=?, approved_by=?, approved_at=?,"
                      " decision=CASE WHEN ?='retain' THEN 'RETAIN' ELSE decision END WHERE msg_key=? AND applied_at IS NULL",
                      (action, by, at.isoformat(), action, key))
    return cur.rowcount == 1


def approved_deletes(con) -> list:
    """Rows approved 'delete' and not yet removed, ordered by mbox position so quarantine files read in archive order."""
    return con.execute("SELECT * FROM messages WHERE approved_action='delete' AND applied_at IS NULL ORDER BY mbox_key").fetchall()


def mark_applied(con, key: str, at: datetime, qfile: str) -> None:
    """The message is now in `qfile` and out of the archive.  From here on every query ignores it."""
    con.execute("UPDATE messages SET applied_at=?, quarantine_file=? WHERE msg_key=?", (at.isoformat(), qfile, key))
