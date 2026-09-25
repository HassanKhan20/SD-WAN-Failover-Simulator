"""Per-path health detection: absolute thresholds plus deviation from an
adaptive (EWMA) baseline, with hysteresis so a path does not flap.

Pure Python: no I/O, no clock. The agent feeds it one Sample per second.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

from sdwan.config import DetectorConfig
from sdwan.probe import Sample


class Status(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class PathHealth:
    status: Status
    reason: str
    changed: bool


class _Ewma:
    """Exponentially weighted mean and variance."""

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.mean: float | None = None
        self.var = 0.0

    def update(self, x: float) -> None:
        if self.mean is None:
            self.mean = x
            self.var = 0.0
            return
        d = x - self.mean
        self.mean += self.alpha * d
        self.var = (1 - self.alpha) * (self.var + self.alpha * d * d)

    @property
    def std(self) -> float:
        return math.sqrt(self.var)


class PathMonitor:
    def __init__(self, cfg: DetectorConfig):
        self.cfg = cfg
        self.rtt = _Ewma(cfg.ewma_alpha)
        self.jitter = _Ewma(cfg.ewma_alpha)
        self.throughput = _Ewma(cfg.ewma_alpha)
        self.samples_seen = 0
        self.verdicts: deque[tuple[bool, list[str]]] = deque(maxlen=cfg.window)
        self.consecutive_clean = 0
        self.status = Status.UNKNOWN
        self.reason = ""

    @property
    def baseline_rtt_ms(self) -> float | None:
        return self.rtt.mean

    @property
    def baseline_throughput_mbps(self) -> float | None:
        return self.throughput.mean

    def _judge(self, s: Sample) -> list[str]:
        cfg = self.cfg
        reasons: list[str] = []
        if s.loss_pct > cfg.loss_pct_max:
            reasons.append(f"loss {s.loss_pct:.1f}% > {cfg.loss_pct_max:.1f}%")
        if s.rtt_ms is None:
            reasons.append("no replies")
        elif s.rtt_ms > cfg.rtt_ms_max:
            reasons.append(f"rtt {s.rtt_ms:.1f}ms > {cfg.rtt_ms_max:.1f}ms")
        elif self.samples_seen >= cfg.warmup_samples and self.rtt.mean is not None:
            std = max(self.rtt.std, cfg.min_std_ms)
            z = (s.rtt_ms - self.rtt.mean) / std
            if z > cfg.zscore_max:
                reasons.append(
                    f"rtt {s.rtt_ms:.1f}ms is {z:.1f} std above baseline {self.rtt.mean:.1f}ms"
                )
        if s.jitter_ms is not None and s.jitter_ms > cfg.jitter_ms_max:
            reasons.append(f"jitter {s.jitter_ms:.1f}ms > {cfg.jitter_ms_max:.1f}ms")
        base_tp = self.throughput.mean
        if (
            s.throughput_mbps is not None
            and base_tp is not None
            and s.throughput_mbps < cfg.throughput_drop_ratio * base_tp
        ):
            reasons.append(
                f"throughput {s.throughput_mbps:.1f}Mbps < "
                f"{cfg.throughput_drop_ratio * 100:.0f}% of baseline {base_tp:.1f}Mbps"
            )
        return reasons

    def _learn(self, s: Sample) -> None:
        """Update baselines from a sample judged clean."""
        if s.rtt_ms is not None:
            self.rtt.update(s.rtt_ms)
        if s.jitter_ms is not None:
            self.jitter.update(s.jitter_ms)
        # A failed burst reports 0.0; it must never define what "normal" is.
        if s.throughput_mbps is not None and s.throughput_mbps > 0:
            self.throughput.update(s.throughput_mbps)

    def observe(self, s: Sample) -> PathHealth:
        reasons = self._judge(s)
        bad = bool(reasons)
        self.verdicts.append((bad, reasons))
        self.samples_seen += 1
        if bad:
            self.consecutive_clean = 0
        else:
            self.consecutive_clean += 1
            self._learn(s)

        prev = self.status
        if self.status is Status.DEGRADED:
            if self.consecutive_clean >= self.cfg.recover_needed:
                self.status = Status.HEALTHY
                self.reason = ""
            elif bad:
                self.reason = "; ".join(reasons)
        else:
            n_bad = sum(1 for b, _ in self.verdicts if b)
            if n_bad >= self.cfg.bad_needed:
                self.status = Status.DEGRADED
                last_bad = next(r for b, r in reversed(self.verdicts) if b)
                self.reason = "; ".join(last_bad)
            else:
                self.status = Status.HEALTHY
                self.reason = ""
        return PathHealth(self.status, self.reason, changed=self.status is not prev)
