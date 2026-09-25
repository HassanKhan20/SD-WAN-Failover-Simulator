"""Chaos injection: degrade or restore one WAN path with `tc netem` on the
edge's interface for that path, and record what was done as a CHAOS event.
"""

from __future__ import annotations

from sdwan.config import Topology
from sdwan.net import Net, netem_args
from sdwan.store import Store


def degrade(
    topology: Topology,
    site: str,
    path: str,
    net: Net,
    store: Store,
    ts: float,
    loss: float | None = None,
    delay: float | None = None,
    jitter: float | None = None,
    rate: str | None = None,
) -> str:
    """Apply netem on the interface holding the path's local IP. Returns the
    interface name. Raises KeyError for an unknown path and ValueError when
    no impairment was requested."""
    p = topology.site(site).paths[path]
    args = netem_args(loss=loss, delay_ms=delay, jitter_ms=jitter, rate=rate)
    dev = net.iface_for_ip(p.local_ip)
    net.apply_netem(dev, loss=loss, delay_ms=delay, jitter_ms=jitter, rate=rate)
    store.write_event(ts, site, "CHAOS", path=path, detail="degrade: " + " ".join(args))
    return dev


def restore(topology: Topology, site: str, path: str, net: Net, store: Store, ts: float) -> str:
    """Remove any netem from the path's interface. Returns the interface name."""
    p = topology.site(site).paths[path]
    dev = net.iface_for_ip(p.local_ip)
    net.clear_netem(dev)
    store.write_event(ts, site, "CHAOS", path=path, detail="restore")
    return dev
