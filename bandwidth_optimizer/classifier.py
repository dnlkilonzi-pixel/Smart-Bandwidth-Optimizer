"""
Traffic classifier.

Inspects packet metadata (protocol, source/destination port, payload hints)
and assigns a TrafficPriority.  All classification rules are data-driven so
callers can extend the rule table without touching this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import TrafficPriority


# ─────────────────────────── data model ──────────────────────────────────────

@dataclass
class Packet:
    """
    Minimal representation of a network packet.

    ``payload`` is the raw (uncompressed) application-layer bytes.
    Other fields mirror typical IP/TCP/UDP header fields.
    """
    src_ip: str = "0.0.0.0"
    dst_ip: str = "0.0.0.0"
    src_port: int = 0
    dst_port: int = 0
    protocol: str = "tcp"          # "tcp", "udp", "icmp", …
    payload: bytes = b""
    size_bytes: int = 0            # total packet size (headers + payload)
    # Set by the classifier; callers may also set it before enqueuing
    priority: Optional[TrafficPriority] = None

    def __post_init__(self) -> None:
        if self.size_bytes == 0:
            self.size_bytes = len(self.payload)


# ─────────────────────────── built-in rules ───────────────────────────────────

@dataclass
class ClassificationRule:
    """
    A single rule that maps traffic characteristics to a priority.

    All supplied fields act as AND-conditions.  Omit a field (leave as None)
    to treat it as a wildcard.
    """
    priority: TrafficPriority
    # Port sets (match src OR dst port)
    ports: Tuple[int, ...] = field(default_factory=tuple)
    protocols: Tuple[str, ...] = field(default_factory=tuple)
    # Optional regex matched against the first 64 bytes of payload (ASCII)
    payload_pattern: Optional[str] = None

    def matches(self, packet: Packet) -> bool:
        if self.ports:
            if packet.src_port not in self.ports and packet.dst_port not in self.ports:
                return False
        if self.protocols:
            if packet.protocol.lower() not in self.protocols:
                return False
        if self.payload_pattern is not None:
            snippet = packet.payload[:64].decode("latin-1", errors="replace")
            if not re.search(self.payload_pattern, snippet, re.IGNORECASE):
                return False
        return True


# Default rule table – evaluated top-to-bottom; first match wins.
DEFAULT_RULES: List[ClassificationRule] = [
    # ── CRITICAL ──────────────────────────────────────────────────────────
    # VoIP: SIP (5060/5061) + RTP (dynamic, but commonly 5004, 5005)
    ClassificationRule(
        priority=TrafficPriority.CRITICAL,
        ports=(5060, 5061, 5004, 5005),
        protocols=("udp", "tcp"),
    ),
    # ICMP (ping, unreachables) – keep alive for diagnostics
    ClassificationRule(
        priority=TrafficPriority.CRITICAL,
        protocols=("icmp",),
    ),

    # ── HIGH ──────────────────────────────────────────────────────────────
    # DNS
    ClassificationRule(
        priority=TrafficPriority.HIGH,
        ports=(53,),
        protocols=("udp", "tcp"),
    ),
    # HTTPS / TLS
    ClassificationRule(
        priority=TrafficPriority.HIGH,
        ports=(443,),
        protocols=("tcp",),
    ),
    # SSH
    ClassificationRule(
        priority=TrafficPriority.HIGH,
        ports=(22,),
        protocols=("tcp",),
    ),
    # HTTP
    ClassificationRule(
        priority=TrafficPriority.HIGH,
        ports=(80, 8080),
        protocols=("tcp",),
    ),

    # ── MEDIUM ────────────────────────────────────────────────────────────
    # SMTP / IMAP / POP3
    ClassificationRule(
        priority=TrafficPriority.MEDIUM,
        ports=(25, 465, 587, 993, 995, 110, 143),
        protocols=("tcp",),
    ),
    # XMPP/chat
    ClassificationRule(
        priority=TrafficPriority.MEDIUM,
        ports=(5222, 5269),
        protocols=("tcp",),
    ),

    # ── LOW ───────────────────────────────────────────────────────────────
    # Software update servers (HTTP on non-standard ports) or SFTP
    ClassificationRule(
        priority=TrafficPriority.LOW,
        ports=(21, 989, 990),
        protocols=("tcp",),
    ),
    # NTP – time sync is low priority but should not be dropped
    ClassificationRule(
        priority=TrafficPriority.LOW,
        ports=(123,),
        protocols=("udp",),
    ),

    # ── BACKGROUND ────────────────────────────────────────────────────────
    # BitTorrent
    ClassificationRule(
        priority=TrafficPriority.BACKGROUND,
        ports=(6881, 6882, 6883, 6884, 6885, 6886, 6887, 6888, 6889),
        protocols=("tcp", "udp"),
    ),
    # BitTorrent DHT
    ClassificationRule(
        priority=TrafficPriority.BACKGROUND,
        ports=(6969,),
        protocols=("udp",),
    ),
]


# ─────────────────────────── classifier ──────────────────────────────────────

class TrafficClassifier:
    """
    Classifies packets by applying an ordered list of ClassificationRules.

    Usage::

        classifier = TrafficClassifier()
        packet = Packet(dst_port=443, protocol="tcp", payload=b"...")
        priority = classifier.classify(packet)
    """

    def __init__(
        self,
        rules: Optional[List[ClassificationRule]] = None,
        default_priority: TrafficPriority = TrafficPriority.MEDIUM,
    ) -> None:
        self._rules: List[ClassificationRule] = (
            list(rules) if rules is not None else list(DEFAULT_RULES)
        )
        self._default_priority = default_priority

    # ── public API ────────────────────────────────────────────────────────

    def classify(self, packet: Packet) -> TrafficPriority:
        """Return the priority for *packet* and mutate ``packet.priority``."""
        for rule in self._rules:
            if rule.matches(packet):
                packet.priority = rule.priority
                return rule.priority
        packet.priority = self._default_priority
        return self._default_priority

    def add_rule(self, rule: ClassificationRule, index: int = 0) -> None:
        """Insert *rule* at *index* (default: prepend, highest precedence)."""
        self._rules.insert(index, rule)

    def remove_rule(self, index: int) -> ClassificationRule:
        """Remove and return the rule at *index*."""
        return self._rules.pop(index)

    @property
    def rules(self) -> List[ClassificationRule]:
        return list(self._rules)

    # ── helpers ───────────────────────────────────────────────────────────

    def classify_batch(self, packets: List[Packet]) -> List[TrafficPriority]:
        """Classify a list of packets and return priorities in the same order."""
        return [self.classify(p) for p in packets]
