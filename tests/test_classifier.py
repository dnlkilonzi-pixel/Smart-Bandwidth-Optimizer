"""
Tests for bandwidth_optimizer.classifier
"""

import pytest

from bandwidth_optimizer.classifier import (
    ClassificationRule,
    Packet,
    TrafficClassifier,
)
from bandwidth_optimizer.config import TrafficPriority


class TestPacket:
    def test_size_inferred_from_payload(self):
        pkt = Packet(payload=b"hello")
        assert pkt.size_bytes == 5

    def test_explicit_size_not_overridden(self):
        pkt = Packet(payload=b"hello", size_bytes=1500)
        assert pkt.size_bytes == 1500

    def test_default_priority_is_none(self):
        pkt = Packet()
        assert pkt.priority is None


class TestTrafficClassifier:
    def setup_method(self):
        self.clf = TrafficClassifier()

    def _pkt(self, **kwargs) -> Packet:
        return Packet(**kwargs)

    def test_voip_is_critical(self):
        pkt = self._pkt(dst_port=5060, protocol="udp")
        assert self.clf.classify(pkt) == TrafficPriority.CRITICAL

    def test_rtp_is_critical(self):
        pkt = self._pkt(dst_port=5004, protocol="udp")
        assert self.clf.classify(pkt) == TrafficPriority.CRITICAL

    def test_icmp_is_critical(self):
        pkt = self._pkt(protocol="icmp")
        assert self.clf.classify(pkt) == TrafficPriority.CRITICAL

    def test_dns_is_high(self):
        pkt = self._pkt(dst_port=53, protocol="udp")
        assert self.clf.classify(pkt) == TrafficPriority.HIGH

    def test_https_is_high(self):
        pkt = self._pkt(dst_port=443, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.HIGH

    def test_ssh_is_high(self):
        pkt = self._pkt(dst_port=22, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.HIGH

    def test_http_is_high(self):
        pkt = self._pkt(dst_port=80, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.HIGH

    def test_smtp_is_medium(self):
        pkt = self._pkt(dst_port=25, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.MEDIUM

    def test_ftp_is_low(self):
        pkt = self._pkt(dst_port=21, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.LOW

    def test_ntp_is_low(self):
        pkt = self._pkt(dst_port=123, protocol="udp")
        assert self.clf.classify(pkt) == TrafficPriority.LOW

    def test_bittorrent_is_background(self):
        pkt = self._pkt(dst_port=6881, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.BACKGROUND

    def test_unknown_port_gets_default(self):
        pkt = self._pkt(dst_port=9999, protocol="tcp")
        priority = self.clf.classify(pkt)
        assert priority == TrafficPriority.MEDIUM

    def test_priority_is_written_to_packet(self):
        pkt = self._pkt(dst_port=443, protocol="tcp")
        self.clf.classify(pkt)
        assert pkt.priority == TrafficPriority.HIGH

    def test_classify_batch(self):
        packets = [
            self._pkt(dst_port=5060, protocol="udp"),
            self._pkt(dst_port=53,   protocol="udp"),
        ]
        priorities = self.clf.classify_batch(packets)
        assert priorities[0] == TrafficPriority.CRITICAL
        assert priorities[1] == TrafficPriority.HIGH

    def test_add_custom_rule_prepend(self):
        rule = ClassificationRule(
            priority=TrafficPriority.CRITICAL,
            ports=(12345,),
            protocols=("tcp",),
        )
        self.clf.add_rule(rule, index=0)
        pkt = self._pkt(dst_port=12345, protocol="tcp")
        assert self.clf.classify(pkt) == TrafficPriority.CRITICAL

    def test_remove_rule(self):
        initial_count = len(self.clf.rules)
        rule = ClassificationRule(priority=TrafficPriority.LOW, ports=(11111,))
        self.clf.add_rule(rule, index=0)
        assert len(self.clf.rules) == initial_count + 1
        self.clf.remove_rule(0)
        assert len(self.clf.rules) == initial_count

    def test_payload_pattern_matching(self):
        rule = ClassificationRule(
            priority=TrafficPriority.BACKGROUND,
            payload_pattern=r"BitTorrent",
        )
        self.clf.add_rule(rule, index=0)
        pkt = Packet(
            dst_port=9999,
            protocol="tcp",
            payload=b"BitTorrent protocol data",
        )
        assert self.clf.classify(pkt) == TrafficPriority.BACKGROUND
