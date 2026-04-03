#!/usr/bin/env python3
"""
Demo 4 – Multi-Node Fleet Coordination.

What it shows
-------------
Each node optimizes locally.  The AgentCoordinator aggregates PVM metrics
from all nodes into a fleet-wide view, exposing:

  - Fleet value efficiency % (weighted across all nodes)
  - Best and worst-performing nodes (by value efficiency)
  - Per-node breakdown for drill-down
  - Legacy nodes (non-PVM) listed separately

This is the first step toward distributed economic routing:
"Route new high-value flows toward nodes with headroom."

Run:
    python demos/04_fleet_coordination.py
"""

from __future__ import annotations

import json

from bandwidth_optimizer.coordinator import AgentCoordinator

LINE = "─" * 62


def main() -> None:
    print(f"\n{LINE}")
    print("  FLEET COORDINATION — Global Value View Across Nodes")
    print(LINE)

    # ── simulate 3 PVM nodes + 1 legacy node ──────────────────────────────
    coord = AgentCoordinator(agent_ttl=60.0)

    nodes = [
        (
            "edge-us-east",
            {
                "packets_received": 45_000,
                "value": {
                    "value_efficiency_pct": 98.2,
                    "value_delivered_per_sec": 4820.0,
                    "value_lost_per_sec": 87.3,
                },
            },
        ),
        (
            "edge-eu-west",
            {
                "packets_received": 32_000,
                "value": {
                    "value_efficiency_pct": 74.1,
                    "value_delivered_per_sec": 1483.0,
                    "value_lost_per_sec": 518.2,
                },
            },
        ),
        (
            "edge-ap-south",
            {
                "packets_received": 18_000,
                "value": {
                    "value_efficiency_pct": 91.5,
                    "value_delivered_per_sec": 2190.0,
                    "value_lost_per_sec": 203.5,
                },
            },
        ),
        (
            "legacy-dc-01",
            {
                "packets_received": 5_000,
                # No "value" key → non-PVM node
            },
        ),
    ]

    print()
    print("  Ingesting heartbeats from 4 nodes (3 PVM + 1 legacy)…")
    print()
    for node_id, stats in nodes:
        coord.ingest(node_id, stats)
        if "value" in stats:
            eff = stats["value"]["value_efficiency_pct"]
            print(f"    ✓  {node_id:<15}  PVM  eff={eff:.1f}%")
        else:
            print(f"    ✓  {node_id:<15}  legacy (no PVM)")

    # ── fleet summary ──────────────────────────────────────────────────────
    print()
    print("  Fleet value summary (GET /agents/value):")
    print()
    summary = coord.fleet_value_summary()
    print(json.dumps(summary, indent=4))

    # ── interpret the result ───────────────────────────────────────────────
    print()
    print("  Interpretation:")
    eff = summary["fleet_value_efficiency_pct"]
    lost = summary["fleet_value_lost_per_sec"]
    worst = summary["worst_node"]
    best  = summary["best_node"]
    print(f"    Fleet efficiency:  {eff:.1f}%")
    print(f"    Value lost/sec:    {lost:.1f} units")
    print(f"    Best node:         {best}")
    print(f"    Worst node:        {worst}  ← investigate / rebalance flows here")
    print()
    print("  Action: Route new high-value flows toward edge-us-east.")
    print("  Trigger a deeper investigation on edge-eu-west (74.1% eff).")
    print()
    print(f"{LINE}\n")


if __name__ == "__main__":
    main()
