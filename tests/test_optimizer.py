"""
Tests for bandwidth_optimizer.optimizer (BandwidthOptimizer)
"""

import pytest

from bandwidth_optimizer import (
    BandwidthOptimizer,
    DeploymentMode,
    OptimizerConfig,
    Packet,
    TrafficPriority,
)


def _make_packet(dst_port: int, protocol: str = "tcp", size: int = 200) -> Packet:
    return Packet(
        dst_port=dst_port,
        protocol=protocol,
        payload=b"X" * size,
        size_bytes=size,
    )


class TestBandwidthOptimizer:
    def setup_method(self):
        self.cfg = OptimizerConfig(
            mode=DeploymentMode.LOCAL_SERVER,
            total_bandwidth_bps=10 * 1024 * 1024,
            max_queue_size=64,
            compression_threshold_bytes=50,
        )
        self.opt = BandwidthOptimizer(config=self.cfg)

    # ── classification ────────────────────────────────────────────────────

    def test_process_classifies_packet(self):
        pkt = _make_packet(dst_port=443, protocol="tcp")
        assert pkt.priority is None
        self.opt.process(pkt)
        assert pkt.priority == TrafficPriority.HIGH

    def test_process_respects_preset_priority(self):
        pkt = _make_packet(dst_port=9999, protocol="tcp")
        pkt.priority = TrafficPriority.CRITICAL  # pre-set
        result = self.opt.process(pkt)
        assert pkt.priority == TrafficPriority.CRITICAL
        assert not result.dropped

    # ── compression ───────────────────────────────────────────────────────

    def test_large_repetitive_payload_is_compressed(self):
        pkt = Packet(
            dst_port=80, protocol="tcp",
            payload=b"A" * 1000,
            size_bytes=1000,
        )
        result = self.opt.process(pkt)
        if not result.dropped:
            # If not dropped, it should have been compressed
            assert result.compressed
            assert result.bytes_saved > 0

    def test_small_payload_not_compressed(self):
        pkt = Packet(
            dst_port=80, protocol="tcp",
            payload=b"A" * 10,   # below threshold of 50
            size_bytes=10,
        )
        result = self.opt.process(pkt)
        if not result.dropped:
            assert not result.compressed

    # ── queue ──────────────────────────────────────────────────────────────

    def test_dequeue_returns_packets_in_priority_order(self):
        # Enqueue packets with different priorities
        self.opt.process(Packet(
            dst_port=6881, protocol="tcp",
            payload=b"bg" * 100, size_bytes=200,
        ))
        self.opt.process(Packet(
            dst_port=5060, protocol="udp",
            payload=b"voip" * 50, size_bytes=200,
        ))
        # VoIP (CRITICAL) must come out before BitTorrent (BACKGROUND)
        first = self.opt.dequeue()
        assert first is not None
        assert first.priority == TrafficPriority.CRITICAL

    def test_queue_size_increases_after_process(self):
        before = self.opt.queue_size()
        pkt = Packet(dst_port=443, protocol="tcp",
                     payload=b"A" * 100, size_bytes=100)
        result = self.opt.process(pkt)
        if not result.dropped:
            assert self.opt.queue_size() == before + 1

    def test_drain_empties_queue(self):
        for port in [443, 80, 53]:
            self.opt.process(Packet(
                dst_port=port, protocol="tcp",
                payload=b"D" * 100, size_bytes=100,
            ))
        packets = list(self.opt.drain())
        assert self.opt.queue_size() == 0

    # ── stats ──────────────────────────────────────────────────────────────

    def test_stats_structure(self):
        self.opt.process(_make_packet(443))
        s = self.opt.stats()
        assert "mode" in s
        assert "packets_received" in s
        assert "packets_dropped" in s
        assert "drop_rate" in s
        assert "bytes_saved_compression" in s
        assert "queue" in s
        assert "drops_by_priority" in s

    def test_stats_counts_received(self):
        self.opt.reset_stats()
        for _ in range(5):
            self.opt.process(_make_packet(443))
        s = self.opt.stats()
        assert s["packets_received"] == 5

    def test_reset_stats(self):
        for _ in range(3):
            self.opt.process(_make_packet(443))
        self.opt.reset_stats()
        s = self.opt.stats()
        assert s["packets_received"] == 0
        assert s["packets_dropped"] == 0

    # ── deployment modes ──────────────────────────────────────────────────

    def test_router_mode(self):
        cfg = OptimizerConfig(mode=DeploymentMode.ROUTER)
        opt = BandwidthOptimizer(config=cfg)
        s = opt.stats()
        assert s["mode"] == "router"

    def test_isp_edge_mode(self):
        cfg = OptimizerConfig(mode=DeploymentMode.ISP_EDGE)
        opt = BandwidthOptimizer(config=cfg)
        s = opt.stats()
        assert s["mode"] == "isp_edge"

    # ── batch processing ──────────────────────────────────────────────────

    def test_process_batch(self):
        packets = [_make_packet(443) for _ in range(5)]
        results = self.opt.process_batch(packets)
        assert len(results) == 5
        received = self.opt.stats()["packets_received"]
        assert received >= 5
