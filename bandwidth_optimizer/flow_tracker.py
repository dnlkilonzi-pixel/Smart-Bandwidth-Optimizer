"""
Flow-level traffic intelligence.

Operates at the 5-tuple level (src_ip, dst_ip, src_port, dst_port, protocol)
to track per-flow statistics and derive behavioural scores that the optimizer
can use to make better priority decisions.

Key concepts
------------
FlowKey
    Immutable 5-tuple identifying a single bidirectional flow.  Both
    directions (A→B and B→A) map to the *same* key so counters accumulate
    for the whole conversation.

FlowRecord
    Per-flow statistics: byte count, packet count, inter-packet timing,
    burst detection.  Derives three scores:

    * **latency_score** – 0.0..1.0; high score → flow is latency-sensitive
      (small, frequent, bursty packets → VoIP / gaming pattern).
    * **bandwidth_score** – 0.0..1.0; high score → flow uses a lot of
      bandwidth (large continuous transfers → bulk download pattern).
    * **burst_score** – 0.0..1.0; high score → traffic arrives in bursts
      rather than at a steady rate.

FlowTracker
    Thread-safe flow table with TTL-based expiry.  Automatically removes
    idle flows.  Also provides ``priority_hint()`` which combines flow
    scores with the base priority to suggest an adjusted priority.

Integration
-----------
``BandwidthOptimizer.process()`` calls ``FlowTracker.update()`` *before* the
classifier, so flow history is available to the filter and scheduler stages.
The adjusted priority from ``priority_hint()`` may be stored back onto the
packet.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .config import TrafficPriority
from .classifier import Packet


# ─────────────────────────── 5-tuple flow key ────────────────────────────────

@dataclass(frozen=True)
class FlowKey:
    """
    Canonical, direction-independent 5-tuple flow identifier.

    The two endpoints are always stored in a deterministic order so that
    packets in both directions contribute to the same ``FlowRecord``.
    """
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol: str

    @classmethod
    def from_packet(cls, packet: Packet) -> "FlowKey":
        """Return the canonical FlowKey for *packet* (normalises direction)."""
        ep_a = (packet.src_ip, packet.src_port)
        ep_b = (packet.dst_ip, packet.dst_port)
        # Canonical order: sort so the "smaller" endpoint is always first
        if ep_a <= ep_b:
            return cls(
                src_ip=packet.src_ip,
                dst_ip=packet.dst_ip,
                src_port=packet.src_port,
                dst_port=packet.dst_port,
                protocol=packet.protocol.lower(),
            )
        return cls(
            src_ip=packet.dst_ip,
            dst_ip=packet.src_ip,
            src_port=packet.dst_port,
            dst_port=packet.src_port,
            protocol=packet.protocol.lower(),
        )


# ─────────────────────────── flow record ─────────────────────────────────────

@dataclass
class FlowRecord:
    """
    Per-flow statistics and behavioural scores.

    All timestamps use ``time.monotonic()``.
    """

    # ── identity / lifecycle ──────────────────────────────────────────────
    key: FlowKey
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)

    # ── counters ──────────────────────────────────────────────────────────
    packet_count: int = 0
    byte_count: int = 0

    # ── inter-packet timing (exponential moving average, seconds) ─────────
    _ema_ipt: float = 0.0          # inter-packet time EMA
    _last_pkt_time: float = 0.0
    _ipt_alpha: float = 0.2        # EMA smoothing factor

    # ── packet-size moving average ────────────────────────────────────────
    _ema_pkt_size: float = 0.0
    _size_alpha: float = 0.2

    # ── burst detection ───────────────────────────────────────────────────
    # Count packets in the current burst window
    _burst_window_start: float = 0.0
    _burst_window_pkts: int = 0
    _burst_window_duration: float = 0.1   # 100 ms window
    _max_burst_rate: float = 0.0           # peak packets/second seen

    # ── scores (updated by update()) ──────────────────────────────────────
    latency_score: float = 0.0
    bandwidth_score: float = 0.0
    burst_score: float = 0.0

    def update(self, packet: Packet) -> None:
        """
        Incorporate *packet* into the flow record and refresh all scores.

        Call this once per packet, in arrival order.
        """
        now = time.monotonic()
        self.last_seen = now
        self.packet_count += 1
        self.byte_count += packet.size_bytes

        # ── inter-packet time EMA ─────────────────────────────────────────
        if self._last_pkt_time > 0.0:
            ipt = now - self._last_pkt_time
            if self._ema_ipt == 0.0:
                self._ema_ipt = ipt
            else:
                self._ema_ipt = (
                    self._ipt_alpha * ipt
                    + (1 - self._ipt_alpha) * self._ema_ipt
                )
        self._last_pkt_time = now

        # ── packet size EMA ───────────────────────────────────────────────
        if self._ema_pkt_size == 0.0:
            self._ema_pkt_size = packet.size_bytes
        else:
            self._ema_pkt_size = (
                self._size_alpha * packet.size_bytes
                + (1 - self._size_alpha) * self._ema_pkt_size
            )

        # ── burst window ──────────────────────────────────────────────────
        if now - self._burst_window_start > self._burst_window_duration:
            if self._burst_window_duration > 0:
                rate = self._burst_window_pkts / self._burst_window_duration
                self._max_burst_rate = max(self._max_burst_rate, rate)
            self._burst_window_start = now
            self._burst_window_pkts = 0
        self._burst_window_pkts += 1

        # ── refresh scores ────────────────────────────────────────────────
        self._refresh_scores()

    def _refresh_scores(self) -> None:
        """Recompute latency / bandwidth / burst scores from current stats."""
        # Latency score: small packets + short inter-packet time → latency-sensitive
        # Normalise avg packet size: 0=very small (high score), 1500=large (low score)
        size_factor = max(0.0, 1.0 - self._ema_pkt_size / 1500.0)

        # IPT factor: ≤10 ms → high score; ≥500 ms → low score
        if self._ema_ipt > 0.0:
            ipt_factor = max(0.0, 1.0 - (self._ema_ipt - 0.01) / 0.49)
            ipt_factor = min(1.0, ipt_factor)
        else:
            ipt_factor = 0.0

        self.latency_score = min(1.0, 0.6 * size_factor + 0.4 * ipt_factor)

        # Bandwidth score: large packets + high byte rate → bandwidth-hungry
        duration = max(0.001, self.last_seen - self.first_seen)
        byte_rate = self.byte_count / duration          # bytes/s
        # Normalise to a 10 MB/s baseline; cap at 1.0
        self.bandwidth_score = min(1.0, byte_rate / (10 * 1024 * 1024))

        # Burst score: high peak burst rate → bursty traffic
        # Normalise to 1000 packets/s baseline
        self.burst_score = min(1.0, self._max_burst_rate / 1000.0)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.first_seen

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_seen

    def to_dict(self) -> dict:
        return {
            "src_ip": self.key.src_ip,
            "dst_ip": self.key.dst_ip,
            "src_port": self.key.src_port,
            "dst_port": self.key.dst_port,
            "protocol": self.key.protocol,
            "packet_count": self.packet_count,
            "byte_count": self.byte_count,
            "latency_score": round(self.latency_score, 3),
            "bandwidth_score": round(self.bandwidth_score, 3),
            "burst_score": round(self.burst_score, 3),
            "age_seconds": round(self.age_seconds, 3),
            "idle_seconds": round(self.idle_seconds, 3),
        }


# ─────────────────────────── flow tracker ────────────────────────────────────

class FlowTracker:
    """
    Thread-safe 5-tuple flow table with TTL-based expiry.

    Parameters
    ----------
    flow_ttl:
        Seconds of inactivity before a flow is removed from the table.
    max_flows:
        Maximum number of concurrent flows.  When the table is full the
        oldest (longest-idle) flow is evicted.
    priority_boost_threshold:
        Minimum ``latency_score`` for a flow to receive a one-level priority
        boost via ``priority_hint()``.
    priority_demote_threshold:
        Minimum ``bandwidth_score`` for a flow to receive a one-level priority
        demotion via ``priority_hint()``.

    Usage::

        tracker = FlowTracker()
        record  = tracker.update(packet)
        adjusted = tracker.priority_hint(packet.priority, record)
    """

    def __init__(
        self,
        flow_ttl: float = 120.0,
        max_flows: int = 65536,
        priority_boost_threshold: float = 0.7,
        priority_demote_threshold: float = 0.8,
    ) -> None:
        self._ttl = flow_ttl
        self._max_flows = max_flows
        self._boost_threshold = priority_boost_threshold
        self._demote_threshold = priority_demote_threshold
        self._table: Dict[FlowKey, FlowRecord] = {}
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────

    def update(self, packet: Packet) -> FlowRecord:
        """
        Update the flow record for *packet* and return it.

        Creates a new record if this is the first packet of the flow.
        Expired flows are purged lazily on each call.
        """
        key = FlowKey.from_packet(packet)
        with self._lock:
            self._evict_expired()
            if key not in self._table:
                if len(self._table) >= self._max_flows:
                    self._evict_oldest()
                self._table[key] = FlowRecord(key=key)
            record = self._table[key]
        record.update(packet)
        return record

    def get(self, packet: Packet) -> Optional[FlowRecord]:
        """Return the existing FlowRecord for *packet*, or ``None``."""
        key = FlowKey.from_packet(packet)
        with self._lock:
            return self._table.get(key)

    def priority_hint(
        self,
        base_priority: TrafficPriority,
        record: FlowRecord,
    ) -> TrafficPriority:
        """
        Suggest an adjusted priority based on flow behaviour scores.

        Rules (applied in order, first match wins):
        1. **Boost** – ``latency_score`` ≥ threshold AND base is not already
           CRITICAL → move one level higher.
        2. **Demote** – ``bandwidth_score`` ≥ threshold AND base is not already
           BACKGROUND → move one level lower.
        3. Otherwise return *base_priority* unchanged.
        """
        priorities = list(TrafficPriority)
        idx = priorities.index(base_priority)

        if record.latency_score >= self._boost_threshold and idx > 0:
            return priorities[idx - 1]

        if record.bandwidth_score >= self._demote_threshold and idx < len(priorities) - 1:
            return priorities[idx + 1]

        return base_priority

    def all_flows(self) -> list:
        """Return a snapshot of all active flow records as dicts."""
        with self._lock:
            return [r.to_dict() for r in self._table.values()]

    def flow_count(self) -> int:
        with self._lock:
            return len(self._table)

    def evict_expired(self) -> int:
        """Manually trigger TTL-based eviction; returns number evicted."""
        with self._lock:
            before = len(self._table)
            self._evict_expired()
            return before - len(self._table)

    # ── internal ──────────────────────────────────────────────────────────

    def _evict_expired(self) -> None:
        """Remove flows idle for more than ``_ttl`` seconds (must hold lock)."""
        now = time.monotonic()
        expired = [
            k for k, r in self._table.items()
            if (now - r.last_seen) > self._ttl
        ]
        for k in expired:
            del self._table[k]

    def _evict_oldest(self) -> None:
        """Remove the single most-idle flow (must hold lock)."""
        if not self._table:
            return
        oldest_key = max(self._table, key=lambda k: self._table[k].idle_seconds)
        del self._table[oldest_key]
