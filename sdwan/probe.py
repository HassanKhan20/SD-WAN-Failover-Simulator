"""Path probing: UDP echo round trips, window summaries and throughput bursts.

The pure part (`ProbeRecord`, `Sample`, `summarize_window`) has no I/O and is
unit tested directly. The runtime part (responder, prober, throughput) runs in
threads inside the edge agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class ProbeRecord:
    """One probe datagram: when it was sent and, if it came back, when."""

    seq: int
    send_ns: int
    recv_ns: int | None = None

    @property
    def rtt_ms(self) -> float | None:
        if self.recv_ns is None:
            return None
        return (self.recv_ns - self.send_ns) / 1_000_000


@dataclass(frozen=True)
class Sample:
    """One second of telemetry for one path, as stored in the samples table."""

    ts: float
    path: str
    rtt_ms: float | None
    jitter_ms: float | None
    loss_pct: float
    throughput_mbps: float | None = None


def summarize_window(
    records: Iterable[ProbeRecord], start_ns: int, end_ns: int
) -> tuple[float | None, float | None, float] | None:
    """Summarize probes sent in the half-open window (start_ns, end_ns].

    Returns (rtt_ms, jitter_ms, loss_pct), or None if nothing was sent in the
    window. rtt and jitter are None when nothing came back. Jitter is the mean
    absolute difference between consecutive RTTs, in send order.
    """
    sent = sorted(
        (r for r in records if start_ns < r.send_ns <= end_ns), key=lambda r: r.send_ns
    )
    if not sent:
        return None
    rtts = [r.rtt_ms for r in sent if r.recv_ns is not None]
    loss_pct = (len(sent) - len(rtts)) / len(sent) * 100.0
    if not rtts:
        return None, None, loss_pct
    rtt_ms = sum(rtts) / len(rtts)
    if len(rtts) < 2:
        jitter_ms = 0.0
    else:
        diffs = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
        jitter_ms = sum(diffs) / len(diffs)
    return rtt_ms, jitter_ms, loss_pct
