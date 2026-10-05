"""SQLite FTS5 storage layer.

SQLite + FTS5 only — zero dependencies, works everywhere Python runs.
Each item is stored with a raw text column and a normalized column
(Arabic-aware), so search works in English and Arabic alike.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

from .arabic import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    created_at TEXT,
    raw TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    normalized, content='items', content_rowid='id'
);
"""


class Archive:
    """Thin wrapper around an SQLite archive database."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add(self, source: str, text: str, kind: str = "text",
            created_at: str | None = None) -> int:
        """Insert one item; returns its id."""
        cur = self.conn.execute(
            "INSERT INTO items (source, kind, created_at, raw) VALUES (?,?,?,?)",
            (source, kind,
             created_at or datetime.now(timezone.utc).isoformat(),
             text.strip()),
        )
        rowid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO items_fts (rowid, normalized) VALUES (?, ?)",
            (rowid, normalize(text)),
        )
        self.conn.commit()
        return rowid

    def search(self, query: str, limit: int = 20):
        """Full-text search (Arabic-normalized).

        Tokens are OR-joined and ranked with bm25, so a natural-language
        question such as "When is the meeting?" still surfaces the relevant
        entry even when none of the question words appear verbatim in it.
        """
        q = normalize(query)
        if not q:
            return []
        tokens = [t for t in re.split(r"\s+", q) if t]
        match_expr = " OR ".join(f'"{t}"' for t in tokens)
        rows = self.conn.execute(
            "SELECT id, source, kind, created_at, raw, "
            "bm25(items_fts) AS rank "
            "FROM items_fts JOIN items ON items.id = items_fts.rowid "
            "WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
            (match_expr, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        sources = self.conn.execute(
            "SELECT source, COUNT(*) c FROM items GROUP BY source"
        ).fetchall()
        return {"items": n, "sources": {r["source"]: r["c"] for r in sources}}

    def close(self):
        self.conn.close()
