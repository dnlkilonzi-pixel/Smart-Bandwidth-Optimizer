"""
Tests for bandwidth_optimizer.benchmark (Performance Benchmarking Layer)
"""

import pytest

from bandwidth_optimizer import BandwidthOptimizer, OptimizerConfig, Packet
from bandwidth_optimizer.benchmark import (
    BenchmarkConfig,
    BenchmarkResult,
    Benchmarker,
    LatencyStats,
    _default_packet_factory,
)


# ── LatencyStats ──────────────────────────────────────────────────────────────

class TestLatencyStats:
    def test_from_empty_samples(self):
        ls = LatencyStats.from_samples([])
        assert ls.sample_count == 0
        assert ls.mean_us == 0.0

    def test_from_single_sample(self):
        ls = LatencyStats.from_samples([42.0])
        assert ls.min_us == pytest.approx(42.0)
        assert ls.max_us == pytest.approx(42.0)
        assert ls.mean_us == pytest.approx(42.0)
        assert ls.sample_count == 1

    def test_percentiles_ordered(self):
        samples = [float(i) for i in range(1, 101)]
        ls = LatencyStats.from_samples(samples)
        assert ls.min_us <= ls.p50_us <= ls.p95_us <= ls.p99_us <= ls.max_us

    def test_to_dict_keys(self):
        ls = LatencyStats.from_samples([1.0, 2.0, 3.0])
        d = ls.to_dict()
        for key in ("min_us", "mean_us", "p50_us", "p95_us", "p99_us", "max_us",
                    "sample_count"):
            assert key in d


# ── BenchmarkResult ───────────────────────────────────────────────────────────

class TestBenchmarkResult:
    def _make_result(self, n=100, dropped=10, bytes_in=1000, bytes_out=800):
        ls = LatencyStats.from_samples([1.0] * n)
        return BenchmarkResult(
            packets_processed=n,
            duration_seconds=1.0,
            packets_per_second=float(n),
            total_latency=ls,
            packets_dropped=dropped,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
        )

    def test_drop_rate(self):
        r = self._make_result(n=100, dropped=20)
        assert r.drop_rate == pytest.approx(0.2)

    def test_compression_ratio(self):
        r = self._make_result(bytes_in=1000, bytes_out=600)
        assert r.compression_ratio == pytest.approx(0.6)

    def test_memory_delta(self):
        ls = LatencyStats.from_samples([1.0])
        r = BenchmarkResult(
            packets_processed=1, duration_seconds=1.0, packets_per_second=1.0,
            total_latency=ls,
            memory_rss_before_bytes=1000, memory_rss_after_bytes=1500,
        )
        assert r.memory_delta_bytes == 500

    def test_summary_contains_expected_strings(self):
        r = self._make_result()
        s = r.summary()
        assert "Throughput" in s
        assert "Drop rate" in s
        assert "Compression" in s
        assert "Memory delta" in s

    def test_to_dict_keys(self):
        r = self._make_result()
        d = r.to_dict()
        for key in ("packets_processed", "duration_seconds", "packets_per_second",
                    "total_latency", "stage_latency", "drop_rate", "compression_ratio",
                    "memory_delta_bytes"):
            assert key in d


# ── packet factory ────────────────────────────────────────────────────────────

class TestDefaultPacketFactory:
    def test_produces_packets(self):
        factory = _default_packet_factory(200)
        pkt = factory()
        assert isinstance(pkt, Packet)
        assert pkt.size_bytes == 200
        assert len(pkt.payload) == 200

    def test_zero_size_uses_random(self):
        factory = _default_packet_factory(0)
        sizes = set(factory().size_bytes for _ in range(20))
        assert len(sizes) > 1   # random sizes differ


# ── Benchmarker ───────────────────────────────────────────────────────────────

class TestBenchmarker:
    def test_run_returns_result(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert isinstance(result, BenchmarkResult)

    def test_packets_processed_matches_config(self):
        cfg = BenchmarkConfig(n_packets=200, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert result.packets_processed == 200

    def test_packets_per_second_positive(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert result.packets_per_second > 0

    def test_total_latency_positive(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert result.total_latency.mean_us > 0

    def test_stage_latency_populated_when_enabled(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=True)
        result = Benchmarker.run(cfg=cfg)
        assert len(result.stage_latency) > 0
        assert "flow_track" in result.stage_latency
        assert "classify" in result.stage_latency

    def test_stage_latency_empty_when_disabled(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert result.stage_latency == {}

    def test_custom_optimizer(self):
        opt = BandwidthOptimizer(OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,  # no drops
            max_queue_size=50_000,
        ))
        cfg = BenchmarkConfig(n_packets=50, warmup_packets=5, measure_stages=False)
        result = Benchmarker.run(optimizer=opt, cfg=cfg)
        assert result.packets_processed == 50

    def test_custom_packet_factory(self):
        def fixed_factory():
            return Packet(dst_port=53, protocol="udp",
                          payload=b"\x00" * 32, size_bytes=32)

        cfg = BenchmarkConfig(
            n_packets=50, warmup_packets=5, measure_stages=False,
            packet_factory=fixed_factory,
        )
        result = Benchmarker.run(cfg=cfg)
        assert result.bytes_in == 50 * 32

    def test_drop_rate_within_range(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=10, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert 0.0 <= result.drop_rate <= 1.0

    def test_compression_ratio_positive(self):
        cfg = BenchmarkConfig(n_packets=100, warmup_packets=0,
                              packet_size_bytes=512, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        assert result.compression_ratio >= 0.0

    def test_to_dict_serialisable(self):
        import json
        cfg = BenchmarkConfig(n_packets=50, warmup_packets=5, measure_stages=False)
        result = Benchmarker.run(cfg=cfg)
        # Should not raise
        json.dumps(result.to_dict())
