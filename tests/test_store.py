import sqlite3
import threading

import pytest

from sdwan.probe import Sample
from sdwan.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_schema_created_on_init(tmp_path):
    path = tmp_path / "t.db"
    Store(str(path)).close()
    con = sqlite3.connect(path)
    names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert {"samples", "events", "traffic", "path_state"} <= names


def test_sample_round_trip_keeps_null_throughput(store):
    store.write_sample("hq", Sample(ts=100.0, path="wan_a", rtt_ms=1.5, jitter_ms=0.2, loss_pct=0.0))
    rows = store.latest_samples()
    assert len(rows) == 1
    assert (rows[0]["site"], rows[0]["path"]) == ("hq", "wan_a")
    assert rows[0]["rtt_ms"] == 1.5
    assert rows[0]["throughput_mbps"] is None
    assert store.sample_count() == 1


def test_latest_samples_returns_newest_row_per_path(store):
    store.write_sample("hq", Sample(100.0, "wan_a", 1.0, 0.1, 0.0))
    store.write_sample("hq", Sample(101.0, "wan_a", 2.0, 0.1, 0.0))
    store.write_sample("hq", Sample(101.0, "wan_b", 3.0, 0.1, 0.0))
    rows = {(r["site"], r["path"]): r for r in store.latest_samples()}
    assert rows[("hq", "wan_a")]["rtt_ms"] == 2.0
    assert rows[("hq", "wan_b")]["rtt_ms"] == 3.0


def test_last_throughput_skips_null_rows(store):
    store.write_sample("hq", Sample(100.0, "wan_a", 1.0, 0.1, 0.0, throughput_mbps=800.0))
    store.write_sample("hq", Sample(101.0, "wan_a", 1.0, 0.1, 0.0))
    assert store.last_throughput() == {("hq", "wan_a"): 800.0}


def test_upsert_path_state_keeps_one_row_with_newest_status(store):
    store.upsert_path_state("hq", "wan_a", "HEALTHY", True, "", 100.0)
    store.upsert_path_state("hq", "wan_a", "DEGRADED", False, "loss 40% > 10%", 105.0)
    states = store.path_states()
    assert len(states) == 1
    assert states[0]["status"] == "DEGRADED"
    assert states[0]["active"] == 0
    assert states[0]["reason"] == "loss 40% > 10%"


def test_recent_events_newest_first_and_limited(store):
    for i in range(12):
        store.write_event(100.0 + i, "hq", "DEGRADED", path="wan_a", detail=str(i))
    events = store.recent_events(10)
    assert len(events) == 10
    assert events[0]["detail"] == "11"
    assert events[-1]["detail"] == "2"


def test_traffic_summary_over_window(store):
    store.write_traffic(90.0, "hq", ok=10, failed=0, avg_latency_ms=1.0)  # outside window
    store.write_traffic(95.0, "hq", ok=8, failed=2, avg_latency_ms=2.0)
    store.write_traffic(100.0, "hq", ok=6, failed=4, avg_latency_ms=4.0)
    s = store.traffic_summary(seconds=10, now=101.0)
    assert (s["ok"], s["failed"]) == (14, 6)
    assert s["success_pct"] == pytest.approx(70.0)
    assert s["avg_latency_ms"] == pytest.approx(3.0)


def test_traffic_summary_empty(store):
    s = store.traffic_summary(seconds=10, now=101.0)
    assert (s["ok"], s["failed"]) == (0, 0)
    assert s["success_pct"] is None
    assert s["avg_latency_ms"] is None


def test_writes_from_multiple_threads(store):
    def work(site):
        for i in range(50):
            store.write_sample(site, Sample(float(i), "wan_a", 1.0, 0.0, 0.0))

    threads = [threading.Thread(target=work, args=(s,)) for s in ("hq", "branch")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.sample_count() == 100
