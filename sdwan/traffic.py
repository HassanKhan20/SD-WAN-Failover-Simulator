"""Application traffic that crosses the WAN: a TCP request/response server on
the branch and a client on HQ that keeps one persistent connection, sends a
request every interval, and records success rate and latency per second.

This is the traffic the failover keeps running. Its success rate is what the
monitor shows in the traffic panel.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from sdwan.store import Store

REQUEST_SIZE = 64
_POLL_S = 0.2
_RECONNECT_BACKOFF_S = 0.2


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def _serve(conn: socket.socket) -> None:
    with conn:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            while True:
                request = _recv_exact(conn, REQUEST_SIZE)
                conn.sendall(request)
        except OSError:
            return


def run_server(port: int, stop: threading.Event, bind_ip: str = "0.0.0.0") -> None:
    """Echo each 64-byte request back. Returns when `stop` is set."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    sock.listen(8)
    sock.settimeout(_POLL_S)
    try:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_serve, args=(conn,), daemon=True).start()
    finally:
        sock.close()


class TrafficClient:
    def __init__(
        self,
        target: str,
        port: int,
        interval_ms: int = 100,
        site: str = "hq",
        store: Store | None = None,
        timeout_s: float = 1.0,
        out: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
    ):
        self.target = target
        self.port = port
        self.interval_s = interval_ms / 1000
        self.site = site
        self.store = store
        self.timeout_s = timeout_s
        self.out = out
        self.clock = clock
        self.totals = {"ok": 0, "failed": 0}
        self._conn: socket.socket | None = None
        self._request = bytes(range(REQUEST_SIZE))

    def _connect(self) -> None:
        conn = socket.create_connection((self.target, self.port), timeout=self.timeout_s)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def _one_request(self) -> float | None:
        """Latency in ms of one request, or None if it failed."""
        try:
            if self._conn is None:
                self._connect()
            t0 = time.perf_counter()
            self._conn.sendall(self._request)
            reply = _recv_exact(self._conn, REQUEST_SIZE)
            if reply != self._request:
                raise ConnectionError("bad reply")
            return (time.perf_counter() - t0) * 1000
        except OSError:
            self._drop()
            return None

    def _flush(self, ts: float, ok: int, failed: int, latencies: list[float]) -> None:
        avg = sum(latencies) / len(latencies) if latencies else None
        if self.store is not None:
            self.store.write_traffic(ts, self.site, ok, failed, avg)
        total = ok + failed
        rate = f"{ok / total * 100:5.1f}%" if total else "  n/a"
        lat = f"{avg:6.1f} ms" if avg is not None else "   n/a"
        self.out(f"{time.strftime('%H:%M:%S', time.localtime(ts))} ok={ok:<3} failed={failed:<3} success={rate} latency={lat}")

    def run(self, stop: threading.Event) -> None:
        ok = failed = 0
        latencies: list[float] = []
        window_start = self.clock()
        try:
            while not stop.is_set():
                latency = self._one_request()
                if latency is None:
                    failed += 1
                    self.totals["failed"] += 1
                    stop.wait(_RECONNECT_BACKOFF_S)
                else:
                    ok += 1
                    self.totals["ok"] += 1
                    latencies.append(latency)
                    stop.wait(self.interval_s)
                now = self.clock()
                if now - window_start >= 1.0:
                    self._flush(now, ok, failed, latencies)
                    ok = failed = 0
                    latencies = []
                    window_start = now
            if ok or failed:
                self._flush(self.clock(), ok, failed, latencies)
        finally:
            self._drop()
