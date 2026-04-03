"""
Packet filter / dropper.

Two complementary mechanisms keep bandwidth within limits:

1. **Token-bucket rate limiter** – each priority class gets its own bucket
   that refills at a configured rate.  A packet may only pass if the bucket
   has enough tokens (bytes).

2. **RED (Random Early Detection)** – once the shared queue reaches a
   configurable fill level the filter starts *probabilistically* dropping
   packets before the queue becomes completely full, providing early
   backpressure to senders.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .classifier import Packet
from .config import OptimizerConfig, TrafficPriority


# ─────────────────────────── token bucket ────────────────────────────────────

class TokenBucket:
    """
    Token-bucket rate limiter for a single traffic class.

    Tokens represent bytes; the bucket refills at ``refill_rate`` bytes/second
    up to ``capacity`` bytes.
    """

    def __init__(self, refill_rate: float, capacity: float) -> None:
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._refill_rate = refill_rate   # bytes / second
        self._capacity = capacity          # bytes
        self._tokens = capacity            # start full
        self._last_refill: float = time.monotonic()

    # ── public ────────────────────────────────────────────────────────────

    def consume(self, nbytes: int) -> bool:
        """
        Attempt to consume *nbytes* tokens.

        :returns: ``True`` if the tokens were available (packet allowed),
                  ``False`` otherwise (packet should be dropped).
        """
        self._refill()
        if nbytes <= self._tokens:
            self._tokens -= nbytes
            return True
        return False

    def available(self) -> float:
        """Return current token count (after refilling for elapsed time)."""
        self._refill()
        return self._tokens

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    @property
    def capacity(self) -> float:
        return self._capacity

    # ── internal ──────────────────────────────────────────────────────────

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._refill_rate,
        )
        self._last_refill = now


# ─────────────────────────── drop decision ───────────────────────────────────

@dataclass
class DropDecision:
    """Result of PacketFilter.should_drop()."""
    drop: bool
    reason: str = ""


# ─────────────────────────── packet filter ───────────────────────────────────

class PacketFilter:
    """
    Decides whether a packet should be forwarded or dropped.

    Decision pipeline (AND logic – any True means drop):
      1. CRITICAL packets are *never* dropped by the rate-limiter (though they
         still consume tokens so the accounting stays accurate).
      2. Token-bucket check: does the priority class have enough tokens?
      3. RED check: is the queue fill level above the min threshold?
    """

    def __init__(
        self,
        config: Optional[OptimizerConfig] = None,
        current_queue_size_fn=None,
    ) -> None:
        """
        :param config: Optimizer configuration.
        :param current_queue_size_fn: Zero-argument callable that returns the
            current number of packets in the shared queue.  Used by RED.
            If *None*, RED is effectively disabled.
        """
        self._config = config or OptimizerConfig()
        self._queue_size_fn = current_queue_size_fn
        self._buckets: Dict[TrafficPriority, TokenBucket] = {}
        self._init_buckets()
        self._drop_counts: Dict[TrafficPriority, int] = {
            p: 0 for p in TrafficPriority
        }

    # ── public API ────────────────────────────────────────────────────────

    def should_drop(self, packet: Packet) -> DropDecision:
        """
        Evaluate the packet against all drop policies.

        :param packet: A classified packet (``packet.priority`` must be set).
        :returns: DropDecision – caller should drop the packet if ``.drop`` is True.
        """
        priority = packet.priority or TrafficPriority.MEDIUM

        # 1. Token-bucket check
        bucket = self._buckets[priority]
        allowed = bucket.consume(packet.size_bytes)
        if not allowed and priority is not TrafficPriority.CRITICAL:
            self._drop_counts[priority] += 1
            return DropDecision(
                drop=True,
                reason=f"rate_limit:{priority.name}",
            )

        # 2. RED check (only for non-CRITICAL traffic)
        if priority is not TrafficPriority.CRITICAL:
            red_drop, red_reason = self._red_check(priority)
            if red_drop:
                self._drop_counts[priority] += 1
                return DropDecision(drop=True, reason=red_reason)

        return DropDecision(drop=False)

    def drop_counts(self) -> Dict[TrafficPriority, int]:
        """Return a copy of the drop counters per priority class."""
        return dict(self._drop_counts)

    def reset_drop_counts(self) -> None:
        for p in TrafficPriority:
            self._drop_counts[p] = 0

    def bucket_available(self, priority: TrafficPriority) -> float:
        """Return available tokens (bytes) for *priority*."""
        return self._buckets[priority].available()

    # ── RED ───────────────────────────────────────────────────────────────

    def _red_check(self, priority: TrafficPriority) -> Tuple[bool, str]:
        if self._queue_size_fn is None:
            return False, ""
        current = self._queue_size_fn()
        max_q = self._config.max_queue_size
        if max_q <= 0:
            return False, ""
        fill = current / max_q
        min_t = self._config.red_min_threshold
        max_t = self._config.red_max_threshold

        if fill < min_t:
            return False, ""
        if fill >= max_t:
            return True, f"red:queue_full:{priority.name}"

        # Probabilistic drop: linear probability between min and max threshold
        drop_prob = (fill - min_t) / (max_t - min_t)
        # Higher priority classes get a reduced drop probability
        priority_factor = priority.value / len(TrafficPriority)
        adjusted_prob = drop_prob * priority_factor

        if random.random() < adjusted_prob:
            return True, f"red:prob_drop:{priority.name}:{adjusted_prob:.2f}"
        return False, ""

    # ── internal ──────────────────────────────────────────────────────────

    def _init_buckets(self) -> None:
        cfg = self._config
        for priority in TrafficPriority:
            rate = cfg.effective_refill_rate(priority)
            if rate <= 0:
                # Give a tiny floor so the bucket is always functional
                rate = 1.0
            capacity = rate * cfg.token_bucket_capacity_multiplier
            self._buckets[priority] = TokenBucket(
                refill_rate=rate,
                capacity=capacity,
            )
