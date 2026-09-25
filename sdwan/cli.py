"""Command-line entry point: `sdwan <command>`. One CLI runs every role in the
containers (agent, traffic, monitor, status, degrade, restore)."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from sdwan.config import default_config_path, load_topology

DEFAULT_DB = "/data/sdwan.db"


def _db_default() -> str:
    return os.environ.get("SDWAN_DB", DEFAULT_DB)


def _site_default() -> str | None:
    return os.environ.get("SDWAN_SITE")


def _add_common(p: argparse.ArgumentParser, site: bool = True) -> None:
    p.add_argument("--config", default=None, help="topology TOML (default: $SDWAN_CONFIG or /etc/sdwan/topology.toml)")
    p.add_argument("--db", default=None, help="SQLite path (default: $SDWAN_DB or /data/sdwan.db)")
    if site:
        p.add_argument("--site", default=None, help="site name (default: $SDWAN_SITE)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdwan", description="SD-WAN failover simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_common(sub.add_parser("agent", help="run the edge agent: probe, detect, fail over"))

    traffic = sub.add_parser("traffic", help="application traffic server or client")
    roles = traffic.add_subparsers(dest="role", required=True)
    server = roles.add_parser("server", help="TCP request/response server")
    server.add_argument("--port", type=int, default=7000)
    server.add_argument("--bind", default="0.0.0.0")
    client = roles.add_parser("client", help="persistent TCP client that records success rate")
    client.add_argument("--target", required=True)
    client.add_argument("--port", type=int, default=7000)
    client.add_argument("--interval-ms", type=int, default=100)
    _add_common(client)

    _add_common(sub.add_parser("monitor", help="live terminal view"), site=False)

    status = sub.add_parser("status", help="one-shot path state and recent events")
    status.add_argument("--json", action="store_true")
    _add_common(status, site=False)

    degrade = sub.add_parser("degrade", help="impair a WAN path with tc netem")
    degrade.add_argument("path")
    degrade.add_argument("--loss", type=float, help="packet loss percent")
    degrade.add_argument("--delay", type=float, help="added delay in ms")
    degrade.add_argument("--jitter", type=float, help="delay jitter in ms (needs --delay)")
    degrade.add_argument("--rate", help="rate limit, e.g. 1mbit")
    _add_common(degrade)

    restore = sub.add_parser("restore", help="remove impairment from a WAN path")
    restore.add_argument("path")
    _add_common(restore)
    return parser


def _site(ns: argparse.Namespace) -> str:
    site = ns.site or _site_default()
    if not site:
        raise SystemExit("error: --site or SDWAN_SITE is required")
    return site


def _topology(ns: argparse.Namespace):
    return load_topology(ns.config or default_config_path())


def _db(ns: argparse.Namespace) -> str:
    return ns.db or _db_default()


def cmd_agent(ns: argparse.Namespace) -> int:
    from sdwan.agent import Agent
    from sdwan.net import IpRouteNet
    from sdwan.store import Store

    Agent(_topology(ns), _site(ns), IpRouteNet(), Store(_db(ns))).run()
    return 0


def cmd_traffic(ns: argparse.Namespace) -> int:
    from sdwan.traffic import TrafficClient, run_server

    stop = threading.Event()
    if ns.role == "server":
        print(f"traffic server listening on {ns.bind}:{ns.port}", flush=True)
        run_server(ns.port, stop, bind_ip=ns.bind)
        return 0
    from sdwan.store import Store

    site = ns.site or _site_default() or "hq"
    client = TrafficClient(
        ns.target,
        ns.port,
        interval_ms=ns.interval_ms,
        site=site,
        store=Store(_db(ns)),
        out=lambda line: print(line, flush=True),
    )
    print(f"traffic client -> {ns.target}:{ns.port} every {ns.interval_ms} ms", flush=True)
    client.run(stop)
    return 0


def cmd_monitor(ns: argparse.Namespace) -> int:
    from sdwan.monitor import run_monitor

    run_monitor(_db(ns))
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    from sdwan.monitor import print_status

    print_status(_db(ns), as_json=ns.json)
    return 0


def cmd_degrade(ns: argparse.Namespace) -> int:
    from sdwan.chaos import degrade
    from sdwan.net import IpRouteNet
    from sdwan.store import Store

    site = _site(ns)
    dev = degrade(
        _topology(ns), site, ns.path, IpRouteNet(), Store(_db(ns)), time.time(),
        loss=ns.loss, delay=ns.delay, jitter=ns.jitter, rate=ns.rate,
    )
    print(f"degraded {ns.path} on {site} ({dev})")
    return 0


def cmd_restore(ns: argparse.Namespace) -> int:
    from sdwan.chaos import restore
    from sdwan.net import IpRouteNet
    from sdwan.store import Store

    site = _site(ns)
    dev = restore(_topology(ns), site, ns.path, IpRouteNet(), Store(_db(ns)), time.time())
    print(f"restored {ns.path} on {site} ({dev})")
    return 0


COMMANDS = {
    "agent": cmd_agent,
    "traffic": cmd_traffic,
    "monitor": cmd_monitor,
    "status": cmd_status,
    "degrade": cmd_degrade,
    "restore": cmd_restore,
}


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return COMMANDS[ns.command](ns)
    except KeyboardInterrupt:
        return 130
    except (KeyError, ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
