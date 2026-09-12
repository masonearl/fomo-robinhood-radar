"""Small durable checkpoints shared by the collector, watcher and status page."""
from __future__ import annotations

import json
import sqlite3

from .. import db


def get(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM pipeline_state WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "details": json.loads(row["details_json"])}


def put(conn: sqlite3.Connection, name: str, status: str, details: dict,
        *, success: bool = False, now: int | None = None) -> None:
    now = db.now() if now is None else now
    with db.tx(conn):
        conn.execute(
            "INSERT INTO pipeline_state(name,updated_at,last_success,status,details_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at, status=excluded.status, "
            "last_success=COALESCE(excluded.last_success,pipeline_state.last_success), "
            "details_json=excluded.details_json",
            (name, now, now if success else None, status, json.dumps(details)),
        )
