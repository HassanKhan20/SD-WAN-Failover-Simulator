# SD-WAN Failover Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker-based two-site SD-WAN simulator whose Python edge agents probe two WAN paths, store telemetry in SQLite, detect degradation, and re-route traffic to the healthy path in under five seconds.

**Architecture:** One Python package (`sdwan`) with a single CLI. Pure-logic modules (probe summary, detector, failover) are unit tested on the host. I/O modules (net, store, probe runtime, traffic) sit behind small classes so the agent can be tested with fakes. One Docker image runs every role; Docker Compose wires four networks and five services.

**Tech Stack:** Python 3.12 (image) / 3.14 (host), `rich`, `pytest`, SQLite, iproute2 (`ip`, `tc netem`), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-25-sdwan-failover-simulator-design.md`

## Global Constraints

- Runtime dependency is `rich` only; dev dependency `pytest`.
- Code runs on Python 3.12 and 3.14. Use `tomllib`, dataclasses, `threading` (no asyncio).
- Addresses, ports and tunables come from `config/topology.toml` exactly as written in spec section 4.
- Interfaces are resolved by IP via `ip -j addr`, never assumed.
- Event kinds are exactly: `STARTUP`, `DEGRADED`, `RECOVERED`, `FAILOVER`, `FAILBACK`, `ALL_DEGRADED`, `CHAOS`, `ERROR`.
- SQLite in WAL mode with `busy_timeout = 5000`; one connection per thread.
- Commit after every task; push at the end.

## Review Focus

1. A window with zero probes sent (agent just started, or clock skew): `summarize_window` must return `None`, and the agent must skip the sample rather than write `loss_pct = NaN`. Test in Task 2.
2. `rtt_ms is None` in the detector (100% loss): the z-score rule must not raise on `None`. Test in Task 4.
3. Both paths degraded then one recovers: failover must resume as soon as a candidate exists and the `ALL_DEGRADED` guard must reset. Test in Task 5.
4. `tc qdisc del` when no netem is installed: `restore` must succeed and still log the `CHAOS` event. Test in Task 6 and Task 9.
5. Monitor started before the agent created the database: must render "waiting for data", not crash. Test in Task 10.

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`, `sdwan/__init__.py`, `sdwan/__main__.py`, `sdwan/config.py`, `config/topology.toml`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Produces: `load_topology(path) -> Topology`; `default_config_path() -> str`; frozen dataclasses `Path(name, local_ip, peer_ip)`, `Site(name, lan, edge_lan_ip, app_ip, peer, paths: dict[str, Path])`, `ProbeConfig`, `DetectorConfig`, `FailoverConfig`, `Topology(sites, probe, detector, failover)` with `remote_lan(site) -> str` and `site(name) -> Site`.

- [ ] Write `config/topology.toml` verbatim from spec section 4.
- [ ] Write `tests/test_config.py`: loads the shipped file; asserts `hq.paths["wan_a"].peer_ip == "10.100.0.12"`; `remote_lan("hq") == "10.2.0.0/24"`; `failover.preferred == "wan_a"`; `detector.window == 5`; missing site raises `KeyError`.
- [ ] Run `pytest tests/test_config.py -v`; expect ImportError.
- [ ] Implement `sdwan/config.py` with `tomllib`.
- [ ] Run tests; expect PASS.
- [ ] Commit: `feat: project scaffold and topology config`.

### Task 2: Probe window summary (pure)

**Files:**
- Create: `sdwan/probe.py` (summary part), `tests/test_probe.py`

**Interfaces:**
- Produces: `ProbeRecord(seq, send_ns, recv_ns: int | None)`; `Sample(ts, path, rtt_ms, jitter_ms, loss_pct, throughput_mbps)`; `summarize_window(records, start_ns, end_ns) -> tuple[float | None, float | None, float] | None`.

- [ ] Tests: all four received gives loss 0 and mean RTT; one of four lost gives loss 25; none received gives `(None, None, 100.0)`; jitter of RTTs 10, 12, 11 ms is 1.5; a record with `send_ns == start_ns` is excluded and `send_ns == end_ns` is included; empty window returns `None`.
- [ ] Run; expect fail. Implement. Run; expect pass. Commit: `feat: probe window summary`.

### Task 3: SQLite store

**Files:**
- Create: `sdwan/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Store(path)` with `write_sample(site, sample)`, `write_event(ts, site, kind, path=None, detail=None)`, `write_traffic(ts, site, ok, failed, avg_latency_ms)`, `upsert_path_state(site, path, status, active, reason, ts)`, `latest_samples() -> list[dict]` (latest row per site/path), `last_throughput() -> dict[tuple[str, str], float]`, `path_states() -> list[dict]`, `recent_events(n=10) -> list[dict]`, `traffic_summary(seconds=10, now=None) -> dict`, `sample_count() -> int`, `close()`.

- [ ] Tests on `tmp_path / "t.db"`: schema exists after init; sample round trip with `None` throughput; upsert twice keeps one row with the newer status; events come back newest first; traffic summary over two rows gives `success_pct` and average latency; `latest_samples` returns the newest row per path.
- [ ] Run; fail. Implement (thread-local connections, WAL, busy_timeout). Run; pass. Commit: `feat: sqlite telemetry store`.

### Task 4: Detector

**Files:**
- Create: `sdwan/detector.py`, `tests/test_detector.py`

**Interfaces:**
- Consumes: `Sample`, `DetectorConfig`.
- Produces: `Status` enum (`UNKNOWN`, `HEALTHY`, `DEGRADED`); `PathHealth(status, reason, changed)`; `PathMonitor(cfg)` with `observe(sample) -> PathHealth` and `.status`.

- [ ] Tests: clean stream stays HEALTHY with `changed` only on the first sample; three lossy samples in five flip to DEGRADED with a reason containing `loss`; one spike does not flip; `rtt_ms=None` does not raise and counts as bad; z-score fires after warm-up on a 5x RTT jump but not before warm-up; recovery needs `recover_needed` clean samples; baseline RTT is unchanged during a degraded episode; throughput below 25% of baseline is bad.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: path health detector`.

### Task 5: Failover state machine

**Files:**
- Create: `sdwan/failover.py`, `tests/test_failover.py`

**Interfaces:**
- Consumes: `PathHealth`, `Status`, `FailoverConfig`.
- Produces: `DecisionKind` enum; `Decision(kind, from_path, to_path, reason)`; `FailoverState(active, last_switch_ts, preferred_healthy_since, all_degraded_logged)`; `initial_state(cfg) -> FailoverState`; `decide(state, healths, now, cfg) -> tuple[FailoverState, Decision | None]`.

- [ ] Tests: failover when active degraded and other healthy; no decision inside `min_dwell_s`; failback only after `failback_hold_s` of preferred healthy; `ALL_DEGRADED` once per episode then resumes failover when a path recovers; preferred chosen over another candidate; no decision when everything healthy on preferred.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: failover state machine`.

### Task 6: Net wrappers

**Files:**
- Create: `sdwan/net.py`, `tests/test_net.py`

**Interfaces:**
- Produces: `Net` protocol with `iface_for_ip(ip) -> str`, `set_route(dest, via, dev)`, `current_route(dest) -> str | None`, `apply_netem(dev, loss=None, delay_ms=None, jitter_ms=None, rate=None)`, `clear_netem(dev)`; `IpRouteNet(runner=subprocess.run)`; `FakeNet` recording calls; pure helpers `iface_from_addr_json(text, ip) -> str` and `netem_args(loss, delay_ms, jitter_ms, rate) -> list[str]`.

- [ ] Tests: `iface_from_addr_json` finds `eth1` for a given IP in sample `ip -j addr` output and raises `LookupError` otherwise; `netem_args(loss=30, delay_ms=150, jitter_ms=40, rate="1mbit")` returns `["loss", "30%", "delay", "150ms", "40ms", "rate", "1mbit"]`; `netem_args()` with nothing raises `ValueError`; `IpRouteNet.set_route` calls `ip route replace ...`; `clear_netem` swallows "Cannot delete qdisc with handle of zero" but re-raises other errors.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: ip and tc wrappers`.

### Task 7: Probe runtime

**Files:**
- Modify: `sdwan/probe.py`
- Test: `tests/test_probe_runtime.py`

**Interfaces:**
- Produces: `EchoResponder(port)` thread; `Prober(path, cfg, clock_ns=time.monotonic_ns)` with `start()`, `stop()`, `summarize(now_ns) -> tuple | None` (window `(now-1.5s, now-0.5s]`); `ThroughputSink(port, nbytes)` thread; `measure_throughput(local_ip, peer_ip, port, nbytes, timeout=3.0) -> float`; `ThroughputLoop(path, cfg)` thread with `take() -> float | None`.

- [ ] Loopback tests: responder on 127.0.0.1 and a prober with `local_ip=127.0.0.1` produce loss 0 after 1.5 s; prober against a closed port gives loss 100; sink plus `measure_throughput` on loopback returns a positive Mbps.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: udp probes and throughput bursts`.

### Task 8: Traffic server and client

**Files:**
- Create: `sdwan/traffic.py`, `tests/test_traffic.py`

**Interfaces:**
- Produces: `run_server(port, stop: threading.Event)`; `TrafficClient(target, port, interval_ms, site, store: Store | None)` with `run(stop: threading.Event)` and per-second `traffic` rows.

- [ ] Loopback test: server and client run for 1.5 s with a temp store; at least one `traffic` row has `ok > 0` and `failed == 0`; client against a closed port records `failed > 0` and does not raise.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: traffic generator`.

### Task 9: Chaos, agent and CLI

**Files:**
- Create: `sdwan/chaos.py`, `sdwan/agent.py`, `sdwan/cli.py`, `tests/test_chaos.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `degrade(topology, site, path, net, store, ts, loss, delay, jitter, rate)`; `restore(topology, site, path, net, store, ts)`; `Agent(topology, site, net, store, sampler, clock)` with `start_route()` and `tick(now)`; CLI subcommands `agent`, `traffic server|client`, `monitor`, `status`, `degrade`, `restore`.

- [ ] Chaos tests with `FakeNet` and temp store: `degrade` applies netem on the interface owning `local_ip` and logs a `CHAOS` event with the options; `restore` clears and logs.
- [ ] Agent tests with `FakeNet`, temp store and a scripted `sampler`: `start_route` sets the route via the preferred peer and logs `STARTUP`; feeding lossy samples on `wan_a` for four ticks produces a `FAILOVER` event, a route via `10.200.0.12`, and `path_state` with `wan_b` active; a `RuntimeError` from `set_route` becomes an `ERROR` event and the loop continues.
- [ ] Implement `cli.py` with argparse and wire `python -m sdwan --help`.
- [ ] Run; pass. Commit: `feat: chaos commands, edge agent and cli`.

### Task 10: Monitor and status

**Files:**
- Create: `sdwan/monitor.py`, `tests/test_monitor.py`

**Interfaces:**
- Produces: `build_layout(store) -> rich renderable`; `run_monitor(db_path)`; `status_dict(store) -> dict`; `print_status(db_path, as_json)`.

- [ ] Tests: `status_dict` on a store with two path states returns `paths` and `events` keys; rendering the layout with `rich.console.Console(record=True)` includes `HEALTHY` and the event kind; `print_status` on a missing DB path prints `waiting for data` and exits 0.
- [ ] Run; fail. Implement. Run; pass. Commit: `feat: live monitor and status`.

### Task 11: Docker image, compose and host wrapper

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `sdwanctl.py`, `.dockerignore`

- [ ] Write `Dockerfile` (python:3.12-slim, iproute2, iputils-ping, tcpdump, pip install ., copy config to `/etc/sdwan/topology.toml`, entrypoint `sdwan`).
- [ ] Write `docker-compose.yml` per spec sections 2 and 3, with IPAM subnets, static addresses, `cap_add`, sysctls, the `telemetry` volume and the `monitor` service under profile `tools`.
- [ ] Write `sdwanctl.py` with `up`, `down`, `monitor`, `status`, `degrade`, `restore`, `doctor`, `e2e`, `--site`.
- [ ] Run `python sdwanctl.py doctor`; expect netem RTT about 200 ms.
- [ ] Run `python sdwanctl.py up`; wait 20 s; `python sdwanctl.py status`; expect both paths HEALTHY, `wan_a` active on both sites, and hq-app logs showing successes.
- [ ] Run `degrade wan_a --loss 40`; within 5 s `status` shows `wan_b` active. `restore wan_a`; within 40 s `wan_a` active again.
- [ ] Commit: `feat: docker image, compose topology and host wrapper`.

### Task 12: End-to-end proof

**Files:**
- Create: `scripts/e2e_test.py`

- [ ] Implement per spec section 16: up, wait healthy, degrade, measure time to `wan_b` active (assert < 10 s), restore, wait for `wan_a` (assert < 60 s), summary, down unless `--keep`.
- [ ] Run `python sdwanctl.py e2e`; record the measured failover time.
- [ ] Commit: `test: end-to-end failover timing proof`.

### Task 13: README

**Files:**
- Modify: `README.md`

- [ ] Write per spec section 17, including the measured failover time from Task 12.
- [ ] Commit: `docs: readme with architecture, quickstart and demo`.
- [ ] Push `main` to `origin`.
