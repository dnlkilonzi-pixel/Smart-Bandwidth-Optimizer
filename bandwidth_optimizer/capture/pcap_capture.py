"""
Cross-platform libpcap/scapy packet capture backend.

Uses ``scapy`` for both live capture (sniff) and offline pcap file replay.
Works on Linux, macOS, and Windows wherever libpcap / Npcap is installed.

Prerequisites
-------------
* ``pip install scapy``
* Live capture requires libpcap (``apt install libpcap-dev``) and
  root / CAP_NET_RAW privilege.
* Offline pcap reading works without elevated privileges.

If ``scapy`` is not installed, importing this module succeeds but
instantiating ``LibpcapCapture`` raises ``ImportError`` with a helpful message.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterator, Optional

from ..classifier import Packet
from .base import BaseCapture, CapturedPacket


def _scapy_pkt_to_packet(scapy_pkt) -> Optional[Packet]:
    """
    Convert a scapy ``Packet`` object to our internal ``Packet`` dataclass.

    Returns ``None`` if the frame lacks an IP layer.
    """
    try:
        from scapy.layers.inet import IP, TCP, UDP, ICMP  # type: ignore
        from scapy.layers.inet6 import IPv6  # type: ignore
    except ImportError:
        return None

    if IP not in scapy_pkt and IPv6 not in scapy_pkt:
        return None

    layer = scapy_pkt[IP] if IP in scapy_pkt else scapy_pkt[IPv6]

    src_ip = str(layer.src)
    dst_ip = str(layer.dst)
    src_port = dst_port = 0
    protocol = "unknown"
    payload = b""

    if TCP in scapy_pkt:
        protocol = "tcp"
        src_port = int(scapy_pkt[TCP].sport)
        dst_port = int(scapy_pkt[TCP].dport)
        payload = bytes(scapy_pkt[TCP].payload)
    elif UDP in scapy_pkt:
        protocol = "udp"
        src_port = int(scapy_pkt[UDP].sport)
        dst_port = int(scapy_pkt[UDP].dport)
        payload = bytes(scapy_pkt[UDP].payload)
    elif ICMP in scapy_pkt:
        protocol = "icmp"
        payload = bytes(scapy_pkt[ICMP].payload)

    return Packet(
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=src_port,
        dst_port=dst_port,
        protocol=protocol,
        payload=payload,
        size_bytes=len(scapy_pkt),
    )


class LibpcapCapture(BaseCapture):
    """
    Capture packets using scapy/libpcap.

    Can operate in two modes:
    * **Live capture** – sniff on a network interface (requires root).
    * **Offline replay** – read from a ``.pcap`` file.

    Parameters
    ----------
    interface:
        Network interface to sniff on (e.g. ``"eth0"``).  Ignored when
        ``pcap_file`` is specified.
    pcap_file:
        Path to a ``.pcap`` file to replay.  When set, ``interface`` is
        unused.
    bpf_filter:
        BPF filter expression (e.g. ``"tcp port 443"``).  Applied by the
        kernel for live capture or by scapy for pcap replay.
    timeout:
        Sniff timeout in seconds per call to scapy's ``sniff()``.  Lower
        values make the capture more responsive to ``stop()``.
    """

    def __init__(
        self,
        interface: str = "eth0",
        pcap_file: Optional[str] = None,
        bpf_filter: str = "",
        timeout: float = 1.0,
    ) -> None:
        super().__init__(interface=interface)
        self._pcap_file = pcap_file
        self._bpf_filter = bpf_filter
        self._timeout = timeout
        self._queue: queue.Queue = queue.Queue(maxsize=4096)

    def start(self) -> None:
        try:
            import scapy  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "scapy is required for LibpcapCapture.\n"
                "Install it with: pip install scapy\n"
                "(Live capture also requires libpcap and root/CAP_NET_RAW)"
            ) from exc
        super().start()

    def packets(self) -> Iterator[CapturedPacket]:
        """Yield packets; blocks until the source is exhausted or stop() called."""
        if self._pcap_file:
            yield from self._replay_pcap()
        else:
            yield from self._live_sniff()

    # ── live capture ──────────────────────────────────────────────────────

    def _live_sniff(self) -> Iterator[CapturedPacket]:
        from scapy.sendrecv import sniff  # type: ignore

        while self._running:
            pkts = sniff(
                iface=self._interface,
                filter=self._bpf_filter or None,
                timeout=self._timeout,
                store=True,
            )
            for sp in pkts:
                if not self._running:
                    return
                pkt = _scapy_pkt_to_packet(sp)
                if pkt is None:
                    self._record_dropped()
                    continue
                self._record_captured(pkt)
                yield CapturedPacket(
                    packet=pkt,
                    interface=self._interface,
                    raw_frame=bytes(sp),
                )

    # ── pcap replay ───────────────────────────────────────────────────────

    def _replay_pcap(self) -> Iterator[CapturedPacket]:
        from scapy.utils import rdpcap  # type: ignore

        pkts = rdpcap(self._pcap_file)
        for sp in pkts:
            if not self._running:
                return
            pkt = _scapy_pkt_to_packet(sp)
            if pkt is None:
                self._record_dropped()
                continue
            self._record_captured(pkt)
            yield CapturedPacket(
                packet=pkt,
                interface=self._interface,
                raw_frame=bytes(sp),
            )
