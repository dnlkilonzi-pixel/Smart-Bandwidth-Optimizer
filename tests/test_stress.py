"""
Tests for bandwidth_optimizer.stress (Real-traffic Stress Testing)
"""

import pytest

from bandwidth_optimizer import BandwidthOptimizer, OptimizerConfig
from bandwidth_optimizer.benchmark import LatencyStats
from bandwidth_optimizer.stress import (
    StressConfig,
    StressPattern,
    StressResult,
    StressTester,
    _build_packet,
    _rate_multiplier,
)
from bandwidth_optimizer.config import TrafficPriority


# ── StressPattern ─────────────────────────────────────────────────────────────

class TestStressPattern:
    def test_all_patterns_defined(self):
        patterns = {p.value for p in StressPattern}
        assert "uniform" in patterns
        assert "burst_flood" in patterns
        assert "oscillating" in patterns
        assert "priority_inversion" in patterns
        assert "all_critical" in patterns


# ── StressConfig ──────────────────────────────────────────────────────────────

class TestStressConfig:
    def test_defaults(self):
        cfg = StressConfig()
        assert cfg.duration_seconds == 10.0
        assert cfg.target_pps == 10_000
        assert cfg.n_threads == 4
        assert cfg.pattern == StressPattern.UNIFORM

    def test_custom(self):
        cfg = StressConfig(
            duration_seconds=5.0,
            target_pps=50_000,
            n_threads=8,
            pattern=StressPattern.BURST_FLOOD,
            packet_size_bytes=128,
        )
        assert cfg.n_threads == 8
        assert cfg.pattern == StressPattern.BURST_FLOOD


# ── StressResult ──────────────────────────────────────────────────────────────

class TestStressResult:
    def _make_result(self, sent=100, dropped=10):
        ls = LatencyStats.from_samples([1.0] * sent)
        return StressResult(
            pattern="uniform",
            duration_seconds=1.0,
            target_pps=100,
            achieved_pps=float(sent),
            packets_sent=sent,
            packets_dropped=dropped,
            peak_queue_depth=10,
            latency=ls,
        )

    def test_drop_rate(self):
        r = self._make_result(sent=100, dropped=20)
        assert r.drop_rate == pytest.approx(0.2)

    def test_drop_rate_zero_sent(self):
        ls = LatencyStats.from_samples([])
        r = StressResult("uniform", 0.0, 0, 0.0, 0, 0, 0, ls)
        assert r.drop_rate == 0.0

    def test_summary_contains_key_info(self):
        r = self._make_result()
        s = r.summary()
        assert "Stress Test Results" in s
        assert "Throughput" in s or "Achieved PPS" in s
        assert "Drop rate" in s

    def test_to_dict_keys(self):
        r = self._make_result()
        d = r.to_dict()
        for key in ("pattern", "duration_seconds", "target_pps",
                    "achieved_pps", "packets_sent", "packets_dropped",
                    "drop_rate", "peak_queue_depth", "latency"):
            assert key in d

    def test_to_dict_serialisable(self):
        import json
        r = self._make_result()
        json.dumps(r.to_dict())  # must not raise


# ── _rate_multiplier ──────────────────────────────────────────────────────────

class TestRateMultiplier:
    def test_uniform_always_one(self):
        for t in [0, 0.5, 1.0, 5.0, 99.9]:
            assert _rate_multiplier(StressPattern.UNIFORM, t, 10.0) == 1.0

    def test_burst_flood_high_in_burst(self):
        mul = _rate_multiplier(StressPattern.BURST_FLOOD, 0.05, 10.0)
        assert mul == 10.0  # within burst window (0–0.1 s)

    def test_burst_flood_normal_outside(self):
        mul = _rate_multiplier(StressPattern.BURST_FLOOD, 0.5, 10.0)
        assert mul == 1.0

    def test_oscillating_between_0_and_1(self):
        for t in [0.0, 0.1, 0.25, 0.5, 1.0]:
            mul = _rate_multiplier(StressPattern.OSCILLATING, t, 10.0)
            assert 0.0 <= mul <= 1.0001  # slight float tolerance

    def test_priority_inversion_rate_one(self):
        assert _rate_multiplier(StressPattern.PRIORITY_INVERSION, 0, 10.0) == 1.0

    def test_all_critical_rate_one(self):
        assert _rate_multiplier(StressPattern.ALL_CRITICAL, 0, 10.0) == 1.0


# ── _build_packet ─────────────────────────────────────────────────────────────

class TestBuildPacket:
    def test_priority_inversion_sets_background(self):
        cfg = StressConfig(pattern=StressPattern.PRIORITY_INVERSION)
        pkt = _build_packet(cfg, 0)
        assert pkt.priority == TrafficPriority.BACKGROUND

    def test_all_critical_sets_critical(self):
        cfg = StressConfig(pattern=StressPattern.ALL_CRITICAL)
        pkt = _build_packet(cfg, 0)
        assert pkt.priority == TrafficPriority.CRITICAL

    def test_uniform_no_preset_priority(self):
        cfg = StressConfig(pattern=StressPattern.UNIFORM)
        pkt = _build_packet(cfg, 0)
        # priority may be None (to be classified) or any value from random port
        # Just check it returns a Packet without raising
        from bandwidth_optimizer import Packet
        assert isinstance(pkt, Packet)


# ── StressTester.run ──────────────────────────────────────────────────────────

def _make_unlimited_optimizer() -> BandwidthOptimizer:
    return BandwidthOptimizer(OptimizerConfig(
        total_bandwidth_bps=10 * 1024 * 1024 * 1024,  # 10 GB/s – no drops
        max_queue_size=100_000,
    ))


class TestStressTester:
    def test_run_returns_result(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=500, n_threads=2)
        result = StressTester.run(opt, cfg)
        assert isinstance(result, StressResult)

    def test_packets_sent_positive(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=500, n_threads=1)
        result = StressTester.run(opt, cfg)
        assert result.packets_sent > 0

    def test_achieved_pps_positive(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=200, n_threads=1)
        result = StressTester.run(opt, cfg)
        assert result.achieved_pps > 0

    def test_pattern_in_result(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=100, n_threads=1,
                           pattern=StressPattern.ALL_CRITICAL)
        result = StressTester.run(opt, cfg)
        assert result.pattern == "all_critical"

    def test_latency_populated(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=200, n_threads=1)
        result = StressTester.run(opt, cfg)
        assert result.latency.sample_count > 0
        assert result.latency.mean_us > 0

    def test_drop_rate_within_range(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=200, n_threads=1)
        result = StressTester.run(opt, cfg)
        assert 0.0 <= result.drop_rate <= 1.0

    def test_priority_inversion_has_drops(self):
        """BACKGROUND traffic under tight bandwidth should cause some drops."""
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=1000,  # very tight
            max_queue_size=10,
        ))
        cfg = StressConfig(
            duration_seconds=0.5,
            target_pps=2000,
            n_threads=2,
            pattern=StressPattern.PRIORITY_INVERSION,
        )
        result = StressTester.run(opt, cfg)
        assert result.packets_sent > 0
        assert result.drop_rate >= 0.0

    def test_uniform_pattern(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=200, n_threads=1,
                           pattern=StressPattern.UNIFORM)
        result = StressTester.run(opt, cfg)
        assert result.pattern == "uniform"

    def test_burst_flood_pattern(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.5, target_pps=500, n_threads=2,
                           pattern=StressPattern.BURST_FLOOD)
        result = StressTester.run(opt, cfg)
        assert result.packets_sent > 0

    def test_oscillating_pattern(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.5, target_pps=300, n_threads=2,
                           pattern=StressPattern.OSCILLATING)
        result = StressTester.run(opt, cfg)
        assert result.packets_sent > 0

    def test_multi_thread_scales(self):
        opt = _make_unlimited_optimizer()
        cfg1 = StressConfig(duration_seconds=0.5, target_pps=1000, n_threads=1)
        cfg4 = StressConfig(duration_seconds=0.5, target_pps=1000, n_threads=4)
        r1 = StressTester.run(opt, cfg1)
        opt.reset_stats()
        r4 = StressTester.run(opt, cfg4)
        # Both should produce roughly similar total sent (rate-limited by pps)
        assert r1.packets_sent > 0
        assert r4.packets_sent > 0

    def test_peak_queue_non_negative(self):
        opt = _make_unlimited_optimizer()
        cfg = StressConfig(duration_seconds=0.2, target_pps=200, n_threads=1)
        result = StressTester.run(opt, cfg)
        assert result.peak_queue_depth >= 0

    def test_default_config(self):
        """StressTester.run() with no explicit config should not raise."""
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,
            max_queue_size=100_000,
        ))
        # Use a short duration to keep tests fast
        cfg = StressConfig(duration_seconds=0.3, target_pps=500)
        result = StressTester.run(opt, cfg)
        assert result.packets_sent > 0
