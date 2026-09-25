"""The edge agent: probes every WAN path, stores telemetry, judges path
health, decides failover, and changes the kernel route.

`tick()` is one iteration and is unit tested with a FakeNet and a scripted
sampler. `run()` wires the real probers and calls tick once a second.
"""

from __future__ import annotations

import time
import traceback
from typing import Callable

from sdwan.config import Topology
from sdwan.detector import PathHealth, PathMonitor, Status
from sdwan.failover import Decision, DecisionKind, decide, initial_state
from sdwan.net import Net, NetError
from sdwan.probe import EchoResponder, Prober, Sample, ThroughputLoop, ThroughputSink
from sdwan.store import Store

Sampler = Callable[[float], dict[str, Sample]]


class Agent:
    def __init__(
        self,
        topology: Topology,
        site: str,
        net: Net,
        store: Store,
        sampler: Sampler | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
    ):
        self.topology = topology
        self.site = topology.site(site)
        self.remote_lan = topology.remote_lan(site)
        self.net = net
        self.store = store
        self.sampler = sampler
        self.clock = clock
        self.log = log
        self.monitors = {name: PathMonitor(topology.detector) for name in self.site.paths}
        self.healths = {name: PathHealth(Status.UNKNOWN, "", False) for name in self.site.paths}
        self.state = initial_state(topology.failover)
        self._probers: dict[str, Prober] = {}
        self._tp_loops: dict[str, ThroughputLoop] = {}
        self._threads: list = []

    # -- routing --------------------------------------------------------------

    def _route_via(self, path: str) -> None:
        p = self.site.paths[path]
        dev = self.net.iface_for_ip(p.local_ip)
        self.net.set_route(self.remote_lan, via=p.peer_ip, dev=dev)

    def start_route(self) -> None:
        now = self.clock()
        self._route_via(self.state.active)
        self.store.write_event(
            now,
            self.site.name,
            "STARTUP",
            path=self.state.active,
            detail=f"route {self.remote_lan} via {self.state.active}",
        )
        self._write_states(now)
        self.log(f"[{self.site.name}] route {self.remote_lan} via {self.state.active}")

    def _write_states(self, now: float) -> None:
        for name, health in self.healths.items():
            self.store.upsert_path_state(
                self.site.name,
                name,
                health.status.value,
                active=(name == self.state.active),
                reason=health.reason,
                ts=now,
            )

    # -- one iteration --------------------------------------------------------

    def tick(self, now: float) -> None:
        samples = self.sampler(now) if self.sampler else {}
        for name, sample in samples.items():
            if sample is None or name not in self.monitors:
                continue
            self.store.write_sample(self.site.name, sample)
            prev = self.healths[name].status
            health = self.monitors[name].observe(sample)
            self.healths[name] = health
            if health.changed and health.status is Status.DEGRADED:
                self.store.write_event(now, self.site.name, "DEGRADED", path=name, detail=health.reason)
                self.log(f"[{self.site.name}] {name} DEGRADED: {health.reason}")
            elif health.changed and health.status is Status.HEALTHY and prev is Status.DEGRADED:
                self.store.write_event(now, self.site.name, "RECOVERED", path=name)
                self.log(f"[{self.site.name}] {name} RECOVERED")

        new_state, decision = decide(self.state, self.healths, now, self.topology.failover)
        if decision is None or self._apply(decision, now):
            self.state = new_state
        self._write_states(now)

    def _apply(self, decision: Decision, now: float) -> bool:
        """Carry out a decision. Returns False if the route change failed, in
        which case the caller keeps the old state so the next tick retries."""
        if decision.kind is DecisionKind.ALL_DEGRADED:
            self.store.write_event(now, self.site.name, "ALL_DEGRADED", path=decision.from_path, detail=decision.reason)
            self.log(f"[{self.site.name}] ALL_DEGRADED: {decision.reason}")
            return True
        detail = f"{decision.from_path} -> {decision.to_path}: {decision.reason}"
        try:
            self._route_via(decision.to_path)
        except (NetError, LookupError, OSError) as exc:
            self.store.write_event(now, self.site.name, "ERROR", path=decision.to_path, detail=f"{decision.kind.value} failed: {exc}")
            self.log(f"[{self.site.name}] {decision.kind.value} to {decision.to_path} failed: {exc}")
            return False
        self.store.write_event(now, self.site.name, decision.kind.value, path=decision.to_path, detail=detail)
        self.log(f"[{self.site.name}] {decision.kind.value} {detail}")
        return True

    # -- real runtime ---------------------------------------------------------

    def start(self) -> None:
        """Start responder, sink, probers and throughput loops, and install
        the sampler that reads them."""
        probe = self.topology.probe
        responder = EchoResponder(probe.echo_port)
        sink = ThroughputSink(probe.throughput_port, probe.throughput_bytes)
        responder.start()
        sink.start()
        self._threads = [responder, sink]
        for name, p in self.site.paths.items():
            prober = Prober(p, probe)
            prober.start()
            self._probers[name] = prober
            loop = ThroughputLoop(p, probe)
            loop.start()
            self._tp_loops[name] = loop
        self.sampler = self._sample

    def _sample(self, now: float) -> dict[str, Sample]:
        now_ns = time.monotonic_ns()
        out: dict[str, Sample] = {}
        for name, prober in self._probers.items():
            summary = prober.summarize(now_ns)
            if summary is None:
                continue
            rtt, jitter, loss = summary
            out[name] = Sample(
                ts=now,
                path=name,
                rtt_ms=rtt,
                jitter_ms=jitter,
                loss_pct=loss,
                throughput_mbps=self._tp_loops[name].take(),
            )
        return out

    def stop(self) -> None:
        for prober in self._probers.values():
            prober.stop()
        for loop in self._tp_loops.values():
            loop.stop()
        for t in self._threads:
            t.stop()

    def run(self) -> None:
        self.start_route()
        self.start()
        self.log(f"[{self.site.name}] agent running; paths={list(self.site.paths)}")
        next_tick = time.monotonic() + 1.0
        try:
            while True:
                time.sleep(max(0.0, next_tick - time.monotonic()))
                next_tick += 1.0
                try:
                    self.tick(self.clock())
                except Exception as exc:  # keep the loop alive whatever happens
                    self.log(f"[{self.site.name}] tick failed: {exc}\n{traceback.format_exc()}")
                    try:
                        self.store.write_event(self.clock(), self.site.name, "ERROR", detail=f"tick failed: {exc}")
                    except Exception:
                        pass
        finally:
            self.stop()
