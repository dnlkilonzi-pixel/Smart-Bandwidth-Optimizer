"""
Abstract base class for all packet capture backends.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..classifier import Packet


@dataclass
class CapturedPacket:
    """
    A packet as returned by a capture backend.

    Wraps a ``Packet`` with capture-layer metadata (arrival timestamp,
    interface name, raw frame bytes when available).
    """
    packet: Packet
    timestamp: float = field(default_factory=time.monotonic)
    interface: str = "unknown"
    # Raw link-layer frame (populated by pcap/NFQUEUE backends, None otherwise)
    raw_frame: Optional[bytes] = None

    # When set to True by the NFQUEUE backend the packet will be accepted
    # (re-injected into the network stack) after processing.  The caller is
    # responsible for calling ``accept()`` or ``drop()`` on the underlying
    # NFQUEUE packet object via the ``nfqueue_pkt`` handle if present.
    nfqueue_handle: Optional[object] = None


@dataclass
class CaptureStats:
    """Running counters for a capture session."""
    packets_captured: int = 0
    packets_dropped: int = 0
    bytes_captured: int = 0

    @property
    def drop_rate(self) -> float:
        total = self.packets_captured + self.packets_dropped
        return self.packets_dropped / total if total else 0.0


class BaseCapture(ABC):
    """
    Abstract packet capture backend.

    Subclasses must implement :meth:`packets` to yield ``CapturedPacket``
    objects.  Optional lifecycle methods (:meth:`start`, :meth:`stop`) allow
    backends to manage resources (open sockets, register NFQUEUE callbacks,
    etc.).

    Usage::

        with MyCapture(...) as cap:
            for captured in cap.packets():
                result = optimizer.process(captured.packet)
                if captured.nfqueue_handle and not result.dropped:
                    captured.nfqueue_handle.accept()
    """

    def __init__(self, interface: str = "any") -> None:
        self._interface = interface
        self._stats = CaptureStats()
        self._running = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open the capture source.  Called automatically by ``__enter__``."""
        self._running = True

    def stop(self) -> None:
        """Close the capture source.  Called automatically by ``__exit__``."""
        self._running = False

    def __enter__(self) -> "BaseCapture":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── abstract ──────────────────────────────────────────────────────────

    @abstractmethod
    def packets(self) -> Iterator[CapturedPacket]:
        """
        Yield packets from the capture source.

        This generator runs until :meth:`stop` is called or the source is
        exhausted (e.g., end of a pcap file).
        """

    # ── stats ─────────────────────────────────────────────────────────────

    @property
    def stats(self) -> CaptureStats:
        return self._stats

    def _record_captured(self, pkt: Packet) -> None:
        self._stats.packets_captured += 1
        self._stats.bytes_captured += pkt.size_bytes

    def _record_dropped(self) -> None:
        self._stats.packets_dropped += 1
