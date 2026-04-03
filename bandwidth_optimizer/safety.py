"""
Production safety layer – fail-safe / circuit-breaker wrapper.

Wraps ``BandwidthOptimizer`` to make it production-grade by adding:

1. **Fail-safe mode** – defines the system's behaviour when an unexpected
   exception occurs inside the optimizer pipeline:

   * ``FAIL_OPEN``   – pass the original packet through *unmodified* (bypass).
                       Chosen when dropping traffic is worse than wrong QoS.
   * ``FAIL_CLOSED`` – treat the packet as dropped (safe for strict networks).

2. **Circuit-breaker** – after ``circuit_threshold`` *consecutive* errors the
   breaker trips and the system enters BYPASSED state.  In BYPASSED state all
   packets are forwarded unmodified (fail-open semantics) without calling the
   optimizer at all, so a runaway crash loop cannot degrade throughput.

3. **Health reporting** – ``SafetyGuard.health()`` returns a ``HealthStatus``
   instance with current state, error counts, and last error message.

4. **Auto-recovery** – a ``reset()`` call (or a watchdog thread) can clear
   error counts and restore HEALTHY operation.

Usage::

    from bandwidth_optimizer.safety import FailMode, SafetyGuard

    guard = SafetyGuard(optimizer, fail_mode=FailMode.FAIL_OPEN,
                        circuit_threshold=10)
    result = guard.process(packet)   # never raises
    print(guard.health())
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .classifier import Packet
from .optimizer import BandwidthOptimizer, ProcessResult


# ─────────────────────────── enums / config ───────────────────────────────────

class FailMode(Enum):
    """
    Defines what happens to a packet when the optimizer pipeline raises.

    FAIL_OPEN
        Forward the original (unprocessed) packet.  Traffic keeps flowing;
        QoS is lost temporarily.  Best for consumer-facing services.

    FAIL_CLOSED
        Drop the packet.  Use when forwarding malformed traffic is
        unacceptable (e.g., security-sensitive environments).
    """
    FAIL_OPEN   = "fail_open"
    FAIL_CLOSED = "fail_closed"


class CircuitState(Enum):
    """Current state of the circuit-breaker."""
    HEALTHY  = "healthy"    # optimizer is working normally
    DEGRADED = "degraded"   # errors detected but below threshold
    BYPASSED = "bypassed"   # circuit open – optimizer calls skipped


# ─────────────────────────── health status ────────────────────────────────────

@dataclass
class HealthStatus:
    """Snapshot of the safety guard's health at a point in time."""
    state: CircuitState
    fail_mode: FailMode
    total_errors: int
    consecutive_errors: int
    circuit_threshold: int
    last_error: str = ""
    last_error_time: Optional[float] = None
    uptime_seconds: float = 0.0

    @property
    def is_healthy(self) -> bool:
        return self.state == CircuitState.HEALTHY

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "fail_mode": self.fail_mode.value,
            "total_errors": self.total_errors,
            "consecutive_errors": self.consecutive_errors,
            "circuit_threshold": self.circuit_threshold,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "uptime_seconds": round(self.uptime_seconds, 2),
        }


# ─────────────────────────── safety guard ─────────────────────────────────────

class SafetyGuard:
    """
    Production-safe wrapper around ``BandwidthOptimizer``.

    ``process()`` is guaranteed never to raise an exception.  On errors it
    applies *fail_mode* logic and updates internal health counters.

    Parameters
    ----------
    optimizer:
        The wrapped ``BandwidthOptimizer`` instance.
    fail_mode:
        Packet disposition on optimizer exception
        (``FAIL_OPEN`` = forward unchanged, ``FAIL_CLOSED`` = drop).
    circuit_threshold:
        Number of consecutive errors that trip the circuit-breaker and
        switch to full bypass mode.  0 = never trip.
    """

    def __init__(
        self,
        optimizer: BandwidthOptimizer,
        fail_mode: FailMode = FailMode.FAIL_OPEN,
        circuit_threshold: int = 5,
    ) -> None:
        self._optimizer = optimizer
        self._fail_mode = fail_mode
        self._circuit_threshold = circuit_threshold

        self._lock = threading.Lock()
        self._state = CircuitState.HEALTHY
        self._total_errors: int = 0
        self._consecutive_errors: int = 0
        self._last_error: str = ""
        self._last_error_time: Optional[float] = None
        self._start_time: float = time.monotonic()

        # Packet counters
        self._total_in: int = 0
        self._bypass_count: int = 0  # packets handled by fail-safe, not optimizer

    # ── public API ────────────────────────────────────────────────────────

    def process(self, packet: Packet) -> ProcessResult:
        """
        Process *packet* safely; never raises.

        If the circuit is BYPASSED, the packet is forwarded immediately
        without calling the underlying optimizer.

        :returns: A ``ProcessResult`` – ``dropped=True`` if fail-closed,
                  otherwise the optimized (or unmodified bypass) result.
        """
        with self._lock:
            self._total_in += 1
            bypassed = self._state == CircuitState.BYPASSED

        if bypassed:
            return self._bypass_result(packet, reason="circuit_open")

        try:
            result = self._optimizer.process(packet)
            with self._lock:
                # Successful call resets consecutive error counter
                self._consecutive_errors = 0
                if self._state == CircuitState.DEGRADED:
                    self._state = CircuitState.HEALTHY
            return result

        except Exception as exc:  # noqa: BLE001
            return self._handle_error(packet, exc)

    def health(self) -> HealthStatus:
        """Return a snapshot of the current health status."""
        with self._lock:
            return HealthStatus(
                state=self._state,
                fail_mode=self._fail_mode,
                total_errors=self._total_errors,
                consecutive_errors=self._consecutive_errors,
                circuit_threshold=self._circuit_threshold,
                last_error=self._last_error,
                last_error_time=self._last_error_time,
                uptime_seconds=time.monotonic() - self._start_time,
            )

    def reset(self) -> None:
        """
        Clear all error counters and return to HEALTHY state.

        Call after the underlying cause has been fixed.
        """
        with self._lock:
            self._state = CircuitState.HEALTHY
            self._total_errors = 0
            self._consecutive_errors = 0
            self._last_error = ""
            self._last_error_time = None
            self._bypass_count = 0

    # ── delegate optimizer interface ──────────────────────────────────────

    def dequeue(self):
        """Pass-through to the wrapped optimizer's dequeue."""
        return self._optimizer.dequeue()

    def stats(self) -> dict:
        """Return optimizer stats merged with safety guard health info."""
        base = self._optimizer.stats()
        base["safety"] = self.health().to_dict()
        base["safety"]["bypass_count"] = self._bypass_count
        return base

    @property
    def optimizer(self) -> BandwidthOptimizer:
        return self._optimizer

    # ── properties ────────────────────────────────────────────────────────

    @property
    def fail_mode(self) -> FailMode:
        return self._fail_mode

    @fail_mode.setter
    def fail_mode(self, value: FailMode) -> None:
        with self._lock:
            self._fail_mode = value

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    # ── internal ──────────────────────────────────────────────────────────

    def _handle_error(self, packet: Packet, exc: Exception) -> ProcessResult:
        """Record the error, update circuit-breaker state, apply fail-mode."""
        with self._lock:
            self._total_errors += 1
            self._consecutive_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._last_error_time = time.time()

            if (
                self._circuit_threshold > 0
                and self._consecutive_errors >= self._circuit_threshold
            ):
                self._state = CircuitState.BYPASSED
            else:
                self._state = CircuitState.DEGRADED

            fail_mode = self._fail_mode

        return self._bypass_result(
            packet,
            reason=f"error:{type(exc).__name__}",
            force_drop=(fail_mode == FailMode.FAIL_CLOSED),
        )

    def _bypass_result(
        self,
        packet: Packet,
        reason: str = "bypass",
        force_drop: bool = False,
    ) -> ProcessResult:
        """Return a ProcessResult for a bypassed packet."""
        with self._lock:
            self._bypass_count += 1
        if force_drop:
            return ProcessResult(packet=packet, dropped=True, drop_reason=reason)
        return ProcessResult(packet=packet, dropped=False, drop_reason="")
