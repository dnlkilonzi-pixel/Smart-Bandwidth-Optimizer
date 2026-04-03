"""
Tests for bandwidth_optimizer.packet_filter
"""

import time

import pytest

from bandwidth_optimizer.classifier import Packet
from bandwidth_optimizer.config import OptimizerConfig, TrafficPriority
from bandwidth_optimizer.packet_filter import PacketFilter, TokenBucket


class TestTokenBucket:
    def test_starts_full(self):
        bucket = TokenBucket(refill_rate=100.0, capacity=200.0)
        assert bucket.available() == pytest.approx(200.0, abs=1.0)

    def test_consume_allowed_when_tokens_available(self):
        bucket = TokenBucket(refill_rate=1000.0, capacity=500.0)
        assert bucket.consume(100) is True
        assert bucket.available() < 500.0

    def test_consume_blocked_when_insufficient_tokens(self):
        bucket = TokenBucket(refill_rate=1.0, capacity=50.0)
        # Drain the bucket
        bucket.consume(50)
        assert bucket.consume(100) is False

    def test_refills_over_time(self):
        bucket = TokenBucket(refill_rate=1000.0, capacity=1000.0)
        bucket.consume(1000)   # empty it
        time.sleep(0.05)       # wait 50 ms → ~50 bytes refilled
        assert bucket.available() >= 40.0  # allow some timing slack

    def test_invalid_refill_rate(self):
        with pytest.raises(ValueError):
            TokenBucket(refill_rate=0, capacity=100)

    def test_invalid_capacity(self):
        with pytest.raises(ValueError):
            TokenBucket(refill_rate=100, capacity=0)


class TestPacketFilter:
    def _make_packet(self, priority: TrafficPriority, size: int = 100) -> Packet:
        pkt = Packet(payload=b"\x00" * size, size_bytes=size)
        pkt.priority = priority
        return pkt

    def test_critical_packet_always_passes_rate_limit(self):
        """CRITICAL packets should pass even when the bucket is exhausted."""
        cfg = OptimizerConfig(
            total_bandwidth_bps=100,
            token_refill_rate={TrafficPriority.CRITICAL: 1.0},
        )
        pf = PacketFilter(config=cfg)
        # Drain CRITICAL bucket first
        pkt = self._make_packet(TrafficPriority.CRITICAL, size=10000)
        decision = pf.should_drop(pkt)
        # First packet: bucket starts full at capacity; may or may not drop
        # but we just need to verify the CRITICAL bypass:
        pkt2 = self._make_packet(TrafficPriority.CRITICAL, size=10000)
        # After the first consumption, force a second large packet – CRITICAL
        # should never be dropped by the rate limiter alone
        for _ in range(50):
            result = pf.should_drop(self._make_packet(TrafficPriority.CRITICAL, size=500))
            # CRITICAL must not be rate-limited
            if result.drop:
                assert "rate_limit" not in result.reason

    def test_background_packet_dropped_when_bucket_empty(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=100,
            # Very small refill rate so the bucket empties quickly
            token_refill_rate={TrafficPriority.BACKGROUND: 0.001},
            token_bucket_capacity_multiplier=0.1,
        )
        pf = PacketFilter(config=cfg)
        dropped = False
        for _ in range(200):
            pkt = self._make_packet(TrafficPriority.BACKGROUND, size=1500)
            decision = pf.should_drop(pkt)
            if decision.drop:
                dropped = True
                break
        assert dropped, "Expected at least one BACKGROUND packet to be dropped"

    def test_drop_counts_accumulate(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=100,
            token_refill_rate={TrafficPriority.LOW: 0.001},
            token_bucket_capacity_multiplier=0.01,
        )
        pf = PacketFilter(config=cfg)
        for _ in range(100):
            pf.should_drop(self._make_packet(TrafficPriority.LOW, size=1000))
        counts = pf.drop_counts()
        assert counts[TrafficPriority.LOW] > 0

    def test_reset_drop_counts(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=100,
            token_refill_rate={TrafficPriority.LOW: 0.001},
            token_bucket_capacity_multiplier=0.01,
        )
        pf = PacketFilter(config=cfg)
        for _ in range(50):
            pf.should_drop(self._make_packet(TrafficPriority.LOW, size=1000))
        pf.reset_drop_counts()
        counts = pf.drop_counts()
        assert all(v == 0 for v in counts.values())

    def test_red_drops_at_full_queue(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024,
            max_queue_size=100,
            red_min_threshold=0.0,
            red_max_threshold=0.5,
        )
        # Simulate a completely full queue
        pf = PacketFilter(
            config=cfg,
            current_queue_size_fn=lambda: 100,  # 100% full
        )
        pkt = self._make_packet(TrafficPriority.HIGH, size=64)
        decision = pf.should_drop(pkt)
        assert decision.drop
        assert "red" in decision.reason

    def test_no_red_when_queue_empty(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024,
            max_queue_size=100,
            red_min_threshold=0.5,
        )
        pf = PacketFilter(
            config=cfg,
            current_queue_size_fn=lambda: 0,  # empty queue
        )
        pkt = self._make_packet(TrafficPriority.HIGH, size=64)
        decision = pf.should_drop(pkt)
        assert not decision.drop

    def test_bucket_available(self):
        cfg = OptimizerConfig(total_bandwidth_bps=1000)
        pf = PacketFilter(config=cfg)
        available = pf.bucket_available(TrafficPriority.HIGH)
        assert available > 0
