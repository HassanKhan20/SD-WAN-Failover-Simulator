#!/usr/bin/env python3
"""End-to-end proof: bring the topology up from a clean volume, degrade WAN A,
measure how long the HQ edge takes to move its route to WAN B, restore WAN A,
and wait for the fail-back. Exits non-zero if any step misses its budget.

Run from the repo root with Docker available:

    python scripts/e2e_test.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = ["docker", "compose"]

HEALTHY_BUDGET_S = 60
FAILOVER_BUDGET_S = 10
FAILBACK_BUDGET_S = 60


def sh(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=capture, text=True)


def status(site: str = "hq") -> dict | None:
    p = subprocess.run(
        COMPOSE + ["exec", "-T", f"{site}-edge", "sdwan", "status", "--json"],
        cwd=ROOT, capture_output=True, text=True,
    )
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def wait_for(predicate, budget_s: float, poll_s: float = 0.5) -> tuple[bool, float]:
    """Poll until predicate(status) is true. Returns (ok, seconds elapsed)."""
    t0 = time.time()
    while time.time() - t0 < budget_s:
        d = status()
        if d and predicate(d):
            return True, time.time() - t0
        time.sleep(poll_s)
    return False, time.time() - t0


def all_healthy(d: dict) -> bool:
    paths = d["paths"]
    return len(paths) == 4 and all(p["status"] == "HEALTHY" for p in paths) and d["active"] == {
        "hq": "wan_a", "branch": "wan_a",
    }


def event_gap(d: dict, first_kind: str, second_kind: str, site: str = "hq") -> float | None:
    """Seconds between the newest `first_kind` event and the `second_kind`
    event that followed it, using the agents' own timestamps."""
    events = list(reversed(d["events"]))  # oldest first
    t_first = None
    for e in events:
        if e["site"] == site and e["kind"] == first_kind:
            t_first = e["ts"]
        elif e["site"] == site and e["kind"] == second_kind and t_first is not None:
            return e["ts"] - t_first
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    ap.add_argument("--loss", type=float, default=40.0, help="packet loss percent to inject")
    ns = ap.parse_args()
    results: list[str] = []
    failed = False

    try:
        sh(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)
        sh(COMPOSE + ["up", "-d", "--build"])

        ok, secs = wait_for(all_healthy, HEALTHY_BUDGET_S)
        results.append(f"startup: both sites healthy on wan_a after {secs:.1f}s" + ("" if ok else "  [FAIL]"))
        if not ok:
            return finish(results, True, ns.keep)

        t0 = time.time()
        sh(COMPOSE + ["exec", "-T", "hq-edge", "sdwan", "degrade", "wan_a", "--loss", str(ns.loss)])
        ok, secs = wait_for(lambda d: d["active"].get("hq") == "wan_b", FAILOVER_BUDGET_S)
        d = status() or {"events": []}
        gap = event_gap(d, "CHAOS", "FAILOVER")
        results.append(
            f"failover: hq active -> wan_b after {secs:.1f}s host wall clock"
            + (f", {gap:.1f}s by agent timestamps" if gap is not None else "")
            + (f" (budget {FAILOVER_BUDGET_S}s)" if ok else f"  [FAIL: not within {FAILOVER_BUDGET_S}s]")
        )
        failed |= not ok
        ok_b, secs_b = wait_for(lambda d: d["active"].get("branch") == "wan_b", 5)
        results.append(f"failover: branch active -> wan_b {'confirmed' if ok_b else 'MISSING'} (+{secs_b:.1f}s)")
        failed |= not ok_b

        traffic = subprocess.run(
            COMPOSE + ["exec", "-T", "hq-edge", "python", "-c",
                       "from sdwan.store import Store; import json; print(json.dumps(Store('/data/sdwan.db').traffic_summary(15)))"],
            cwd=ROOT, capture_output=True, text=True,
        )
        try:
            t = json.loads(traffic.stdout)
            results.append(
                f"traffic during failover (last 15s): {t['ok']} ok / {t['failed']} failed, "
                f"success {t['success_pct']:.1f}%"
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            results.append("traffic summary unavailable")

        t1 = time.time()
        sh(COMPOSE + ["exec", "-T", "hq-edge", "sdwan", "restore", "wan_a"])
        ok, secs = wait_for(
            lambda d: d["active"].get("hq") == "wan_a" and d["active"].get("branch") == "wan_a",
            FAILBACK_BUDGET_S,
        )
        results.append(
            f"failback: both sites back on wan_a after {secs:.1f}s"
            + (f" (budget {FAILBACK_BUDGET_S}s)" if ok else f"  [FAIL: not within {FAILBACK_BUDGET_S}s]")
        )
        failed |= not ok
        return finish(results, failed, ns.keep)
    except subprocess.CalledProcessError as exc:
        results.append(f"command failed: {exc}")
        return finish(results, True, ns.keep)


def finish(results: list[str], failed: bool, keep: bool) -> int:
    print("\n=== end-to-end summary ===")
    for line in results:
        print("  " + line)
    print("  result:", "FAIL" if failed else "PASS")
    if keep:
        print("  stack left running (--keep)")
    else:
        sh(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
