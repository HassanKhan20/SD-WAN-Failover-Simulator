from pathlib import Path

import pytest

from sdwan.agent import Agent
from sdwan.config import load_topology
from sdwan.net import FakeNet, NetError
from sdwan.probe import Sample
from sdwan.store import Store

TOPO = load_topology(Path(__file__).resolve().parents[1] / "config" / "topology.toml")
IFACES = {"10.100.0.11": "eth1", "10.200.0.11": "eth2"}
REMOTE = "10.2.0.0/24"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def clean(now, path):
    return Sample(now, path, 1.0, 0.1, 0.0)


def lossy(now, path):
    return Sample(now, path, 1.0, 0.1, 40.0)


class Script:
    """Sampler whose wan_a health can be flipped mid-test."""

    def __init__(self):
        self.bad = False

    def __call__(self, now):
        mk = lossy if self.bad else clean
        return {"wan_a": mk(now, "wan_a"), "wan_b": clean(now, "wan_b")}


def kinds(store):
    return [e["kind"] for e in store.recent_events(20)]


def test_start_route_sets_preferred_and_logs_startup(store):
    net = FakeNet(IFACES)
    agent = Agent(TOPO, "hq", net, store, sampler=lambda now: {})
    agent.start_route()
    assert net.routes == {REMOTE: ("10.100.0.12", "eth1")}
    assert store.recent_events(1)[0]["kind"] == "STARTUP"
    states = {s["path"]: s for s in store.path_states()}
    assert states["wan_a"]["active"] == 1
    assert states["wan_b"]["active"] == 0
    assert states["wan_a"]["status"] == "UNKNOWN"


def test_tick_skips_paths_without_a_sample(store):
    net = FakeNet(IFACES)
    agent = Agent(TOPO, "hq", net, store, sampler=lambda now: {})
    agent.start_route()
    agent.tick(100.0)
    assert store.sample_count() == 0
    assert all(s["status"] == "UNKNOWN" for s in store.path_states())


def test_lossy_wan_a_triggers_failover_to_wan_b(store):
    net = FakeNet(IFACES)
    script = Script()
    agent = Agent(TOPO, "hq", net, store, sampler=script)
    agent.start_route()
    for t in range(10):
        agent.tick(100.0 + t)
    assert "FAILOVER" not in kinds(store)
    script.bad = True
    for t in range(10, 14):
        agent.tick(100.0 + t)
    assert "DEGRADED" in kinds(store)
    assert net.routes[REMOTE] == ("10.200.0.12", "eth2")
    failover = next(e for e in store.recent_events(20) if e["kind"] == "FAILOVER")
    assert failover["path"] == "wan_b"
    assert failover["detail"] == "wan_a -> wan_b: loss 40.0% > 10.0%"
    states = {s["path"]: s for s in store.path_states()}
    assert states["wan_b"]["active"] == 1
    assert states["wan_a"]["active"] == 0
    assert states["wan_a"]["status"] == "DEGRADED"
    assert "loss" in states["wan_a"]["reason"]
    assert store.sample_count() == 28


def test_recovery_logs_recovered_and_fails_back_after_hold(store):
    net = FakeNet(IFACES)
    script = Script()
    agent = Agent(TOPO, "hq", net, store, sampler=script)
    agent.start_route()
    for t in range(10):
        agent.tick(100.0 + t)
    script.bad = True
    for t in range(10, 14):
        agent.tick(100.0 + t)
    script.bad = False
    for t in range(14, 60):
        agent.tick(100.0 + t)
    assert "RECOVERED" in kinds(store)
    assert "FAILBACK" in kinds(store)
    assert net.routes[REMOTE] == ("10.100.0.12", "eth1")


def test_set_route_failure_becomes_error_event_and_retries(store):
    net = FakeNet(IFACES)
    script = Script()
    script.bad = True
    agent = Agent(TOPO, "hq", net, store, sampler=script, log=lambda s: None)
    agent.start_route()
    net.fail_next = NetError("RTNETLINK answers: Network unreachable")
    for t in range(3):
        agent.tick(100.0 + t)
    assert "ERROR" in kinds(store)
    assert "FAILOVER" not in kinds(store)
    assert net.routes[REMOTE] == ("10.100.0.12", "eth1")
    agent.tick(103.0)
    assert "FAILOVER" in kinds(store)
    assert net.routes[REMOTE] == ("10.200.0.12", "eth2")


def test_all_degraded_is_logged_once(store):
    net = FakeNet(IFACES)
    agent = Agent(
        TOPO, "hq", net, store, sampler=lambda now: {"wan_a": lossy(now, "wan_a"), "wan_b": lossy(now, "wan_b")}
    )
    agent.start_route()
    for t in range(8):
        agent.tick(100.0 + t)
    assert kinds(store).count("ALL_DEGRADED") == 1
    assert net.routes[REMOTE] == ("10.100.0.12", "eth1")
