"""
Packet Value Model (PVM) – business-value-aware network scheduling.

Core insight
------------
Every existing QoS system allocates bandwidth by *traffic class* — a static
hierarchy (CRITICAL > HIGH > MEDIUM…).  The PVM reframes the problem:

  **Network traffic has economic value, not just technical priority.**
  The scheduler should maximise the total business value delivered per bit,
  not just enforce a tier.

Components
----------
FlowValuePolicy
    Maps packets/flows to continuous *value coefficients* (0.0 – ∞) based
    on ``PolicyRule`` definitions loaded from YAML.  A rule with
    ``value_coefficient: 50`` means matching flows are worth 50× the
    bandwidth of a default (coefficient=1.0) flow.

ValueScheduler
    Drop-in replacement for ``PriorityScheduler`` that sorts the transmit
    queue by ``effective_value = value_coefficient × priority_multiplier``
    instead of the raw ``TrafficPriority`` integer.  Within the same
    effective value, FIFO ordering is preserved.

ValueSLAContract
    Per-tenant SLA expressed as a *minimum delivered-value-rate* ($/s or
    arbitrary value units/s) rather than a latency ceiling.  Checked by
    calling :meth:`ValueSLAContract.is_violated`.

ValueLossTracker
    Sliding-window tracker that records value units delivered and dropped.
    Exposes ``value_delivered_per_sec``, ``value_lost_per_sec``, and
    ``value_efficiency_pct`` – the metrics that resonate with CFOs.

Usage
-----
::

    from bandwidth_optimizer.policy import PolicyLoader
    from bandwidth_optimizer.value import FlowValuePolicy, ValueScheduler
    from bandwidth_optimizer import BandwidthOptimizer

    policy = PolicyLoader.load_file("my_policy.yaml")
    vp = FlowValuePolicy.from_policy(policy)
    optimizer = BandwidthOptimizer(flow_value_policy=vp)
    result = optimizer.process(packet)
    print(optimizer.value_tracker.value_efficiency_pct)
"""

from __future__ import annotations

import collections
import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .classifier import Packet
from .config import TrafficPriority
from .policy import PolicyRule
from .scheduler import PriorityScheduler


# ── priority → base multiplier ────────────────────────────────────────────────
# Used so that, at the default value_coefficient=1.0, scheduling order matches
# the original priority hierarchy.  A value_coefficient can override this.
_PRIORITY_MULTIPLIER: Dict[TrafficPriority, float] = {
    TrafficPriority.CRITICAL:   5.0,
    TrafficPriority.HIGH:       4.0,
    TrafficPriority.MEDIUM:     3.0,
    TrafficPriority.LOW:        2.0,
    TrafficPriority.BACKGROUND: 1.0,
}


# ─────────────────────────── FlowValuePolicy ─────────────────────────────────

class FlowValuePolicy:
    """
    Maps packets to continuous business-value coefficients.

    Rules are evaluated top-to-bottom (first match wins), exactly like the
    ``TrafficClassifier`` rule table.  Packets that match no rule receive the
    ``default_coefficient`` (1.0 by default).

    Parameters
    ----------
    rules:
        Ordered list of ``PolicyRule`` objects carrying ``value_coefficient``
        values.  Rules without ports/protocols act as wildcards for those
        fields.
    default_coefficient:
        Fallback coefficient for packets that match no rule.

    Usage::

        policy = PolicyLoader.load_file("my_policy.yaml")
        vp = FlowValuePolicy.from_policy(policy)
        coeff = vp.get_coefficient(packet)   # e.g. 50.0
    """

    def __init__(
        self,
        rules: Optional[List[PolicyRule]] = None,
        default_coefficient: float = 1.0,
    ) -> None:
        self._rules: List[PolicyRule] = list(rules or [])
        self._default = max(0.0, default_coefficient)

    # ── public API ────────────────────────────────────────────────────────

    def get_coefficient(self, packet: Packet) -> float:
        """
        Return the value coefficient for *packet*.

        Evaluates rules in order; returns the first matching rule's
        ``value_coefficient``.  Returns ``default_coefficient`` if no rule
        matches.
        """
        for rule in self._rules:
            if self._rule_matches(rule, packet):
                return rule.value_coefficient
        return self._default

    def assign(self, packet: Packet) -> float:
        """
        Look up the value coefficient, assign it to ``packet.value_coefficient``,
        and return it.
        """
        coeff = self.get_coefficient(packet)
        packet.value_coefficient = coeff
        return coeff

    @classmethod
    def from_policy(
        cls,
        policy,  # bandwidth_optimizer.policy.Policy (avoid circular import)
        default_coefficient: float = 1.0,
    ) -> "FlowValuePolicy":
        """
        Build a ``FlowValuePolicy`` from a loaded ``Policy`` object.

        Each ``PolicyRule`` already carries a ``value_coefficient`` parsed
        from the ``value_coefficient:`` YAML field.
        """
        return cls(rules=list(policy.rules), default_coefficient=default_coefficient)

    # ── internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _rule_matches(rule: PolicyRule, packet: Packet) -> bool:
        """Evaluate a single PolicyRule against *packet* (port + protocol only)."""
        if rule.ports:
            if packet.src_port not in rule.ports and packet.dst_port not in rule.ports:
                return False
        if rule.protocols:
            if packet.protocol.lower() not in rule.protocols:
                return False
        return True


# ─────────────────────────── ValueScheduler ──────────────────────────────────

@dataclass(order=True)
class _ValueEntry:
    """
    Heap entry for ValueScheduler.

    Sorted by ``(-effective_value, sequence)`` so the highest-value packet
    is always at the top of the min-heap.
    """
    neg_effective_value: float   # negated so min-heap → highest value first
    sequence: int
    packet: Packet = field(compare=False)


class ValueScheduler:
    """
    Value-weighted priority queue – drop-in replacement for PriorityScheduler.

    Unlike ``PriorityScheduler`` (which sorts by discrete priority tier),
    ``ValueScheduler`` sorts by::

        effective_value = packet.value_coefficient × priority_multiplier[priority]

    This means a BACKGROUND flow with ``value_coefficient=100`` will be
    scheduled ahead of a CRITICAL flow with ``value_coefficient=0.5``, if
    that reflects the business reality.

    At the default ``value_coefficient=1.0`` the ordering is identical to
    ``PriorityScheduler`` – fully backward-compatible.

    Parameters
    ----------
    max_queue_size:
        Maximum packets in the queue.  Overflow evicts the lowest-value
        waiting packet.

    Usage::

        sched = ValueScheduler(max_queue_size=512)
        sched.enqueue(packet)
        next_pkt = sched.dequeue()
    """

    def __init__(self, max_queue_size: int = 1024) -> None:
        self._max_size = max(1, max_queue_size)
        self._heap: List[_ValueEntry] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._enqueue_counts: Dict[TrafficPriority, int] = {
            p: 0 for p in TrafficPriority
        }
        self._dequeue_counts: Dict[TrafficPriority, int] = {
            p: 0 for p in TrafficPriority
        }
        self._dropped_overflow: int = 0

    # ── public API ────────────────────────────────────────────────────────

    def enqueue(self, packet: Packet) -> bool:
        """
        Add *packet* to the queue.

        The packet's ``value_coefficient`` and ``priority`` are used to
        compute its ``effective_value``.  If the queue is full, the
        lowest-value waiting packet is evicted to make room (new packet is
        dropped only if its effective value is lower than everything queued).

        :returns: ``True`` if accepted, ``False`` if dropped.
        """
        priority = packet.priority or TrafficPriority.MEDIUM
        packet.enqueued_at = time.monotonic()
        eff = self._effective_value(packet)
        entry = _ValueEntry(
            neg_effective_value=-eff,
            sequence=next(self._counter),
            packet=packet,
        )

        with self._lock:
            if len(self._heap) >= self._max_size:
                worst_idx = self._find_worst_index()
                worst_entry = self._heap[worst_idx]
                # worst entry has the *highest* neg_eff_val (lowest real value)
                if worst_entry.neg_effective_value < entry.neg_effective_value:
                    # new packet has lower effective value than worst queued
                    self._dropped_overflow += 1
                    return False
                self._heap[worst_idx] = self._heap[-1]
                self._heap.pop()
                heapq.heapify(self._heap)
                self._dropped_overflow += 1

            heapq.heappush(self._heap, entry)
            self._enqueue_counts[priority] += 1
            return True

    def dequeue(self) -> Optional[Packet]:
        """Remove and return the highest-value packet, or ``None`` if empty."""
        with self._lock:
            if not self._heap:
                return None
            entry = heapq.heappop(self._heap)
            priority = entry.packet.priority or TrafficPriority.MEDIUM
            self._dequeue_counts[priority] += 1
            return entry.packet

    def peek(self) -> Optional[Packet]:
        """Return the next packet without removing it."""
        with self._lock:
            return self._heap[0].packet if self._heap else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)

    def is_empty(self) -> bool:
        return len(self) == 0

    def is_full(self) -> bool:
        with self._lock:
            return len(self._heap) >= self._max_size

    @property
    def max_queue_size(self) -> int:
        return self._max_size

    def drain(self):
        """Yield all packets in value order and clear the queue."""
        while not self.is_empty():
            pkt = self.dequeue()
            if pkt is not None:
                yield pkt

    # ── statistics ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            current_size = len(self._heap)
        return {
            "current_queue_size": current_size,
            "max_queue_size": self._max_size,
            "fill_ratio": current_size / self._max_size,
            "enqueue_counts": dict(self._enqueue_counts),
            "dequeue_counts": dict(self._dequeue_counts),
            "dropped_overflow": self._dropped_overflow,
        }

    def reset_stats(self) -> None:
        with self._lock:
            for p in TrafficPriority:
                self._enqueue_counts[p] = 0
                self._dequeue_counts[p] = 0
            self._dropped_overflow = 0

    # ── internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _effective_value(packet: Packet) -> float:
        """Compute the scheduling value for *packet*."""
        priority = packet.priority or TrafficPriority.MEDIUM
        multiplier = _PRIORITY_MULTIPLIER.get(priority, 1.0)
        return max(0.0, packet.value_coefficient) * multiplier

    def _find_worst_index(self) -> int:
        """Return index of the lowest-value entry (highest neg_effective_value)."""
        worst_idx = 0
        for i, entry in enumerate(self._heap):
            if entry.neg_effective_value > self._heap[worst_idx].neg_effective_value:
                worst_idx = i
            elif (
                entry.neg_effective_value == self._heap[worst_idx].neg_effective_value
                and entry.sequence < self._heap[worst_idx].sequence
            ):
                worst_idx = i
        return worst_idx


# ─────────────────────────── ValueSLAContract ────────────────────────────────

@dataclass
class ValueSLAContract:
    """
    Per-tenant SLA expressed as a minimum *delivered-value-rate* guarantee.

    Unlike a latency-ceiling SLA (which is a technical metric), a
    value-rate SLA is directly tied to business impact.  An SLA violation
    means the tenant is receiving less value per second than contracted.

    Parameters
    ----------
    tenant_id:
        Unique name or identifier for this tenant / customer.
    min_value_rate_per_sec:
        Minimum value units per second that must be delivered.  Below this
        the contract is considered violated.
    value_coefficient:
        The ``value_coefficient`` expected for this tenant's flows.  Used
        only for informational / documentation purposes.

    Usage::

        contract = ValueSLAContract("voip_tenant", min_value_rate_per_sec=100.0)
        if contract.is_violated(tracker.value_delivered_per_sec):
            alert("SLA breach!")
    """

    tenant_id: str
    min_value_rate_per_sec: float
    value_coefficient: float = 1.0

    def is_violated(self, current_rate: float) -> bool:
        """Return ``True`` if *current_rate* is below the contracted minimum."""
        return current_rate < self.min_value_rate_per_sec

    def to_dict(self, current_rate: float = 0.0) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "min_value_rate_per_sec": self.min_value_rate_per_sec,
            "value_coefficient": self.value_coefficient,
            "current_value_rate_per_sec": round(current_rate, 4),
            "violated": self.is_violated(current_rate),
        }


# ─────────────────────────── ValueLossTracker ────────────────────────────────

class ValueLossTracker:
    """
    Sliding-window tracker for business-value delivered vs. dropped.

    Records each *deliver* and *drop* event as a (timestamp, value) pair and
    computes rolling per-second rates over a configurable window.

    The ``value_efficiency_pct`` property is the key dashboard metric:
    "We are delivering X% of the potential value we could deliver."

    Parameters
    ----------
    window_seconds:
        Rolling window duration for rate calculations (default 5 s).

    Usage::

        tracker = ValueLossTracker()
        tracker.record_delivered(packet.value_coefficient)
        tracker.record_dropped(packet.value_coefficient)
        print(tracker.value_efficiency_pct)   # 97.3
    """

    def __init__(self, window_seconds: float = 5.0) -> None:
        self._window = max(0.1, window_seconds)
        self._delivered: Deque[Tuple[float, float]] = collections.deque()
        self._dropped: Deque[Tuple[float, float]] = collections.deque()
        self._total_delivered: float = 0.0
        self._total_dropped: float = 0.0
        self._lock = threading.Lock()

    # ── record events ─────────────────────────────────────────────────────

    def record_delivered(self, value: float) -> None:
        """Record that *value* units of business value were successfully delivered."""
        now = time.monotonic()
        with self._lock:
            self._delivered.append((now, value))
            self._total_delivered += value
            self._evict_old(now)

    def record_dropped(self, value: float) -> None:
        """Record that *value* units of business value were dropped / lost."""
        now = time.monotonic()
        with self._lock:
            self._dropped.append((now, value))
            self._total_dropped += value
            self._evict_old(now)

    # ── aggregate properties ──────────────────────────────────────────────

    @property
    def value_delivered_total(self) -> float:
        """Cumulative value units delivered since the tracker was created."""
        with self._lock:
            return self._total_delivered

    @property
    def value_lost_total(self) -> float:
        """Cumulative value units dropped since the tracker was created."""
        with self._lock:
            return self._total_dropped

    @property
    def value_delivered_per_sec(self) -> float:
        """Value units delivered per second over the rolling window."""
        with self._lock:
            return self._rate(self._delivered)

    @property
    def value_lost_per_sec(self) -> float:
        """Value units dropped per second over the rolling window."""
        with self._lock:
            return self._rate(self._dropped)

    @property
    def value_efficiency_pct(self) -> float:
        """
        Percentage of potential value that was successfully delivered.

        ``100 × delivered / (delivered + dropped)`` over the rolling window.
        Returns 100.0 when nothing has been processed yet.
        """
        with self._lock:
            d = self._rate(self._delivered)
            l = self._rate(self._dropped)
            total = d + l
            return 100.0 * d / total if total > 0 else 100.0

    def to_dict(self) -> dict:
        """Return a JSON-serialisable snapshot of all value metrics."""
        return {
            "value_delivered_total": round(self._total_delivered, 4),
            "value_lost_total": round(self._total_dropped, 4),
            "value_delivered_per_sec": round(self.value_delivered_per_sec, 4),
            "value_lost_per_sec": round(self.value_lost_per_sec, 4),
            "value_efficiency_pct": round(self.value_efficiency_pct, 2),
            "window_seconds": self._window,
        }

    def reset(self) -> None:
        """Clear all accumulated data."""
        with self._lock:
            self._delivered.clear()
            self._dropped.clear()
            self._total_delivered = 0.0
            self._total_dropped = 0.0

    # ── internal ──────────────────────────────────────────────────────────

    def _rate(self, events: Deque[Tuple[float, float]]) -> float:
        """Sum events in the window and divide by window length (must hold lock)."""
        now = time.monotonic()
        cutoff = now - self._window
        total = sum(v for ts, v in events if ts >= cutoff)
        return total / self._window

    def _evict_old(self, now: float) -> None:
        """Remove events older than two windows (must hold lock)."""
        cutoff = now - 2 * self._window
        while self._delivered and self._delivered[0][0] < cutoff:
            self._delivered.popleft()
        while self._dropped and self._dropped[0][0] < cutoff:
            self._dropped.popleft()
