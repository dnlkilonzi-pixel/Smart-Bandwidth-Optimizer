"""
Main orchestrator – BandwidthOptimizer.

Ties together the classifier, compressor, packet filter, and scheduler into a
single, easy-to-use interface.

Typical flow for each incoming packet::

    optimizer = BandwidthOptimizer()
    result = optimizer.process(packet)
    if not result.dropped:
        forward(result.packet)   # result.packet.payload may be compressed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from .classifier import Packet, TrafficClassifier
from .compressor import PayloadCompressor
from .config import DeploymentMode, OptimizerConfig, TrafficPriority
from .flow_tracker import FlowRecord, FlowTracker
from .packet_filter import DropDecision, PacketFilter
from .scheduler import PriorityScheduler


@dataclass
class ProcessResult:
    """Outcome of BandwidthOptimizer.process()."""
    packet: Packet
    dropped: bool
    drop_reason: str = ""
    compressed: bool = False
    original_payload_size: int = 0
    compressed_payload_size: int = 0
    flow_record: Optional[FlowRecord] = None

    @property
    def bytes_saved(self) -> int:
        return max(0, self.original_payload_size - self.compressed_payload_size)


class BandwidthOptimizer:
    """
    Smart Bandwidth Optimizer.

    Processes incoming packets through four stages:
      1. **Classification** – assign a TrafficPriority.
      2. **Filtering**       – drop if rate-limited or RED queue pressure.
      3. **Compression**     – compress payload when beneficial.
      4. **Scheduling**      – enqueue in the priority queue for transmission.

    Call :meth:`process` to push a packet through all stages.
    Call :meth:`dequeue` to pull the next forwarding-ready packet.

    Example::

        cfg = OptimizerConfig(mode=DeploymentMode.ROUTER,
                              total_bandwidth_bps=5 * 1024 * 1024)
        opt = BandwidthOptimizer(config=cfg)
        result = opt.process(Packet(dst_port=443, protocol="tcp", payload=b"..."))
    """

    def __init__(self, config: Optional[OptimizerConfig] = None) -> None:
        self._config = config or OptimizerConfig()
        self._classifier = TrafficClassifier()
        self._compressor = PayloadCompressor(config=self._config)
        self._scheduler = PriorityScheduler(
            max_queue_size=self._config.max_queue_size
        )
        self._packet_filter = PacketFilter(
            config=self._config,
            current_queue_size_fn=lambda: len(self._scheduler),
        )
        self._flow_tracker = FlowTracker()
        # Running totals
        self._total_in: int = 0
        self._total_dropped: int = 0
        self._total_bytes_saved: int = 0

    # ── core pipeline ─────────────────────────────────────────────────────

    def process(self, packet: Packet) -> ProcessResult:
        """
        Run *packet* through the full optimization pipeline.

        :param packet: Incoming packet (priority may be unset; will be classified).
        :returns: ProcessResult describing the outcome.
        """
        self._total_in += 1

        # Stage 0 – flow tracking (update stats before classification)
        flow_record = self._flow_tracker.update(packet)

        # Stage 1 – classify
        if packet.priority is None:
            self._classifier.classify(packet)

        # Stage 1b – flow-aware priority adjustment
        if packet.priority is not None:
            packet.priority = self._flow_tracker.priority_hint(
                packet.priority, flow_record
            )

        # Stage 2 – filter / drop
        decision: DropDecision = self._packet_filter.should_drop(packet)
        if decision.drop:
            self._total_dropped += 1
            return ProcessResult(
                packet=packet,
                dropped=True,
                drop_reason=decision.reason,
                flow_record=flow_record,
            )

        # Stage 3 – compress payload
        original_size = len(packet.payload)
        result = self._compressor.compress(packet.payload)
        if result.was_compressed:
            packet.payload = result.data
            packet.size_bytes = packet.size_bytes - original_size + len(result.data)
            self._total_bytes_saved += result.space_saved_bytes

        # Stage 4 – enqueue
        self._scheduler.enqueue(packet)

        return ProcessResult(
            packet=packet,
            dropped=False,
            compressed=result.was_compressed,
            original_payload_size=original_size,
            compressed_payload_size=len(packet.payload),
            flow_record=flow_record,
        )

    def process_batch(self, packets: List[Packet]) -> List[ProcessResult]:
        """Process a list of packets and return their results."""
        return [self.process(p) for p in packets]

    # ── scheduler interface ───────────────────────────────────────────────

    def dequeue(self) -> Optional[Packet]:
        """
        Pull the next packet from the priority queue for forwarding.

        :returns: The highest-priority waiting packet, or None if the queue
                  is empty.
        """
        return self._scheduler.dequeue()

    def drain(self) -> Iterator[Packet]:
        """Yield all queued packets in priority order."""
        return self._scheduler.drain()

    def queue_size(self) -> int:
        """Return the current number of packets in the queue."""
        return len(self._scheduler)

    # ── statistics ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """
        Return a summary of optimizer activity.

        The returned dict is JSON-serialisable (all values are plain Python
        types).
        """
        sched_stats = self._scheduler.stats()
        drop_counts = self._packet_filter.drop_counts()
        return {
            "mode": self._config.mode.value,
            "total_bandwidth_bps": self._config.total_bandwidth_bps,
            "packets_received": self._total_in,
            "packets_dropped": self._total_dropped,
            "drop_rate": (
                self._total_dropped / self._total_in
                if self._total_in
                else 0.0
            ),
            "bytes_saved_compression": self._total_bytes_saved,
            "queue": sched_stats,
            "drops_by_priority": {
                p.name: count for p, count in drop_counts.items()
            },
            "flows": {
                "active": self._flow_tracker.flow_count(),
            },
        }

    def reset_stats(self) -> None:
        """Reset all running counters."""
        self._total_in = 0
        self._total_dropped = 0
        self._total_bytes_saved = 0
        self._packet_filter.reset_drop_counts()
        self._scheduler.reset_stats()

    # ── properties ────────────────────────────────────────────────────────

    @property
    def config(self) -> OptimizerConfig:
        return self._config

    @property
    def classifier(self) -> TrafficClassifier:
        return self._classifier

    @property
    def compressor(self) -> PayloadCompressor:
        return self._compressor

    @property
    def flow_tracker(self) -> FlowTracker:
        return self._flow_tracker
