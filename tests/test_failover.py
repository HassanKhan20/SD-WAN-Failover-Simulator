from sdwan.config import FailoverConfig
from sdwan.detector import PathHealth, Status
from sdwan.failover import DecisionKind, FailoverState, decide, initial_state

CFG = FailoverConfig(preferred="wan_a", min_dwell_s=5, failback_hold_s=15)


def H(status, reason=""):
    return PathHealth(status, reason, changed=False)


HEALTHY = H(Status.HEALTHY)
DEGRADED = H(Status.DEGRADED, "loss 40.0% > 10.0%")


def test_initial_state_is_on_preferred_path():
    assert initial_state(CFG).active == "wan_a"


def test_no_decision_when_all_healthy_on_preferred():
    s, d = decide(initial_state(CFG), {"wan_a": HEALTHY, "wan_b": HEALTHY}, 100.0, CFG)
    assert d is None
    assert s.active == "wan_a"


def test_failover_when_active_degraded_and_other_healthy():
    s, d = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": HEALTHY}, 100.0, CFG)
    assert d.kind is DecisionKind.FAILOVER
    assert (d.from_path, d.to_path) == ("wan_a", "wan_b")
    assert d.reason == "loss 40.0% > 10.0%"
    assert s.active == "wan_b"
    assert s.last_switch_ts == 100.0


def test_no_switch_inside_min_dwell():
    s, _ = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": HEALTHY}, 100.0, CFG)
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": DEGRADED}, 103.0, CFG)
    assert d is None
    assert s.active == "wan_b"
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": DEGRADED}, 105.0, CFG)
    assert d.kind is DecisionKind.FAILOVER
    assert d.to_path == "wan_a"


def test_failback_only_after_hold():
    s, _ = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": HEALTHY}, 100.0, CFG)
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 110.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 124.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 125.0, CFG)
    assert d.kind is DecisionKind.FAILBACK
    assert (d.from_path, d.to_path) == ("wan_b", "wan_a")
    assert s.active == "wan_a"
    assert s.last_switch_ts == 125.0


def test_preferred_flapping_resets_the_hold():
    s, _ = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": HEALTHY}, 100.0, CFG)
    s, _ = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 110.0, CFG)
    s, _ = decide(s, {"wan_a": DEGRADED, "wan_b": HEALTHY}, 120.0, CFG)
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 126.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 140.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": HEALTHY}, 141.0, CFG)
    assert d.kind is DecisionKind.FAILBACK


def test_all_degraded_logged_once_then_failover_when_one_recovers():
    s, d = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": DEGRADED}, 100.0, CFG)
    assert d.kind is DecisionKind.ALL_DEGRADED
    assert s.active == "wan_a"
    s, d = decide(s, {"wan_a": DEGRADED, "wan_b": DEGRADED}, 101.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": DEGRADED, "wan_b": HEALTHY}, 102.0, CFG)
    assert d.kind is DecisionKind.FAILOVER
    assert d.to_path == "wan_b"


def test_all_degraded_guard_resets_after_active_healthy():
    s, _ = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": DEGRADED}, 100.0, CFG)
    s, d = decide(s, {"wan_a": HEALTHY, "wan_b": DEGRADED}, 101.0, CFG)
    assert d is None
    s, d = decide(s, {"wan_a": DEGRADED, "wan_b": DEGRADED}, 102.0, CFG)
    assert d.kind is DecisionKind.ALL_DEGRADED


def test_preferred_chosen_over_other_candidates():
    state = FailoverState(active="wan_c")
    _, d = decide(state, {"wan_a": HEALTHY, "wan_b": HEALTHY, "wan_c": DEGRADED}, 100.0, CFG)
    assert d.to_path == "wan_a"


def test_first_candidate_by_name_when_preferred_is_degraded():
    state = FailoverState(active="wan_c")
    healths = {"wan_a": DEGRADED, "wan_b": HEALTHY, "wan_c": DEGRADED, "wan_d": HEALTHY}
    _, d = decide(state, healths, 100.0, CFG)
    assert d.to_path == "wan_b"


def test_unknown_status_is_not_a_candidate():
    _, d = decide(initial_state(CFG), {"wan_a": DEGRADED, "wan_b": H(Status.UNKNOWN)}, 100.0, CFG)
    assert d.kind is DecisionKind.ALL_DEGRADED
