import json
import subprocess

import pytest

from sdwan.net import FakeNet, IpRouteNet, NetError, iface_from_addr_json, netem_args

ADDR_JSON = json.dumps(
    [
        {"ifindex": 1, "ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifindex": 2, "ifname": "eth0", "addr_info": [{"family": "inet", "local": "10.1.0.2"}]},
        {
            "ifindex": 3,
            "ifname": "eth1",
            "addr_info": [
                {"family": "inet6", "local": "fe80::1"},
                {"family": "inet", "local": "10.100.0.11"},
            ],
        },
    ]
)


class Runner:
    """Stands in for subprocess.run: records commands, returns canned results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, cmd, capture_output=True, text=True, check=False):
        self.calls.append(list(cmd))
        rc, out, err = self.results.get(tuple(cmd), (0, "", ""))
        return subprocess.CompletedProcess(cmd, rc, out, err)


# -- pure helpers -----------------------------------------------------------


def test_iface_from_addr_json_finds_interface_holding_ip():
    assert iface_from_addr_json(ADDR_JSON, "10.100.0.11") == "eth1"
    assert iface_from_addr_json(ADDR_JSON, "10.1.0.2") == "eth0"


def test_iface_from_addr_json_raises_when_absent():
    with pytest.raises(LookupError):
        iface_from_addr_json(ADDR_JSON, "10.200.0.11")


def test_netem_args_full():
    assert netem_args(loss=30, delay_ms=150, jitter_ms=40, rate="1mbit") == [
        "loss", "30%", "delay", "150ms", "40ms", "rate", "1mbit",
    ]


def test_netem_args_delay_only():
    assert netem_args(delay_ms=100) == ["delay", "100ms"]


def test_netem_args_jitter_requires_delay():
    with pytest.raises(ValueError):
        netem_args(jitter_ms=40)


def test_netem_args_empty_raises():
    with pytest.raises(ValueError):
        netem_args()


# -- IpRouteNet -------------------------------------------------------------


def test_set_route_runs_ip_route_replace():
    r = Runner()
    IpRouteNet(runner=r).set_route("10.2.0.0/24", via="10.100.0.12", dev="eth1")
    assert r.calls == [["ip", "route", "replace", "10.2.0.0/24", "via", "10.100.0.12", "dev", "eth1"]]


def test_iface_for_ip_uses_ip_json_output():
    r = Runner({("ip", "-j", "addr", "show"): (0, ADDR_JSON, "")})
    assert IpRouteNet(runner=r).iface_for_ip("10.100.0.11") == "eth1"


def test_apply_netem_uses_qdisc_replace():
    r = Runner()
    IpRouteNet(runner=r).apply_netem("eth1", loss=30)
    assert r.calls == [["tc", "qdisc", "replace", "dev", "eth1", "root", "netem", "loss", "30%"]]


def test_clear_netem_ignores_missing_qdisc():
    cmd = ("tc", "qdisc", "del", "dev", "eth1", "root")
    r = Runner({cmd: (2, "", "Error: Cannot delete qdisc with handle of zero.")})
    IpRouteNet(runner=r).clear_netem("eth1")
    assert r.calls == [list(cmd)]


def test_clear_netem_reraises_other_errors():
    cmd = ("tc", "qdisc", "del", "dev", "eth1", "root")
    r = Runner({cmd: (1, "", 'Cannot find device "eth1"')})
    with pytest.raises(NetError, match="Cannot find device"):
        IpRouteNet(runner=r).clear_netem("eth1")


def test_failed_command_raises_net_error_with_stderr():
    cmd = ("ip", "route", "replace", "10.2.0.0/24", "via", "x", "dev", "eth1")
    r = Runner({cmd: (2, "", "Error: any valid prefix is expected rather than \"x\".")})
    with pytest.raises(NetError, match="valid prefix"):
        IpRouteNet(runner=r).set_route("10.2.0.0/24", via="x", dev="eth1")


def test_current_route_parses_gateway():
    cmd = ("ip", "-j", "route", "show", "10.2.0.0/24")
    out = json.dumps([{"dst": "10.2.0.0/24", "gateway": "10.100.0.12", "dev": "eth1"}])
    r = Runner({cmd: (0, out, "")})
    assert IpRouteNet(runner=r).current_route("10.2.0.0/24") == "10.100.0.12"


def test_current_route_none_when_absent():
    cmd = ("ip", "-j", "route", "show", "10.2.0.0/24")
    r = Runner({cmd: (0, "[]", "")})
    assert IpRouteNet(runner=r).current_route("10.2.0.0/24") is None


# -- FakeNet ----------------------------------------------------------------


def test_fake_net_records_calls_and_tracks_state():
    net = FakeNet(ifaces={"10.100.0.11": "eth1", "10.200.0.11": "eth2"})
    assert net.iface_for_ip("10.100.0.11") == "eth1"
    with pytest.raises(LookupError):
        net.iface_for_ip("1.2.3.4")
    net.set_route("10.2.0.0/24", via="10.100.0.12", dev="eth1")
    assert net.current_route("10.2.0.0/24") == "10.100.0.12"
    net.apply_netem("eth1", loss=30)
    assert net.netem == {"eth1": ["loss", "30%"]}
    net.clear_netem("eth1")
    assert net.netem == {}
    assert net.calls == [
        ("set_route", "10.2.0.0/24", "10.100.0.12", "eth1"),
        ("apply_netem", "eth1", ["loss", "30%"]),
        ("clear_netem", "eth1"),
    ]


def test_fake_net_can_fail_next_call():
    net = FakeNet(ifaces={})
    net.fail_next = NetError("boom")
    with pytest.raises(NetError):
        net.set_route("10.2.0.0/24", via="10.100.0.12", dev="eth1")
    net.set_route("10.2.0.0/24", via="10.100.0.12", dev="eth1")  # only fails once
