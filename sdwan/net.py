"""Thin wrappers over `ip` and `tc` so the agent and chaos commands never
build shell commands themselves, plus a FakeNet for tests.

Interfaces are always found by the IP address they hold: Docker does not
guarantee that eth1 is the first network listed in the compose file.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable, Protocol


class NetError(RuntimeError):
    """An `ip` or `tc` command failed. The message carries the tool's stderr."""


class Net(Protocol):
    def iface_for_ip(self, ip: str) -> str: ...

    def set_route(self, dest: str, via: str, dev: str) -> None: ...

    def current_route(self, dest: str) -> str | None: ...

    def apply_netem(
        self,
        dev: str,
        loss: float | None = None,
        delay_ms: float | None = None,
        jitter_ms: float | None = None,
        rate: str | None = None,
    ) -> None: ...

    def clear_netem(self, dev: str) -> None: ...


# -- pure helpers -----------------------------------------------------------


def iface_from_addr_json(text: str, ip: str) -> str:
    """Name of the interface holding `ip`, from `ip -j addr show` output."""
    for iface in json.loads(text or "[]"):
        for addr in iface.get("addr_info", []):
            if addr.get("family") == "inet" and addr.get("local") == ip:
                return iface["ifname"]
    raise LookupError(f"no interface holds {ip}")


def netem_args(
    loss: float | None = None,
    delay_ms: float | None = None,
    jitter_ms: float | None = None,
    rate: str | None = None,
) -> list[str]:
    """Argument list for `tc qdisc ... netem`. tc wants jitter right after delay."""
    args: list[str] = []
    if loss is not None:
        args += ["loss", f"{loss:g}%"]
    if delay_ms is not None:
        args += ["delay", f"{delay_ms:g}ms"]
        if jitter_ms is not None:
            args.append(f"{jitter_ms:g}ms")
    elif jitter_ms is not None:
        raise ValueError("jitter requires delay")
    if rate:
        args += ["rate", str(rate)]
    if not args:
        raise ValueError("no netem options given")
    return args


# -- real implementation ----------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]

_MISSING_QDISC = ("handle of zero", "No such file or directory")


class IpRouteNet:
    def __init__(self, runner: Runner = subprocess.run):
        self._run = runner

    def _cmd(self, cmd: list[str], ok_stderr: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        proc = self._run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if any(s in err for s in ok_stderr):
                return proc
            raise NetError(f"{' '.join(cmd)}: {err or f'exit {proc.returncode}'}")
        return proc

    def iface_for_ip(self, ip: str) -> str:
        return iface_from_addr_json(self._cmd(["ip", "-j", "addr", "show"]).stdout, ip)

    def set_route(self, dest: str, via: str, dev: str) -> None:
        self._cmd(["ip", "route", "replace", dest, "via", via, "dev", dev])

    def current_route(self, dest: str) -> str | None:
        out = self._cmd(["ip", "-j", "route", "show", dest]).stdout
        routes = json.loads(out or "[]")
        return routes[0].get("gateway") if routes else None

    def apply_netem(
        self,
        dev: str,
        loss: float | None = None,
        delay_ms: float | None = None,
        jitter_ms: float | None = None,
        rate: str | None = None,
    ) -> None:
        args = netem_args(loss=loss, delay_ms=delay_ms, jitter_ms=jitter_ms, rate=rate)
        self._cmd(["tc", "qdisc", "replace", "dev", dev, "root", "netem", *args])

    def clear_netem(self, dev: str) -> None:
        self._cmd(["tc", "qdisc", "del", "dev", dev, "root"], ok_stderr=_MISSING_QDISC)


# -- test double ------------------------------------------------------------


class FakeNet:
    """In-memory Net: remembers routes and netem, records every call."""

    def __init__(self, ifaces: dict[str, str] | None = None):
        self.ifaces = dict(ifaces or {})
        self.routes: dict[str, tuple[str, str]] = {}
        self.netem: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.fail_next: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def iface_for_ip(self, ip: str) -> str:
        try:
            return self.ifaces[ip]
        except KeyError:
            raise LookupError(f"no interface holds {ip}") from None

    def set_route(self, dest: str, via: str, dev: str) -> None:
        self._maybe_fail()
        self.routes[dest] = (via, dev)
        self.calls.append(("set_route", dest, via, dev))

    def current_route(self, dest: str) -> str | None:
        route = self.routes.get(dest)
        return route[0] if route else None

    def apply_netem(
        self,
        dev: str,
        loss: float | None = None,
        delay_ms: float | None = None,
        jitter_ms: float | None = None,
        rate: str | None = None,
    ) -> None:
        self._maybe_fail()
        args = netem_args(loss=loss, delay_ms=delay_ms, jitter_ms=jitter_ms, rate=rate)
        self.netem[dev] = args
        self.calls.append(("apply_netem", dev, args))

    def clear_netem(self, dev: str) -> None:
        self._maybe_fail()
        self.netem.pop(dev, None)
        self.calls.append(("clear_netem", dev))
