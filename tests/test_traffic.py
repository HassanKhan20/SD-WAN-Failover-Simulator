import socket
import threading
import time

import pytest

from sdwan.store import Store
from sdwan.traffic import TrafficClient, run_server


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def run_client_for(client, seconds):
    stop = threading.Event()
    t = threading.Thread(target=client.run, args=(stop,), daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(3)
    assert not t.is_alive()


def test_client_records_successes_against_server(store):
    port = free_tcp_port()
    stop = threading.Event()
    server = threading.Thread(target=run_server, args=(port, stop), kwargs={"bind_ip": "127.0.0.1"}, daemon=True)
    server.start()
    lines = []
    client = TrafficClient("127.0.0.1", port, interval_ms=20, site="hq", store=store, out=lines.append)
    try:
        run_client_for(client, 1.6)
    finally:
        stop.set()
        server.join(3)
    summary = store.traffic_summary(seconds=60)
    assert summary["ok"] > 0
    assert summary["failed"] == 0
    assert summary["avg_latency_ms"] is not None and summary["avg_latency_ms"] < 100
    assert client.totals["ok"] == summary["ok"]
    assert lines and "ok" in lines[0]


def test_client_against_closed_port_records_failures_without_raising(store):
    client = TrafficClient("127.0.0.1", free_tcp_port(), interval_ms=20, site="hq", store=store, timeout_s=0.2, out=lambda s: None)
    run_client_for(client, 1.5)
    summary = store.traffic_summary(seconds=60)
    assert summary["failed"] > 0
    assert summary["ok"] == 0
    assert summary["avg_latency_ms"] is None


def test_client_works_without_a_store():
    port = free_tcp_port()
    stop = threading.Event()
    server = threading.Thread(target=run_server, args=(port, stop), kwargs={"bind_ip": "127.0.0.1"}, daemon=True)
    server.start()
    client = TrafficClient("127.0.0.1", port, interval_ms=20, site="hq", store=None, out=lambda s: None)
    try:
        run_client_for(client, 0.5)
    finally:
        stop.set()
        server.join(3)
    assert client.totals["ok"] > 0
