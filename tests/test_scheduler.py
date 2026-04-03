"""
Tests for bandwidth_optimizer.scheduler
"""

import threading

import pytest

from bandwidth_optimizer.classifier import Packet
from bandwidth_optimizer.config import TrafficPriority
from bandwidth_optimizer.scheduler import PriorityScheduler


def _make_packet(priority: TrafficPriority, payload: bytes = b"x") -> Packet:
    pkt = Packet(payload=payload)
    pkt.priority = priority
    return pkt


class TestPriorityScheduler:
    def test_enqueue_and_dequeue_single(self):
        sched = PriorityScheduler(max_queue_size=10)
        pkt = _make_packet(TrafficPriority.HIGH)
        assert sched.enqueue(pkt)
        out = sched.dequeue()
        assert out is pkt

    def test_dequeue_empty_returns_none(self):
        sched = PriorityScheduler(max_queue_size=10)
        assert sched.dequeue() is None

    def test_priority_order(self):
        """CRITICAL packets should come out before BACKGROUND ones."""
        sched = PriorityScheduler(max_queue_size=100)
        bg   = _make_packet(TrafficPriority.BACKGROUND)
        low  = _make_packet(TrafficPriority.LOW)
        crit = _make_packet(TrafficPriority.CRITICAL)
        med  = _make_packet(TrafficPriority.MEDIUM)
        high = _make_packet(TrafficPriority.HIGH)

        for pkt in [bg, low, crit, med, high]:
            sched.enqueue(pkt)

        order = [sched.dequeue() for _ in range(5)]
        priorities = [p.priority for p in order]
        assert priorities == [
            TrafficPriority.CRITICAL,
            TrafficPriority.HIGH,
            TrafficPriority.MEDIUM,
            TrafficPriority.LOW,
            TrafficPriority.BACKGROUND,
        ]

    def test_fifo_within_same_priority(self):
        sched = PriorityScheduler(max_queue_size=100)
        p1 = _make_packet(TrafficPriority.MEDIUM, b"first")
        p2 = _make_packet(TrafficPriority.MEDIUM, b"second")
        p3 = _make_packet(TrafficPriority.MEDIUM, b"third")
        for p in [p1, p2, p3]:
            sched.enqueue(p)
        assert sched.dequeue() is p1
        assert sched.dequeue() is p2
        assert sched.dequeue() is p3

    def test_len(self):
        sched = PriorityScheduler(max_queue_size=10)
        assert len(sched) == 0
        sched.enqueue(_make_packet(TrafficPriority.HIGH))
        assert len(sched) == 1

    def test_is_empty(self):
        sched = PriorityScheduler(max_queue_size=10)
        assert sched.is_empty()
        sched.enqueue(_make_packet(TrafficPriority.LOW))
        assert not sched.is_empty()

    def test_is_full(self):
        sched = PriorityScheduler(max_queue_size=2)
        sched.enqueue(_make_packet(TrafficPriority.HIGH))
        sched.enqueue(_make_packet(TrafficPriority.MEDIUM))
        assert sched.is_full()

    def test_overflow_drops_lowest_priority(self):
        """When full, a new higher-priority packet evicts the worst one."""
        sched = PriorityScheduler(max_queue_size=3)
        bg1 = _make_packet(TrafficPriority.BACKGROUND)
        bg2 = _make_packet(TrafficPriority.BACKGROUND)
        bg3 = _make_packet(TrafficPriority.BACKGROUND)
        for p in [bg1, bg2, bg3]:
            sched.enqueue(p)

        # Enqueue a CRITICAL packet – one BACKGROUND should be evicted
        crit = _make_packet(TrafficPriority.CRITICAL)
        accepted = sched.enqueue(crit)
        assert accepted
        assert len(sched) == 3

        # CRITICAL should come out first
        first = sched.dequeue()
        assert first is crit

    def test_overflow_drops_incoming_if_lower_priority(self):
        """When full with high-priority packets, a low-priority newcomer is dropped."""
        sched = PriorityScheduler(max_queue_size=2)
        c1 = _make_packet(TrafficPriority.CRITICAL)
        c2 = _make_packet(TrafficPriority.CRITICAL)
        sched.enqueue(c1)
        sched.enqueue(c2)

        bg = _make_packet(TrafficPriority.BACKGROUND)
        accepted = sched.enqueue(bg)
        assert not accepted
        assert len(sched) == 2

    def test_peek_does_not_remove(self):
        sched = PriorityScheduler(max_queue_size=10)
        pkt = _make_packet(TrafficPriority.HIGH)
        sched.enqueue(pkt)
        peeked = sched.peek()
        assert peeked is pkt
        assert len(sched) == 1

    def test_drain(self):
        sched = PriorityScheduler(max_queue_size=10)
        for _ in range(5):
            sched.enqueue(_make_packet(TrafficPriority.MEDIUM))
        drained = list(sched.drain())
        assert len(drained) == 5
        assert sched.is_empty()

    def test_stats(self):
        sched = PriorityScheduler(max_queue_size=10)
        sched.enqueue(_make_packet(TrafficPriority.HIGH))
        sched.dequeue()
        stats = sched.stats()
        assert stats["enqueue_counts"][TrafficPriority.HIGH] == 1
        assert stats["dequeue_counts"][TrafficPriority.HIGH] == 1
        assert stats["current_queue_size"] == 0

    def test_thread_safety(self):
        """Concurrent enqueue/dequeue should not crash or corrupt state."""
        sched = PriorityScheduler(max_queue_size=200)
        errors = []

        def producer():
            try:
                for _ in range(100):
                    sched.enqueue(_make_packet(TrafficPriority.MEDIUM))
            except Exception as exc:
                errors.append(exc)

        def consumer():
            try:
                for _ in range(50):
                    sched.dequeue()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=producer) for _ in range(4)]
        threads += [threading.Thread(target=consumer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
