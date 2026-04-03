"""
Linux NFQUEUE packet capture backend.

Uses ``netfilterqueue`` (a Python binding for Linux netfilter NFQUEUE) to
intercept live packets in the kernel's forwarding path.  Each packet can be
*accepted* (forwarded) or *dropped* after the optimizer decides.

Prerequisites
-------------
1. Install the binding::

       pip install netfilterqueue

2. Add an iptables rule to divert traffic to queue 0::

       iptables -I INPUT   -j NFQUEUE --queue-num 0
       iptables -I OUTPUT  -j NFQUEUE --queue-num 0
       iptables -I FORWARD -j NFQUEUE --queue-num 0

3. Run as root (or with CAP_NET_ADMIN)::

       sudo python main.py serve --capture nfqueue

If ``netfilterqueue`` is not installed, importing this module succeeds but
instantiating ``NFQueueCapture`` raises ``ImportError`` with a helpful message.

Packet parsing
--------------
Raw NFQUEUE payloads are IP datagrams.  We parse them with Python's ``struct``
module (no third-party parser needed) extracting:
    * IP version, protocol, src/dst addresses
    * TCP/UDP src/dst ports (if applicable)
    * Application payload (bytes after transport header)
"""

from __future__ import annotations

import ipaddress
import struct
from typing import Iterator, Optional

from ..classifier import Packet
from .base import BaseCapture, CapturedPacket


# ── protocol constants ────────────────────────────────────────────────────────

_PROTO_MAP = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6"}


def _parse_ip_packet(raw: bytes) -> Optional[Packet]:
    """
    Parse a raw IP datagram into a ``Packet``.

    Supports IPv4 only; returns ``None`` for malformed or non-IP data.
    """
    if len(raw) < 20:
        return None
    version = (raw[0] >> 4) & 0xF
    if version != 4:
        return None

    ihl = (raw[0] & 0xF) * 4  # IP header length in bytes
    proto_num = raw[9]
    src_ip = str(ipaddress.IPv4Address(raw[12:16]))
    dst_ip = str(ipaddress.IPv4Address(raw[16:20]))
    protocol = _PROTO_MAP.get(proto_num, str(proto_num))

    src_port = dst_port = 0
    payload_start = ihl

    if proto_num in (6, 17):  # TCP or UDP
        if len(raw) < ihl + 8:
            return None
        src_port, dst_port = struct.unpack_from("!HH", raw, ihl)
        if proto_num == 6:  # TCP
            tcp_data_offset = ((raw[ihl + 12] >> 4) & 0xF) * 4
            payload_start = ihl + tcp_data_offset
        else:  # UDP
            payload_start = ihl + 8

    payload = raw[payload_start:]

    return Packet(
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=src_port,
        dst_port=dst_port,
        protocol=protocol,
        payload=payload,
        size_bytes=len(raw),
    )


# ── NFQUEUE capture ───────────────────────────────────────────────────────────

class NFQueueCapture(BaseCapture):
    """
    Capture live packets from Linux netfilter NFQUEUE.

    Parameters
    ----------
    queue_num:
        NFQUEUE number to bind to (must match the iptables rule).
    interface:
        Logical name used in ``CapturedPacket.interface``.
    max_payload:
        Maximum bytes of each packet payload to copy into userspace.
    """

    def __init__(
        self,
        queue_num: int = 0,
        interface: str = "nfqueue",
        max_payload: int = 65535,
    ) -> None:
        super().__init__(interface=interface)
        self._queue_num = queue_num
        self._max_payload = max_payload
        self._nfqueue = None
        self._pending: list = []

    def start(self) -> None:
        try:
            import netfilterqueue  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "netfilterqueue is required for NFQueueCapture.\n"
                "Install it with: pip install netfilterqueue\n"
                "(Linux only; requires libnetfilter_queue-dev)"
            ) from exc
        super().start()
        self._netfilterqueue = netfilterqueue
        self._nfqueue = netfilterqueue.NetfilterQueue()
        self._nfqueue.bind(self._queue_num, self._callback, self._max_payload)

    def stop(self) -> None:
        super().stop()
        if self._nfqueue is not None:
            try:
                self._nfqueue.unbind()
            except Exception:
                pass
            self._nfqueue = None

    def packets(self) -> Iterator[CapturedPacket]:
        """
        Yield packets from the NFQUEUE.

        This method blocks internally while waiting for kernel packets.
        The caller must call ``stop()`` from another thread to terminate.
        """
        import select

        if self._nfqueue is None:
            raise RuntimeError("Call start() before packets()")

        fd = self._nfqueue.get_fd()
        while self._running:
            # Non-blocking wait with 0.1 s timeout so stop() is respected
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                self._nfqueue.run_socket(self._nfqueue.get_socket())
            while self._pending:
                yield self._pending.pop(0)

    # ── internal ──────────────────────────────────────────────────────────

    def _callback(self, nfq_pkt) -> None:
        """Called by netfilterqueue for each intercepted packet."""
        raw = nfq_pkt.get_payload()
        pkt = _parse_ip_packet(raw)
        if pkt is None:
            nfq_pkt.accept()
            self._record_dropped()
            return
        self._record_captured(pkt)
        self._pending.append(
            CapturedPacket(
                packet=pkt,
                interface=self._interface,
                raw_frame=raw,
                nfqueue_handle=nfq_pkt,
            )
        )
