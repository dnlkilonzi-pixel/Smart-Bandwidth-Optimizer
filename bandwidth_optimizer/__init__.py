"""
Smart Bandwidth Optimizer package.

Exports the primary public API.
"""

from .classifier import ClassificationRule, Packet, TrafficClassifier
from .compressor import CompressionResult, PayloadCompressor
from .config import (
    DEFAULT_BANDWIDTH_BUDGET,
    DeploymentMode,
    OptimizerConfig,
    TrafficPriority,
)
from .optimizer import BandwidthOptimizer, ProcessResult
from .packet_filter import DropDecision, PacketFilter, TokenBucket
from .scheduler import PriorityScheduler

__all__ = [
    # config
    "DeploymentMode",
    "OptimizerConfig",
    "TrafficPriority",
    "DEFAULT_BANDWIDTH_BUDGET",
    # classifier
    "Packet",
    "TrafficClassifier",
    "ClassificationRule",
    # compressor
    "PayloadCompressor",
    "CompressionResult",
    # packet filter
    "TokenBucket",
    "PacketFilter",
    "DropDecision",
    # scheduler
    "PriorityScheduler",
    # orchestrator
    "BandwidthOptimizer",
    "ProcessResult",
]
