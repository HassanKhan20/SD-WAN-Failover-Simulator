from pathlib import Path

import pytest

from sdwan.config import load_topology

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "topology.toml"


@pytest.fixture(scope="module")
def topo():
    return load_topology(CONFIG)


def test_path_addresses_are_mirrored_between_sites(topo):
    assert topo.sites["hq"].paths["wan_a"].peer_ip == "10.100.0.12"
    assert topo.sites["branch"].paths["wan_a"].local_ip == "10.100.0.12"


def test_path_name_is_set_from_table_key(topo):
    assert topo.sites["hq"].paths["wan_b"].name == "wan_b"


def test_remote_lan_follows_peer(topo):
    assert topo.remote_lan("hq") == "10.2.0.0/24"
    assert topo.remote_lan("branch") == "10.1.0.0/24"


def test_tunables_are_loaded(topo):
    assert topo.failover.preferred == "wan_a"
    assert topo.failover.min_dwell_s == 5
    assert topo.detector.window == 5
    assert topo.detector.throughput_drop_ratio == 0.25
    assert topo.probe.interval_ms == 250
    assert topo.probe.throughput_bytes == 262144


def test_unknown_site_raises_key_error(topo):
    with pytest.raises(KeyError):
        topo.site("mars")
