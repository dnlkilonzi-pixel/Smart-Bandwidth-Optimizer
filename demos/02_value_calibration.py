#!/usr/bin/env python3
"""
Demo 2 – Value Calibration with Industry-Standard Presets.

What it shows
-------------
New users of PVM face a "blank-canvas" problem: what value_coefficient
should they assign?  The ValueCoefficientsGuide solves this with
industry-standard presets derived from the business cost of dropping
or delaying each traffic type.

This demo:
  1. Prints the built-in preset table with rationale.
  2. Shows how FlowValuePolicy.from_presets() builds a ready policy.
  3. Demonstrates per-flow coefficient lookup for every preset type.
  4. Shows revenue scaling (calibrate to your actual SLA exposure).

Run:
    python demos/02_value_calibration.py
"""

from __future__ import annotations

from bandwidth_optimizer import Packet
from bandwidth_optimizer.value import FlowValuePolicy, ValueCoefficientsGuide, TRAFFIC_PRESETS

LINE = "─" * 68


def main() -> None:
    print(f"\n{LINE}")
    print("  VALUE CALIBRATION — Industry-Standard Presets")
    print(LINE)

    # ── 1. Print the preset table ──────────────────────────────────────────
    print()
    print("  Built-in presets (ValueCoefficientsGuide):")
    print()
    print(f"  {'Name':<20} {'Coeff':>7}  {'Priority':<10}  Rationale (abbreviated)")
    print(f"  {'-'*20} {'-'*7}  {'-'*10}  {'-'*35}")
    for p in TRAFFIC_PRESETS:
        print(
            f"  {p.name:<20} {p.value_coefficient:>7.1f}  "
            f"{p.priority.name:<10}  {p.rationale[:52]}…"
        )

    # ── 2. Build policy from presets ───────────────────────────────────────
    print()
    print("  Building policy: vp = FlowValuePolicy.from_presets()")
    vp = FlowValuePolicy.from_presets()

    # ── 3. Test packets ────────────────────────────────────────────────────
    print()
    print("  Per-packet coefficient lookup:")
    print()
    test_cases = [
        (5060, "udp",  "VoIP SIP signalling"),
        (5004, "udp",  "VoIP RTP audio"),
        (443,  "tcp",  "HTTPS API call"),
        (8801, "udp",  "Video conferencing"),
        (80,   "tcp",  "HTTP web browsing"),
        (53,   "udp",  "DNS query"),
        (25,   "tcp",  "Email SMTP"),
        (6881, "tcp",  "BitTorrent (bulk)"),
        (9999, "tcp",  "Unknown traffic (default)"),
    ]
    for port, proto, label in test_cases:
        pkt = Packet(
            src_ip="10.0.0.1", dst_ip="8.8.8.8",
            src_port=12345, dst_port=port, protocol=proto,
            payload=b"X" * 100,
        )
        coeff = vp.get_coefficient(pkt)
        bar = "█" * int(coeff / 5)
        print(f"  {label:<30} port={port:>5}/{proto:<3}  coeff={coeff:>6.1f}  {bar}")

    # ── 4. Revenue scaling ─────────────────────────────────────────────────
    print()
    print("  Revenue scaling (revenue_scale=10 → $1k/hr VoIP SLA exposure):")
    print()
    vp_scaled = FlowValuePolicy.from_presets(revenue_scale=10.0)
    for p in TRAFFIC_PRESETS:
        pkt = Packet(
            src_ip="10.0.0.1", dst_ip="8.8.8.8",
            src_port=12345, dst_port=p.ports[0], protocol=p.protocols[0],
            payload=b"X" * 100,
        )
        base  = vp.get_coefficient(pkt)
        scaled = vp_scaled.get_coefficient(pkt)
        print(f"  {p.name:<20}  base={base:>6.1f}  scaled={scaled:>7.1f}")

    # ── 5. Custom overrides ────────────────────────────────────────────────
    print()
    print("  Per-preset override (enterprise VoIP = 200):")
    vp_custom = FlowValuePolicy.from_presets(overrides={"voip": 200.0})
    pkt = Packet(
        src_ip="10.0.0.1", dst_ip="8.8.8.8",
        src_port=12345, dst_port=5060, protocol="udp",
        payload=b"INVITE",
    )
    print(f"    voip coeff: {vp_custom.get_coefficient(pkt):.1f}  (was 100.0)")
    print()
    print(f"{LINE}\n")


if __name__ == "__main__":
    main()
