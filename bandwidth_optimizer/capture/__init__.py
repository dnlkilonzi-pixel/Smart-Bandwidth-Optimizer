"""
Packet capture abstraction layer.

Provides a unified interface for reading packets from different sources:

* ``SimulatedCapture``  – inject pre-built ``Packet`` objects (testing / demo)
* ``NFQueueCapture``    – intercept live packets on Linux via NFQUEUE/iptables
* ``LibpcapCapture``    – passive capture via libpcap/scapy (cross-platform)

All backends implement ``BaseCapture`` and yield ``CapturedPacket`` objects
that can be fed directly into ``BandwidthOptimizer.process()``.

Deployment notes
----------------
``NFQueueCapture`` requires:
    * Linux kernel with netfilter support
    * ``pip install netfilterqueue``  (or ``python-nfqueue`` on some distros)
    * iptables rule to redirect traffic, e.g.:
        ``iptables -I INPUT -j NFQUEUE --queue-num 0``
    * Root / CAP_NET_ADMIN privilege

``LibpcapCapture`` requires:
    * ``pip install scapy``
    * ``libpcap`` installed (``apt install libpcap-dev`` on Debian/Ubuntu)
    * Root / CAP_NET_RAW privilege for live capture
"""

from .base import BaseCapture, CapturedPacket, CaptureStats
from .simulated import SimulatedCapture
from .nfqueue_capture import NFQueueCapture
from .pcap_capture import LibpcapCapture

__all__ = [
    "BaseCapture",
    "CapturedPacket",
    "CaptureStats",
    "SimulatedCapture",
    "NFQueueCapture",
    "LibpcapCapture",
]
