import socket
import time

import pytest

from sdwan.config import Path, ProbeConfig
from sdwan.probe import (
    EchoResponder,
    ProbeRecord,
    Prober,
    ThroughputLoop,
    ThroughputSink,
    measure_throughput,
)

MS = 1_000_000
LOOP = Path(name="wan_a", local_ip="127.0.0.1", peer_ip="127.0.0.1")


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfg(echo_port, tp_port=1, tp_interval=60.0):
    return ProbeConfig(
        interval_ms=50,
        timeout_ms=200,
        echo_port=echo_port,
        throughput_port=tp_port,
        throughput_interval_s=tp_interval,
        throughput_bytes=65536,
    )


def test_prober_against_responder_sees_no_loss():
    port = free_udp_port()
    responder = EchoResponder(port, bind_ip="127.0.0.1")
    responder.start()
    prober = Prober(LOOP, cfg(port))
    prober.start()
    try:
        time.sleep(1.2)
        rtt, jitter, loss = prober.summarize(time.monotonic_ns())
        assert loss == 0.0
        assert rtt is not None and 0 <= rtt < 100
        assert jitter is not None
    finally:
        prober.stop()
        responder.stop()


def test_prober_against_closed_port_sees_full_loss():
    prober = Prober(LOOP, cfg(free_udp_port()))
    prober.start()
    try:
        time.sleep(1.2)
        result = prober.summarize(time.monotonic_ns())
        assert result is not None
        assert result[0] is None
        assert result[2] == 100.0
    finally:
        prober.stop()


def test_summarize_before_any_probe_returns_none():
    prober = Prober(LOOP, cfg(free_udp_port()))
    assert prober.summarize(time.monotonic_ns()) is None


def test_reply_after_timeout_counts_as_lost():
    prober = Prober(LOOP, cfg(free_udp_port()))
    now = time.monotonic_ns()
    prober.inject(ProbeRecord(1, now - 1000 * MS, now - 1000 * MS + 300 * MS))  # 300 ms > 200 ms timeout
    prober.inject(ProbeRecord(2, now - 900 * MS, now - 900 * MS + 10 * MS))
    rtt, _, loss = prober.summarize(now)
    assert loss == 50.0
    assert rtt == pytest.approx(10.0)


def test_old_records_are_pruned():
    prober = Prober(LOOP, cfg(free_udp_port()))
    now = time.monotonic_ns()
    prober.inject(ProbeRecord(1, now - 10_000 * MS, None))
    prober.inject(ProbeRecord(2, now - 1000 * MS, now - 990 * MS))
    prober.summarize(now)
    assert prober.record_count() == 1


def test_throughput_burst_on_loopback_is_positive():
    port = free_tcp_port()
    sink = ThroughputSink(port, nbytes=65536, bind_ip="127.0.0.1")
    sink.start()
    try:
        assert measure_throughput("127.0.0.1", "127.0.0.1", port, 65536) > 0
    finally:
        sink.stop()


def test_throughput_against_closed_port_is_zero():
    assert measure_throughput("127.0.0.1", "127.0.0.1", free_tcp_port(), 1024, timeout=0.5) == 0.0


def test_throughput_loop_take_returns_fresh_value_once():
    port = free_tcp_port()
    sink = ThroughputSink(port, nbytes=65536, bind_ip="127.0.0.1")
    sink.start()
    loop = ThroughputLoop(LOOP, cfg(free_udp_port(), tp_port=port, tp_interval=60.0))
    loop.start()
    try:
        deadline = time.time() + 5
        value = None
        while value is None and time.time() < deadline:
            value = loop.take()
            time.sleep(0.05)
        assert value is not None and value > 0
        assert loop.take() is None
    finally:
        loop.stop()
        sink.stop()
