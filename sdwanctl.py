#!/usr/bin/env python3
"""Host-side helper for the SD-WAN Failover Simulator.

Forwards to Docker Compose so the demo commands are identical on Windows,
macOS and Linux. Standard library only.

    python sdwanctl.py doctor                  check netem and forwarding work here
    python sdwanctl.py up                      build and start the topology
    python sdwanctl.py monitor                 live terminal view (Ctrl-C to leave)
    python sdwanctl.py degrade wan_a --loss 30 --delay 150
    python sdwanctl.py restore wan_a
    python sdwanctl.py status [--json]
    python sdwanctl.py logs [service]
    python sdwanctl.py e2e [--keep]            end-to-end failover timing proof
    python sdwanctl.py down                    stop and remove everything
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ["docker", "compose"]
IMAGE = "sdwan-sim"


def run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd), file=sys.stderr, flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=capture, text=True)


def edge(site: str) -> str:
    return f"{site}-edge"


def exec_in(site: str, args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return run(COMPOSE + ["exec", "-T", edge(site), "sdwan", *args], check=False, capture=capture)


# -- commands -----------------------------------------------------------------


def cmd_up(ns) -> int:
    return run(COMPOSE + ["up", "-d", "--build"], check=False).returncode


def cmd_down(ns) -> int:
    return run(COMPOSE + ["down", "-v", "--remove-orphans"], check=False).returncode


def cmd_build(ns) -> int:
    return run(COMPOSE + ["build"], check=False).returncode


def cmd_logs(ns) -> int:
    cmd = COMPOSE + ["logs", "--tail", "50"]
    if ns.follow:
        cmd.append("-f")
    if ns.service:
        cmd.append(ns.service)
    return run(cmd, check=False).returncode


def cmd_monitor(ns) -> int:
    return run(COMPOSE + ["run", "--rm", "monitor"], check=False).returncode


def cmd_status(ns) -> int:
    args = ["status"] + (["--json"] if ns.json else [])
    proc = exec_in(ns.site, args, capture=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def cmd_degrade(ns) -> int:
    args = ["degrade", ns.path]
    for flag in ("loss", "delay", "jitter", "rate"):
        value = getattr(ns, flag)
        if value is not None:
            args += [f"--{flag}", str(value)]
    return exec_in(ns.site, args).returncode


def cmd_restore(ns) -> int:
    return exec_in(ns.site, ["restore", ns.path]).returncode


def cmd_doctor(ns) -> int:
    """Prove that tc netem and IP forwarding work in this Docker engine."""
    print("building image ...", file=sys.stderr)
    if run(COMPOSE + ["build", "-q", "hq-edge"], check=False).returncode != 0:
        print("doctor: image build failed", file=sys.stderr)
        return 1
    script = (
        "tc qdisc add dev lo root netem delay 100ms"
        " && ping -c 3 -i 0.2 127.0.0.1 | tail -1"
        " && cat /proc/sys/net/ipv4/ip_forward"
    )
    proc = run(
        [
            "docker", "run", "--rm", "--cap-add", "NET_ADMIN",
            "--sysctl", "net.ipv4.ip_forward=1", "--entrypoint", "sh", IMAGE, "-c", script,
        ],
        check=False,
        capture=True,
    )
    out = proc.stdout.strip()
    print(out)
    if proc.returncode != 0:
        print("doctor: FAIL\n" + proc.stderr, file=sys.stderr)
        return 1
    match = re.search(r"= [\d.]+/([\d.]+)/", out)
    avg = float(match.group(1)) if match else None
    forwarding = out.splitlines()[-1].strip() == "1"
    ok = avg is not None and 150 <= avg <= 400 and forwarding
    print(f"netem delay 100ms -> ping avg {avg} ms (expect ~200): {'ok' if avg and 150 <= avg <= 400 else 'FAIL'}")
    print(f"net.ipv4.ip_forward=1: {'ok' if forwarding else 'FAIL'}")
    print("doctor: PASS" if ok else "doctor: FAIL")
    return 0 if ok else 1


def cmd_e2e(ns) -> int:
    cmd = [sys.executable, os.path.join("scripts", "e2e_test.py")]
    if ns.keep:
        cmd.append("--keep")
    return run(cmd, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdwanctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("up").set_defaults(fn=cmd_up)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    logs = sub.add_parser("logs")
    logs.add_argument("service", nargs="?")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(fn=cmd_logs)
    sub.add_parser("monitor").set_defaults(fn=cmd_monitor)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--site", default="hq")
    status.set_defaults(fn=cmd_status)
    degrade = sub.add_parser("degrade")
    degrade.add_argument("path")
    degrade.add_argument("--loss", type=float)
    degrade.add_argument("--delay", type=float)
    degrade.add_argument("--jitter", type=float)
    degrade.add_argument("--rate")
    degrade.add_argument("--site", default="hq")
    degrade.set_defaults(fn=cmd_degrade)
    restore = sub.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--site", default="hq")
    restore.set_defaults(fn=cmd_restore)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    e2e = sub.add_parser("e2e")
    e2e.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    e2e.set_defaults(fn=cmd_e2e)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return ns.fn(ns)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
