"""
Priority-queue packet scheduler.

Packets are enqueued with their assigned priority and dequeued in strict
priority order (CRITICAL first, BACKGROUND last).  Within the same priority
class the order is FIFO.

A configurable ``max_queue_size`` limits total queue depth.  Attempts to
enqueue beyond that limit will drop the lowest-priority packet currently in
the queue (tail-drop on the lowest class).
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from .classifier import Packet
from .config import TrafficPriority


@dataclass(order=True)
class _QueueEntry:
    """Heap entry wrapper – sorting is by (priority_value, sequence_number)."""
    priority_value: int
    sequence: int
    packet: Packet = field(compare=False)


class PriorityScheduler:
    """
    Thread-safe priority-queue scheduler.

    Packets with a lower ``TrafficPriority`` numeric value (i.e., CRITICAL=1)
    are dequeued first.

    Usage::

        scheduler = PriorityScheduler(max_queue_size=512)
        scheduler.enqueue(packet)
        next_packet = scheduler.dequeue()  # returns None if empty
    """

    def __init__(self, max_queue_size: int = 1024) -> None:
        self._max_size = max(1, max_queue_size)
        self._heap: List[_QueueEntry] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._enqueue_counts: Dict[TrafficPriority, int] = {
            p: 0 for p in TrafficPriority
        }
        self._dequeue_counts: Dict[TrafficPriority, int] = {
            p: 0 for p in TrafficPriority
        }
        self._dropped_overflow: int = 0

    # ── public API ────────────────────────────────────────────────────────

    def enqueue(self, packet: Packet) -> bool:
        """
        Add *packet* to the queue.

        If the queue is full, the lowest-priority waiting packet is evicted to
        make room (the new packet is only accepted if it has equal or higher
        priority than the evicted packet; otherwise the new packet is dropped).

        :returns: ``True`` if the packet was accepted, ``False`` if dropped.
        """
        priority = packet.priority or TrafficPriority.MEDIUM
        # Stamp the packet so sojourn time can be measured downstream
        packet.enqueued_at = time.monotonic()
        entry = _QueueEntry(
            priority_value=priority.value,
            sequence=next(self._counter),
            packet=packet,
        )
        with self._lock:
            if len(self._heap) >= self._max_size:
                # Find the worst (highest priority_value) entry in the heap
                worst_idx = self._find_worst_index()
                worst_entry = self._heap[worst_idx]
                if worst_entry.priority_value < entry.priority_value:
                    # New packet is lower priority than everything already
                    # queued – drop it
                    self._dropped_overflow += 1
                    return False
                # Evict the worst existing packet
                self._heap[worst_idx] = self._heap[-1]
                self._heap.pop()
                heapq.heapify(self._heap)
                self._dropped_overflow += 1

            heapq.heappush(self._heap, entry)
            self._enqueue_counts[priority] += 1
            return True

    def dequeue(self) -> Optional[Packet]:
        """
        Remove and return the highest-priority packet.

        :returns: The next packet, or ``None`` if the queue is empty.
        """
        with self._lock:
            if not self._heap:
                return None
            entry = heapq.heappop(self._heap)
            priority = entry.packet.priority or TrafficPriority.MEDIUM
            self._dequeue_counts[priority] += 1
            return entry.packet

    def peek(self) -> Optional[Packet]:
        """Return the next packet without removing it."""
        with self._lock:
            return self._heap[0].packet if self._heap else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)

    def is_empty(self) -> bool:
        return len(self) == 0

    def is_full(self) -> bool:
        with self._lock:
            return len(self._heap) >= self._max_size

    @property
    def max_queue_size(self) -> int:
        return self._max_size

    def drain(self) -> Iterator[Packet]:
        """Yield all packets in priority order and clear the queue."""
        while not self.is_empty():
            pkt = self.dequeue()
            if pkt is not None:
                yield pkt

    # ── statistics ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            current_size = len(self._heap)
        return {
            "current_queue_size": current_size,
            "max_queue_size": self._max_size,
            "fill_ratio": current_size / self._max_size,
            "enqueue_counts": dict(self._enqueue_counts),
            "dequeue_counts": dict(self._dequeue_counts),
            "dropped_overflow": self._dropped_overflow,
        }

    def reset_stats(self) -> None:
        with self._lock:
            for p in TrafficPriority:
                self._enqueue_counts[p] = 0
                self._dequeue_counts[p] = 0
            self._dropped_overflow = 0

    # ── internal ──────────────────────────────────────────────────────────

    def _find_worst_index(self) -> int:
        """Return index of the highest priority_value entry (worst priority)."""
        worst_idx = 0
        for i, entry in enumerate(self._heap):
            if entry.priority_value > self._heap[worst_idx].priority_value:
                worst_idx = i
            elif (
                entry.priority_value == self._heap[worst_idx].priority_value
                and entry.sequence < self._heap[worst_idx].sequence
            ):
                # Among same priority, prefer evicting the oldest
                worst_idx = i
        return worst_idx
