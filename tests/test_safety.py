"""
Tests for bandwidth_optimizer.safety (Production Safety Layer)
"""

import threading
import time

import pytest

from bandwidth_optimizer import BandwidthOptimizer, OptimizerConfig, Packet
from bandwidth_optimizer.optimizer import ProcessResult
from bandwidth_optimizer.safety import CircuitState, FailMode, SafetyGuard


def _make_packet(size: int = 200, port: int = 443) -> Packet:
    return Packet(
        src_ip="10.0.0.1", dst_ip="10.0.0.2",
        src_port=1234, dst_port=port,
        protocol="tcp",
        payload=b"X" * size, size_bytes=size,
    )


def _make_guard(fail_mode=FailMode.FAIL_OPEN, threshold=5):
    opt = BandwidthOptimizer()
    return SafetyGuard(opt, fail_mode=fail_mode, circuit_threshold=threshold)


# ── basic operation ────────────────────────────────────────────────────────────

class TestSafetyGuardNormalOperation:
    def test_process_returns_result(self):
        guard = _make_guard()
        result = guard.process(_make_packet())
        assert isinstance(result, ProcessResult)

    def test_healthy_initial_state(self):
        guard = _make_guard()
        assert guard.state == CircuitState.HEALTHY
        assert guard.health().is_healthy

    def test_stats_includes_safety(self):
        guard = _make_guard()
        s = guard.stats()
        assert "safety" in s
        assert s["safety"]["state"] == CircuitState.HEALTHY.value

    def test_dequeue_delegates(self):
        guard = _make_guard()
        guard.process(_make_packet())
        pkt = guard.dequeue()
        # may be None if packet was dropped, but should not raise
        assert pkt is None or isinstance(pkt, Packet)

    def test_optimizer_property(self):
        opt = BandwidthOptimizer()
        guard = SafetyGuard(opt)
        assert guard.optimizer is opt

    def test_fail_mode_readable(self):
        guard = SafetyGuard(BandwidthOptimizer(), fail_mode=FailMode.FAIL_CLOSED)
        assert guard.fail_mode == FailMode.FAIL_CLOSED


# ── error handling ─────────────────────────────────────────────────────────────

class BrokenOptimizer(BandwidthOptimizer):
    """Optimizer that always raises on process()."""
    def process(self, packet):
        raise RuntimeError("simulated pipeline crash")


class TestSafetyGuardErrorHandling:
    def test_fail_open_forwards_on_error(self):
        guard = SafetyGuard(BrokenOptimizer(), fail_mode=FailMode.FAIL_OPEN,
                            circuit_threshold=100)
        result = guard.process(_make_packet())
        assert not result.dropped
        assert guard.health().total_errors == 1

    def test_fail_closed_drops_on_error(self):
        guard = SafetyGuard(BrokenOptimizer(), fail_mode=FailMode.FAIL_CLOSED,
                            circuit_threshold=100)
        result = guard.process(_make_packet())
        assert result.dropped
        assert "error" in result.drop_reason

    def test_degraded_after_first_error(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=10)
        guard.process(_make_packet())
        assert guard.state == CircuitState.DEGRADED

    def test_circuit_trips_after_threshold(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=3)
        for _ in range(3):
            guard.process(_make_packet())
        assert guard.state == CircuitState.BYPASSED

    def test_bypassed_state_forwards_without_optimizer(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=1)
        guard.process(_make_packet())   # trips circuit
        assert guard.state == CircuitState.BYPASSED
        # Next call should bypass optimizer (no additional errors)
        before_errors = guard.health().total_errors
        result = guard.process(_make_packet())
        assert guard.health().total_errors == before_errors  # no new error
        assert not result.dropped  # fail-open is default

    def test_consecutive_errors_reset_on_success(self):
        """After a successful call, consecutive_errors must drop to 0."""
        class FlakyOptimizer(BandwidthOptimizer):
            call_count = 0
            def process(self, pkt):
                self.call_count += 1
                if self.call_count <= 2:
                    raise ValueError("transient error")
                return super().process(pkt)

        guard = SafetyGuard(FlakyOptimizer(), circuit_threshold=10)
        guard.process(_make_packet())   # error 1
        guard.process(_make_packet())   # error 2
        assert guard.health().consecutive_errors == 2
        guard.process(_make_packet())   # success
        assert guard.health().consecutive_errors == 0
        assert guard.state == CircuitState.HEALTHY

    def test_process_never_raises(self):
        """process() must never propagate any exception."""
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=100)
        for _ in range(10):
            try:
                guard.process(_make_packet())
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"process() raised unexpectedly: {e}")

    def test_last_error_message_recorded(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=100)
        guard.process(_make_packet())
        h = guard.health()
        assert "RuntimeError" in h.last_error
        assert h.last_error_time is not None


# ── reset ─────────────────────────────────────────────────────────────────────

class TestSafetyGuardReset:
    def test_reset_clears_state(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=1)
        guard.process(_make_packet())
        assert guard.state == CircuitState.BYPASSED
        guard.reset()
        assert guard.state == CircuitState.HEALTHY
        assert guard.health().total_errors == 0
        assert guard.health().consecutive_errors == 0

    def test_reset_resumes_normal_operation(self):
        class OnceFailOptimizer(BandwidthOptimizer):
            fail = True
            def process(self, pkt):
                if self.fail:
                    self.fail = False
                    raise RuntimeError("once")
                return super().process(pkt)

        opt = OnceFailOptimizer()
        guard = SafetyGuard(opt, circuit_threshold=1)
        guard.process(_make_packet())   # trips
        assert guard.state == CircuitState.BYPASSED
        guard.reset()
        result = guard.process(_make_packet())   # now works
        assert isinstance(result, ProcessResult)
        assert guard.state == CircuitState.HEALTHY


# ── health status ─────────────────────────────────────────────────────────────

class TestHealthStatus:
    def test_to_dict_contains_expected_keys(self):
        guard = _make_guard()
        d = guard.health().to_dict()
        for key in ("state", "fail_mode", "total_errors", "consecutive_errors",
                    "circuit_threshold", "last_error", "uptime_seconds"):
            assert key in d

    def test_uptime_increases(self):
        guard = _make_guard()
        t1 = guard.health().uptime_seconds
        time.sleep(0.05)
        t2 = guard.health().uptime_seconds
        assert t2 >= t1

    def test_bypass_count_in_stats(self):
        guard = SafetyGuard(BrokenOptimizer(), circuit_threshold=1)
        guard.process(_make_packet())   # error → bypass starts
        guard.process(_make_packet())   # bypassed
        s = guard.stats()
        assert s["safety"]["bypass_count"] >= 1


# ── thread safety ─────────────────────────────────────────────────────────────

class TestSafetyGuardThreadSafety:
    def test_concurrent_process_does_not_corrupt_state(self):
        guard = _make_guard()
        results = []
        lock = threading.Lock()

        def worker():
            for _ in range(100):
                r = guard.process(_make_packet())
                with lock:
                    results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert len(results) == 400
        h = guard.health()
        assert h.total_errors == 0


# ── fail_mode setter ──────────────────────────────────────────────────────────

class TestFailModeSetter:
    def test_fail_mode_can_be_changed_at_runtime(self):
        guard = _make_guard(fail_mode=FailMode.FAIL_OPEN)
        assert guard.fail_mode == FailMode.FAIL_OPEN
        guard.fail_mode = FailMode.FAIL_CLOSED
        assert guard.fail_mode == FailMode.FAIL_CLOSED
