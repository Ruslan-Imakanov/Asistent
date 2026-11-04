"""SQLite storage: tasks, communications, contacts, products, sales, usage, events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',   -- todo|in_progress|review|rework|done|blocked|cancelled
    priority INTEGER NOT NULL DEFAULT 3,   -- 1 (highest) .. 5
    created_by TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    review_notes TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,                 -- email|telegram|telegram_channel
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    task_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review|pending_owner|approved|sent|dry_run|rejected|failed
    review_note TEXT NOT NULL DEFAULT '',
    is_cold INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    from_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    external_id TEXT UNIQUE,
    received_at REAL NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
    addr TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'email',
    name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'lead',    -- lead|contacted|replied|negotiating|customer|lost
    opted_out INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    folder TEXT NOT NULL DEFAULT '',
    payment_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',  -- draft|on_sale|archived
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    customer TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    source TEXT NOT NULL DEFAULT '',
    external_id TEXT UNIQUE,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    task_id INTEGER,
    answer TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open|answered
    created_at REAL NOT NULL,
    answered_at REAL
);
CREATE TABLE IF NOT EXISTS notes (
    agent TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (agent, key)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TASK_STATUSES = {"todo", "in_progress", "review", "rework", "done", "blocked", "cancelled"}


def day_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.listeners: list[Callable[[dict], None]] = []

    # ---------- low level ----------
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---------- settings ----------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- events ----------
    def log(self, agent: str, kind: str, text: str, **data: Any) -> dict:
        ts = time.time()
        cur = self.execute("INSERT INTO events(ts,agent,kind,text,data) VALUES(?,?,?,?,?)",
                           (ts, agent, kind, text, json.dumps(data, ensure_ascii=False, default=str)))
        event = {"id": cur.lastrowid, "ts": ts, "agent": agent, "kind": kind, "text": text, "data": data}
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # listeners must never break the office
                pass
        return event

    def recent_events(self, limit: int = 100) -> list[dict]:
        rows = self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return list(reversed(rows))

    # ---------- tasks ----------
    def create_task(self, title: str, description: str, assignee: str, created_by: str,
                    priority: int = 3) -> int:
        now = time.time()
        priority = max(1, min(5, int(priority)))
        cur = self.execute(
            "INSERT INTO tasks(title,description,assignee,created_by,priority,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", (title, description, assignee, created_by, priority, now, now))
        return int(cur.lastrowid)

    def task(self, task_id: int) -> dict | None:
        row = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["files"] = json.loads(row["files"])
        return row

    def tasks(self, status: str | None = None, assignee: str | None = None, limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM tasks WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if assignee:
            sql += " AND assignee=?"
            params.append(assignee)
        sql += " ORDER BY priority ASC, id ASC LIMIT ?"
        params.append(limit)
        rows = self.query(sql, params)
        for r in rows:
            r["files"] = json.loads(r["files"])
        return rows

    def update_task(self, task_id: int, **fields: Any) -> None:
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise ValueError(f"bad status {fields['status']}")
        if "files" in fields:
            fields["files"] = json.dumps(fields["files"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def next_task_for(self, assignee: str) -> dict | None:
        rows = self.query(
            "SELECT id FROM tasks WHERE assignee=? AND status IN ('rework','todo') "
            "ORDER BY CASE status WHEN 'rework' THEN 0 ELSE 1 END, priority, id LIMIT 1", (assignee,))
        return self.task(rows[0]["id"]) if rows else None

    # ---------- usage ----------
    def add_usage(self, agent: str, model: str, inp: int, out: int, cost: float) -> None:
        self.execute("INSERT INTO usage(ts,agent,model,input_tokens,output_tokens,cost_usd) "
                     "VALUES(?,?,?,?,?,?)", (time.time(), agent, model, inp, out, cost))

    def spend_today(self) -> float:
        row = self.one("SELECT COALESCE(SUM(cost_usd),0) AS s FROM usage WHERE ts>=?", (day_start(),))
        return float(row["s"]) if row else 0.0

    # ---------- outbox ----------
    def outbound_count_since(self, since: float, cold_only: bool = False) -> int:
        sql = ("SELECT COUNT(*) AS c FROM outbox WHERE created_at>=? "
               "AND status IN ('approved','sent','dry_run','pending_review','pending_owner')")
        if cold_only:
            sql += " AND is_cold=1"
        return int(self.one(sql, (since,))["c"])

    def messages_to_contact_since(self, addr: str, since: float) -> int:
        return int(self.one(
            "SELECT COUNT(*) AS c FROM outbox WHERE lower(to_addr)=lower(?) AND created_at>=? "
            "AND status IN ('approved','sent','dry_run','pending_review','pending_owner')",
            (addr, since))["c"])

    def week_ago(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()

    # ---------- contacts ----------
    def contact(self, addr: str) -> dict | None:
        return self.one("SELECT * FROM contacts WHERE lower(addr)=lower(?)", (addr.strip(),))

    def upsert_contact(self, addr: str, **fields: Any) -> None:
        addr = addr.strip()
        if not self.contact(addr):
            self.execute("INSERT INTO contacts(addr,created_at) VALUES(?,?)", (addr, time.time()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.execute(f"UPDATE contacts SET {cols} WHERE lower(addr)=lower(?)", (*fields.values(), addr))

    def has_inbound_from(self, addr: str) -> bool:
        return bool(self.one("SELECT 1 AS x FROM inbox WHERE lower(from_addr)=lower(?) LIMIT 1", (addr,)))
