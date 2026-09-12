"""Isolated synthetic evidence verifies the local pipeline without funding or network."""
import json
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fomo_agent import api, db
from fomo_agent.config import settings
from fomo_agent.pipeline import backfill, hot, rules, score, state, system_status, track, watch
from fomo_agent.pipeline.provenance import classify
from fomo_agent.sources.rpc import USDG

W = ["0x" + f"{i:040x}" for i in range(100, 106)]
TOKEN = "0x" + "f" * 40


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "pipeline.db")
    monkeypatch.setattr(settings, "scorer_mode", "rules")
    c = db.connect()
    with db.tx(c):
        for i, address in enumerate(W):
            db.upsert_trader(c, address, chain="robinhood", fomo_handle=f"test{i}",
                             status="tracking", stats_json=json.dumps({"net_pnl": 999_000_000}))
    yield c
    c.close()


def fill(c, address, token, side, quantity, usd, ts, kind="trade"):
    with db.tx(c):
        sig = "0x" + hashlib.sha256(f"{address}:{token}:{side}:{ts}".encode()).hexdigest()
        db.insert_trade(c, sig=sig, address=address,
                        chain="robinhood", mint=token, side=side, token_amount=quantity,
                        usd_value=usd, ts=ts, source="rpc", kind=kind)


def history(c, address, *, loss=False, one_hit=False):
    now = db.now()
    for i in range(12):
        token = "0x" + f"{i + 1000:040x}"
        ts = now - 72 * 3600 + i * 3 * 3600
        out = 50 if loss else 150
        if one_hit:
            out = 100_000 if i == 0 else 101
        fill(c, address, token, "buy", 10, 100, ts)
        fill(c, address, token, "sell", 10, out, ts + 60)


def test_partial_sales_and_rebuys_close_at_full_cost(conn):
    now = db.now()
    for i, (side, quantity, usd) in enumerate(
        [("buy", 10, 100), ("sell", 4, 48), ("buy", 6, 120), ("sell", 12, 264)]
    ):
        fill(conn, W[0], TOKEN, side, quantity, usd, now - 400 + i * 60)
    e = rules.evidence(conn, W[0])
    assert e["closed_cycles"] == 1
    assert e["matched_cost_usd"] == 220
    assert e["matched_pnl_usd"] == pytest.approx(92 - 532 * 0.003)
    assert e["open_cost_usd"] == 0


def test_missing_inventory_and_same_second_order_are_not_free_profit(conn):
    now = db.now()
    fill(conn, W[0], TOKEN, "sell", 10, 1_000_000, now - 200)
    fill(conn, W[0], TOKEN, "buy", 10, 100, now - 100)
    fill(conn, W[0], TOKEN, "sell", 10, 200, now - 50)
    e = rules.evidence(conn, W[0])
    assert e["closed_cycles"] == 0 and e["unknown_cost_tokens"] == 1
    fill(conn, W[1], TOKEN, "buy", 10, 100, now - 1)
    fill(conn, W[1], TOKEN, "sell", 10, 200, now - 1)
    assert rules.evidence(conn, W[1])["closed_cycles"] == 0


def test_unknown_provenance_gifts_quotes_and_future_fills_do_not_score(conn):
    now = db.now()
    for i, kind in enumerate((None, "flow", "dust", "direct")):
        mint = "0x" + f"{500 + i:040x}"
        fill(conn, W[0], mint, "buy", 10, 1, now - 200, kind)
        fill(conn, W[0], mint, "sell", 10, 10_000_000, now - 100, kind)
    fill(conn, W[0], USDG, "buy", 10, 1, now - 200)
    fill(conn, W[0], USDG, "sell", 10, 10_000_000, now - 100)
    fill(conn, W[0], TOKEN, "buy", 10, 1, now + 10)
    fill(conn, W[0], TOKEN, "sell", 10, 1_000_000, now + 20)
    result = rules.judge(rules.evidence(conn, W[0]))
    assert result.score < 60 and result.status == "watch"
    assert rules.evidence(conn, W[0])["verified_fills"] == 0


def test_warmup_and_one_hit_or_large_open_inventory_cannot_enter_signals(conn):
    history(conn, W[0], one_hit=True)
    assert rules.judge(rules.evidence(conn, W[0])).score < 60
    history(conn, W[1])
    fill(conn, W[1], TOKEN, "buy", 10, 5000, db.now() - 1)
    assert rules.judge(rules.evidence(conn, W[1])).score < 60
    fill(conn, W[2], TOKEN, "buy", 10, 10, db.now() - 60)
    fill(conn, W[2], TOKEN, "sell", 10, 10000, db.now() - 30)
    assert rules.judge(rules.evidence(conn, W[2])).score < 60


def test_profitable_and_losing_records_have_auditable_scores_and_remain_observable(conn):
    history(conn, W[0])
    history(conn, W[1], loss=True)
    outcome = score.score_all(conn)
    assert outcome["scored"] == 6 and outcome["cost_usd"] == 0
    winner, loser = db.get_trader(conn, W[0]), db.get_trader(conn, W[1])
    assert winner["score"] >= 80 and loser["status"] == "dropped"
    assert json.loads(winner["tags"])["evidence"]["closed_cycles"] == 12
    assert "dropped" in track.tracked_statuses()
    assert W[1] in watch.roster(conn)
    # A single-wallet request must also stay on the configured free scorer.
    result, cost = score.score_trader(conn, W[0])
    assert result.score == winner["score"] and cost == 0


def test_verified_history_to_score_to_live_burst_api(conn):
    now = db.now()
    for i, address in enumerate(W[:5]):
        history(conn, address)
        fill(conn, address, TOKEN, "buy", 10, 100, now - 300 + i * 30, "flow")
    # Give a sixth wallet a strong record, but only an outside-key gift of this token.
    history(conn, W[5])
    fill(conn, W[5], TOKEN, "buy", 10, 1000, now - 10, "direct")
    classify(conn, since=0)
    score.score_all(conn)
    api._responses.clear()
    with TestClient(api.app) as client:
        data = client.get("/api/hot").json()
        assert len(data["now"]) == 1
        burst = data["now"][0]
        assert burst["mint"] == TOKEN and burst["wallets"] == 5
        assert "test5" not in burst["who"]
        assert all(s >= 80 for s in burst["scores"])
        assert client.get("/api/leaderboard?status=all").json()["count"] == 6
    assert conn.execute("SELECT COUNT(*) FROM bursts").fetchone()[0] == 0


def test_quiet_market_is_healthy_but_stale_watcher_is_not(conn):
    now = db.now()
    history(conn, W[0])
    score.score_all(conn)
    with db.tx(conn):
        run = db.run_start(conn, "track")
        db.run_finish(conn, run, {})
    state.put(conn, "watcher", "ok", {"head": 100, "fills": 0}, success=True, now=now)
    assert system_status.snapshot(conn, now)["state"] == "ready"
    assert system_status.snapshot(conn, now + settings.pipeline_stale_s + 1)["state"] == "degraded"
    state.put(conn, "watcher", "error", {"error": "rate limited"}, now=now + 1)
    assert state.get(conn, "watcher")["last_success"] == now
    assert not system_status.snapshot(conn, now + 1)["checks"][0]["ok"]


class HistoryChain:
    def __init__(self, fail=False):
        self.requests = 0
        self.scans = []
        self.fail = fail
    def load_decimals(self, values): pass
    def known_decimals(self): return {}
    def block_number(self): return 300
    def block_timestamp(self, number): return db.now() - (300 - number) * 1000
    def windows(self, since, head, span):
        for last in range(head, 0, -100):
            yield max(1, last - 99), last
    def scan(self, wallets, first, last):
        self.requests += 2
        self.scans.append((first, last))
        if self.fail:
            raise RuntimeError("temporary source failure")
        return {}


def test_history_checkpoint_resumes_instead_of_repeating_latest_range(conn):
    first, second = HistoryChain(), HistoryChain()
    backfill.backfill(conn, days=7, rpc=first, max_requests=2, resume=True)
    backfill.backfill(conn, days=7, rpc=second, max_requests=2, resume=True)
    assert first.scans == [(201, 300)]
    assert second.scans == [(101, 200)]
    assert state.get(conn, "backfill")["details"]["next_block"] == 100
    third = HistoryChain()
    backfill.backfill(conn, days=7, rpc=third, max_requests=2, resume=True)
    assert state.get(conn, "backfill")["details"]["complete"]
    assert backfill.backfill(conn, days=7, rpc=HistoryChain(), resume=True)["skipped"]


def test_failed_history_range_does_not_advance_checkpoint(conn):
    backfill.backfill(conn, days=7, rpc=HistoryChain(), max_requests=2, resume=True)
    failing = HistoryChain(fail=True)
    backfill.backfill(conn, days=7, rpc=failing, max_requests=2, resume=True)
    saved = state.get(conn, "backfill")
    assert saved["status"] == "error" and saved["details"]["next_block"] == 200


def test_ai_path_receives_valid_complete_context(monkeypatch):
    import anthropic
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            content=[SimpleNamespace(type="text", text=json.dumps({
                "score": 50, "status": "watch", "summary": "Insufficient evidence.",
                "confidence": 0.2,
            }))],
        )
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    ctx = {"large": "x" * 5000, "open_positions": [{"cost_usd": 123, "pnl_usd": -10}]}
    result, _ = score.call_claude(ctx, "test-model")
    assert result.status == "watch"
    assert json.loads(captured["messages"][0]["content"]) == ctx
