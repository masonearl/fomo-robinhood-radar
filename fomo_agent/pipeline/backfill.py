"""Walk the chain backwards and fill in the tape from before we were watching.

Everything the product says about a position is bounded by when tracking started. A name a wallet
entered earlier shows a size and a value and no profit, because the entry price is missing — the
`held` state on a trader page, and every dash in the PnL column, is that boundary showing.

The boundary is not fundamental. `eth_getLogs` will answer for any range; it just refuses ranges it
finds too expensive, and how expensive a range is depends on how far back it is. The live tracker
gets away with 200k blocks because recent blocks are hot; the same width a week back answers "log
query timed out". So the width is not a constant to be tuned once — it is a negotiation, and the
endpoint is the one that knows. A range that times out is halved and both halves are retried, down
to a floor; a range that answers is kept.

Newest first matters: a run that is interrupted has still filled the part nearest today, which is
the part anybody reads.

It is free. The cost is time — a few hundred requests against a rate limit that exists to be
polite, not because anybody is charging.
"""
from __future__ import annotations

import logging
import hashlib
import sqlite3
import time

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, RobinhoodRPC
from .track import tracked_statuses
from . import state

log = logging.getLogger(__name__)

# The endpoint says "log query timed out" for a range it will not serve, and "rate limited" when it
# wants a pause. They need opposite responses: narrow for the first, wait for the second.
TOO_WIDE = "timed out"
TOO_FAST = "rate limited"


def wallets_to_backfill(conn: sqlite3.Connection) -> list[str]:
    """Every tracked wallet on this chain, shallowest tape first.

    Ordering by how far back we already have fills means a repeated run widens the shallowest
    histories rather than deepening the ones that are already deep.
    """
    rows = conn.execute(
        f"SELECT t.address address, MIN(tr.ts) first_ts FROM traders t "
        f"LEFT JOIN trades tr ON tr.address = t.address "
        f"WHERE t.chain = ? AND t.status IN ({','.join('?' * len(tracked_statuses()))}) "
        "GROUP BY t.address ORDER BY COALESCE(first_ts, 0) DESC",
        (CHAIN, *tracked_statuses()),
    ).fetchall()
    return [r["address"] for r in rows if r["address"].startswith("0x")]


def store(conn: sqlite3.Connection, found: dict, rpc: RobinhoodRPC) -> tuple[int, int]:
    """Write one range's fills. Per range, so an interrupted run keeps what it already found."""
    fills = new = 0
    with db.tx(conn):
        for trades in found.values():
            for t in trades:
                fills += 1
                new += db.insert_trade(conn, **t.model_dump())
        db.save_token_decimals(conn, rpc.known_decimals())
    return fills, new


def backfill(conn: sqlite3.Connection, days: int = 30, rpc: RobinhoodRPC | None = None,
             max_requests: int | None = None, resume: bool = False) -> dict:
    """Fill the tape back `days`, newest range first, stopping at a request budget.

    Every wallet goes into the same query: the topic filter takes a list, so one range costs the
    same two requests whether the roster is one wallet or three hundred.
    """
    wallets = wallets_to_backfill(conn)
    stats = {"wallets": len(wallets), "ranges": 0, "narrowed": 0, "waited": 0, "failed": 0,
             "fills": 0, "new": 0, "requests": 0, "days": days, "reached_h": 0.0,
             "stopped_early": False}
    if not wallets:
        log.info("backfill: nothing tracked on %s", CHAIN)
        return stats

    rpc = rpc or RobinhoodRPC()
    rpc.load_decimals(db.token_decimals(conn))
    roster_key = hashlib.sha256("\n".join(sorted(wallets)).encode()).hexdigest()
    saved = state.get(conn, "backfill") if resume else None
    previous = saved["details"] if saved else {}
    same = previous.get("roster") == roster_key and previous.get("days") == days
    if same and previous.get("complete"):
        return {"skipped": "history target complete", **previous}
    budget = max_requests if max_requests is not None else settings.backfill_max_requests
    floor = settings.backfill_min_window_blocks
    head = previous["next_block"] if same else rpc.block_number()
    head_ts = rpc.block_timestamp(head)
    since = previous["since"] if same else db.now() - days * 86400
    progress = {"roster": roster_key, "days": days, "since": since,
                "next_block": head, "complete": False,
                "oldest_scanned_ts": previous.get("oldest_scanned_ts") if same else None}

    # A stack rather than a loop, because a range that will not answer becomes two ranges.
    pending = list(rpc.windows(since, head=head, span=settings.backfill_window_blocks))
    pending.reverse()   # newest ends up on top

    if resume:
        state.put(conn, "backfill", "running", progress)
    while pending:
        if rpc.requests >= budget:
            stats["stopped_early"] = True
            log.info("backfill: stopping at the %d-request budget", budget)
            break
        first, last = pending.pop()
        try:
            found = rpc.scan(wallets, first, last)
        except Exception as e:  # noqa: BLE001 - the message is the whole point here
            text = str(e)
            if TOO_WIDE in text and last - first > floor:
                mid = (first + last) // 2
                pending.extend([(first, mid), (mid + 1, last)])   # narrower halves, newest first
                stats["narrowed"] += 1
                continue
            if TOO_FAST in text:
                stats["waited"] += 1
                time.sleep(settings.backfill_cooldown_s)
                pending.append((first, last))                     # the same range, later
                continue
            stats["failed"] += 1
            log.warning("backfill %d..%d gave up: %s", first, last, text)
            if resume:
                state.put(conn, "backfill", "error", {**progress, "error": text[:300]})
                break  # never checkpoint past a failed range
            continue

        stats["ranges"] += 1
        f, n = store(conn, found, rpc)
        stats["fills"] += f
        stats["new"] += n
        stats["reached_h"] = max(stats["reached_h"], (head - first) * 0.1 / 3600)
        if resume:
            progress["next_block"] = max(0, first - 1)
            progress["oldest_scanned_ts"] = rpc.block_timestamp(first)
            progress["complete"] = not pending
            state.put(conn, "backfill", "complete" if not pending else "collecting",
                      progress, success=True)

    stats["requests"] = rpc.requests
    if resume and not pending and not stats["failed"]:
        progress["complete"] = True
        state.put(conn, "backfill", "complete", progress, success=True)
    from .provenance import classify, refresh_medians
    stats["kinds"] = classify(conn, since=0)
    refresh_medians(conn)
    log.info("backfill: %s", stats)
    return stats
