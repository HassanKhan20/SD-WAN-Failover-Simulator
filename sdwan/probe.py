"""Path probing: UDP echo round trips, window summaries and throughput bursts.

The pure part (`ProbeRecord`, `Sample`, `summarize_window`) has no I/O and is
unit tested directly. The runtime part (responder, prober, throughput) runs in
threads inside the edge agent.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from sdwan.config import Path, ProbeConfig

# -- pure part --------------------------------------------------------------


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


# -- runtime part -----------------------------------------------------------

_PACKET = struct.Struct("!IQ4x")  # seq u32, send_ns u64, 4 bytes pad = 16 bytes
_SOCKET_POLL_S = 0.2
_KEEP_NS = 5_000_000_000  # keep 5 s of records for summaries


class EchoResponder(threading.Thread):
    """Returns every UDP datagram to its sender. One per edge, all WAN IPs."""

    def __init__(self, port: int, bind_ip: str = "0.0.0.0"):
        super().__init__(name=f"echo:{port}", daemon=True)
        self._halt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(_SOCKET_POLL_S)

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
                self.sock.sendto(data, addr)
            except socket.timeout:
                continue
            except OSError:
                continue
        self.sock.close()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)


class Prober:
    """Sends a timestamped datagram every interval over one path and keeps
    the records the agent summarizes once a second."""

    def __init__(
        self,
        path: Path,
        cfg: ProbeConfig,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.path = path
        self.cfg = cfg
        self.clock_ns = clock_ns
        self._records: dict[int, ProbeRecord] = {}
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._seq = 0
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.path.local_ip, 0))
        self._sock.settimeout(_SOCKET_POLL_S)
        self._threads = [
            threading.Thread(target=self._send_loop, name=f"probe-tx:{self.path.name}", daemon=True),
            threading.Thread(target=self._recv_loop, name=f"probe-rx:{self.path.name}", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._halt.set()
        for t in self._threads:
            t.join(timeout=2)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def inject(self, record: ProbeRecord) -> None:
        """Test hook: add a record as if it had been sent."""
        with self._lock:
            self._records[record.seq] = record

    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    def _send_loop(self) -> None:
        interval = self.cfg.interval_ms / 1000
        target = (self.path.peer_ip, self.cfg.echo_port)
        while not self._halt.is_set():
            self._seq += 1
            send_ns = self.clock_ns()
            with self._lock:
                self._records[self._seq] = ProbeRecord(self._seq, send_ns)
            try:
                self._sock.sendto(_PACKET.pack(self._seq, send_ns), target)
            except OSError:
                pass  # stays recorded as sent and unanswered: a loss
            self._halt.wait(interval)

    def _recv_loop(self) -> None:
        while not self._halt.is_set():
            try:
                data, _ = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                # Windows raises ConnectionResetError on ICMP port unreachable.
                continue
            if len(data) < _PACKET.size:
                continue
            seq, _ = _PACKET.unpack(data[: _PACKET.size])
            recv_ns = self.clock_ns()
            with self._lock:
                rec = self._records.get(seq)
                if rec is not None and rec.recv_ns is None:
                    rec.recv_ns = recv_ns

    def summarize(self, now_ns: int) -> tuple[float | None, float | None, float] | None:
        """Summary of the most recent full second whose timeouts have expired:
        probes sent in (now - 1.5 s, now - 0.5 s]. Replies slower than the
        timeout count as lost. Also prunes records older than 5 s."""
        timeout_ns = self.cfg.timeout_ms * 1_000_000
        start_ns = now_ns - 1_500_000_000
        end_ns = now_ns - 500_000_000
        with self._lock:
            for seq in [s for s, r in self._records.items() if r.send_ns < now_ns - _KEEP_NS]:
                del self._records[seq]
            window = [
                ProbeRecord(r.seq, r.send_ns, None)
                if r.recv_ns is not None and r.recv_ns - r.send_ns > timeout_ns
                else ProbeRecord(r.seq, r.send_ns, r.recv_ns)
                for r in self._records.values()
            ]
        return summarize_window(window, start_ns, end_ns)


class ThroughputSink(threading.Thread):
    """Accepts a burst of `nbytes`, then answers with one byte."""

    def __init__(self, port: int, nbytes: int, bind_ip: str = "0.0.0.0"):
        super().__init__(name=f"tp-sink:{port}", daemon=True)
        self.nbytes = nbytes
        self._halt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, port))
        self.sock.listen(4)
        self.sock.settimeout(_SOCKET_POLL_S)

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        self.sock.close()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(5.0)
                got = 0
                while got < self.nbytes:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    got += len(chunk)
                conn.sendall(b"\x01")
            except OSError:
                return

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)


def measure_throughput(
    local_ip: str, peer_ip: str, port: int, nbytes: int, timeout: float = 3.0
) -> float:
    """Mbps achieved sending `nbytes` to the peer's sink; 0.0 on any failure."""
    payload = bytes(nbytes)
    try:
        with socket.create_connection(
            (peer_ip, port), timeout=timeout, source_address=(local_ip, 0)
        ) as s:
            t0 = time.perf_counter()
            s.sendall(payload)
            ack = s.recv(1)
            elapsed = time.perf_counter() - t0
    except OSError:
        return 0.0
    if not ack or elapsed <= 0:
        return 0.0
    return nbytes * 8 / elapsed / 1e6


class ThroughputLoop(threading.Thread):
    """Runs a burst every `throughput_interval_s`; `take()` hands each new
    result to the agent exactly once."""

    def __init__(self, path: Path, cfg: ProbeConfig):
        super().__init__(name=f"tp-loop:{path.name}", daemon=True)
        self.path = path
        self.cfg = cfg
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._latest: float | None = None

    def run(self) -> None:
        while not self._halt.is_set():
            mbps = measure_throughput(
                self.path.local_ip,
                self.path.peer_ip,
                self.cfg.throughput_port,
                self.cfg.throughput_bytes,
            )
            with self._lock:
                self._latest = mbps
            self._halt.wait(self.cfg.throughput_interval_s)

    def take(self) -> float | None:
        with self._lock:
            value, self._latest = self._latest, None
        return value

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)
