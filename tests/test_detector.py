import pytest

from sdwan.config import DetectorConfig
from sdwan.detector import PathMonitor, Status
from sdwan.probe import Sample

CFG = DetectorConfig(
    window=5,
    bad_needed=3,
    recover_needed=10,
    warmup_samples=10,
    ewma_alpha=0.1,
    loss_pct_max=10.0,
    rtt_ms_max=250.0,
    jitter_ms_max=50.0,
    zscore_max=3.0,
    min_std_ms=2.0,
    throughput_drop_ratio=0.25,
)


def sample(i, rtt=1.0, jitter=0.1, loss=0.0, tp=None):
    return Sample(ts=float(i), path="wan_a", rtt_ms=rtt, jitter_ms=jitter, loss_pct=loss, throughput_mbps=tp)


def feed(mon, samples):
    return [mon.observe(s) for s in samples]


def test_first_sample_moves_unknown_to_healthy():
    mon = PathMonitor(CFG)
    assert mon.status is Status.UNKNOWN
    h = mon.observe(sample(0))
    assert h.status is Status.HEALTHY
    assert h.changed


def test_clean_stream_stays_healthy_without_further_changes():
    mon = PathMonitor(CFG)
    healths = feed(mon, [sample(i, rtt=1.0 + (i % 3) * 0.1) for i in range(30)])
    assert all(h.status is Status.HEALTHY for h in healths)
    assert [h.changed for h in healths] == [True] + [False] * 29


def test_three_lossy_of_five_flips_to_degraded_with_reason():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i) for i in range(10)])
    h1 = mon.observe(sample(10, loss=40.0))
    h2 = mon.observe(sample(11, loss=40.0))
    assert h1.status is Status.HEALTHY
    assert h2.status is Status.HEALTHY
    h3 = mon.observe(sample(12, loss=40.0))
    assert h3.status is Status.DEGRADED
    assert h3.changed
    assert "loss 40.0% > 10.0%" in h3.reason


def test_single_spike_does_not_flip():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i) for i in range(10)])
    h = mon.observe(sample(10, rtt=None, jitter=None, loss=100.0))
    assert h.status is Status.HEALTHY
    healths = feed(mon, [sample(i) for i in range(11, 20)])
    assert all(h.status is Status.HEALTHY for h in healths)


def test_no_replies_counts_as_bad_without_raising():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i) for i in range(10)])
    healths = feed(mon, [sample(i, rtt=None, jitter=None, loss=100.0) for i in range(10, 13)])
    assert healths[-1].status is Status.DEGRADED
    assert "no replies" in healths[-1].reason


def test_rtt_zscore_does_not_fire_before_warmup():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i, rtt=1.0) for i in range(5)])
    healths = feed(mon, [sample(i, rtt=20.0) for i in range(5, 8)])
    assert all(h.status is Status.HEALTHY for h in healths)


def test_rtt_zscore_fires_after_warmup():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i, rtt=1.0) for i in range(20)])
    healths = feed(mon, [sample(i, rtt=20.0) for i in range(20, 23)])
    assert healths[-1].status is Status.DEGRADED
    assert "rtt 20.0ms" in healths[-1].reason
    assert "baseline" in healths[-1].reason


def test_absolute_rtt_ceiling_fires_even_before_warmup():
    mon = PathMonitor(CFG)
    healths = feed(mon, [sample(i, rtt=300.0) for i in range(3)])
    assert healths[-1].status is Status.DEGRADED
    assert "rtt 300.0ms > 250.0ms" in healths[-1].reason


def test_high_jitter_is_bad():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i) for i in range(10)])
    healths = feed(mon, [sample(i, jitter=60.0) for i in range(10, 13)])
    assert healths[-1].status is Status.DEGRADED
    assert "jitter 60.0ms > 50.0ms" in healths[-1].reason


def test_recovery_requires_recover_needed_clean_samples():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i) for i in range(10)])
    feed(mon, [sample(i, loss=50.0) for i in range(10, 13)])
    assert mon.status is Status.DEGRADED
    healths = feed(mon, [sample(i) for i in range(13, 22)])  # 9 clean
    assert all(h.status is Status.DEGRADED for h in healths)
    assert not any(h.changed for h in healths)
    h = mon.observe(sample(22))  # 10th clean
    assert h.status is Status.HEALTHY
    assert h.changed
    assert h.reason == ""


def test_baseline_frozen_while_degraded():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i, rtt=1.0) for i in range(20)])
    base = mon.baseline_rtt_ms
    feed(mon, [sample(i, rtt=200.0, loss=50.0) for i in range(20, 30)])
    assert mon.status is Status.DEGRADED
    assert mon.baseline_rtt_ms == pytest.approx(base)


def test_throughput_drop_below_ratio_is_bad():
    mon = PathMonitor(CFG)
    feed(mon, [sample(i, tp=800.0) for i in range(20)])
    healths = feed(mon, [sample(i, tp=100.0) for i in range(20, 23)])
    assert healths[-1].status is Status.DEGRADED
    assert "throughput 100.0Mbps" in healths[-1].reason


def bursty(i, tp_when_measured, every=5):
    """Sample stream shaped like the real agent: a burst result only every
    `every` samples, None in between."""
    return sample(i, tp=tp_when_measured if i % every == 0 else None)


def test_repeated_low_bursts_alone_degrade_the_path():
    mon = PathMonitor(CFG)
    feed(mon, [bursty(i, 800.0) for i in range(20)])
    assert mon.status is Status.HEALTHY
    healths = feed(mon, [bursty(i, 5.0) for i in range(20, 30)])
    assert healths[-1].status is Status.DEGRADED
    assert "throughput 5.0Mbps" in healths[-1].reason


def test_good_burst_clears_sticky_throughput_verdict():
    mon = PathMonitor(CFG)
    feed(mon, [bursty(i, 800.0) for i in range(20)])
    feed(mon, [bursty(i, 5.0) for i in range(20, 30)])
    assert mon.status is Status.DEGRADED
    healths = feed(mon, [bursty(i, 800.0) for i in range(30, 45)])
    assert healths[-1].status is Status.HEALTHY


def test_throughput_without_baseline_is_ignored():
    mon = PathMonitor(CFG)
    healths = feed(mon, [sample(i, tp=0.0) for i in range(3)])
    assert healths[-1].status is Status.HEALTHY
