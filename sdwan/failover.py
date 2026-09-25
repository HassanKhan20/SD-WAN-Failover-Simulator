"""Path selection state machine: fail over off a degraded active path, fail
back to the preferred path after it has stayed healthy for a hold-down, and
never switch twice inside the dwell time.

Pure: the agent passes the clock in and applies the returned Decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from sdwan.config import FailoverConfig
from sdwan.detector import PathHealth, Status


class DecisionKind(str, Enum):
    FAILOVER = "FAILOVER"
    FAILBACK = "FAILBACK"
    ALL_DEGRADED = "ALL_DEGRADED"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    from_path: str | None
    to_path: str | None
    reason: str


@dataclass(frozen=True)
class FailoverState:
    active: str
    last_switch_ts: float = float("-inf")
    preferred_healthy_since: float | None = None
    all_degraded_logged: bool = False


def initial_state(cfg: FailoverConfig) -> FailoverState:
    return FailoverState(active=cfg.preferred)


def _is(healths: dict[str, PathHealth], path: str, status: Status) -> bool:
    h = healths.get(path)
    return h is not None and h.status is status


def decide(
    state: FailoverState, healths: dict[str, PathHealth], now: float, cfg: FailoverConfig
) -> tuple[FailoverState, Decision | None]:
    # 1. Track how long the preferred path has been continuously healthy.
    if _is(healths, cfg.preferred, Status.HEALTHY):
        since = now if state.preferred_healthy_since is None else state.preferred_healthy_since
    else:
        since = None
    active_degraded = _is(healths, state.active, Status.DEGRADED)
    state = replace(
        state,
        preferred_healthy_since=since,
        all_degraded_logged=state.all_degraded_logged and active_degraded,
    )

    # 2. Dwell: never switch twice in quick succession.
    if now - state.last_switch_ts < cfg.min_dwell_s:
        return state, None

    # 3. Active path is degraded: move to a healthy one if there is one.
    if active_degraded:
        candidates = sorted(
            p for p, h in healths.items() if p != state.active and h.status is Status.HEALTHY
        )
        if candidates:
            to = cfg.preferred if cfg.preferred in candidates else candidates[0]
            decision = Decision(
                DecisionKind.FAILOVER, state.active, to, healths[state.active].reason
            )
            return (
                replace(state, active=to, last_switch_ts=now, all_degraded_logged=False),
                decision,
            )
        if not state.all_degraded_logged:
            decision = Decision(
                DecisionKind.ALL_DEGRADED,
                state.active,
                None,
                f"no healthy path available; staying on {state.active}",
            )
            return replace(state, all_degraded_logged=True), decision
        return state, None

    # 4. Fail back once the preferred path has been healthy long enough.
    if (
        state.active != cfg.preferred
        and since is not None
        and now - since >= cfg.failback_hold_s
    ):
        decision = Decision(
            DecisionKind.FAILBACK,
            state.active,
            cfg.preferred,
            f"{cfg.preferred} healthy for {now - since:.0f}s",
        )
        return replace(state, active=cfg.preferred, last_switch_ts=now), decision

    return state, None
