"""
Tests for bandwidth_optimizer.flow_tracker (Upgrade 2 – flow intelligence)
"""

import time

import pytest

from bandwidth_optimizer.classifier import Packet
from bandwidth_optimizer.config import TrafficPriority
from bandwidth_optimizer.flow_tracker import FlowKey, FlowRecord, FlowTracker


def _pkt(src_ip="10.0.0.1", dst_ip="10.0.0.2", src_port=1234, dst_port=443,
         protocol="tcp", size=200) -> Packet:
    return Packet(
        src_ip=src_ip, dst_ip=dst_ip,
        src_port=src_port, dst_port=dst_port,
        protocol=protocol,
        payload=b"A" * size, size_bytes=size,
    )


class TestFlowKey:
    def test_same_key_for_both_directions(self):
        pkt_fwd = _pkt(src_ip="1.1.1.1", src_port=1000,
                       dst_ip="2.2.2.2", dst_port=443)
        pkt_rev = _pkt(src_ip="2.2.2.2", src_port=443,
                       dst_ip="1.1.1.1", dst_port=1000)
        assert FlowKey.from_packet(pkt_fwd) == FlowKey.from_packet(pkt_rev)

    def test_different_protocols_different_keys(self):
        pkt_tcp = _pkt(protocol="tcp")
        pkt_udp = _pkt(protocol="udp")
        assert FlowKey.from_packet(pkt_tcp) != FlowKey.from_packet(pkt_udp)

    def test_different_ports_different_keys(self):
        pkt_a = _pkt(dst_port=443)
        pkt_b = _pkt(dst_port=80)
        assert FlowKey.from_packet(pkt_a) != FlowKey.from_packet(pkt_b)

    def test_hashable(self):
        key = FlowKey.from_packet(_pkt())
        d = {key: "value"}
        assert d[key] == "value"

    def test_protocol_normalised_to_lowercase(self):
        pkt = _pkt(protocol="TCP")
        key = FlowKey.from_packet(pkt)
        assert key.protocol == "tcp"


class TestFlowRecord:
    def test_initial_state(self):
        key = FlowKey.from_packet(_pkt())
        rec = FlowRecord(key=key)
        assert rec.packet_count == 0
        assert rec.byte_count == 0
        assert rec.latency_score == 0.0

    def test_update_increments_counters(self):
        pkt = _pkt(size=500)
        key = FlowKey.from_packet(pkt)
        rec = FlowRecord(key=key)
        rec.update(pkt)
        assert rec.packet_count == 1
        assert rec.byte_count == 500

    def test_scores_computed_after_multiple_updates(self):
        key = FlowKey.from_packet(_pkt())
        rec = FlowRecord(key=key)
        for _ in range(20):
            rec.update(_pkt(size=50))   # small packets → latency-sensitive
        # latency_score should be higher for small frequent packets
        assert rec.latency_score > 0.0
        assert 0.0 <= rec.latency_score <= 1.0
        assert 0.0 <= rec.bandwidth_score <= 1.0
        assert 0.0 <= rec.burst_score <= 1.0

    def test_large_packets_raise_bandwidth_score(self):
        key = FlowKey.from_packet(_pkt())
        rec = FlowRecord(key=key)
        # Simulate 10 MB/s worth of traffic in a short window
        for _ in range(100):
            rec.update(_pkt(size=1500))
        assert rec.bandwidth_score > rec.latency_score

    def test_to_dict_contains_expected_keys(self):
        pkt = _pkt()
        key = FlowKey.from_packet(pkt)
        rec = FlowRecord(key=key)
        rec.update(pkt)
        d = rec.to_dict()
        for field in ("src_ip", "dst_ip", "src_port", "dst_port", "protocol",
                      "packet_count", "byte_count",
                      "latency_score", "bandwidth_score", "burst_score"):
            assert field in d

    def test_age_and_idle_seconds(self):
        pkt = _pkt()
        key = FlowKey.from_packet(pkt)
        rec = FlowRecord(key=key)
        time.sleep(0.05)
        assert rec.age_seconds >= 0.04
        assert rec.idle_seconds >= 0.04


class TestFlowTracker:
    def test_creates_new_record_on_first_packet(self):
        tracker = FlowTracker()
        pkt = _pkt()
        record = tracker.update(pkt)
        assert record.packet_count == 1
        assert tracker.flow_count() == 1

    def test_same_flow_accumulates(self):
        tracker = FlowTracker()
        pkt = _pkt()
        tracker.update(pkt)
        tracker.update(pkt)
        assert tracker.flow_count() == 1
        record = tracker.get(pkt)
        assert record.packet_count == 2

    def test_different_flows_tracked_separately(self):
        tracker = FlowTracker()
        tracker.update(_pkt(dst_port=443))
        tracker.update(_pkt(dst_port=80))
        assert tracker.flow_count() == 2

    def test_get_returns_none_for_unknown_flow(self):
        tracker = FlowTracker()
        assert tracker.get(_pkt()) is None

    def test_expired_flows_are_evicted(self):
        tracker = FlowTracker(flow_ttl=0.05)
        tracker.update(_pkt())
        assert tracker.flow_count() == 1
        time.sleep(0.1)
        evicted = tracker.evict_expired()
        assert evicted == 1
        assert tracker.flow_count() == 0

    def test_max_flows_evicts_oldest(self):
        tracker = FlowTracker(max_flows=3)
        for port in range(5):
            tracker.update(_pkt(dst_port=port + 1000))
        assert tracker.flow_count() <= 3

    def test_priority_hint_boosts_latency_sensitive(self):
        tracker = FlowTracker(priority_boost_threshold=0.0)
        pkt = _pkt()
        record = tracker.update(pkt)
        # Manually set a high latency score
        record.latency_score = 1.0
        hint = tracker.priority_hint(TrafficPriority.HIGH, record)
        assert hint.value < TrafficPriority.HIGH.value  # boosted

    def test_priority_hint_demotes_bandwidth_heavy(self):
        tracker = FlowTracker(priority_demote_threshold=0.0)
        pkt = _pkt()
        record = tracker.update(pkt)
        record.bandwidth_score = 1.0
        hint = tracker.priority_hint(TrafficPriority.LOW, record)
        assert hint.value > TrafficPriority.LOW.value  # demoted

    def test_priority_hint_no_change_for_neutral(self):
        tracker = FlowTracker()
        pkt = _pkt()
        record = tracker.update(pkt)
        record.latency_score = 0.0
        record.bandwidth_score = 0.0
        hint = tracker.priority_hint(TrafficPriority.MEDIUM, record)
        assert hint == TrafficPriority.MEDIUM

    def test_critical_never_boosted(self):
        tracker = FlowTracker(priority_boost_threshold=0.0)
        pkt = _pkt()
        record = tracker.update(pkt)
        record.latency_score = 1.0
        hint = tracker.priority_hint(TrafficPriority.CRITICAL, record)
        assert hint == TrafficPriority.CRITICAL

    def test_all_flows_snapshot(self):
        tracker = FlowTracker()
        for port in [443, 80, 53]:
            tracker.update(_pkt(dst_port=port))
        flows = tracker.all_flows()
        assert len(flows) == 3
        assert all(isinstance(f, dict) for f in flows)

    def test_optimizer_exposes_flow_tracker(self):
        """BandwidthOptimizer.flow_tracker property should be accessible."""
        from bandwidth_optimizer import BandwidthOptimizer
        opt = BandwidthOptimizer()
        assert isinstance(opt.flow_tracker, FlowTracker)

    def test_optimizer_process_updates_flow(self):
        """Processing a packet should create a flow record."""
        from bandwidth_optimizer import BandwidthOptimizer, Packet
        opt = BandwidthOptimizer()
        pkt = Packet(dst_port=443, protocol="tcp",
                     payload=b"X" * 100, size_bytes=100)
        opt.process(pkt)
        assert opt.flow_tracker.flow_count() >= 1
