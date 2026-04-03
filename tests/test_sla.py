"""
Tests for bandwidth_optimizer.sla (SLA Enforcement + Backpressure Model)
"""

import time
import threading

import pytest

from bandwidth_optimizer import BandwidthOptimizer, OptimizerConfig, Packet
from bandwidth_optimizer.config import TrafficPriority
from bandwidth_optimizer.sla import (
    BackpressureLevel,
    BackpressureMonitor,
    BackpressureState,
    DEFAULT_SLA_PIPELINE_CEILING_US,
    DEFAULT_SLA_SOJOURN_CEILING_MS,
    SLAConfig,
    SLAMonitor,
    SLAViolation,
)


def _make_packet(port: int = 443, size: int = 200,
                 priority: TrafficPriority = None) -> Packet:
    pkt = Packet(
        src_ip="10.0.0.1", dst_ip="10.0.0.2",
        src_port=1234, dst_port=port,
        protocol="tcp",
        payload=b"X" * size, size_bytes=size,
    )
    if priority is not None:
        pkt.priority = priority
    return pkt


def _make_monitor(pipeline_us=None, sojourn_ms=None) -> SLAMonitor:
    opt = BandwidthOptimizer(OptimizerConfig(
        total_bandwidth_bps=10 * 1024 * 1024 * 1024,  # no drops
        max_queue_size=10_000,
    ))
    cfg = SLAConfig(
        pipeline_ceiling_us=pipeline_us or dict(DEFAULT_SLA_PIPELINE_CEILING_US),
        sojourn_ceiling_ms=sojourn_ms or dict(DEFAULT_SLA_SOJOURN_CEILING_MS),
    )
    return SLAMonitor(opt, cfg)


# ── SLAConfig defaults ────────────────────────────────────────────────────────

class TestSLAConfig:
    def test_defaults_have_all_priorities(self):
        cfg = SLAConfig()
        for p in TrafficPriority:
            assert p in cfg.pipeline_ceiling_us
            assert p in cfg.sojourn_ceiling_ms

    def test_critical_tighter_than_background(self):
        cfg = SLAConfig()
        assert (cfg.pipeline_ceiling_us[TrafficPriority.CRITICAL] <
                cfg.pipeline_ceiling_us[TrafficPriority.BACKGROUND])
        assert (cfg.sojourn_ceiling_ms[TrafficPriority.CRITICAL] <
                cfg.sojourn_ceiling_ms[TrafficPriority.BACKGROUND])

    def test_custom_ceilings(self):
        cfg = SLAConfig(
            pipeline_ceiling_us={TrafficPriority.HIGH: 999.0},
            sojourn_ceiling_ms={TrafficPriority.HIGH: 50.0},
        )
        assert cfg.pipeline_ceiling_us[TrafficPriority.HIGH] == 999.0


# ── SLAViolation ─────────────────────────────────────────────────────────────

class TestSLAViolation:
    def test_overage_pct(self):
        v = SLAViolation(
            violation_type="pipeline",
            priority=TrafficPriority.HIGH,
            ceiling=100.0,
            actual=150.0,
        )
        assert v.overage_pct == pytest.approx(0.5)

    def test_to_dict_keys(self):
        v = SLAViolation("pipeline", TrafficPriority.CRITICAL, 500.0, 600.0)
        d = v.to_dict()
        for key in ("type", "priority", "ceiling", "actual", "overage_pct", "timestamp"):
            assert key in d

    def test_priority_name_in_dict(self):
        v = SLAViolation("sojourn", TrafficPriority.MEDIUM, 500.0, 700.0)
        assert v.to_dict()["priority"] == "MEDIUM"


# ── SLAMonitor – normal operation ─────────────────────────────────────────────

class TestSLAMonitorNormal:
    def test_process_returns_result(self):
        mon = _make_monitor()
        result = mon.process(_make_packet())
        assert result is not None

    def test_initial_stats_zero(self):
        mon = _make_monitor()
        s = mon.sla_stats()
        assert s.total_violations == 0
        assert s.packets_expired == 0

    def test_stats_dict_keys(self):
        mon = _make_monitor()
        d = mon.sla_stats().to_dict()
        for key in ("total_violations", "pipeline_violations_by_priority",
                    "sojourn_violations_by_priority", "packets_expired",
                    "recent_violations"):
            assert key in d

    def test_reset_clears_violations(self):
        # Force a pipeline violation with a tiny ceiling
        mon = _make_monitor(pipeline_us={p: 0.001 for p in TrafficPriority})
        for _ in range(5):
            mon.process(_make_packet())
        assert mon.sla_stats().total_violations > 0
        mon.reset_sla_stats()
        assert mon.sla_stats().total_violations == 0

    def test_stats_includes_sla(self):
        mon = _make_monitor()
        s = mon.stats()
        assert "sla" in s

    def test_optimizer_property(self):
        opt = BandwidthOptimizer()
        mon = SLAMonitor(opt)
        assert mon.optimizer is opt

    def test_config_property(self):
        cfg = SLAConfig()
        opt = BandwidthOptimizer()
        mon = SLAMonitor(opt, cfg)
        assert mon.config is cfg


# ── SLAMonitor – pipeline violation detection ─────────────────────────────────

class TestSLAMonitorPipelineViolation:
    def test_pipeline_violation_recorded(self):
        # Set ceiling to 0.001 µs (always exceeded)
        mon = _make_monitor(pipeline_us={p: 0.001 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.HIGH
        mon.process(pkt)
        s = mon.sla_stats()
        assert s.total_violations >= 1
        assert s.pipeline_violations["HIGH"] >= 1

    def test_no_violation_when_ceiling_generous(self):
        # 10-second ceiling – no chance of exceeding
        mon = _make_monitor(pipeline_us={p: 10_000_000.0 for p in TrafficPriority})
        for _ in range(10):
            mon.process(_make_packet())
        assert mon.sla_stats().pipeline_violations.get("HIGH", 0) == 0

    def test_violation_type_is_pipeline(self):
        mon = _make_monitor(pipeline_us={p: 0.001 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.CRITICAL
        mon.process(pkt)
        recent = mon.sla_stats().recent_violations
        assert any(v.violation_type == "pipeline" for v in recent)

    def test_recent_violations_capped(self):
        cfg = SLAConfig(
            pipeline_ceiling_us={p: 0.001 for p in TrafficPriority},
            max_violations_tracked=5,
        )
        opt = BandwidthOptimizer()
        mon = SLAMonitor(opt, cfg)
        for _ in range(20):
            pkt = _make_packet()
            pkt.priority = TrafficPriority.MEDIUM
            mon.process(pkt)
        assert len(mon.sla_stats().recent_violations) <= 5


# ── SLAMonitor – sojourn enforcement ─────────────────────────────────────────

class TestSLAMonitorSojourn:
    def test_expired_packet_skipped_at_dequeue(self):
        # Use a 0 ms sojourn ceiling (always expired)
        mon = _make_monitor(sojourn_ms={p: 0.0 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.HIGH
        mon.process(pkt)            # enqueued → enqueued_at set by scheduler
        # Artificially age the packet
        pkt.enqueued_at = time.monotonic() - 1000
        result = mon.dequeue()      # should skip the stale packet
        assert result is None
        assert mon.sla_stats().packets_expired >= 1

    def test_fresh_packet_not_expired(self):
        # Very generous sojourn ceiling
        mon = _make_monitor(sojourn_ms={p: 60_000.0 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.HIGH
        mon.process(pkt)
        result = mon.dequeue()
        # Should NOT be expired
        assert result is not None
        assert mon.sla_stats().packets_expired == 0

    def test_sojourn_violation_recorded(self):
        mon = _make_monitor(sojourn_ms={p: 0.0 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.MEDIUM
        mon.process(pkt)
        pkt.enqueued_at = time.monotonic() - 1000
        mon.dequeue()
        s = mon.sla_stats()
        assert s.sojourn_violations.get("MEDIUM", 0) >= 1

    def test_dequeue_returns_none_on_empty_queue(self):
        mon = _make_monitor()
        assert mon.dequeue() is None

    def test_sojourn_violation_type(self):
        mon = _make_monitor(sojourn_ms={p: 0.0 for p in TrafficPriority})
        pkt = _make_packet()
        pkt.priority = TrafficPriority.LOW
        mon.process(pkt)
        pkt.enqueued_at = time.monotonic() - 1000
        mon.dequeue()
        recent = mon.sla_stats().recent_violations
        assert any(v.violation_type == "sojourn" for v in recent)


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestSLAMonitorThreadSafety:
    def test_concurrent_process_safe(self):
        mon = _make_monitor(pipeline_us={p: 0.001 for p in TrafficPriority})
        errors = []

        def worker():
            for _ in range(50):
                try:
                    pkt = _make_packet()
                    pkt.priority = TrafficPriority.MEDIUM
                    mon.process(pkt)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert errors == []
        assert mon.sla_stats().total_violations >= 0  # no assertion, just no crash


# ══════════════════════════════════════════════════════════════════════════════
#  BackpressureMonitor
# ══════════════════════════════════════════════════════════════════════════════

class TestBackpressureMonitor:
    def _make_monitor(self, soft=0.60, hard=0.85) -> BackpressureMonitor:
        opt = BandwidthOptimizer(OptimizerConfig(max_queue_size=100))
        return BackpressureMonitor(opt, soft_threshold=soft,
                                   hard_threshold=hard)

    def test_initial_level_none(self):
        mon = self._make_monitor()
        state = mon.update()
        assert state.level == BackpressureLevel.NONE

    def test_state_returns_last_update(self):
        mon = self._make_monitor()
        s1 = mon.update()
        s2 = mon.state()
        assert s1.level == s2.level

    def test_soft_backpressure_at_fill(self):
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,
            max_queue_size=10,
        ))
        mon = BackpressureMonitor(opt, soft_threshold=0.5, hard_threshold=0.9)

        # Fill queue past soft threshold
        for _ in range(7):  # 70% fill
            pkt = Packet(dst_port=443, protocol="tcp",
                         payload=b"X" * 64, size_bytes=64,
                         priority=TrafficPriority.CRITICAL)
            opt.process(pkt)

        state = mon.update()
        assert state.queue_fill_ratio > 0.5
        assert state.level in (BackpressureLevel.SOFT, BackpressureLevel.HARD)

    def test_hard_backpressure_at_fill(self):
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,
            max_queue_size=10,
        ))
        mon = BackpressureMonitor(opt, soft_threshold=0.5, hard_threshold=0.85)

        for _ in range(10):  # 100% fill
            pkt = Packet(dst_port=443, protocol="tcp",
                         payload=b"X" * 64, size_bytes=64,
                         priority=TrafficPriority.CRITICAL)
            opt.process(pkt)

        state = mon.update()
        assert state.level == BackpressureLevel.HARD
        assert state.recommended_throttle_pct == 100.0

    def test_none_level_zero_throttle(self):
        mon = self._make_monitor()
        state = mon.update()
        if state.level == BackpressureLevel.NONE:
            assert state.recommended_throttle_pct == 0.0

    def test_soft_throttle_between_0_and_100(self):
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,
            max_queue_size=10,
        ))
        mon = BackpressureMonitor(opt, soft_threshold=0.5, hard_threshold=0.9)
        for _ in range(7):
            pkt = Packet(dst_port=443, protocol="tcp",
                         payload=b"X" * 64, size_bytes=64,
                         priority=TrafficPriority.CRITICAL)
            opt.process(pkt)
        state = mon.update()
        if state.level == BackpressureLevel.SOFT:
            assert 0.0 < state.recommended_throttle_pct < 100.0

    def test_invalid_thresholds_raise(self):
        opt = BandwidthOptimizer()
        with pytest.raises(ValueError):
            BackpressureMonitor(opt, soft_threshold=0.9, hard_threshold=0.5)

    def test_to_dict_keys(self):
        mon = self._make_monitor()
        state = mon.update()
        d = state.to_dict()
        for key in ("level", "queue_fill_ratio", "arrival_rate_pps",
                    "drain_rate_pps", "recommended_throttle_pct"):
            assert key in d

    def test_rates_zero_on_single_sample(self):
        mon = self._make_monitor()
        state = mon.update()
        # Only one sample – can't compute rate
        assert state.arrival_rate_pps == 0.0
        assert state.drain_rate_pps == 0.0


# ── BackpressureLevel enum ────────────────────────────────────────────────────

class TestBackpressureLevel:
    def test_values(self):
        assert BackpressureLevel.NONE.value == "none"
        assert BackpressureLevel.SOFT.value == "soft"
        assert BackpressureLevel.HARD.value == "hard"
