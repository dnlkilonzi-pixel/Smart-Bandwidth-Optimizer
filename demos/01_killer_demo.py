#!/usr/bin/env python3
"""
Demo 1 – The Killer Scenario: VoIP survives a bulk-transfer storm.

What it shows
-------------
During a network saturation event caused by a bulk software update,
a VoIP call arrives.  The Packet Value Model (PVM) with industry-standard
presets ensures the VoIP packet:

  1. Evicts the lowest-value queued bulk packet to make room.
  2. Gets forwarded *first*, before any of the waiting bulk traffic.

Without PVM, a naive QoS system might only look at priority tier and let all
HIGH-priority web traffic ahead of the VoIP packet because of rule ordering.
PVM uses continuous value coefficients, so 100× (VoIP) always beats 0.5×
(bulk) regardless of tier.

Run:
    python demos/01_killer_demo.py
"""

from __future__ import annotations

from bandwidth_optimizer import (
    BandwidthOptimizer,
    DeploymentMode,
    FlowValuePolicy,
    OptimizerConfig,
    Packet,
)

LINE = "─" * 62


def main() -> None:
    # ── setup ─────────────────────────────────────────────────────────────
    # Small queue (5 slots) to make congestion visible.
    # 1 MB/s link – realistic home/office scenario.
    vp = FlowValuePolicy.from_presets()
    cfg = OptimizerConfig(
        mode=DeploymentMode.ROUTER,
        total_bandwidth_bps=1_000_000,
        max_queue_size=5,
    )
    optimizer = BandwidthOptimizer(config=cfg, flow_value_policy=vp)

    print(f"\n{LINE}")
    print("  KILLER DEMO — Value-Aware Network Under Congestion")
    print(LINE)
    print()
    print("  Scenario: A bulk software update (6881/tcp) saturates a 1 MB/s")
    print("  link with 5-packet queue depth.  A VoIP call arrives mid-storm.")
    print()

    # ── Phase 1: flood with bulk traffic ─────────────────────────────────
    print("  Phase 1 – Bulk update fills the queue")
    print()
    for i in range(5):
        pkt = Packet(
            src_ip=f"10.0.0.{i}",
            dst_ip="8.8.8.8",
            src_port=50000 + i,
            dst_port=6881,
            protocol="tcp",
            payload=b"UPDATE_CHUNK_" + bytes([i]) * 400,
        )
        result = optimizer.process(pkt)
        status = "QUEUED " if not result.dropped else "DROPPED"
        print(
            f"    [{status}]  bulk[{i}]  coeff={pkt.value_coefficient:>4.1f}  "
            f"{pkt.src_ip}:{pkt.src_port} → :{pkt.dst_port}"
        )

    print()
    print(f"    Queue depth: {optimizer.queue_size()}/5  (full)")
    print()

    # ── Phase 2: VoIP call arrives ────────────────────────────────────────
    print("  Phase 2 – VoIP call arrives (RTP, port 5004)")
    print()
    voip = Packet(
        src_ip="192.168.1.10",
        dst_ip="10.0.0.1",
        src_port=5004,
        dst_port=5004,
        protocol="udp",
        payload=b"\x80\x60" + b"\x00" * 160,
    )
    result = optimizer.process(voip)
    status = "QUEUED " if not result.dropped else "DROPPED"
    print(
        f"    [{status}]  voip   coeff={voip.value_coefficient:>5.1f}  "
        f"{voip.src_ip}:{voip.src_port} → :{voip.dst_port}  "
        f"← evicted 1 bulk packet!"
    )
    print()

    # ── Phase 3: drain shows VoIP went first ─────────────────────────────
    print("  Phase 3 – Scheduler drains: who transmits first?")
    print()
    for rank, pkt in enumerate(optimizer.drain(), 1):
        kind = "VoIP" if pkt.dst_port == 5004 else "bulk"
        marker = "  ← FIRST!" if rank == 1 else ""
        print(
            f"    {rank}.  [{kind:4}]  coeff={pkt.value_coefficient:>5.1f}  "
            f"{pkt.src_ip}:{pkt.src_port} → :{pkt.dst_port}{marker}"
        )

    # ── value metrics ─────────────────────────────────────────────────────
    stats = optimizer.stats()
    v = stats.get("value", {})
    print()
    print("  Value Metrics")
    print(f"    Value delivered : {v.get('value_delivered_total', 0):.1f} units")
    print(f"    Value lost      : {v.get('value_lost_total', 0):.1f} units")
    print(f"    Efficiency      : {v.get('value_efficiency_pct', 100.0):.1f}%")
    print()
    print("  Result: VoIP call quality protected. Bulk update uses leftover.")
    print(f"{LINE}\n")


if __name__ == "__main__":
    main()
