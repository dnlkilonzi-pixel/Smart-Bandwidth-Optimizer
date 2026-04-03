"""
Tests for bandwidth_optimizer.capture (Upgrade 1 – packet interception layer)
"""

import time

import pytest

from bandwidth_optimizer.capture import (
    BaseCapture,
    CapturedPacket,
    CaptureStats,
    LibpcapCapture,
    NFQueueCapture,
    SimulatedCapture,
)
from bandwidth_optimizer.classifier import Packet


def _make_packets(n: int = 5):
    return [
        Packet(
            src_ip="10.0.0.1",
            dst_ip="10.0.0.2",
            src_port=1000 + i,
            dst_port=443,
            protocol="tcp",
            payload=b"hello" * 10,
        )
        for i in range(n)
    ]


class TestCapturedPacket:
    def test_default_timestamp_set(self):
        pkt = Packet()
        cp = CapturedPacket(packet=pkt)
        assert cp.timestamp > 0

    def test_interface_default(self):
        cp = CapturedPacket(packet=Packet())
        assert cp.interface == "unknown"

    def test_nfqueue_handle_none_by_default(self):
        cp = CapturedPacket(packet=Packet())
        assert cp.nfqueue_handle is None


class TestCaptureStats:
    def test_drop_rate_zero_when_empty(self):
        stats = CaptureStats()
        assert stats.drop_rate == 0.0

    def test_drop_rate_calculation(self):
        stats = CaptureStats(packets_captured=90, packets_dropped=10)
        assert stats.drop_rate == pytest.approx(0.1)


class TestSimulatedCapture:
    def test_yields_all_packets(self):
        pkts = _make_packets(5)
        cap = SimulatedCapture(source=pkts)
        with cap:
            captured = list(cap.packets())
        assert len(captured) == 5

    def test_captured_packet_type(self):
        pkts = _make_packets(3)
        cap = SimulatedCapture(source=pkts)
        with cap:
            for cp in cap.packets():
                assert isinstance(cp, CapturedPacket)
                assert isinstance(cp.packet, Packet)

    def test_stats_accumulate(self):
        pkts = _make_packets(4)
        cap = SimulatedCapture(source=pkts)
        with cap:
            list(cap.packets())
        assert cap.stats.packets_captured == 4
        assert cap.stats.bytes_captured > 0

    def test_interface_propagated(self):
        pkts = _make_packets(2)
        cap = SimulatedCapture(source=pkts, interface="eth99")
        with cap:
            for cp in cap.packets():
                assert cp.interface == "eth99"

    def test_callable_source(self):
        queue = list(_make_packets(3))

        def source():
            return queue.pop(0) if queue else None

        cap = SimulatedCapture(source=source)
        with cap:
            captured = list(cap.packets())
        assert len(captured) == 3

    def test_stop_mid_stream(self):
        """stop() before exhausting the source should terminate iteration."""
        pkts = _make_packets(100)
        cap = SimulatedCapture(source=pkts)
        cap.start()
        collected = []
        for cp in cap.packets():
            collected.append(cp)
            if len(collected) >= 3:
                cap.stop()
                break
        assert len(collected) >= 1

    def test_context_manager(self):
        pkts = _make_packets(2)
        with SimulatedCapture(source=pkts) as cap:
            captured = list(cap.packets())
        assert len(captured) == 2
        assert not cap._running


class TestNFQueueCapture:
    def test_import_error_without_lib(self):
        """Without netfilterqueue installed, start() should raise ImportError."""
        cap = NFQueueCapture(queue_num=99)
        with pytest.raises(ImportError, match="netfilterqueue"):
            cap.start()

    def test_instantiation_succeeds(self):
        cap = NFQueueCapture(queue_num=0)
        assert cap._queue_num == 0


class TestLibpcapCapture:
    def test_import_error_without_scapy(self):
        """Without scapy installed, start() should raise ImportError."""
        import sys
        scapy_backup = sys.modules.pop("scapy", None)
        try:
            cap = LibpcapCapture(interface="lo")
            with pytest.raises(ImportError, match="scapy"):
                cap.start()
        finally:
            if scapy_backup is not None:
                sys.modules["scapy"] = scapy_backup

    def test_instantiation_with_pcap_file(self):
        cap = LibpcapCapture(pcap_file="/tmp/test.pcap", bpf_filter="tcp")
        assert cap._pcap_file == "/tmp/test.pcap"
        assert cap._bpf_filter == "tcp"
