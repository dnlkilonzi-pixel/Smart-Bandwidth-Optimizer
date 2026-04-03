"""
SLA Enforcement and Backpressure Model.

Two complementary systems that provide **deterministic performance guarantees**:

1. **SLA Enforcement** – per-priority latency ceilings
   ~~~~~~~~~~~~~~~~~~
   * ``SLAConfig`` – maps each ``TrafficPriority`` to a maximum allowed
     *pipeline latency* (how long ``process()`` may take, in µs) and a
     *sojourn time* ceiling (how long a packet may wait in the queue, in ms).
   * ``SLAMonitor`` – wraps any ``BandwidthOptimizer`` and enforces both limits:
     - ``process()`` is timed; if the call exceeds the priority's ceiling a
       ``SLAViolation`` is recorded.
     - ``dequeue()`` checks each packet's ``enqueued_at`` stamp (set by the
       scheduler); stale packets are silently expired and the next one returned.
   * ``SLAStats`` – aggregate violation counters accessible via
     ``SLAMonitor.sla_stats()``.

2. **Backpressure Model** – upstream throttle signals
   ~~~~~~~~~~~~~~~~~~~
   * ``BackpressureLevel`` – three-level signal: NONE / SOFT / HARD.
   * ``BackpressureState`` – snapshot of queue fill ratio, arrival/drain rates,
     and computed throttle recommendation.
   * ``BackpressureMonitor`` – wraps an optimizer reference and samples stats
     at regular intervals to compute a rolling backpressure state.  Callers
     poll ``state()`` and can honour the ``recommended_throttle_pct`` to slow
     their upstream packet injection rate.

Default SLA ceilings (conservative starting points – tune for your hardware)::

    CRITICAL   pipeline ≤   500 µs,  sojourn ≤    50 ms
    HIGH       pipeline ≤  1000 µs,  sojourn ≤   100 ms
    MEDIUM     pipeline ≤  2000 µs,  sojourn ≤   500 ms
    LOW        pipeline ≤  5000 µs,  sojourn ≤  2000 ms
    BACKGROUND pipeline ≤ 10000 µs,  sojourn ≤  5000 ms

Usage::

    from bandwidth_optimizer.sla import SLAConfig, SLAMonitor
    from bandwidth_optimizer.sla import BackpressureMonitor

    mon = SLAMonitor(optimizer, SLAConfig())
    result = mon.process(packet)   # timed; violations recorded automatically
    next_pkt = mon.dequeue()       # sojourn-expired packets are skipped
    print(mon.sla_stats())

    bp = BackpressureMonitor(optimizer)
    state = bp.update()
    print(state.level, state.recommended_throttle_pct)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from .classifier import Packet
from .config import TrafficPriority
from .optimizer import BandwidthOptimizer, ProcessResult


# ─────────────────────────── default SLA budgets ─────────────────────────────

#: Maximum pipeline processing time per priority (µs).
DEFAULT_SLA_PIPELINE_CEILING_US: Dict[TrafficPriority, float] = {
    TrafficPriority.CRITICAL:      500.0,   # 0.5 ms
    TrafficPriority.HIGH:         1000.0,   # 1 ms
    TrafficPriority.MEDIUM:       2000.0,   # 2 ms
    TrafficPriority.LOW:          5000.0,   # 5 ms
    TrafficPriority.BACKGROUND:  10000.0,   # 10 ms
}

#: Maximum time a packet may wait in the priority queue per priority (ms).
DEFAULT_SLA_SOJOURN_CEILING_MS: Dict[TrafficPriority, float] = {
    TrafficPriority.CRITICAL:      50.0,    # 50 ms
    TrafficPriority.HIGH:         100.0,    # 100 ms
    TrafficPriority.MEDIUM:       500.0,    # 500 ms
    TrafficPriority.LOW:         2000.0,    # 2 s
    TrafficPriority.BACKGROUND:  5000.0,    # 5 s
}


# ─────────────────────────── SLA configuration ───────────────────────────────

@dataclass
class SLAConfig:
    """
    Per-priority latency budgets.

    Both dicts are fully optional – any priority class absent from the dict
    has its ceiling enforcement disabled (no violations recorded for it).

    Attributes
    ----------
    pipeline_ceiling_us:
        Max time (µs) the full ``process()`` call may take per priority.
    sojourn_ceiling_ms:
        Max time (ms) a packet may wait in the queue before being expired.
    max_violations_tracked:
        Maximum number of ``SLAViolation`` objects held in the recent-violations
        ring buffer (oldest are discarded when full).
    """
    pipeline_ceiling_us: Dict[TrafficPriority, float] = field(
        default_factory=lambda: dict(DEFAULT_SLA_PIPELINE_CEILING_US)
    )
    sojourn_ceiling_ms: Dict[TrafficPriority, float] = field(
        default_factory=lambda: dict(DEFAULT_SLA_SOJOURN_CEILING_MS)
    )
    max_violations_tracked: int = 200


# ─────────────────────────── violation record ────────────────────────────────

@dataclass
class SLAViolation:
    """
    A single SLA breach.

    Attributes
    ----------
    violation_type:
        ``"pipeline"`` – processing took too long; ``"sojourn"`` – waited too
        long in the queue.
    priority:
        Priority class of the offending packet.
    ceiling:
        The applicable limit (µs for pipeline, ms for sojourn).
    actual:
        Measured value (same units as ``ceiling``).
    timestamp:
        ``time.time()`` at the moment of detection.
    """
    violation_type: str       # "pipeline" | "sojourn"
    priority: TrafficPriority
    ceiling: float            # µs (pipeline) or ms (sojourn)
    actual: float             # same units
    timestamp: float = field(default_factory=time.time)

    @property
    def overage_pct(self) -> float:
        """How much over the ceiling the actual value was (0.0 = exact hit)."""
        return (self.actual - self.ceiling) / self.ceiling if self.ceiling else 0.0

    def to_dict(self) -> dict:
        return {
            "type": self.violation_type,
            "priority": self.priority.name,
            "ceiling": self.ceiling,
            "actual": round(self.actual, 2),
            "overage_pct": round(self.overage_pct * 100, 1),
            "timestamp": self.timestamp,
        }


# ─────────────────────────── SLA stats ───────────────────────────────────────

@dataclass
class SLAStats:
    """Aggregate SLA violation counters."""
    pipeline_violations: Dict[str, int] = field(default_factory=dict)
    sojourn_violations: Dict[str, int] = field(default_factory=dict)
    packets_expired: int = 0     # sojourn-expired, dropped at dequeue
    total_violations: int = 0
    recent_violations: List[SLAViolation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_violations": self.total_violations,
            "pipeline_violations_by_priority": self.pipeline_violations,
            "sojourn_violations_by_priority": self.sojourn_violations,
            "packets_expired": self.packets_expired,
            "recent_violations": [v.to_dict() for v in self.recent_violations[-10:]],
        }


# ─────────────────────────── SLA monitor ─────────────────────────────────────

class SLAMonitor:
    """
    SLA-enforcing wrapper around ``BandwidthOptimizer``.

    Instruments each pipeline call and queue dequeue to detect and record
    latency ceiling breaches.

    ``process()`` – measures total pipeline time, records pipeline violations.
    ``dequeue()`` – checks sojourn time of each dequeued packet; expired packets
    are *discarded* (not returned to the caller) and ``SLAStats.packets_expired``
    is incremented.  The loop continues until a valid packet is found or the
    queue is empty.

    Thread-safe (all counters protected by an internal lock).
    """

    def __init__(
        self,
        optimizer: BandwidthOptimizer,
        config: Optional[SLAConfig] = None,
    ) -> None:
        self._optimizer = optimizer
        self._config = config or SLAConfig()
        self._lock = threading.Lock()

        # Violation counters per priority (keyed by TrafficPriority.name)
        self._pipeline_violations: Dict[str, int] = {
            p.name: 0 for p in TrafficPriority
        }
        self._sojourn_violations: Dict[str, int] = {
            p.name: 0 for p in TrafficPriority
        }
        self._packets_expired: int = 0
        self._total_violations: int = 0
        # Ring buffer of recent violations
        self._recent: Deque[SLAViolation] = deque(
            maxlen=self._config.max_violations_tracked
        )

    # ── core interface ────────────────────────────────────────────────────

    def process(self, packet: Packet) -> ProcessResult:
        """
        Process *packet*, measuring pipeline latency and flagging violations.

        Never raises (delegates all exceptions to the underlying optimizer).
        """
        t_start = time.perf_counter_ns()
        result = self._optimizer.process(packet)
        elapsed_us = (time.perf_counter_ns() - t_start) / 1000.0

        priority = packet.priority
        if priority is not None:
            ceiling_us = self._config.pipeline_ceiling_us.get(priority)
            if ceiling_us is not None and elapsed_us > ceiling_us:
                self._record_violation(
                    SLAViolation(
                        violation_type="pipeline",
                        priority=priority,
                        ceiling=ceiling_us,
                        actual=elapsed_us,
                    )
                )

        return result

    def dequeue(self) -> Optional[Packet]:
        """
        Pull the next forwarding-ready packet, skipping sojourn-expired ones.

        :returns: A fresh packet, or ``None`` if the queue is empty.
        """
        while True:
            packet = self._optimizer.dequeue()
            if packet is None:
                return None

            priority = packet.priority
            enqueued_at = packet.enqueued_at

            if priority is not None and enqueued_at is not None:
                ceiling_ms = self._config.sojourn_ceiling_ms.get(priority)
                if ceiling_ms is not None:
                    sojourn_ms = (time.monotonic() - enqueued_at) * 1000.0
                    if sojourn_ms > ceiling_ms:
                        v = SLAViolation(
                            violation_type="sojourn",
                            priority=priority,
                            ceiling=ceiling_ms,
                            actual=sojourn_ms,
                        )
                        self._record_violation(v)
                        with self._lock:
                            self._packets_expired += 1
                        continue   # discard expired packet, try next

            return packet

    def sla_stats(self) -> SLAStats:
        """Return a snapshot of all SLA counters."""
        with self._lock:
            return SLAStats(
                pipeline_violations=dict(self._pipeline_violations),
                sojourn_violations=dict(self._sojourn_violations),
                packets_expired=self._packets_expired,
                total_violations=self._total_violations,
                recent_violations=list(self._recent),
            )

    def reset_sla_stats(self) -> None:
        """Reset all violation counters."""
        with self._lock:
            for p in TrafficPriority:
                self._pipeline_violations[p.name] = 0
                self._sojourn_violations[p.name] = 0
            self._packets_expired = 0
            self._total_violations = 0
            self._recent.clear()

    # ── delegate optimizer interface ──────────────────────────────────────

    def stats(self) -> dict:
        """Return optimizer stats merged with SLA stats."""
        base = self._optimizer.stats()
        base["sla"] = self.sla_stats().to_dict()
        return base

    def reset_stats(self) -> None:
        self._optimizer.reset_stats()
        self.reset_sla_stats()

    @property
    def optimizer(self) -> BandwidthOptimizer:
        return self._optimizer

    @property
    def config(self) -> SLAConfig:
        return self._config

    # ── internal ──────────────────────────────────────────────────────────

    def _record_violation(self, v: SLAViolation) -> None:
        with self._lock:
            if v.violation_type == "pipeline":
                self._pipeline_violations[v.priority.name] += 1
            else:
                self._sojourn_violations[v.priority.name] += 1
            self._total_violations += 1
            self._recent.append(v)


# ═══════════════════════════════════════════════════════════════════════════
#  Backpressure Model
# ═══════════════════════════════════════════════════════════════════════════

class BackpressureLevel(Enum):
    """
    Three-level upstream throttle signal.

    NONE
        Queue is healthy; senders may transmit at full rate.
    SOFT
        Queue is filling; senders should reduce rate by
        ``BackpressureState.recommended_throttle_pct`` percent.
    HARD
        Queue is near capacity; senders should pause transmission
        entirely until the signal drops to SOFT or NONE.
    """
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass
class BackpressureState:
    """
    Point-in-time backpressure snapshot.

    Attributes
    ----------
    level:
        Current backpressure severity.
    queue_fill_ratio:
        Fraction of the queue currently occupied (0.0–1.0).
    arrival_rate_pps:
        Estimated packet arrival rate (packets/second) over the last window.
    drain_rate_pps:
        Estimated packet drain rate (packets/second) over the last window.
    recommended_throttle_pct:
        Suggested sender slowdown as a percentage (0 = no throttle,
        100 = stop sending).  Computed as a linear function of queue fill
        between the soft and hard thresholds.
    """
    level: BackpressureLevel
    queue_fill_ratio: float
    arrival_rate_pps: float
    drain_rate_pps: float
    recommended_throttle_pct: float

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "queue_fill_ratio": round(self.queue_fill_ratio, 4),
            "arrival_rate_pps": round(self.arrival_rate_pps, 1),
            "drain_rate_pps": round(self.drain_rate_pps, 1),
            "recommended_throttle_pct": round(self.recommended_throttle_pct, 1),
        }


class BackpressureMonitor:
    """
    Formalised upstream back-pressure signalling.

    Samples the optimizer's stats on each ``update()`` call and computes
    arrival/drain rates over a rolling window.  The current
    ``BackpressureState`` reflects the latest sample.

    Parameters
    ----------
    optimizer:
        The ``BandwidthOptimizer`` (or ``SLAMonitor`` / ``SafetyGuard``
        wrapper) to monitor.
    soft_threshold:
        Queue fill ratio above which SOFT backpressure is signalled.
    hard_threshold:
        Queue fill ratio above which HARD backpressure is signalled.
    window_seconds:
        Rolling window length for rate estimation.
    """

    def __init__(
        self,
        optimizer,
        soft_threshold: float = 0.60,
        hard_threshold: float = 0.85,
        window_seconds: float = 5.0,
    ) -> None:
        if not (0 < soft_threshold < hard_threshold <= 1.0):
            raise ValueError(
                "soft_threshold must be < hard_threshold; both in (0, 1]"
            )
        self._optimizer = optimizer
        self._soft = soft_threshold
        self._hard = hard_threshold
        self._window = window_seconds
        self._lock = threading.Lock()

        # Rolling samples: (timestamp, packets_received, packets_forwarded)
        self._samples: Deque[Tuple[float, int, int]] = deque()
        self._last_state = BackpressureState(
            level=BackpressureLevel.NONE,
            queue_fill_ratio=0.0,
            arrival_rate_pps=0.0,
            drain_rate_pps=0.0,
            recommended_throttle_pct=0.0,
        )

    # ── public API ────────────────────────────────────────────────────────

    def update(self) -> BackpressureState:
        """
        Sample current optimizer metrics and return the updated state.

        Should be called periodically (e.g., once per second from a
        background thread or before each API response).
        """
        raw = self._optimizer.stats()
        fill_ratio = raw["queue"]["fill_ratio"]
        packets_received = raw["packets_received"]

        # Sum all dequeue counts to get packets forwarded
        dequeue_counts = raw["queue"]["dequeue_counts"]
        packets_forwarded = sum(
            (v if isinstance(v, int) else 0)
            for v in dequeue_counts.values()
        )

        now = time.monotonic()

        with self._lock:
            self._samples.append((now, packets_received, packets_forwarded))
            # Evict samples older than the window
            cutoff = now - self._window
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

            # Compute rates from the window
            arrival_pps, drain_pps = self._compute_rates()

            # Compute level and throttle recommendation
            level, throttle_pct = self._compute_level(fill_ratio)

            state = BackpressureState(
                level=level,
                queue_fill_ratio=fill_ratio,
                arrival_rate_pps=arrival_pps,
                drain_rate_pps=drain_pps,
                recommended_throttle_pct=throttle_pct,
            )
            self._last_state = state

        return state

    def state(self) -> BackpressureState:
        """Return the most recently computed state (no re-sampling)."""
        with self._lock:
            return self._last_state

    # ── internal ──────────────────────────────────────────────────────────

    def _compute_rates(self) -> Tuple[float, float]:
        """Compute arrival/drain PPS from the rolling window (must hold lock)."""
        if len(self._samples) < 2:
            return 0.0, 0.0
        oldest = self._samples[0]
        newest = self._samples[-1]
        dt = newest[0] - oldest[0]
        if dt <= 0:
            return 0.0, 0.0
        arrival_pps = (newest[1] - oldest[1]) / dt
        drain_pps = (newest[2] - oldest[2]) / dt
        return max(0.0, arrival_pps), max(0.0, drain_pps)

    def _compute_level(
        self, fill_ratio: float
    ) -> Tuple[BackpressureLevel, float]:
        """Determine backpressure level and throttle recommendation."""
        if fill_ratio >= self._hard:
            # Linear scale: 100% throttle at hard_threshold
            throttle_pct = 100.0
            return BackpressureLevel.HARD, throttle_pct

        if fill_ratio >= self._soft:
            # Linear interpolation between soft and hard thresholds
            frac = (fill_ratio - self._soft) / (self._hard - self._soft)
            throttle_pct = round(frac * 100.0, 1)
            return BackpressureLevel.SOFT, throttle_pct

        return BackpressureLevel.NONE, 0.0
