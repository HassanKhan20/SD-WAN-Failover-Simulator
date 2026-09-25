# SD-WAN Failover Simulator: Design Spec

Date: 2026-09-25
Status: approved for planning

## 1. Purpose

A portfolio project that demonstrates, end to end and on a laptop, how an
SD-WAN edge keeps traffic flowing when one WAN path degrades. A viewer runs
one `docker compose up`, watches a live terminal view, injects loss or
latency on a path with one command, and sees the network fail over to the
healthy path in under five seconds while application traffic keeps running.

Success criteria:

- Two sites (HQ and one branch) connected by two independent WAN paths,
  modeled as Docker networks, with real Linux routing in edge containers.
- Python probes continuously measure RTT, jitter, packet loss and throughput
  per path and store them in SQLite.
- A detector flags a degraded path from its telemetry.
- The edge changes its kernel route to the healthy path automatically.
  Measured failover time from injecting degradation to route change is
  under five seconds.
- A live terminal monitor shows path health, the active path, traffic
  success rate and an event log.
- Unit tests cover the detector, failover state machine, probe summaries,
  storage and config. An end-to-end script proves the timing claim.

Out of scope: a web dashboard, more than one branch, scripted chaos
timelines, BGP/OSPF, IPsec, real hardware.

## 2. Topology

Four Docker bridge networks with fixed IPAM. Docker owns `.1` on each.

| Network      | Subnet          | hq-edge      | branch-edge  | app                  |
|--------------|-----------------|--------------|--------------|----------------------|
| `hq_lan`     | 10.1.0.0/24     | 10.1.0.2     | none         | hq-app 10.1.0.10     |
| `branch_lan` | 10.2.0.0/24     | none         | 10.2.0.2     | branch-app 10.2.0.10 |
| `wan_a`      | 10.100.0.0/24   | 10.100.0.11  | 10.100.0.12  | none                 |
| `wan_b`      | 10.200.0.0/24   | 10.200.0.11  | 10.200.0.12  | none                 |

```
 hq_lan 10.1.0.0/24                                   branch_lan 10.2.0.0/24
 +--------+      +---------+  wan_a 10.100.0.0/24  +------------+      +------------+
 | hq-app |------| hq-edge |=======================| branch-edge|------| branch-app |
 | .10    |      | .2      |  wan_b 10.200.0.0/24  | .2         |      | .10        |
 +--------+      +---------+=======================+------------+      +------------+
                   agent                                agent
```

Routing:

- `hq-app`: `ip route add 10.2.0.0/24 via 10.1.0.2` at start.
- `branch-app`: `ip route add 10.1.0.0/24 via 10.2.0.2` at start.
- `hq-edge`: `ip route replace 10.2.0.0/24 via <branch-edge IP on active WAN>`,
  managed by the agent.
- `branch-edge`: `ip route replace 10.1.0.0/24 via <hq-edge IP on active WAN>`,
  managed by the agent.
- Edge sysctls: `net.ipv4.ip_forward=1`, `net.ipv4.conf.all.rp_filter=0`,
  `net.ipv4.conf.default.rp_filter=0`.

Each edge runs its own agent and decides independently. Both agents observe
the same round-trip probes, so they converge on the same active path.
Brief asymmetry during the switch is acceptable; TCP tolerates it.

Interfaces are always resolved by looking up which interface holds a given
IP (`ip -j addr`), never by assuming `eth1`, because Docker does not
guarantee interface order.

## 3. Containers

One image built from `Dockerfile`: `python:3.12-slim` plus `iproute2`,
`iputils-ping`, `tcpdump`, and the `sdwan` package installed with pip.
Entrypoint is the `sdwan` CLI.

| Service       | Command                                                     | Caps / sysctls           | Networks                 |
|---------------|-------------------------------------------------------------|--------------------------|--------------------------|
| `hq-edge`     | `sdwan agent` with `SDWAN_SITE=hq`                          | NET_ADMIN; forwarding on | hq_lan, wan_a, wan_b     |
| `branch-edge` | `sdwan agent` with `SDWAN_SITE=branch`                      | NET_ADMIN; forwarding on | branch_lan, wan_a, wan_b |
| `hq-app`      | add route, then `sdwan traffic client --target 10.2.0.10`   | NET_ADMIN                | hq_lan                   |
| `branch-app`  | add route, then `sdwan traffic server`                      | NET_ADMIN                | branch_lan               |
| `monitor`     | `sdwan monitor` (profile `tools`, tty)                      | none                     | none needed              |

A named volume `telemetry` is mounted at `/data` in every service. The
SQLite file is `/data/sdwan.db`. The topology file is copied into the image
at `/etc/sdwan/topology.toml` and can be overridden with a bind mount.

Environment variables: `SDWAN_SITE` (required for agent), `SDWAN_CONFIG`
(default `/etc/sdwan/topology.toml`), `SDWAN_DB` (default `/data/sdwan.db`).

## 4. Configuration file

`config/topology.toml`:

```toml
[sites.hq]
lan = "10.1.0.0/24"
edge_lan_ip = "10.1.0.2"
app_ip = "10.1.0.10"
peer = "branch"

[sites.hq.paths.wan_a]
local_ip = "10.100.0.11"
peer_ip = "10.100.0.12"

[sites.hq.paths.wan_b]
local_ip = "10.200.0.11"
peer_ip = "10.200.0.12"

[sites.branch]
lan = "10.2.0.0/24"
edge_lan_ip = "10.2.0.2"
app_ip = "10.2.0.10"
peer = "hq"

[sites.branch.paths.wan_a]
local_ip = "10.100.0.12"
peer_ip = "10.100.0.11"

[sites.branch.paths.wan_b]
local_ip = "10.200.0.12"
peer_ip = "10.200.0.11"

[probe]
interval_ms = 250
timeout_ms = 500
echo_port = 5001
throughput_port = 5002
throughput_interval_s = 5
throughput_bytes = 262144

[detector]
window = 5
bad_needed = 3
recover_needed = 10
warmup_samples = 10
ewma_alpha = 0.1
loss_pct_max = 10.0
rtt_ms_max = 250.0
jitter_ms_max = 50.0
zscore_max = 3.0
min_std_ms = 2.0
throughput_drop_ratio = 0.25

[failover]
preferred = "wan_a"
min_dwell_s = 5
failback_hold_s = 15
```

`config.py` parses this into frozen dataclasses (`Topology`, `Site`,
`Path`, `ProbeConfig`, `DetectorConfig`, `FailoverConfig`) using `tomllib`.
The remote LAN for a site is `sites[site.peer].lan`.

## 5. Package layout

```
sdwan/
  __init__.py
  __main__.py     python -m sdwan
  cli.py          argparse subcommands
  config.py       TOML -> dataclasses
  net.py          ip / tc wrappers behind a Net protocol; FakeNet for tests
  probe.py        UDP echo responder, prober, window summary, throughput burst
  store.py        SQLite schema, writers, readers
  detector.py     pure per-path health logic
  failover.py     pure path-selection state machine
  agent.py        wires everything; runs on each edge
  traffic.py      TCP request/response server and client
  chaos.py        degrade / restore via netem
  monitor.py      rich Live terminal view
tests/
  test_config.py test_probe.py test_store.py test_detector.py test_failover.py
scripts/
  e2e_test.py     host-side end-to-end proof
config/topology.toml
Dockerfile
docker-compose.yml
sdwanctl.py       host-side convenience wrapper
pyproject.toml    package metadata; deps: rich; dev: pytest
```

Runtime dependency is `rich` only. Code must run on Python 3.12 (image)
and 3.14 (host tests).

## 6. Probing

`probe.py` has four parts.

**Echo responder.** One UDP socket bound to `0.0.0.0:echo_port`. It returns
every datagram unchanged. Because each peer WAN IP is on-link, the reply
goes back over the same WAN the probe arrived on.

**Prober.** One per path. A UDP socket bound to the path's `local_ip` and an
ephemeral port. Every `interval_ms` it sends a 16-byte datagram
`(seq: u32, send_ns: u64, pad)` to `peer_ip:echo_port` and records
`send_ns` by seq. A receive loop records `recv_ns` for each returned seq.
Probes older than `timeout_ms` without a reply count as lost.

**Window summary** (pure function, tested). At each 1 s tick at time `t`,
summarize probes with send time in `(t - 1.5 s, t - 0.5 s]`, which is the
most recent full second whose timeouts have all expired.

- `rtt_ms`: mean RTT of received probes; `None` if none received.
- `jitter_ms`: mean absolute difference between consecutive received RTTs
  (RFC 3550 style); `0.0` if fewer than two received.
- `loss_pct`: `(sent - received) / sent * 100`.

**Throughput burst.** Every `throughput_interval_s` per path, open a TCP
connection from `local_ip` to `peer_ip:throughput_port` with a 3 s timeout,
send `throughput_bytes`, wait for a one-byte ack, and compute
`Mbps = bytes * 8 / seconds / 1e6`. On any failure the value is `0.0`.
The agent also runs the sink: a TCP server on `throughput_port` that reads
until `throughput_bytes` arrived and replies with one byte.

A sample row carries `throughput_mbps` only on ticks where a burst
completed; otherwise `NULL`.

## 7. Storage

SQLite, WAL mode, `busy_timeout = 5000`, one connection per thread.

```sql
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  path TEXT NOT NULL,
  rtt_ms REAL,
  jitter_ms REAL,
  loss_pct REAL NOT NULL,
  throughput_mbps REAL
);
CREATE INDEX IF NOT EXISTS samples_site_path_ts ON samples(site, path, ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS traffic (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  ok INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  avg_latency_ms REAL
);

CREATE TABLE IF NOT EXISTS path_state (
  site TEXT NOT NULL,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  active INTEGER NOT NULL,
  reason TEXT,
  updated_ts REAL NOT NULL,
  PRIMARY KEY (site, path)
);
```

Event kinds: `STARTUP`, `DEGRADED`, `RECOVERED`, `FAILOVER`, `FAILBACK`,
`ALL_DEGRADED`, `CHAOS`, `ERROR`.

`store.py` exposes a `Store` class with `write_sample`, `write_event`,
`write_traffic`, `upsert_path_state`, and readers `latest_samples(n)`,
`path_states()`, `recent_events(n)`, `traffic_summary(seconds)`,
`sample_count()`.

## 8. Detector

`detector.py` is pure Python with no I/O or clock access; the caller passes
timestamps.

`PathMonitor(config)` holds, for one path:

- EWMA mean and EWMA variance for `rtt_ms` and `jitter_ms`, and EWMA mean
  for `throughput_mbps`, updated only from samples judged clean, so a
  degraded episode does not poison the baseline.
- `samples_seen` counter for warm-up.
- A deque of the last `window` verdicts (`bad: bool`, `reasons: list[str]`).
- `consecutive_clean` counter.
- `status`: `UNKNOWN` until the first sample, then `HEALTHY` or `DEGRADED`.

`observe(sample, ts) -> PathHealth(status, reason, changed: bool)`.

A sample is **bad** if any rule fires. Each firing rule contributes a short
reason string, for example `loss 34.0% > 10.0%`:

1. `loss_pct > loss_pct_max`
2. `rtt_ms is None` (nothing received) or `rtt_ms > rtt_ms_max`
3. warmed up (`samples_seen >= warmup_samples`) and
   `(rtt_ms - ewma_rtt) / max(ewma_std_rtt, min_std_ms) > zscore_max`
4. `jitter_ms > jitter_ms_max`
5. `throughput_mbps is not None`, a throughput baseline exists, and
   `throughput_mbps < throughput_drop_ratio * baseline`

Transitions:

- `HEALTHY -> DEGRADED` when at least `bad_needed` of the last `window`
  verdicts are bad. `reason` is the most recent bad verdict's reasons
  joined with `; `. Resets `consecutive_clean`.
- `DEGRADED -> HEALTHY` when `consecutive_clean >= recover_needed`.

`changed` is true on the tick a transition happened, so the agent can log
`DEGRADED` and `RECOVERED` events.

## 9. Failover state machine

`failover.py` is pure. `FailoverState(active, last_switch_ts,
preferred_healthy_since, all_degraded_logged)`.

`decide(state, healths: dict[str, PathHealth], now, config)
-> tuple[FailoverState, Decision | None]`, evaluated once per tick:

1. Track `preferred_healthy_since`: set to `now` the first tick the
   preferred path is `HEALTHY` after not being so; cleared when it is not.
2. If `now - last_switch_ts < min_dwell_s`, return no decision.
3. If `healths[active].status == DEGRADED`:
   - Candidates are other paths with status `HEALTHY`. Prefer the preferred
     path, else the first candidate by name.
   - If a candidate exists, return `Decision(kind=FAILOVER, from_path,
     to_path, reason)` with `last_switch_ts = now`.
   - If none, return `Decision(kind=ALL_DEGRADED)` once per episode (guarded
     by `all_degraded_logged`), and keep the active path. The guard resets
     on any tick where the active path is `HEALTHY` or a switch happens.
4. Else if `active != preferred`, `healths[preferred].status == HEALTHY`,
   and `now - preferred_healthy_since >= failback_hold_s`, return
   `Decision(kind=FAILBACK, from_path=active, to_path=preferred)`.
5. Otherwise no decision.

Initial state: `active = preferred`, `last_switch_ts = -inf`.

## 10. Agent

`sdwan agent` on each edge:

1. Load config; pick the site from `SDWAN_SITE`; open the store.
2. Set the initial route to the remote LAN via the preferred path and write
   a `STARTUP` event and initial `path_state` rows.
3. Start threads: echo responder, throughput sink, one prober per path,
   one throughput-burst loop per path.
4. Main loop, once per second:
   - For each path, summarize the window, attach the latest throughput
     burst result if new, and write a `samples` row.
   - Feed the sample to that path's `PathMonitor`; on `changed`, write a
     `DEGRADED` or `RECOVERED` event.
   - Call `decide`. On `FAILOVER` or `FAILBACK`, call
     `net.set_route(remote_lan, via=peer_ip, dev=iface)` and write the
     event with `detail = "wan_a -> wan_b: loss 34.0% > 10.0%"`. On
     `ALL_DEGRADED`, write the event.
   - Upsert `path_state` for every path.
5. Any exception from `ip` or `tc` is logged, written as an `ERROR` event,
   and the loop continues. Socket errors in probers count as loss.

Threads, not asyncio, to keep the code readable.

## 11. Traffic

`sdwan traffic server [--port 7000]`: TCP server; for each 64-byte request
it replies with 64 bytes.

`sdwan traffic client --target IP [--port 7000] [--interval-ms 100]`:
keeps one persistent TCP connection. Every interval it sends a request and
waits up to 1 s for the reply. Success and latency are counted; a timeout or
socket error counts as a failure, closes the connection, and reconnects
with a 200 ms backoff. Once per second it writes a `traffic` row with `ok`,
`failed` and `avg_latency_ms` for that second and prints a one-line
summary to stdout.

## 12. Chaos

`sdwan degrade <path> [--loss PCT] [--delay MS] [--jitter MS] [--rate RATE]`
runs inside an edge container. It resolves the interface for the path's
`local_ip`, runs `tc qdisc replace dev <iface> root netem ...` with the
given options, and writes a `CHAOS` event whose detail lists the options.

`sdwan restore <path>` runs `tc qdisc del dev <iface> root` (ignoring
"no such qdisc") and writes a `CHAOS` event with detail `restore`.

Netem on the HQ edge's egress affects the HQ-to-branch direction of that
WAN. Because probes are round trips, both agents observe it.

## 13. Monitor and status

`sdwan monitor`: a `rich` Live layout refreshed twice a second from SQLite.

- Header: title, clock, sample count.
- Paths table: site, path, status (colored), active marker, latest RTT,
  jitter, loss, last known throughput, and the current reason.
- Traffic panel: success rate and average latency over the last 10 s.
- Events panel: the last ten events with time, site, kind, and detail.
- If the database does not exist yet, show "waiting for data".

`sdwan status [--json]`: one-shot print of `path_state` and the last ten
events. `--json` is used by the end-to-end script.

## 14. Host wrapper

`sdwanctl.py` (Python, no dependencies) forwards to Docker Compose so the
README works the same on Windows, macOS and Linux:

| Command                                          | Runs                                                   |
|--------------------------------------------------|--------------------------------------------------------|
| `python sdwanctl.py up`                          | `docker compose up -d --build`                         |
| `python sdwanctl.py down`                        | `docker compose down -v`                               |
| `python sdwanctl.py monitor`                     | `docker compose run --rm monitor`                      |
| `python sdwanctl.py status [--json]`             | `docker compose exec hq-edge sdwan status ...`         |
| `python sdwanctl.py degrade wan_a --loss 30 ...` | `docker compose exec hq-edge sdwan degrade ...`        |
| `python sdwanctl.py restore wan_a`               | `docker compose exec hq-edge sdwan restore wan_a`      |
| `python sdwanctl.py doctor`                      | one-off container check that netem and forwarding work |
| `python sdwanctl.py e2e`                         | `python scripts/e2e_test.py`                           |

`--site branch` on `status`, `degrade` and `restore` targets `branch-edge`.

`doctor` runs the project image with `NET_ADMIN`, applies
`tc qdisc add dev lo root netem delay 100ms`, pings `127.0.0.1` three times,
and reports whether RTT is roughly 200 ms. It also checks that
`sysctl net.ipv4.ip_forward=1` can be set. This is the first thing built,
because the whole design depends on it.

## 15. Timing budget

Degradation starts at `t0`. A sample at tick `T` covers `(T - 1.5 s, T - 0.5 s]`,
so the first window entirely inside the bad period is summarized at most
2.5 s after `t0` and the third at most 4.5 s after `t0` (corrected during
review; the original text said 1.5 s and 3.5 s). The route change is
immediate. For full loss or added delay every window is bad and failover
completes within 4.5 s. For random partial loss some windows happen to be
clean (at 40 % loss with 4 probes, about 13 %), so detection occasionally
needs an extra window: simulated median 3.6 s, 90th percentile under 5 s.
The README states the range, not a single figure. Failback after `restore`
takes `recover_needed` seconds plus `failback_hold_s`, roughly 25 s, which is
intentional damping against flapping.

## 16. Testing

Unit tests (pytest, host, no Docker):

- `test_config.py`: parses the shipped TOML; peer lookup; remote LAN.
- `test_probe.py`: window summary from synthetic probe records: all
  received, partial loss, none received, jitter of a known sequence, window
  boundary inclusion.
- `test_store.py`: schema creation on a temp file; write and read back each
  table; upsert semantics of `path_state`; traffic summary math.
- `test_detector.py`: stays healthy on a clean stream; degrades after
  `bad_needed` lossy samples; does not degrade on a single spike; RTT
  z-score anomaly fires only after warm-up; recovers only after
  `recover_needed` clean samples; baseline is frozen while degraded;
  throughput drop rule.
- `test_failover.py`: failover when active degraded and other healthy;
  no switch inside `min_dwell_s`; failback only after `failback_hold_s`;
  `ALL_DEGRADED` emitted once per episode; preferred chosen over other
  candidates.

End-to-end (`scripts/e2e_test.py`, host, needs Docker):

1. `docker compose up -d --build`; wait until `status --json` on `hq-edge`
   shows both paths `HEALTHY` (60 s limit).
2. Record `t0`; run `degrade wan_a --loss 40`.
3. Poll status every 0.5 s until `hq` active is `wan_b`. Assert elapsed
   under 10 s and print the measured value.
4. Run `restore wan_a`; poll until active is `wan_a`. Assert within 60 s.
5. Print a summary and `docker compose down -v` unless `--keep`.

## 17. README

Architecture diagram, one-paragraph explanation of how failover works,
quickstart (`doctor`, `up`, `monitor`, `degrade`, `restore`, `down`), a
demo transcript, a section on the detector and state machine with the
tunables, and how to run the tests.

## 18. Risks

- `sch_netem` must be present in the Docker Desktop kernel. `doctor`
  verifies it before anything else is built. If absent, the fallback is
  iptables `-m statistic` for loss only, and the spec would be revised.
- SQLite is shared across containers on one named volume inside a single
  Linux VM, which is a supported configuration. The monitor never reads
  through a Windows bind mount.
- Docker interface ordering is handled by IP lookup.
