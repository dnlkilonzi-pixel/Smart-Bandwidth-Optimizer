"""
Performance benchmarking layer.

Provides hard, reproducible metrics for the optimizer pipeline:

* **Throughput** – packets/second sustained over N packets
* **Latency**    – per-packet processing time in microseconds
  (min / mean / p50 / p95 / p99 / max)
* **Per-stage breakdown** – latency attributed to each named stage
  (flow_track, classify, filter, compress, schedule)
* **Memory footprint** – RSS delta and peak RSS during the run
* **Compression ratio** – compressed vs original bytes
* **Drop rate** – fraction of packets dropped by the filter

Typical usage::

    from bandwidth_optimizer.benchmark import Benchmarker, BenchmarkConfig

    result = Benchmarker.run(optimizer, BenchmarkConfig(n_packets=10_000))
    print(result.summary())

CLI::

    python main.py bench --packets 50000 --workers 1
"""

from __future__ import annotations

import gc
import os
import resource
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .classifier import Packet
from .config import OptimizerConfig


# ─────────────────────────── result types ─────────────────────────────────────

@dataclass
class LatencyStats:
    """Latency distribution in *microseconds*."""
    min_us: float
    mean_us: float
    p50_us: float
    p95_us: float
    p99_us: float
    max_us: float
    sample_count: int

    @classmethod
    def from_samples(cls, samples_us: List[float]) -> "LatencyStats":
        """Build from a list of per-packet latency samples (µs)."""
        if not samples_us:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
        s = sorted(samples_us)
        n = len(s)
        return cls(
            min_us=s[0],
            mean_us=statistics.mean(s),
            p50_us=s[int(n * 0.50)],
            p95_us=s[int(n * 0.95)],
            p99_us=s[int(n * 0.99)],
            max_us=s[-1],
            sample_count=n,
        )

    def to_dict(self) -> dict:
        return {
            "min_us": round(self.min_us, 2),
            "mean_us": round(self.mean_us, 2),
            "p50_us": round(self.p50_us, 2),
            "p95_us": round(self.p95_us, 2),
            "p99_us": round(self.p99_us, 2),
            "max_us": round(self.max_us, 2),
            "sample_count": self.sample_count,
        }


@dataclass
class BenchmarkResult:
    """Results of a single benchmark run."""
    # ── throughput ────────────────────────────────────────────────────────
    packets_processed: int
    duration_seconds: float
    packets_per_second: float

    # ── end-to-end latency ────────────────────────────────────────────────
    total_latency: LatencyStats

    # ── per-stage latency (may be empty if stage timing disabled) ─────────
    stage_latency: Dict[str, LatencyStats] = field(default_factory=dict)

    # ── memory ────────────────────────────────────────────────────────────
    memory_rss_before_bytes: int = 0
    memory_rss_after_bytes: int = 0

    @property
    def memory_delta_bytes(self) -> int:
        return max(0, self.memory_rss_after_bytes - self.memory_rss_before_bytes)

    # ── packet outcomes ───────────────────────────────────────────────────
    packets_dropped: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    @property
    def drop_rate(self) -> float:
        return self.packets_dropped / self.packets_processed if self.packets_processed else 0.0

    @property
    def compression_ratio(self) -> float:
        """bytes_out / bytes_in – <1.0 means compression saved space."""
        return self.bytes_out / self.bytes_in if self.bytes_in else 1.0

    def summary(self) -> str:
        """Return a formatted multi-line summary."""
        lines = [
            "=" * 62,
            "  Smart Bandwidth Optimizer – Benchmark Results",
            "=" * 62,
            f"  Packets processed : {self.packets_processed:,}",
            f"  Duration          : {self.duration_seconds:.3f} s",
            f"  Throughput        : {self.packets_per_second:,.0f} pkt/s",
            "",
            "  End-to-end latency (µs):",
            f"    min={self.total_latency.min_us:.1f}  "
            f"mean={self.total_latency.mean_us:.1f}  "
            f"p50={self.total_latency.p50_us:.1f}  "
            f"p95={self.total_latency.p95_us:.1f}  "
            f"p99={self.total_latency.p99_us:.1f}  "
            f"max={self.total_latency.max_us:.1f}",
        ]
        if self.stage_latency:
            lines.append("")
            lines.append("  Per-stage latency (µs mean):")
            for stage, ls in self.stage_latency.items():
                lines.append(f"    {stage:<20s}  mean={ls.mean_us:.2f}  p99={ls.p99_us:.2f}")
        lines += [
            "",
            f"  Drop rate         : {self.drop_rate:.2%}",
            f"  Compression ratio : {self.compression_ratio:.3f}  "
            f"({'saved' if self.compression_ratio < 1 else 'no savings'})",
            f"  Memory delta      : {self.memory_delta_bytes / 1024:.1f} KB",
            "=" * 62,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "packets_processed": self.packets_processed,
            "duration_seconds": round(self.duration_seconds, 4),
            "packets_per_second": round(self.packets_per_second, 1),
            "total_latency": self.total_latency.to_dict(),
            "stage_latency": {k: v.to_dict() for k, v in self.stage_latency.items()},
            "drop_rate": round(self.drop_rate, 4),
            "compression_ratio": round(self.compression_ratio, 4),
            "memory_rss_before_bytes": self.memory_rss_before_bytes,
            "memory_rss_after_bytes": self.memory_rss_after_bytes,
            "memory_delta_bytes": self.memory_delta_bytes,
            "packets_dropped": self.packets_dropped,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }


# ─────────────────────────── benchmark config ─────────────────────────────────

@dataclass
class BenchmarkConfig:
    """
    Parameters for a benchmark run.

    Attributes
    ----------
    n_packets:
        Total number of packets to process.
    packet_size_bytes:
        Fixed payload size of each synthetic packet.  Use 0 for mixed sizes
        (64–1500 bytes, randomly chosen).
    warmup_packets:
        Number of packets to process before starting measurement (warm up
        JIT caches, token buckets, etc.).
    measure_stages:
        When True, instrument each pipeline stage individually.  Adds ~10 %
        overhead but gives per-stage latency breakdowns.
    packet_factory:
        Optional callable ``() -> Packet``; overrides the built-in synthetic
        packet generator.
    """
    n_packets: int = 10_000
    packet_size_bytes: int = 512
    warmup_packets: int = 200
    measure_stages: bool = True
    packet_factory: Optional[Callable[[], Packet]] = None


# ─────────────────────────── benchmarker ──────────────────────────────────────

def _rss_bytes() -> int:
    """Return the current process RSS in bytes (Linux/macOS)."""
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1024 if os.uname().sysname == "Linux" else 1
        )
    except Exception:
        return 0


def _default_packet_factory(size: int) -> Callable[[], Packet]:
    """Return a factory that creates synthetic packets of *size* bytes."""
    import random
    ports  = [443, 80, 5060, 53, 6881, 25, 22, 9999]
    protos = ["tcp", "udp"]

    def factory() -> Packet:
        pkt_size = size if size > 0 else random.randint(64, 1500)
        return Packet(
            src_ip=f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}",
            dst_ip="93.184.216.34",
            src_port=random.randint(1024, 65535),
            dst_port=random.choice(ports),
            protocol=random.choice(protos),
            payload=random.randbytes(pkt_size),
            size_bytes=pkt_size,
        )
    return factory


class Benchmarker:
    """
    Benchmark the ``BandwidthOptimizer`` pipeline.

    ``run()`` is a *class method* that creates a temporary high-bandwidth
    optimizer configuration, processes ``cfg.n_packets`` packets, and
    returns a ``BenchmarkResult``.

    Stage timing uses ``time.perf_counter_ns()`` for sub-microsecond
    resolution.  Memory is sampled via ``resource.getrusage()``.
    """

    @classmethod
    def run(
        cls,
        optimizer=None,
        cfg: Optional[BenchmarkConfig] = None,
    ) -> BenchmarkResult:
        """
        Run a full benchmark.

        Parameters
        ----------
        optimizer:
            ``BandwidthOptimizer`` to benchmark.  If ``None``, a default
            instance with unlimited bandwidth is created (so the filter
            never drops packets, isolating pipeline overhead).
        cfg:
            ``BenchmarkConfig`` with run parameters.
        """
        from .optimizer import BandwidthOptimizer
        from .config import OptimizerConfig

        if optimizer is None:
            optimizer = BandwidthOptimizer(
                OptimizerConfig(
                    total_bandwidth_bps=10 * 1024 * 1024 * 1024,  # 10 GB/s – no drops
                    max_queue_size=100_000,
                    compression_threshold_bytes=256,
                )
            )

        cfg = cfg or BenchmarkConfig()
        factory = cfg.packet_factory or _default_packet_factory(cfg.packet_size_bytes)

        # ── warm-up ───────────────────────────────────────────────────────
        for _ in range(cfg.warmup_packets):
            optimizer.process(factory())
        optimizer.reset_stats()

        # ── force GC before measurement ───────────────────────────────────
        gc.collect()
        rss_before = _rss_bytes()

        # ── measured run ──────────────────────────────────────────────────
        total_samples_ns: List[int] = []
        stage_samples_ns: Dict[str, List[int]] = {
            "flow_track": [],
            "classify":   [],
            "filter":     [],
            "compress":   [],
            "schedule":   [],
        }
        packets_dropped = 0
        bytes_in = 0
        bytes_out = 0

        t_run_start = time.perf_counter_ns()

        for _ in range(cfg.n_packets):
            pkt = factory()
            bytes_in += pkt.size_bytes

            if cfg.measure_stages:
                result = cls._process_with_stage_timing(
                    optimizer, pkt, stage_samples_ns
                )
                total_samples_ns.append(sum(
                    stage_samples_ns[s][-1] for s in stage_samples_ns
                    if stage_samples_ns[s]
                ))
            else:
                t0 = time.perf_counter_ns()
                result = optimizer.process(pkt)
                total_samples_ns.append(time.perf_counter_ns() - t0)

            if result.dropped:
                packets_dropped += 1
            else:
                bytes_out += len(result.packet.payload)

        t_run_end = time.perf_counter_ns()

        gc.collect()
        rss_after = _rss_bytes()

        duration_s = (t_run_end - t_run_start) / 1e9

        # ── convert ns → µs ──────────────────────────────────────────────
        total_us = [ns / 1000.0 for ns in total_samples_ns]
        stage_latency: Dict[str, LatencyStats] = {}
        if cfg.measure_stages:
            for name, samples in stage_samples_ns.items():
                if samples:
                    stage_latency[name] = LatencyStats.from_samples(
                        [ns / 1000.0 for ns in samples]
                    )

        return BenchmarkResult(
            packets_processed=cfg.n_packets,
            duration_seconds=duration_s,
            packets_per_second=cfg.n_packets / duration_s if duration_s > 0 else 0,
            total_latency=LatencyStats.from_samples(total_us),
            stage_latency=stage_latency,
            memory_rss_before_bytes=rss_before,
            memory_rss_after_bytes=rss_after,
            packets_dropped=packets_dropped,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
        )

    # ── internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _process_with_stage_timing(
        optimizer,
        pkt: Packet,
        stage_samples: Dict[str, List[int]],
    ):
        """
        Process *pkt* through the optimizer with per-stage timing.

        We instrument the individual stages manually by temporarily
        running each sub-component in isolation and measuring wall time.
        This is an approximation because the optimizer's ``process()``
        method is monolithic, but it gives a useful relative breakdown.
        """
        from .config import TrafficPriority

        # Stage 0 – flow tracking
        t0 = time.perf_counter_ns()
        flow_record = optimizer._flow_tracker.update(pkt)
        stage_samples["flow_track"].append(time.perf_counter_ns() - t0)

        # Stage 1 – classify
        t0 = time.perf_counter_ns()
        if pkt.priority is None:
            optimizer._classifier.classify(pkt)
        if pkt.priority is not None:
            pkt.priority = optimizer._flow_tracker.priority_hint(pkt.priority, flow_record)
        stage_samples["classify"].append(time.perf_counter_ns() - t0)

        # Stage 2 – filter
        t0 = time.perf_counter_ns()
        decision = optimizer._packet_filter.should_drop(pkt)
        stage_samples["filter"].append(time.perf_counter_ns() - t0)

        if decision.drop:
            from .optimizer import ProcessResult
            optimizer._total_in += 1
            optimizer._total_dropped += 1
            return ProcessResult(packet=pkt, dropped=True,
                                 drop_reason=decision.reason,
                                 flow_record=flow_record)

        # Stage 3 – compress
        t0 = time.perf_counter_ns()
        original_size = len(pkt.payload)
        cr = optimizer._compressor.compress(pkt.payload)
        if cr.was_compressed:
            pkt.payload = cr.data
            pkt.size_bytes = pkt.size_bytes - original_size + len(cr.data)
            optimizer._total_bytes_saved += cr.space_saved_bytes
        stage_samples["compress"].append(time.perf_counter_ns() - t0)

        # Stage 4 – schedule
        t0 = time.perf_counter_ns()
        optimizer._scheduler.enqueue(pkt)
        stage_samples["schedule"].append(time.perf_counter_ns() - t0)

        optimizer._total_in += 1

        from .optimizer import ProcessResult
        return ProcessResult(
            packet=pkt, dropped=False,
            compressed=cr.was_compressed,
            original_payload_size=original_size,
            compressed_payload_size=len(pkt.payload),
            flow_record=flow_record,
        )
