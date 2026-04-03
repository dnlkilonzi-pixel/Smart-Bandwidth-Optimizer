"""
Configuration for the Smart Bandwidth Optimizer.

Supports three deployment modes:
  - router      : embedded on a home/enterprise router
  - local_server: runs on a local server as a traffic proxy
  - isp_edge    : deployed at an ISP edge node for large-scale traffic shaping
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict


class DeploymentMode(Enum):
    ROUTER = "router"
    LOCAL_SERVER = "local_server"
    ISP_EDGE = "isp_edge"


class TrafficPriority(Enum):
    """Numeric value – lower number means higher priority."""
    CRITICAL = 1    # VoIP, real-time video, emergency services
    HIGH = 2        # Interactive web, DNS, SSH
    MEDIUM = 3      # Email, general HTTP
    LOW = 4         # Software updates, cloud sync
    BACKGROUND = 5  # P2P, bulk transfers, torrents


# Bandwidth budget allocated to each priority class (as a fraction of total).
# Must sum to ≤ 1.0; remainder is available as burst headroom.
DEFAULT_BANDWIDTH_BUDGET: Dict[TrafficPriority, float] = {
    TrafficPriority.CRITICAL:   0.30,
    TrafficPriority.HIGH:       0.30,
    TrafficPriority.MEDIUM:     0.20,
    TrafficPriority.LOW:        0.10,
    TrafficPriority.BACKGROUND: 0.05,
}


@dataclass
class OptimizerConfig:
    """Tuneable parameters for the bandwidth optimizer."""

    # ---------- deployment ------------------------------------------------
    mode: DeploymentMode = DeploymentMode.LOCAL_SERVER

    # ---------- bandwidth -------------------------------------------------
    # Maximum total bandwidth in bytes per second (0 = unlimited)
    total_bandwidth_bps: int = 10 * 1024 * 1024   # 10 MB/s default

    # Per-priority bandwidth budget fractions (must each be in [0, 1])
    bandwidth_budget: Dict[TrafficPriority, float] = field(
        default_factory=lambda: dict(DEFAULT_BANDWIDTH_BUDGET)
    )

    # ---------- scheduler -------------------------------------------------
    # Maximum packets waiting in the priority queue
    max_queue_size: int = 1024

    # ---------- compression -----------------------------------------------
    # Minimum payload size (bytes) before compression is attempted
    compression_threshold_bytes: int = 256
    # zlib compression level 1–9 (1=fastest, 9=best ratio)
    compression_level: int = 6

    # ---------- packet dropping (token bucket) ----------------------------
    # Token replenishment rate in bytes/second per priority class.
    # None means "derive from bandwidth_budget × total_bandwidth_bps".
    token_refill_rate: Dict[TrafficPriority, float] = field(
        default_factory=dict
    )
    # Token bucket capacity as a multiple of the refill rate
    token_bucket_capacity_multiplier: float = 2.0

    # ---------- RED (Random Early Detection) parameters -------------------
    # Fraction of queue full that triggers probabilistic early drop
    red_min_threshold: float = 0.50
    # Fraction of queue full beyond which all new packets are dropped
    red_max_threshold: float = 0.90

    def effective_refill_rate(self, priority: TrafficPriority) -> float:
        """Return the token refill rate (bytes/s) for *priority*."""
        if priority in self.token_refill_rate:
            return self.token_refill_rate[priority]
        budget = self.bandwidth_budget.get(priority, 0.0)
        return budget * self.total_bandwidth_bps
