"""
Smart Bandwidth Optimizer package.

Exports the primary public API.
"""

from .agent import AgentConfig, NodeAgent
from .benchmark import BenchmarkConfig, BenchmarkResult, Benchmarker, LatencyStats
from .classifier import ClassificationRule, Packet, TrafficClassifier
from .compressor import CompressionResult, PayloadCompressor
from .config import (
    DEFAULT_BANDWIDTH_BUDGET,
    DeploymentMode,
    OptimizerConfig,
    TrafficPriority,
)
from .coordinator import AgentCoordinator
from .flow_tracker import FlowKey, FlowRecord, FlowTracker
from .optimizer import BandwidthOptimizer, ProcessResult
from .packet_filter import DropDecision, PacketFilter, TokenBucket
from .policy import Policy, PolicyLoadError, PolicyLoader, PolicyRule
from .safety import CircuitState, FailMode, HealthStatus, SafetyGuard
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
    # flow tracker
    "FlowKey",
    "FlowRecord",
    "FlowTracker",
    # policy DSL
    "PolicyRule",
    "Policy",
    "PolicyLoader",
    "PolicyLoadError",
    # orchestrator
    "BandwidthOptimizer",
    "ProcessResult",
    # safety
    "FailMode",
    "CircuitState",
    "HealthStatus",
    "SafetyGuard",
    # benchmarking
    "BenchmarkConfig",
    "BenchmarkResult",
    "LatencyStats",
    "Benchmarker",
    # multi-node
    "AgentConfig",
    "NodeAgent",
    "AgentCoordinator",
]
