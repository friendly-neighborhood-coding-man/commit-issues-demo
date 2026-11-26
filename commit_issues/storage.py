"""SQLite drafts, capped model reservations, and a durable posting ledger."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .debate import ORDER
from .github import AppError

MAX_MODEL_CALLS = 21


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, filename):
        filename = Path(filename)
        filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(filename, timeout=10)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                digest TEXT PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS drafts_by_repo ON drafts(repo);
            CREATE TABLE IF NOT EXISTS model_reservations (request TEXT PRIMARY KEY, calls INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS publications (
                repo TEXT NOT NULL, pr INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (repo, pr)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                digest TEXT NOT NULL, turn INTEGER NOT NULL, comment_id INTEGER, PRIMARY KEY(digest, turn)
            );
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def save(self, plan):
        digest = fingerprint(plan)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO drafts VALUES (?, ?, ?, ?)",
                            (digest, plan["repo"], plan["number"], encode(plan)))
        return digest

    def load(self, digest):
        row = self.db.execute("SELECT payload FROM drafts WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise AppError("No matching private draft in this database.")
        plan = json.loads(row[0])
        if fingerprint(plan) != digest:
            raise AppError("The saved draft failed its approval fingerprint check.")
        return plan

    def memories(self, target):
        row = self.db.execute("""
            SELECT payload FROM drafts WHERE repo = ? AND pr != ? ORDER BY rowid DESC LIMIT 1
        """, (target["repo"], target["number"])).fetchone()
        if row is None:
            return {}
        return {turn["emotion"]: f"{turn['reply_to']} argued about {turn['topic']}."
                for turn in json.loads(row[0])["turns"] if turn["reply_to"]}

    def reserve_model(self, target, model):
        key = fingerprint([target["repo"], target["number"], target["head"], target["base"], model])
        with self.transaction():
            if self.db.execute("SELECT 1 FROM model_reservations WHERE request = ?", (key,)).fetchone():
                raise AppError("This model already has a reservation for this revision. Use its draft or the mock.")
            used, = self.db.execute("SELECT COALESCE(SUM(calls), 0) FROM model_reservations").fetchone()
            if used + len(ORDER) > MAX_MODEL_CALLS:
                raise AppError("The database's lifetime model call budget is exhausted.")
            self.db.execute("INSERT INTO model_reservations VALUES (?, ?)", (key, len(ORDER)))

    def bind_publication(self, plan, digest):
        target = (plan["repo"], plan["number"])
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO publications VALUES (?, ?, ?)", (*target, digest))
            row = self.db.execute("SELECT digest FROM publications WHERE repo = ? AND pr = ?", target).fetchone()
            if row[0] != digest:
                raise AppError("Another draft already owns this PR's seven-comment budget.")

    def deliveries(self, digest):
        return dict(self.db.execute("SELECT turn, comment_id FROM deliveries WHERE digest = ?", (digest,)))

    def claim_turn(self, digest, turn):
        with self.transaction():
            inserted = self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL)", (digest, turn))
            if inserted.rowcount:
                return True
            row = self.db.execute("SELECT comment_id FROM deliveries WHERE digest = ? AND turn = ?",
                                  (digest, turn)).fetchone()
            if row[0] is not None:
                return False
            raise AppError("A write is in flight or unresolved. No duplicate POST was sent.")

    def confirm_turn(self, digest, turn, comment_id):
        with self.db:
            self.db.execute("UPDATE deliveries SET comment_id = ? WHERE digest = ? AND turn = ? AND comment_id IS NULL",
                            (comment_id, digest, turn))
