"""Topology and tunables loaded from a TOML file into frozen dataclasses."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path as FsPath

DEFAULT_CONFIG = "/etc/sdwan/topology.toml"
REPO_CONFIG = FsPath(__file__).resolve().parents[1] / "config" / "topology.toml"


@dataclass(frozen=True)
class Path:
    """One WAN path as seen from one site."""

    name: str
    local_ip: str
    peer_ip: str


@dataclass(frozen=True)
class Site:
    name: str
    lan: str
    edge_lan_ip: str
    app_ip: str
    peer: str
    paths: dict[str, Path]


@dataclass(frozen=True)
class ProbeConfig:
    interval_ms: int
    timeout_ms: int
    echo_port: int
    throughput_port: int
    throughput_interval_s: float
    throughput_bytes: int


@dataclass(frozen=True)
class DetectorConfig:
    window: int
    bad_needed: int
    recover_needed: int
    warmup_samples: int
    ewma_alpha: float
    loss_pct_max: float
    rtt_ms_max: float
    jitter_ms_max: float
    zscore_max: float
    min_std_ms: float
    throughput_drop_ratio: float


@dataclass(frozen=True)
class FailoverConfig:
    preferred: str
    min_dwell_s: float
    failback_hold_s: float


@dataclass(frozen=True)
class Topology:
    sites: dict[str, Site]
    probe: ProbeConfig
    detector: DetectorConfig
    failover: FailoverConfig

    def site(self, name: str) -> Site:
        return self.sites[name]

    def remote_lan(self, site: str) -> str:
        """LAN subnet of the site's peer, i.e. the destination the edge routes."""
        return self.sites[self.sites[site].peer].lan


def _site(name: str, raw: dict) -> Site:
    paths = {
        pname: Path(name=pname, local_ip=p["local_ip"], peer_ip=p["peer_ip"])
        for pname, p in raw["paths"].items()
    }
    return Site(
        name=name,
        lan=raw["lan"],
        edge_lan_ip=raw["edge_lan_ip"],
        app_ip=raw["app_ip"],
        peer=raw["peer"],
        paths=paths,
    )


def load_topology(path: str | os.PathLike) -> Topology:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return Topology(
        sites={name: _site(name, s) for name, s in raw["sites"].items()},
        probe=ProbeConfig(**raw["probe"]),
        detector=DetectorConfig(**raw["detector"]),
        failover=FailoverConfig(**raw["failover"]),
    )


def default_config_path() -> str:
    """SDWAN_CONFIG, else the container path, else the repo copy for host use."""
    env = os.environ.get("SDWAN_CONFIG")
    if env:
        return env
    if FsPath(DEFAULT_CONFIG).exists():
        return DEFAULT_CONFIG
    return str(REPO_CONFIG)
