"""Live terminal view and one-shot status, both read straight from SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import time

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sdwan.store import Store

TITLE = "SD-WAN Failover Simulator"
STATUS_STYLE = {"HEALTHY": "bold green", "DEGRADED": "bold red", "UNKNOWN": "yellow"}
KIND_STYLE = {
    "FAILOVER": "bold red",
    "FAILBACK": "bold green",
    "DEGRADED": "red",
    "RECOVERED": "green",
    "ALL_DEGRADED": "bold magenta",
    "CHAOS": "yellow",
    "ERROR": "bold white on red",
    "STARTUP": "cyan",
}


def _fmt(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def status_dict(store: Store) -> dict:
    paths = store.path_states()
    return {
        "paths": paths,
        "active": {p["site"]: p["path"] for p in paths if p["active"]},
        "events": store.recent_events(10),
        "samples": store.sample_count(),
    }


def _paths_table(store: Store, states: list[dict]) -> Table:
    latest = {(r["site"], r["path"]): r for r in store.latest_samples()}
    throughput = store.last_throughput()
    table = Table(expand=True, show_edge=False, pad_edge=False)
    for col, justify in (
        ("Site", "left"), ("Path", "left"), ("Status", "left"), ("Active", "center"),
        ("RTT ms", "right"), ("Jitter ms", "right"), ("Loss %", "right"), ("Mbps", "right"),
        ("Reason", "left"),
    ):
        table.add_column(col, justify=justify, no_wrap=col != "Reason")
    for s in states:
        sample = latest.get((s["site"], s["path"]), {})
        table.add_row(
            s["site"],
            s["path"],
            Text(s["status"], style=STATUS_STYLE.get(s["status"], "")),
            Text("●", style="bold cyan") if s["active"] else "",
            _fmt(sample.get("rtt_ms")),
            _fmt(sample.get("jitter_ms")),
            _fmt(sample.get("loss_pct")),
            _fmt(throughput.get((s["site"], s["path"]))),
            s.get("reason") or "",
        )
    return table


def _traffic_panel(store: Store) -> Panel:
    t = store.traffic_summary(10)
    if t["success_pct"] is None:
        body = Text("no traffic recorded in the last 10 s", style="dim")
    else:
        style = "bold green" if t["success_pct"] >= 99 else "bold yellow" if t["success_pct"] >= 90 else "bold red"
        body = Text.assemble(
            ("success ", ""), (f"{t['success_pct']:.1f}%", style),
            ("   avg latency ", ""), (f"{_fmt(t['avg_latency_ms'])} ms", "bold"),
            (f"   ({t['ok']} ok / {t['failed']} failed)", "dim"),
        )
    return Panel(body, title="Traffic hq -> branch, last 10 s", title_align="left")


def _events_panel(events: list[dict]) -> Panel:
    table = Table(expand=True, show_edge=False, pad_edge=False, show_header=False)
    table.add_column("time", no_wrap=True, style="dim")
    table.add_column("site", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("path", no_wrap=True)
    table.add_column("detail")
    for e in events:
        table.add_row(
            _clock(e["ts"]),
            e["site"],
            Text(e["kind"], style=KIND_STYLE.get(e["kind"], "")),
            e.get("path") or "",
            e.get("detail") or "",
        )
    if not events:
        table.add_row("", "", Text("no events yet", style="dim"), "", "")
    return Panel(table, title="Events", title_align="left")


def build_layout(store: Store) -> RenderableType:
    states = store.path_states()
    if not states:
        return Panel(Text("waiting for data ...", style="yellow"), title=TITLE)
    header = Text.assemble(
        (TITLE, "bold"), ("   ", ""), (_clock(time.time()), "dim"),
        ("   samples: ", "dim"), (str(store.sample_count()), "dim"),
    )
    return Group(
        Panel(header),
        Panel(_paths_table(store, states), title="Paths", title_align="left"),
        _traffic_panel(store),
        _events_panel(store.recent_events(10)),
    )


def run_monitor(db_path: str, refresh_hz: float = 2.0) -> None:
    console = Console()
    store: Store | None = None
    with Live(build_layout_placeholder(), console=console, refresh_per_second=refresh_hz) as live:
        while True:
            if store is None:
                if not os.path.exists(db_path):
                    time.sleep(0.5)
                    continue
                store = Store(db_path)
            try:
                live.update(build_layout(store))
            except sqlite3.OperationalError as exc:
                live.update(Panel(Text(f"database busy: {exc}", style="yellow"), title=TITLE))
            time.sleep(1.0 / refresh_hz)


def build_layout_placeholder() -> RenderableType:
    return Panel(Text("waiting for data ...", style="yellow"), title=TITLE)


def print_status(db_path: str, as_json: bool = False) -> None:
    if not os.path.exists(db_path):
        if as_json:
            print(json.dumps({"paths": [], "active": {}, "events": [], "samples": 0}))
        else:
            print("waiting for data: no database yet at " + db_path)
        return
    store = Store(db_path)
    try:
        if as_json:
            print(json.dumps(status_dict(store)))
        else:
            Console().print(build_layout(store))
    finally:
        store.close()
