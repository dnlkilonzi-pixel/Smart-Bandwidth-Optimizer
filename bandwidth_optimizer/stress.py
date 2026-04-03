"""
Real-traffic stress testing at scale.

Generates adversarial multi-threaded packet load against the optimizer pipeline
to validate behaviour under:

* **Sustained high throughput** – constant rate at target PPS
* **Burst flood** – sudden 10× spike then back to baseline
* **Oscillating rate** – alternating between 10% and 100% of target PPS
* **Priority inversion** – 100% BACKGROUND traffic (worst-case for drops)
* **All critical** – 100% CRITICAL traffic (tests fast-path behaviour)

The tester uses N worker threads, each generating ``target_pps / n_threads``
packets per second.  Wall-clock timing keeps the per-thread rate accurate
even if individual iterations are slow.

Typical usage::

    from bandwidth_optimizer.stress import StressTester, StressConfig, StressPattern

    result = StressTester.run(
        optimizer,
        StressConfig(duration_seconds=10, target_pps=50_000,
                     pattern=StressPattern.BURST_FLOOD),
    )
    print(result.summary())

CLI::

    python main.py stress --duration 10 --pps 50000 --pattern burst_flood
"""

from __future__ import annotations

import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .benchmark import LatencyStats
from .classifier import Packet
from .config import TrafficPriority


# ─────────────────────────── pattern enum ────────────────────────────────────

class StressPattern(Enum):
    """Adversarial traffic patterns for stress testing."""
    UNIFORM           = "uniform"            # steady state at target PPS
    BURST_FLOOD       = "burst_flood"        # 10× bursts every 1s
    OSCILLATING       = "oscillating"        # 10%–100% of target PPS, 0.5s period
    PRIORITY_INVERSION = "priority_inversion" # all BACKGROUND traffic
    ALL_CRITICAL      = "all_critical"       # all CRITICAL traffic


# ─────────────────────────── config / result ─────────────────────────────────

@dataclass
class StressConfig:
    """
    Parameters for a stress run.

    Attributes
    ----------
    duration_seconds:
        How long to run the stress test.
    target_pps:
        Aggregate target packet rate across all threads (packets/second).
    n_threads:
        Number of parallel sender threads.
    pattern:
        Adversarial traffic pattern.
    packet_size_bytes:
        Payload size per synthetic packet.  0 = random 64–1500.
    burst_multiplier:
        For BURST_FLOOD: peak-to-normal ratio.
    """
    duration_seconds: float = 10.0
    target_pps: int = 10_000
    n_threads: int = 4
    pattern: StressPattern = StressPattern.UNIFORM
    packet_size_bytes: int = 512
    burst_multiplier: float = 10.0


@dataclass
class StressResult:
    """Results of a stress run."""
    pattern: str
    duration_seconds: float
    target_pps: int
    achieved_pps: float
    packets_sent: int
    packets_dropped: int
    peak_queue_depth: int
    latency: LatencyStats

    @property
    def drop_rate(self) -> float:
        return self.packets_dropped / self.packets_sent if self.packets_sent else 0.0

    def summary(self) -> str:
        lines = [
            "=" * 62,
            f"  Stress Test Results  ({self.pattern})",
            "=" * 62,
            f"  Duration          : {self.duration_seconds:.1f} s",
            f"  Target PPS        : {self.target_pps:,}",
            f"  Achieved PPS      : {self.achieved_pps:,.0f}",
            f"  Packets sent      : {self.packets_sent:,}",
            f"  Packets dropped   : {self.packets_dropped:,}",
            f"  Drop rate         : {self.drop_rate:.2%}",
            f"  Peak queue depth  : {self.peak_queue_depth:,}",
            "",
            "  Latency under load (µs):",
            f"    min={self.latency.min_us:.1f}  "
            f"mean={self.latency.mean_us:.1f}  "
            f"p50={self.latency.p50_us:.1f}  "
            f"p95={self.latency.p95_us:.1f}  "
            f"p99={self.latency.p99_us:.1f}  "
            f"max={self.latency.max_us:.1f}",
            "=" * 62,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "duration_seconds": self.duration_seconds,
            "target_pps": self.target_pps,
            "achieved_pps": round(self.achieved_pps, 1),
            "packets_sent": self.packets_sent,
            "packets_dropped": self.packets_dropped,
            "drop_rate": round(self.drop_rate, 4),
            "peak_queue_depth": self.peak_queue_depth,
            "latency": self.latency.to_dict(),
        }


# ─────────────────────────── packet factories ────────────────────────────────

_PORTS  = [443, 80, 5060, 53, 6881, 25, 22, 9999]
_PROTOS = ["tcp", "udp"]


def _make_uniform_packet(size: int) -> Packet:
    pkt_size = size if size > 0 else random.randint(64, 1500)
    return Packet(
        src_ip=f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}",
        dst_ip="93.184.216.34",
        src_port=random.randint(1024, 65535),
        dst_port=random.choice(_PORTS),
        protocol=random.choice(_PROTOS),
        payload=random.randbytes(pkt_size),
        size_bytes=pkt_size,
    )


def _make_priority_packet(
    priority: TrafficPriority, size: int
) -> Packet:
    """Make a packet pre-classified with *priority*."""
    pkt = _make_uniform_packet(size)
    pkt.priority = priority
    return pkt


# ─────────────────────────── stress tester ───────────────────────────────────

class StressTester:
    """
    Multi-threaded stress tester for the optimizer pipeline.

    All state is local to a single ``run()`` call; the class has no instance
    state and ``run()`` is safe to call concurrently from multiple threads.
    """

    @classmethod
    def run(
        cls,
        optimizer,
        cfg: Optional[StressConfig] = None,
    ) -> StressResult:
        """
        Run a stress test and return the ``StressResult``.

        Parameters
        ----------
        optimizer:
            Target ``BandwidthOptimizer`` (or any compatible wrapper).
        cfg:
            ``StressConfig`` with test parameters.
        """
        cfg = cfg or StressConfig()

        # Shared state between threads
        packets_sent = [0]
        packets_dropped = [0]
        peak_queue_depth = [0]
        latency_samples_ns: List[int] = []
        samples_lock = threading.Lock()
        start_event = threading.Event()

        def worker(thread_id: int) -> None:
            per_thread_pps = cfg.target_pps / max(1, cfg.n_threads)
            size = cfg.packet_size_bytes
            deadline = time.monotonic() + cfg.duration_seconds

            # Each thread waits for the start signal
            start_event.wait()

            sent = 0
            dropped = 0
            peak = 0
            samples: List[int] = []
            # Phase offset so threads don't all burst at the same instant
            phase = thread_id / max(1, cfg.n_threads)

            while time.monotonic() < deadline:
                t_now = time.monotonic()
                # Compute instantaneous rate multiplier for this pattern
                elapsed = t_now - run_start + phase
                rate_mul = _rate_multiplier(cfg.pattern, elapsed,
                                            cfg.burst_multiplier)

                current_pps = per_thread_pps * rate_mul
                # Sleep to hit the target rate; avoid busy-spinning
                if current_pps > 0:
                    sleep_s = 1.0 / current_pps
                else:
                    sleep_s = 0.01

                t0_ns = time.perf_counter_ns()
                pkt = _build_packet(cfg, sent)
                result = optimizer.process(pkt)
                elapsed_ns = time.perf_counter_ns() - t0_ns

                sent += 1
                samples.append(elapsed_ns)

                if result.dropped:
                    dropped += 1

                # Sample queue depth (avoid locking optimizer)
                try:
                    q = optimizer.stats()["queue"]["current_queue_size"]
                    if q > peak:
                        peak = q
                except Exception:
                    pass

                # Rate throttle
                remaining = sleep_s - (time.perf_counter_ns() - t0_ns) / 1e9
                if remaining > 0:
                    time.sleep(remaining)

            with samples_lock:
                packets_sent[0] += sent
                packets_dropped[0] += dropped
                if peak > peak_queue_depth[0]:
                    peak_queue_depth[0] = peak
                latency_samples_ns.extend(samples)

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(max(1, cfg.n_threads))
        ]
        for t in threads:
            t.start()

        run_start = time.monotonic()
        start_event.set()

        t_wall_start = time.monotonic()
        for t in threads:
            t.join()
        actual_duration = time.monotonic() - t_wall_start

        latency_us = [ns / 1000.0 for ns in latency_samples_ns]

        return StressResult(
            pattern=cfg.pattern.value,
            duration_seconds=actual_duration,
            target_pps=cfg.target_pps,
            achieved_pps=packets_sent[0] / actual_duration if actual_duration > 0 else 0,
            packets_sent=packets_sent[0],
            packets_dropped=packets_dropped[0],
            peak_queue_depth=peak_queue_depth[0],
            latency=LatencyStats.from_samples(latency_us),
        )


# ─────────────────────────── helpers ─────────────────────────────────────────

def _rate_multiplier(
    pattern: StressPattern,
    elapsed: float,
    burst_multiplier: float,
) -> float:
    """Return the instantaneous rate multiplier for *pattern* at *elapsed* seconds."""
    if pattern == StressPattern.UNIFORM:
        return 1.0

    if pattern == StressPattern.BURST_FLOOD:
        # 10× burst for 100 ms every 1 second
        phase = elapsed % 1.0
        return burst_multiplier if phase < 0.1 else 1.0

    if pattern == StressPattern.OSCILLATING:
        # Sine wave between 10% and 100% of target, 0.5 s period
        import math
        return 0.1 + 0.9 * (0.5 + 0.5 * math.sin(2 * math.pi * elapsed / 0.5))

    if pattern == StressPattern.PRIORITY_INVERSION:
        return 1.0   # rate same; priority overridden in _build_packet

    if pattern == StressPattern.ALL_CRITICAL:
        return 1.0   # rate same; priority overridden in _build_packet

    return 1.0


def _build_packet(cfg: StressConfig, seq: int) -> Packet:
    """Build a single test packet respecting the stress pattern."""
    if cfg.pattern == StressPattern.PRIORITY_INVERSION:
        return _make_priority_packet(TrafficPriority.BACKGROUND,
                                     cfg.packet_size_bytes)
    if cfg.pattern == StressPattern.ALL_CRITICAL:
        return _make_priority_packet(TrafficPriority.CRITICAL,
                                     cfg.packet_size_bytes)
    return _make_uniform_packet(cfg.packet_size_bytes)
