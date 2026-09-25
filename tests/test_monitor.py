import io
import json

import pytest
from rich.console import Console

from sdwan.monitor import build_layout, print_status, status_dict
from sdwan.probe import Sample
from sdwan.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def seed(store):
    store.upsert_path_state("hq", "wan_a", "HEALTHY", True, "", 100.0)
    store.upsert_path_state("hq", "wan_b", "DEGRADED", False, "loss 40.0% > 10.0%", 100.0)
    store.write_sample("hq", Sample(100.0, "wan_a", 1.2, 0.3, 0.0, throughput_mbps=812.5))
    store.write_sample("hq", Sample(100.0, "wan_b", None, None, 100.0))
    store.write_event(100.0, "hq", "FAILOVER", path="wan_b", detail="wan_a -> wan_b: loss 40.0% > 10.0%")
    store.write_traffic(99.5, "hq", ok=9, failed=1, avg_latency_ms=2.5)


def render(renderable):
    console = Console(record=True, width=140, file=io.StringIO(), force_terminal=False)
    console.print(renderable)
    return console.export_text()


def test_status_dict_has_paths_active_and_events(store):
    seed(store)
    d = status_dict(store)
    assert {p["path"] for p in d["paths"]} == {"wan_a", "wan_b"}
    assert d["active"] == {"hq": "wan_a"}
    assert d["events"][0]["kind"] == "FAILOVER"
    assert d["samples"] == 2


def test_layout_renders_states_metrics_and_events(store):
    seed(store)
    text = render(build_layout(store))
    for needle in ("HEALTHY", "DEGRADED", "FAILOVER", "wan_a", "812.5", "1.2", "loss 40.0%"):
        assert needle in text


def test_layout_with_empty_db_says_waiting(store):
    assert "waiting for data" in render(build_layout(store))


def test_print_status_on_missing_db_prints_waiting(tmp_path, capsys):
    print_status(str(tmp_path / "missing.db"), as_json=False)
    assert "waiting for data" in capsys.readouterr().out
    assert not (tmp_path / "missing.db").exists()


def test_print_status_json(store, capsys):
    seed(store)
    print_status(store.path, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert out["active"] == {"hq": "wan_a"}
    assert out["paths"][0]["status"] == "HEALTHY"


def test_print_status_json_on_missing_db_is_valid_json(tmp_path, capsys):
    print_status(str(tmp_path / "missing.db"), as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert out == {"paths": [], "active": {}, "events": [], "samples": 0}
