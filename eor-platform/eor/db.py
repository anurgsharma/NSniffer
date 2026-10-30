"""SQLite persistence. One connection guarded by a re-entrant lock; JSON columns are
encoded/decoded transparently so the rest of the code works with plain dicts."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT, contact_email TEXT NOT NULL,
  billing_currency TEXT NOT NULL DEFAULT 'USD', eor_fee_monthly TEXT NOT NULL, aor_fee_pct TEXT NOT NULL,
  payment_terms_days INTEGER NOT NULL DEFAULT 7, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), full_name TEXT NOT NULL,
  email TEXT NOT NULL, country TEXT NOT NULL, engagement_type TEXT NOT NULL, job_title TEXT,
  start_date TEXT NOT NULL, end_date TEXT, salary_annual TEXT, rate_hourly TEXT, currency TEXT,
  status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, external_ref TEXT, data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timesheets (
  id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES workers(id), period TEXT NOT NULL,
  hours TEXT NOT NULL DEFAULT '0', expenses TEXT NOT NULL DEFAULT '0', status TEXT NOT NULL,
  pay_run_id TEXT, data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pay_runs (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), period TEXT NOT NULL,
  status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(client_id, period));
CREATE TABLE IF NOT EXISTS payslips (
  id TEXT PRIMARY KEY, pay_run_id TEXT NOT NULL REFERENCES pay_runs(id), worker_id TEXT NOT NULL,
  currency TEXT NOT NULL, gross TEXT NOT NULL, net TEXT NOT NULL, employer_cost TEXT NOT NULL,
  total_cost TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, pay_run_id TEXT NOT NULL, period TEXT NOT NULL,
  currency TEXT NOT NULL, amount TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL,
  external_ref TEXT, data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY, recipient TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'logged', data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_type, owner_id, kind);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
"""

PREFIX = {"clients": "cli", "workers": "wrk", "documents": "doc", "tasks": "tsk", "timesheets": "tms",
          "pay_runs": "run", "payslips": "slp", "invoices": "inv", "outbox": "msg", "events": "evt"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d.get("data") or "{}")
        return d

    def insert(self, table, **fields):
        ts = now_iso()
        fields.setdefault("id", f"{PREFIX[table]}_{uuid.uuid4().hex[:12]}")
        fields.setdefault("data", {})
        fields["data"] = json.dumps(fields["data"], default=str)
        fields["created_at"] = fields["updated_at"] = ts
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.lock:
            self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                              [None if v is None else (str(v) if not isinstance(v, (int, str)) else v)
                               for v in fields.values()])
        return self.get(table, fields["id"])

    def update(self, table, id, data=None, **fields):
        """Update columns; `data` is merged into the existing JSON blob."""
        with self.lock:
            if data:
                current = self.get(table, id)["data"]
                current.update(data)
                fields["data"] = json.dumps(current, default=str)
            fields["updated_at"] = now_iso()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = [None if v is None else (str(v) if not isinstance(v, (int, str)) else v) for v in fields.values()]
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", vals + [id])
        return self.get(table, id)

    def get(self, table, id):
        with self.lock:
            return self._row(self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id,)).fetchone())

    def find(self, table, order="created_at", limit=None, **where):
        clauses, vals = [], []
        for k, v in where.items():
            if isinstance(v, (list, tuple, set)):
                clauses.append(f"{k} IN ({', '.join('?' for _ in v)})")
                vals.extend(v)
            elif v is None:
                clauses.append(f"{k} IS NULL")
            else:
                clauses.append(f"{k} = ?")
                vals.append(v)
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, vals).fetchall()]

    def count_by(self, table, column):
        with self.lock:
            rows = self.conn.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}").fetchall()
        return {r[0]: r[1] for r in rows}

    def audit(self, entity_type, entity_id, type, **data):
        return self.insert("events", entity_type=entity_type, entity_id=entity_id, type=type, data=data)
