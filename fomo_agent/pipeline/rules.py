"""Transparent research scores from locally observed, verified position cycles.

These scores are evidence rankings, not predicted returns or calibrated probabilities.
Indexer PnL, follower counts and unpriced open bags do not increase a score.
"""
from __future__ import annotations

from collections import defaultdict
import math
import sqlite3

from .. import db
from ..config import settings
from ..models import ScoreResult
from ..sources.rpc import QUOTE_TOKENS

MODEL = "rules:verified-cycles-v1"


def evidence(conn: sqlite3.Connection, address: str, now: int | None = None) -> dict:
    now = db.now() if now is None else now
    rows = conn.execute(
        "SELECT chain,mint,side,ts,token_amount,usd_value FROM trades "
        "WHERE address=? AND kind='trade' AND ts>=? AND ts<=? ORDER BY ts,sig",
        (address, now - settings.rule_lookback_days * 86400, now),
    ).fetchall()
    by_token: dict[tuple, list] = defaultdict(list)
    for row in rows:
        if row["mint"].lower() not in QUOTE_TOKENS:
            by_token[(row["chain"], row["mint"])].append(row)
    cycles, spans, unknown = [], [], 0
    open_cost = 0.0
    valid_fills = 0
    for key, fills in by_token.items():
        quantity = cost = spent = proceeds = 0.0
        token_cycles = []
        invalid = False
        # A one-second timestamp cannot order a buy and a sell in the same second.
        sides: dict[int, set] = defaultdict(set)
        for fill in fills:
            sides[fill["ts"]].add(fill["side"])
        if any(len(v) > 1 for v in sides.values()):
            unknown += 1
            continue
        for fill in fills:
            amount, usd = fill["token_amount"], fill["usd_value"]
            if (amount is None or usd is None or not math.isfinite(amount)
                    or not math.isfinite(usd) or amount <= 0 or usd <= 0):
                invalid = True
                break
            if fill["side"] == "buy":
                quantity += amount
                cost += usd
                spent += usd
            else:
                # Missing earlier inventory makes this token's cost basis unknown. Never
                # turn a sell received before our first observed buy into free profit.
                tolerance = max(quantity, amount) * 1e-8
                if quantity <= 0 or amount > quantity + tolerance:
                    invalid = True
                    break
                fraction = min(amount / quantity, 1)
                cost *= 1 - fraction
                quantity = max(0, quantity - amount)
                proceeds += usd
                if quantity <= tolerance:
                    buffer = (spent + proceeds) * settings.rule_cost_buffer_bps / 10_000
                    token_cycles.append({
                        "token": ":".join(str(v) for v in key),
                        "cost_usd": spent, "pnl_usd": proceeds - spent - buffer,
                        "closed_at": fill["ts"],
                    })
                    quantity = cost = spent = proceeds = 0.0
        if invalid:
            unknown += 1
            continue
        valid_fills += len(fills)
        spans.extend(fill["ts"] for fill in fills)
        cycles.extend(token_cycles)
        open_cost += cost
    gains = sum(max(c["pnl_usd"], 0) for c in cycles)
    losses = -sum(min(c["pnl_usd"], 0) for c in cycles)
    closed_cost = sum(c["cost_usd"] for c in cycles)
    return {
        "verified_fills": valid_fills, "closed_cycles": len(cycles),
        "closed_tokens": len({c["token"] for c in cycles}),
        "wins": sum(c["pnl_usd"] > 0 for c in cycles),
        "matched_pnl_usd": round(gains - losses, 4),
        "matched_cost_usd": round(closed_cost, 4),
        "gross_gains_usd": round(gains, 4), "gross_losses_usd": round(losses, 4),
        "open_cost_usd": round(open_cost, 4), "unknown_cost_tokens": unknown,
        "observed_span_hours": round((max(spans) - min(spans)) / 3600, 2) if spans else 0,
        "last_verified_ts": max(spans) if spans else None,
        "largest_win_share": max((c["pnl_usd"] for c in cycles), default=0) / gains if gains else 0,
        "cost_buffer_bps": settings.rule_cost_buffer_bps,
        "caveat": "Locally matched cycles only; excludes unknown inventory and unpriced open PnL. "
                  "Cost buffer is an assumption, not actual fee accounting.",
    }


def judge(e: dict) -> ScoreResult:
    n = e["closed_cycles"]
    checks = [
        (e["verified_fills"] >= settings.rule_min_trades, f'{settings.rule_min_trades} verified fills'),
        (n >= settings.rule_min_cycles, f'{settings.rule_min_cycles} closed cycles'),
        (e["closed_tokens"] >= settings.rule_min_tokens, f'{settings.rule_min_tokens} closed tokens'),
        (e["observed_span_hours"] >= settings.rule_min_history_hours,
         f'{settings.rule_min_history_hours:g} hours of observed activity'),
        (e["open_cost_usd"] <= e["matched_cost_usd"] * settings.rule_max_open_cost_ratio,
         "open cost no larger than the configured fraction of matched closed cost"),
    ]
    missing = [name for passed, name in checks if not passed]
    intro = (f'{n} matched closed cycles across {e["closed_tokens"]} tokens; '
             f'${e["matched_pnl_usd"]:,.2f} sample PnL after a '
             f'{settings.rule_cost_buffer_bps:g} bps cost buffer on both legs.')
    if missing:
        # 60 is upstream's trusted-feed cutoff. A low-evidence wallet remains watched,
        # so it can gain evidence instead of falling out of collection permanently.
        progress = sum(passed for passed, _ in checks) / len(checks)
        return ScoreResult(score=40 + round(19 * progress), status="watch", style=[],
                           red_flags=[], confidence=round(0.1 + 0.2 * progress, 2),
                           summary=intro + " Below signal eligibility: need " + ", ".join(missing) + ".")
    win_rate = e["wins"] / n
    pf = e["gross_gains_usd"] / e["gross_losses_usd"] if e["gross_losses_usd"] else 3
    value = 40 + 30 * win_rate + 20 * min(max(pf - 1, 0) / 2, 1) + 10 * min(n / 30, 1)
    flags = []
    if e["largest_win_share"] > 0.6:
        value = min(value, 59)
        flags.append("one-hit")
    if e["matched_pnl_usd"] <= 0:
        value = min(value, 39)
    value = max(0, min(95, round(value)))
    return ScoreResult(
        score=value, status="active" if value >= 70 else "watch" if value >= 40 else "dropped",
        style=[], red_flags=flags, confidence=round(min(0.85, 0.4 + n / 100), 2),
        summary=intro + f" {win_rate:.0%} of observed cycles profitable; "
                "research ranking only, with open PnL and third-party headline profits excluded.",
    )


def score_all(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    from .score import apply_score

    rows = conn.execute("SELECT address FROM traders ORDER BY address").fetchall()
    stats = {"scored": 0, "model": MODEL, "cost_usd": 0.0, "by_status": {}}
    for row in rows[:limit] if limit else rows:
        data = evidence(conn, row["address"])
        result = judge(data)
        apply_score(conn, row["address"], result, MODEL, evidence=data)
        stats["scored"] += 1
        stats["by_status"][result.status] = stats["by_status"].get(result.status, 0) + 1
    return stats
