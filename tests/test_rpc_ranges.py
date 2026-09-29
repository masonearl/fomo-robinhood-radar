"""Data-only regressions: complete ranges, failed checkpoints, and quiet collection."""
import json

import pytest

from fomo_agent import cli, db
from fomo_agent.config import settings
from fomo_agent.pipeline import system_status, track
from fomo_agent.sources.rpc import MAX_LOG_BLOCKS, TRANSFER_TOPIC, RobinhoodRPC, RpcError, topic_for

WALLETS = ["0x" + c * 40 for c in "12"]


@pytest.mark.parametrize("count", [1, 29_999, 30_000, 30_001, 60_000, 200_001])
@pytest.mark.parametrize("outgoing", [False, True])
def test_log_slices_cover_exact_original_range(monkeypatch, count, outgoing):
    first, seen = 123, []
    last = first + count - 1

    def call(method, params):
        assert method == "eth_getLogs"
        q = params[0]
        lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        assert 1 <= hi - lo + 1 <= 30_000
        assert q["topics"][1 if outgoing else 2] == [topic_for(w) for w in WALLETS]
        assert q["topics"][2 if outgoing else 1] is None
        seen.append((lo, hi))
        # Boundary events prove the end of one slice and start of the next are both retained.
        return [{"address": "0x" + "a" * 40, "topics": [TRANSFER_TOPIC, topic_for(WALLETS[0]), topic_for(WALLETS[1])],
                 "data": "0x1", "transactionHash": hex(n), "logIndex": "0x0", "blockNumber": hex(n)}
                for n in sorted({lo, hi})]

    rpc = RobinhoodRPC(url="http://offline")
    monkeypatch.setattr(rpc, "call", call)
    result = rpc.transfers(WALLETS, first, last, outgoing=outgoing)
    assert seen[0][0] == first and seen[-1][1] == last
    assert sum(hi - lo + 1 for lo, hi in seen) == count
    assert all(left[1] + 1 == right[0] for left, right in zip(seen, seen[1:]))
    assert [t["block"] for t in result] == sorted({n for pair in seen for n in pair})
    assert len(result) == len({(t["tx"], t["index"]) for t in result})


def test_failed_slice_does_not_store_partial_range_or_advance_wallet(tmp_path, monkeypatch):
    rpc = RobinhoodRPC(url="http://offline")
    rpc.prime(WALLETS)
    monkeypatch.setattr(settings, "rpc_window_blocks", 200_000)
    monkeypatch.setattr(rpc, "block_number", lambda: 300_000)
    calls = []

    def call(method, params):
        assert method == "eth_getLogs"
        calls.append(params[0])
        if len(calls) == 2:
            raise RpcError("source rejected the second slice")
        return [{"address": "0x" + "a" * 40, "topics": [TRANSFER_TOPIC, topic_for(WALLETS[0]), topic_for(WALLETS[1])],
                 "data": "0x1", "transactionHash": "0x10", "logIndex": "0x0", "blockNumber": "0x186a0"}]

    monkeypatch.setattr(rpc, "call", call)
    conn = db.connect(tmp_path / "partial.db")
    with db.tx(conn):
        db.upsert_trader(conn, WALLETS[0], chain="robinhood", status="tracking")
    with pytest.raises(RpcError, match="second slice"):
        track.track_wallet(conn, rpc, WALLETS[0], "robinhood")
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert db.get_trader(conn, WALLETS[0])["last_tracked_ts"] is None
    assert rpc._fetched_at == 0 and rpc._fills == {}
    conn.close()


def test_successful_empty_scan_is_cached_for_the_whole_roster(monkeypatch):
    rpc = RobinhoodRPC(url="http://offline")
    rpc.prime(WALLETS)
    calls = []
    monkeypatch.setattr(rpc, "block_number", lambda: 300_000)
    monkeypatch.setattr(rpc, "scan", lambda *args: calls.append(args) or {})
    for wallet in WALLETS:
        assert rpc.get_trades(wallet) == []
    assert len(calls) == 1
    assert calls[0][1:] == (300_000 - settings.rpc_window_blocks, 300_000)


@pytest.mark.parametrize("failing", [0, 1, 2])
def test_tracking_errors_are_saved_without_success_freshness(tmp_path, monkeypatch, failing):
    monkeypatch.setattr(settings, "db_path", tmp_path / "tracking.db")
    conn = db.connect()
    with db.tx(conn):
        for w in WALLETS:
            db.upsert_trader(conn, w, chain="robinhood", status="tracking")

    class Tracker:
        def supports(self, chain): return chain == "robinhood"
        def get_trades(self, address, chain, since_ts=None):
            if address in WALLETS[:failing]:
                raise RpcError("synthetic source failure")
            return []

    stats = cli._run("track", track.track_all, [Tracker()])
    row = conn.execute("SELECT * FROM runs WHERE kind='track'").fetchone()
    assert json.loads(row["stats_json"]) == stats
    assert stats["errors"] == failing and stats["wallets"] == len(WALLETS) - failing
    assert (row["error"] is not None) == bool(failing)
    assert system_status.snapshot(conn)["checks"][1]["ok"] == (failing == 0)
    conn.close()
