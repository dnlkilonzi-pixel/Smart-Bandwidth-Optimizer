"""
Simulated packet capture backend.

Accepts an iterable of pre-built ``Packet`` objects (or a callable that
produces them) and emits them as ``CapturedPacket`` objects.  Useful for
unit tests, demos, and replay of recorded traffic.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Iterator, Optional, Union

from ..classifier import Packet
from .base import BaseCapture, CapturedPacket


class SimulatedCapture(BaseCapture):
    """
    Replay pre-built ``Packet`` objects through the capture interface.

    Parameters
    ----------
    source:
        Either an iterable of ``Packet`` objects **or** a zero-argument
        callable that returns the next ``Packet`` (return ``None`` to signal
        end-of-stream).
    interface:
        Logical interface name reported in ``CapturedPacket.interface``.
    inter_packet_delay:
        Seconds to sleep between yielded packets (0 = as fast as possible).
    """

    def __init__(
        self,
        source: Union[Iterable[Packet], Callable[[], Optional[Packet]]],
        interface: str = "sim0",
        inter_packet_delay: float = 0.0,
    ) -> None:
        super().__init__(interface=interface)
        self._source = source
        self._delay = inter_packet_delay

    # ── BaseCapture impl ──────────────────────────────────────────────────

    def packets(self) -> Iterator[CapturedPacket]:
        """Yield packets from the source until exhausted or stop() called."""
        if callable(self._source):
            yield from self._from_callable()
        else:
            yield from self._from_iterable(iter(self._source))

    # ── internal ──────────────────────────────────────────────────────────

    def _from_iterable(self, it: Iterator[Packet]) -> Iterator[CapturedPacket]:
        for pkt in it:
            if not self._running:
                break
            self._record_captured(pkt)
            yield CapturedPacket(
                packet=pkt,
                timestamp=time.monotonic(),
                interface=self._interface,
            )
            if self._delay:
                time.sleep(self._delay)

    def _from_callable(self) -> Iterator[CapturedPacket]:
        while self._running:
            pkt = self._source()  # type: ignore[call-arg]
            if pkt is None:
                break
            self._record_captured(pkt)
            yield CapturedPacket(
                packet=pkt,
                timestamp=time.monotonic(),
                interface=self._interface,
            )
            if self._delay:
                time.sleep(self._delay)
