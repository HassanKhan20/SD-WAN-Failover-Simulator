from pathlib import Path

import pytest

from sdwan.chaos import degrade, restore
from sdwan.config import load_topology
from sdwan.net import FakeNet
from sdwan.store import Store

TOPO = load_topology(Path(__file__).resolve().parents[1] / "config" / "topology.toml")
IFACES = {"10.100.0.11": "eth1", "10.200.0.11": "eth2"}


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_degrade_applies_netem_on_interface_holding_local_ip(store):
    net = FakeNet(IFACES)
    degrade(TOPO, "hq", "wan_a", net, store, ts=100.0, loss=30, delay=150, jitter=40, rate="1mbit")
    assert net.netem == {"eth1": ["loss", "30%", "delay", "150ms", "40ms", "rate", "1mbit"]}
    ev = store.recent_events(1)[0]
    assert (ev["kind"], ev["site"], ev["path"]) == ("CHAOS", "hq", "wan_a")
    assert ev["detail"] == "degrade: loss 30% delay 150ms 40ms rate 1mbit"


def test_degrade_on_branch_uses_branch_addresses(store):
    net = FakeNet({"10.100.0.12": "eth3"})
    degrade(TOPO, "branch", "wan_a", net, store, ts=100.0, loss=10)
    assert net.netem == {"eth3": ["loss", "10%"]}


def test_restore_clears_netem_and_logs(store):
    net = FakeNet(IFACES)
    net.netem["eth1"] = ["loss", "30%"]
    restore(TOPO, "hq", "wan_a", net, store, ts=101.0)
    assert net.netem == {}
    ev = store.recent_events(1)[0]
    assert (ev["kind"], ev["path"], ev["detail"]) == ("CHAOS", "wan_a", "restore")


def test_restore_when_nothing_applied_still_logs(store):
    net = FakeNet(IFACES)
    restore(TOPO, "hq", "wan_a", net, store, ts=101.0)
    assert store.recent_events(1)[0]["detail"] == "restore"


def test_unknown_path_raises_key_error(store):
    with pytest.raises(KeyError):
        degrade(TOPO, "hq", "wan_z", FakeNet(IFACES), store, ts=100.0, loss=10)


def test_degrade_without_options_raises_value_error(store):
    net = FakeNet(IFACES)
    with pytest.raises(ValueError):
        degrade(TOPO, "hq", "wan_a", net, store, ts=100.0)
    assert store.recent_events(1) == []
