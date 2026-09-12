"""Describe service freshness separately from a quiet market and low evidence."""
from __future__ import annotations

import sqlite3

from .. import db
from ..config import settings
from . import state


def snapshot(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = db.now() if now is None else now
    watcher = state.get(conn, "watcher")
    last_tick = watcher["last_success"] if watcher else None
    tick_age = max(0, now - last_tick) if last_tick is not None else None
    watching = tick_age is not None and tick_age <= settings.pipeline_stale_s and watcher["status"] != "error"
    last_track = conn.execute(
        "SELECT MAX(finished_at) FROM runs WHERE kind='track' AND error IS NULL"
    ).fetchone()[0]
    track_age = max(0, now - last_track) if last_track else None
    collecting = track_age is not None and track_age < max(settings.track_interval * 2, settings.pipeline_stale_s)
    total, scored, eligible, last_score, oldest_score = conn.execute(
        "SELECT COUNT(*),COUNT(score),SUM(CASE WHEN score>=60 THEN 1 ELSE 0 END),"
        "MAX(ai_scored_at),MIN(ai_scored_at) FROM traders").fetchone()
    score_age = max(0, now - oldest_score) if oldest_score else None
    freshness = settings.score_interval * 2 if settings.scorer == "rules" else 24 * 3600
    scoring = total > 0 and scored == total and score_age is not None and score_age < freshness
    history = state.get(conn, "backfill")
    return {
        "state": "degraded" if not watching or not collecting else
                 "scoring_required" if not scoring else "ready" if eligible else "collecting_evidence",
        "scorer": settings.scorer, "wallets": total, "scored": scored,
        "signal_eligible": eligible or 0, "last_score_at": last_score,
        "checks": [
            {"name": "Chain watcher", "ok": watching, "age_s": tick_age,
             "detail": watcher["status"] if watcher else "not started"},
            {"name": "Wallet collection", "ok": collecting, "age_s": track_age,
             "detail": "last successful tracking pass"},
            {"name": "Wallet scoring", "ok": scoring, "age_s": score_age,
             "detail": f"{scored}/{total} wallets; {settings.scorer} mode"},
        ],
        "history": history["details"] if history else None,
        "history_status": history["status"] if history else "not started",
        "cost_buffer_bps": settings.rule_cost_buffer_bps,
    }
