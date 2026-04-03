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
ValueCoefficientsGuide
    Industry-standard starting-point coefficients for common traffic types
    (VoIP, interactive API, video, web browsing, email, bulk transfer).
    Eliminates the "I don't know what number to put in" barrier for new users.
    Call :func:`FlowValuePolicy.from_presets` to build a ready-to-run policy.

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

ValueCoefficientTuner
    Heuristic feedback loop that automatically nudges a flow's
    ``value_coefficient`` up when its SLA is being violated (so the
    scheduler defends it more aggressively) and nudges it back down when
    delivery is perfect (preventing runaway inflation).  No ML required.

Usage
-----
::

    from bandwidth_optimizer.policy import PolicyLoader
    from bandwidth_optimizer.value import FlowValuePolicy, ValueScheduler
    from bandwidth_optimizer import BandwidthOptimizer

    # Option A – YAML-driven coefficients
    policy = PolicyLoader.load_file("my_policy.yaml")
    vp = FlowValuePolicy.from_policy(policy)

    # Option B – built-in industry presets (no YAML needed to get started)
    vp = FlowValuePolicy.from_presets()

    optimizer = BandwidthOptimizer(flow_value_policy=vp)
    result = optimizer.process(packet)
    print(optimizer.value_tracker.value_efficiency_pct)

    # Feedback loop – auto-tune coefficients at runtime
    tuner = ValueCoefficientTuner(vp)
    tuner.observe("voip", delivered_rate=85.0, sla_min_rate=100.0)
    # → coefficient for "voip" flows will be boosted until SLA is met
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


# ─────────────────────────── ValueCoefficientsGuide ──────────────────────────

@dataclass(frozen=True)
class _TrafficPreset:
    """A single industry-standard value-coefficient calibration entry."""
    name: str
    description: str
    ports: Tuple[int, ...]
    protocols: Tuple[str, ...]
    priority: TrafficPriority
    value_coefficient: float
    rationale: str


# Industry-standard calibration presets.
#
# Rationale for each coefficient value:
#
#   VoIP / real-time audio (100):
#     A single dropped/delayed RTP packet causes an audible glitch.  Call
#     quality degrades instantly and is directly tied to SLA penalties and
#     churn.  Set the highest coefficient so the scheduler never sacrifices
#     a VoIP frame even under saturation.
#
#   Interactive API (50):
#     Sub-100 ms API latency is a hard requirement for revenue-generating
#     applications (checkout flows, trading, real-time dashboards).
#     Deserves high scheduling weight but not as time-sensitive as audio.
#
#   Video conferencing / streaming (40):
#     Video can absorb a few hundred ms of jitter via playout buffer, so
#     slightly less urgent than VoIP, but buffer underruns cause visible
#     artefacts that users notice immediately.
#
#   Interactive web / DNS / SSH (20):
#     Slow pages cost revenue (Amazon famously found 100 ms latency = 1%
#     sales drop) but the damage is softer and recoverable.  Important but
#     not critical.
#
#   Email / asynchronous messaging (5):
#     A few extra seconds of latency is imperceptible to the user.  Needs
#     some bandwidth guarantee to avoid indefinite starvation.
#
#   Bulk transfers / backups / P2P (0.5):
#     Can use whatever capacity remains after all higher-value flows are
#     served.  Coefficient below 1 means they yield to even unclassified
#     traffic (coefficient=1.0) in a mixed environment.
TRAFFIC_PRESETS: Tuple[_TrafficPreset, ...] = (
    _TrafficPreset(
        name="voip",
        description="VoIP / real-time audio (SIP + RTP)",
        ports=(5060, 5061, 5004, 5005),
        protocols=("udp", "tcp"),
        priority=TrafficPriority.CRITICAL,
        value_coefficient=100.0,
        rationale=(
            "Each dropped RTP packet causes an audible glitch. "
            "VoIP SLA credits and churn risk are immediate and quantifiable."
        ),
    ),
    _TrafficPreset(
        name="interactive_api",
        description="Interactive / revenue-generating API calls (HTTPS)",
        ports=(443, 8443),
        protocols=("tcp",),
        priority=TrafficPriority.HIGH,
        value_coefficient=50.0,
        rationale=(
            "Sub-100 ms latency is a hard requirement for checkout flows, "
            "trading, and real-time dashboards that drive direct revenue."
        ),
    ),
    _TrafficPreset(
        name="video_conference",
        description="Video conferencing and live streaming",
        ports=(8801, 8802, 3478, 3479),
        protocols=("udp", "tcp"),
        priority=TrafficPriority.HIGH,
        value_coefficient=40.0,
        rationale=(
            "Video can absorb ~200 ms jitter via playout buffer but "
            "buffer underruns cause visible artefacts that users notice."
        ),
    ),
    _TrafficPreset(
        name="interactive_web",
        description="Interactive web browsing, DNS, SSH",
        ports=(80, 8080, 53, 22),
        protocols=("tcp", "udp"),
        priority=TrafficPriority.HIGH,
        value_coefficient=20.0,
        rationale=(
            "100 ms of extra latency has measurable revenue impact "
            "(Amazon reported ~1% sales drop per 100 ms). Painful but survivable."
        ),
    ),
    _TrafficPreset(
        name="email",
        description="Email and asynchronous messaging (SMTP / IMAP / POP3)",
        ports=(25, 465, 587, 993, 995, 110, 143),
        protocols=("tcp",),
        priority=TrafficPriority.MEDIUM,
        value_coefficient=5.0,
        rationale=(
            "A few seconds of extra latency is imperceptible to users. "
            "Needs a floor to avoid indefinite starvation."
        ),
    ),
    _TrafficPreset(
        name="bulk_transfer",
        description="Bulk transfers, backups, software updates, P2P",
        ports=(6881, 6882, 6883, 21, 20),
        protocols=("tcp", "udp"),
        priority=TrafficPriority.BACKGROUND,
        value_coefficient=0.5,
        rationale=(
            "Should use only leftover capacity. Coefficient < 1 means "
            "these flows yield even to unclassified traffic."
        ),
    ),
)


class ValueCoefficientsGuide:
    """
    Industry-standard value-coefficient calibration for common traffic types.

    Provides ready-to-use starting points so operators don't have to guess
    what ``value_coefficient`` to assign.  The coefficients encode the
    relative *business cost of dropping or delaying* each traffic class.

    Usage
    -----
    ::

        guide = ValueCoefficientsGuide()
        for preset in guide.presets:
            print(f"{preset.name}: {preset.value_coefficient}  # {preset.rationale}")

        # Build a FlowValuePolicy directly from the presets
        policy = FlowValuePolicy.from_presets()

    Customising coefficients
    ------------------------
    To scale all coefficients to your revenue model, multiply them by a
    ``revenue_scale`` factor::

        guide = ValueCoefficientsGuide(revenue_scale=10.0)
        # VoIP becomes 1000.0, reflecting $1000/hr SLA credit exposure.

    Or override individual entries::

        policy = FlowValuePolicy.from_presets(overrides={"voip": 200.0})
    """

    def __init__(self, revenue_scale: float = 1.0) -> None:
        self._scale = max(0.0, revenue_scale)

    @property
    def presets(self) -> Tuple[_TrafficPreset, ...]:
        """Return all presets (coefficients scaled by ``revenue_scale``)."""
        if self._scale == 1.0:
            return TRAFFIC_PRESETS
        return tuple(
            _TrafficPreset(
                name=p.name,
                description=p.description,
                ports=p.ports,
                protocols=p.protocols,
                priority=p.priority,
                value_coefficient=p.value_coefficient * self._scale,
                rationale=p.rationale,
            )
            for p in TRAFFIC_PRESETS
        )

    def get(self, name: str) -> Optional[_TrafficPreset]:
        """Return the preset named *name*, or ``None`` if not found."""
        for p in TRAFFIC_PRESETS:
            if p.name == name:
                if self._scale != 1.0:
                    return _TrafficPreset(
                        name=p.name,
                        description=p.description,
                        ports=p.ports,
                        protocols=p.protocols,
                        priority=p.priority,
                        value_coefficient=p.value_coefficient * self._scale,
                        rationale=p.rationale,
                    )
                return p
        return None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable summary of all presets."""
        return {
            "revenue_scale": self._scale,
            "presets": [
                {
                    "name": p.name,
                    "description": p.description,
                    "ports": list(p.ports),
                    "protocols": list(p.protocols),
                    "priority": p.priority.name,
                    "value_coefficient": p.value_coefficient * self._scale,
                    "rationale": p.rationale,
                }
                for p in TRAFFIC_PRESETS
            ],
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
        # Runtime overrides applied by ValueCoefficientTuner {rule_name: coefficient}
        self._overrides: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────

    def get_coefficient(self, packet: Packet) -> float:
        """
        Return the value coefficient for *packet*.

        Evaluates rules in order; returns the first matching rule's
        ``value_coefficient`` (subject to any runtime override set by
        ``ValueCoefficientTuner``).  Returns ``default_coefficient`` if no
        rule matches.
        """
        with self._lock:
            for rule in self._rules:
                if self._rule_matches(rule, packet):
                    return self._overrides.get(rule.name, rule.value_coefficient)
            return self._default

    def assign(self, packet: Packet) -> float:
        """
        Look up the value coefficient, assign it to ``packet.value_coefficient``,
        and return it.
        """
        coeff = self.get_coefficient(packet)
        packet.value_coefficient = coeff
        return coeff

    def set_override(self, rule_name: str, coefficient: float) -> None:
        """
        Override the value coefficient for *rule_name* at runtime.

        Used by ``ValueCoefficientTuner`` to adjust coefficients without
        rewriting the YAML policy.  Set to ``None`` or call
        ``clear_override`` to revert to the rule's static value.
        """
        with self._lock:
            self._overrides[rule_name] = max(0.0, coefficient)

    def get_override(self, rule_name: str) -> Optional[float]:
        """Return the current runtime override for *rule_name*, or ``None``."""
        with self._lock:
            return self._overrides.get(rule_name)

    def clear_override(self, rule_name: str) -> None:
        """Remove the runtime override for *rule_name*."""
        with self._lock:
            self._overrides.pop(rule_name, None)

    def overrides_snapshot(self) -> Dict[str, float]:
        """Return a copy of all current runtime overrides."""
        with self._lock:
            return dict(self._overrides)

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

    @classmethod
    def from_presets(
        cls,
        revenue_scale: float = 1.0,
        overrides: Optional[Dict[str, float]] = None,
    ) -> "FlowValuePolicy":
        """
        Build a ``FlowValuePolicy`` from the built-in industry-standard presets.

        This is the fastest way to get started — no YAML file needed.

        Parameters
        ----------
        revenue_scale:
            Multiply all preset coefficients by this factor to calibrate to
            your revenue model.  Example: ``revenue_scale=10`` means VoIP
            becomes 1000, reflecting $1000/hr SLA credit exposure.
        overrides:
            Per-preset name overrides, applied *after* scaling.
            Example: ``{"voip": 200.0, "bulk_transfer": 0.1}``

        Usage::

            # Quick start – sensible defaults
            vp = FlowValuePolicy.from_presets()

            # Calibrated to $10k/hr SLA exposure
            vp = FlowValuePolicy.from_presets(revenue_scale=10.0,
                                               overrides={"voip": 1500.0})
        """
        guide = ValueCoefficientsGuide(revenue_scale=revenue_scale)
        overrides = overrides or {}
        rules = []
        for p in guide.presets:
            coeff = overrides.get(p.name, p.value_coefficient)
            rules.append(PolicyRule(
                name=p.name,
                priority=p.priority,
                ports=p.ports,
                protocols=p.protocols,
                value_coefficient=coeff,
                description=p.description,
            ))
        return cls(rules=rules, default_coefficient=1.0)

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


# ─────────────────────────── ValueCoefficientTuner ───────────────────────────

@dataclass
class _TunerState:
    """Per-rule tuner state."""
    rule_name: str
    base_coefficient: float        # static coefficient from YAML / preset
    current_coefficient: float     # live (possibly adjusted) coefficient
    boost_count: int = 0           # cumulative upward adjustments
    decay_count: int = 0           # cumulative downward adjustments
    last_observed: float = field(default_factory=time.monotonic)


class ValueCoefficientTuner:
    """
    Heuristic feedback loop that auto-adjusts value coefficients at runtime.

    **Why this matters**

    The static ``value_coefficient`` in a YAML rule is a best-guess.  When
    a flow's SLA is being violated — even though it has a high coefficient —
    it means either:

    * The coefficient is too low relative to competing flows, or
    * The link is saturated and the coefficient needs to climb further.

    Conversely, if a flow is *always* delivered at 100% efficiency its
    coefficient may be set too high, consuming scheduling headroom that
    could help other flows.

    **Algorithm (no ML required)**

    On each :meth:`observe` call:

    * **SLA violated** → multiply the flow's coefficient by ``boost_factor``
      (default 1.2), capped at ``max_coefficient``.
    * **SLA met, efficiency < perfection** → no change (in the zone).
    * **Perfect efficiency** → multiply by ``decay_factor`` (default 0.95),
      floored at ``base_coefficient`` so we never go below the static value.

    The adjustments converge: under saturation the violating flow climbs
    until the scheduler gives it enough priority to meet the SLA; once
    the SLA is met the coefficient slowly drifts back to the base value.

    Parameters
    ----------
    policy:
        The ``FlowValuePolicy`` whose coefficients will be adjusted.
    boost_factor:
        Multiplier applied when an SLA is violated (default 1.2 = +20%).
    decay_factor:
        Multiplier applied when delivery is perfect (default 0.95 = −5%).
    max_coefficient:
        Hard ceiling on any coefficient regardless of how many boosts occur.
    perfection_threshold_pct:
        Efficiency percentage above which decay is applied (default 99.5%).
    violation_threshold_pct:
        Efficiency percentage below which a boost is applied (default 90.0%).

    Usage::

        policy = FlowValuePolicy.from_presets()
        tuner = ValueCoefficientTuner(policy)
        optimizer = BandwidthOptimizer(flow_value_policy=policy)

        # Called periodically (e.g. every 5 seconds) from a monitoring loop
        tracker = optimizer.value_tracker
        for rule_name in ["voip", "interactive_api"]:
            tuner.observe(rule_name,
                          delivered_rate=tracker.value_delivered_per_sec,
                          sla_min_rate=100.0)

        # Inspect what the tuner has done
        print(tuner.tuning_report())
    """

    def __init__(
        self,
        policy: FlowValuePolicy,
        boost_factor: float = 1.2,
        decay_factor: float = 0.95,
        max_coefficient: float = 10_000.0,
        perfection_threshold_pct: float = 99.5,
        violation_threshold_pct: float = 90.0,
    ) -> None:
        self._policy = policy
        self._boost_factor = max(1.0, boost_factor)
        self._decay_factor = min(1.0, max(0.0, decay_factor))
        self._max_coeff = max(0.0, max_coefficient)
        self._perfection = min(100.0, max(0.0, perfection_threshold_pct))
        self._violation = min(100.0, max(0.0, violation_threshold_pct))
        self._states: Dict[str, _TunerState] = {}
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────

    def observe(
        self,
        rule_name: str,
        delivered_rate: float,
        sla_min_rate: float,
    ) -> float:
        """
        Report current delivery metrics for *rule_name* and adjust if needed.

        Parameters
        ----------
        rule_name:
            The ``PolicyRule.name`` to adjust (must match a rule in the policy).
        delivered_rate:
            Current delivered-value rate (value units/s) for this flow/rule.
        sla_min_rate:
            Minimum contracted rate.  If ``delivered_rate < sla_min_rate``
            an SLA violation is assumed and the coefficient is boosted.

        :returns: The new (possibly unchanged) coefficient for *rule_name*.
        """
        with self._lock:
            state = self._get_or_create_state(rule_name)
            efficiency = (
                100.0 * delivered_rate / sla_min_rate
                if sla_min_rate > 0
                else 100.0
            )
            if efficiency < self._violation:
                # SLA violated → boost
                new_coeff = min(
                    state.current_coefficient * self._boost_factor,
                    self._max_coeff,
                )
                state.boost_count += 1
            elif efficiency >= self._perfection:
                # Perfect delivery → gentle decay back toward base
                new_coeff = max(
                    state.current_coefficient * self._decay_factor,
                    state.base_coefficient,
                )
                state.decay_count += 1
            else:
                # In the acceptable zone → no change
                new_coeff = state.current_coefficient

            state.current_coefficient = new_coeff
            state.last_observed = time.monotonic()
            self._policy.set_override(rule_name, new_coeff)
            return new_coeff

    def observe_contract(
        self,
        rule_name: str,
        delivered_rate: float,
        contract: ValueSLAContract,
    ) -> float:
        """
        Convenience wrapper: observe using a ``ValueSLAContract`` for the min rate.

        :returns: The new coefficient for *rule_name*.
        """
        return self.observe(
            rule_name,
            delivered_rate=delivered_rate,
            sla_min_rate=contract.min_value_rate_per_sec,
        )

    def reset(self, rule_name: str) -> None:
        """Reset *rule_name* back to its base coefficient."""
        with self._lock:
            if rule_name in self._states:
                state = self._states[rule_name]
                state.current_coefficient = state.base_coefficient
                state.boost_count = 0
                state.decay_count = 0
                self._policy.clear_override(rule_name)

    def reset_all(self) -> None:
        """Reset all rules to their base coefficients."""
        with self._lock:
            for rule_name in list(self._states):
                state = self._states[rule_name]
                state.current_coefficient = state.base_coefficient
                state.boost_count = 0
                state.decay_count = 0
                self._policy.clear_override(rule_name)

    def tuning_report(self) -> dict:
        """Return a JSON-serialisable snapshot of all tuner states."""
        with self._lock:
            return {
                "boost_factor": self._boost_factor,
                "decay_factor": self._decay_factor,
                "max_coefficient": self._max_coeff,
                "rules": {
                    name: {
                        "rule_name": s.rule_name,
                        "base_coefficient": s.base_coefficient,
                        "current_coefficient": round(s.current_coefficient, 4),
                        "boost_count": s.boost_count,
                        "decay_count": s.decay_count,
                    }
                    for name, s in self._states.items()
                },
            }

    # ── internal ──────────────────────────────────────────────────────────

    def _get_or_create_state(self, rule_name: str) -> _TunerState:
        """Return or create the tuner state for *rule_name* (must hold lock)."""
        if rule_name not in self._states:
            # Look up the base coefficient from the policy's rules
            base = 1.0
            for rule in self._policy._rules:
                if rule.name == rule_name:
                    base = rule.value_coefficient
                    break
            self._states[rule_name] = _TunerState(
                rule_name=rule_name,
                base_coefficient=base,
                current_coefficient=self._policy.get_override(rule_name) or base,
            )
        return self._states[rule_name]

