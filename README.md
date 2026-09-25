# SD-WAN Failover Simulator

A two-site company network in Docker with two independent WAN paths between
the sites. Python agents on each edge router probe both paths continuously,
stream round-trip time, jitter, packet loss and throughput into SQLite, flag a
degraded path from that telemetry, and move the kernel route to the healthy
path automatically. Failover takes **3 to 5 seconds** from injecting 40 %
packet loss to the route change (3.4 s in the recorded run below), and
application traffic keeps flowing.

```
 hq_lan 10.1.0.0/24                                    branch_lan 10.2.0.0/24
 +--------+       +---------+   wan_a 10.100.0.0/24   +-------------+       +------------+
 | hq-app |-------| hq-edge |=========================| branch-edge |-------| branch-app |
 |  .10   |       |   .2    |   wan_b 10.200.0.0/24   |     .2      |       |    .10     |
 +--------+       +---------+=========================+-------------+       +------------+
  TCP client       agent:                               agent:               TCP server
  every 100 ms     probe -> detect -> ip route replace   probe -> detect -> ip route replace
```

Everything runs in one `docker compose up`. The failover is real Linux
routing, not an application-level switch: the edge containers forward
packets with `ip_forward`, and the agent changes the next hop with
`ip route replace` when a path goes bad.

## Quickstart

Requirements: Docker Desktop (or any Docker engine with Compose v2) and
Python 3.12+ on the host for the helper script and tests.

```
python sdwanctl.py doctor          # proves tc netem and forwarding work on this Docker engine
python sdwanctl.py up              # builds the image, starts 4 networks and 4 containers
python sdwanctl.py monitor         # live terminal view (Ctrl-C to leave it)
```

In a second terminal, break WAN A and watch the monitor:

```
python sdwanctl.py degrade wan_a --loss 40
python sdwanctl.py restore wan_a
python sdwanctl.py down
```

`degrade` accepts `--loss PCT`, `--delay MS`, `--jitter MS` and `--rate 1mbit`
in any combination, and `--site branch` to impair the branch side instead.

## What a demo looks like

Event log from the HQ edge during one degrade/restore cycle:

```
11:41:03  hq      STARTUP    wan_a   route 10.2.0.0/24 via wan_a
11:41:33  hq      CHAOS      wan_a   degrade: loss 40%
11:41:37  hq      DEGRADED   wan_a   loss 50.0% > 10.0%
11:41:37  hq      FAILOVER   wan_b   wan_a -> wan_b: loss 50.0% > 10.0%
11:41:37  branch  FAILOVER   wan_b   wan_a -> wan_b: loss 50.0% > 10.0%
11:41:59  hq      CHAOS      wan_a   restore
11:42:09  hq      RECOVERED  wan_a
11:42:25  hq      FAILBACK   wan_a   wan_b -> wan_a: wan_a healthy for 16s
```

The traffic client on `hq-app` at the same time. Latency climbs while TCP
retransmits through the lossy path, then drops back as soon as the route
moves:

```
16:41:34 ok=9   failed=0   success=100.0% latency=  23.4 ms
16:41:35 ok=8   failed=0   success=100.0% latency=  78.2 ms
16:41:36 ok=5   failed=0   success=100.0% latency= 125.8 ms
16:41:37 ok=4   failed=0   success=100.0% latency= 158.8 ms
16:41:38 ok=10  failed=0   success=100.0% latency=   0.4 ms
```

`python sdwanctl.py e2e` runs this whole cycle from a clean volume and
asserts the timings:

```
=== end-to-end summary ===
  startup: both sites healthy on wan_a after 1.1s
  failover: hq active -> wan_b after 3.6s host wall clock, 3.4s by agent timestamps (budget 10s)
  failover: branch active -> wan_b confirmed (+0.3s)
  traffic during failover (last 15s): 23 ok / 2 failed, success 92.0%
  failback: both sites back on wan_a after 27.2s (budget 60s)
  result: PASS
```

## How it works

**Probing.** Each edge agent sends a 16-byte UDP datagram every 250 ms over
each WAN path to the peer edge, which echoes it back. Because the peer's WAN
address is directly on that Docker network, the probe and its reply always
travel the path being measured. Once a second the agent summarizes the most
recent full second whose 500 ms timeouts have all expired into RTT, jitter
(mean absolute difference between consecutive RTTs) and loss. Every 5 s it
also pushes a 256 KiB TCP burst to the peer to measure throughput.

**Detection.** A sample is bad if any rule fires: loss above 10 %, RTT above
250 ms, RTT more than 3 standard deviations above the path's own EWMA
baseline, jitter above 50 ms, or throughput below 25 % of its baseline. The
baseline learns only from clean samples, so a bad episode does not poison
it. A path becomes DEGRADED when 3 of the last 5 samples are bad and HEALTHY
again only after 10 consecutive clean samples. Every verdict carries a
human-readable reason that shows up in the monitor and the event log.

**Failover.** A small state machine decides once a second. If the active
path is DEGRADED and another path is HEALTHY, it fails over. If the preferred
path (WAN A) has been HEALTHY for 15 s while traffic is on WAN B, it fails
back. A 5 s dwell after any switch prevents flapping. If every path is
degraded it stays put and logs `ALL_DEGRADED` once. Both edges run this
independently and converge because they observe the same round trips.

**Timing budget.** Each one-second sample covers probes sent between 1.5 s
and 0.5 s earlier, so the first sample that lies entirely inside the bad
period is written at most 2.5 s after loss starts and the third at most
4.5 s. The route change is immediate. With random 40 % loss, about one
window in eight happens to see no loss and counts as clean, so detection
occasionally needs an extra second: across simulated runs the median is
about 3.6 s and nine runs in ten finish under 5 s. Full loss or added delay
detects in the fixed 4.5 s bound. The recorded end-to-end run measured 3.4 s.

**Storage.** One SQLite file in WAL mode on a shared Docker volume, written
by both agents and the traffic client and read by the monitor. Tables:
`samples`, `events`, `traffic`, `path_state`.

## Tunables

All in [config/topology.toml](config/topology.toml), baked into the image:

| Section      | Key                     | Default | Meaning                                  |
|--------------|-------------------------|---------|------------------------------------------|
| `probe`      | `interval_ms`           | 250     | probe send interval per path             |
| `probe`      | `timeout_ms`            | 500     | reply later than this counts as lost     |
| `detector`   | `window` / `bad_needed` | 5 / 3   | samples judged; bad ones needed to flag  |
| `detector`   | `recover_needed`        | 10      | consecutive clean samples to recover     |
| `detector`   | `loss_pct_max`          | 10.0    | absolute loss threshold                  |
| `detector`   | `rtt_ms_max`            | 250.0   | absolute RTT threshold                   |
| `detector`   | `zscore_max`            | 3.0     | RTT deviation from baseline              |
| `failover`   | `preferred`             | wan_a   | path to fail back to                     |
| `failover`   | `min_dwell_s`           | 5       | no two switches closer than this         |
| `failover`   | `failback_hold_s`       | 15      | preferred must be healthy this long      |

## Project layout

```
sdwan/
  config.py     TOML topology -> frozen dataclasses
  probe.py      UDP echo responder, prober, window summary, throughput burst
  store.py      SQLite schema, writers, readers
  detector.py   per-path health: thresholds + EWMA z-score + hysteresis (pure)
  failover.py   path selection state machine (pure)
  agent.py      wires probe -> store -> detector -> failover -> ip route
  traffic.py    TCP request/response server and client
  chaos.py      degrade / restore with tc netem
  monitor.py    rich live view and `sdwan status --json`
  cli.py        the `sdwan` command that every container runs
scripts/e2e_test.py   end-to-end proof used by `sdwanctl.py e2e`
sdwanctl.py           host-side wrapper around docker compose
docker-compose.yml    4 networks, 4 services + monitor, 1 volume
```

## Tests

```
pip install -e ".[dev]"
python -m pytest          # 94 unit tests, no Docker needed, ~9 s
python sdwanctl.py e2e    # needs Docker, ~90 s
```

The detector, failover state machine and probe summary are pure functions
tested exhaustively. The probe runtime and traffic generator are tested over
loopback. The agent is tested against a fake network layer that records
`ip route` calls, including a route change that fails and is retried.

## Inspecting the network yourself

```
docker compose exec hq-edge ip route show 10.2.0.0/24     # current next hop
docker compose exec hq-edge tc qdisc show                 # active netem, if any
docker compose exec hq-edge tcpdump -ni eth1 udp port 5001 -c 5
docker compose exec hq-edge sdwan status                  # same view as the monitor, once
```

## Design notes

The design spec and implementation plan this was built from are in
[docs/superpowers/](docs/superpowers/).
