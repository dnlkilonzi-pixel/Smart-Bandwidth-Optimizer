#!/usr/bin/env python3
"""
Demo 3 – Feedback Loop: auto-tuning coefficients to meet SLA.

What it shows
-------------
Static value_coefficient values are best-guesses.  When network conditions
change (more flows, link saturation, bursty traffic), the static coefficient
may no longer be sufficient to guarantee the contracted SLA.

ValueCoefficientTuner closes the loop automatically:
  - SLA violated?  → boost the coefficient (scheduler gives it more weight)
  - Perfect delivery? → gently decay back toward baseline
  - Acceptable zone?  → no change (stable)

No ML required — pure heuristics.  Converges in a few iterations.

Run:
    python demos/03_feedback_loop.py
"""

from __future__ import annotations

from bandwidth_optimizer import ValueSLAContract
from bandwidth_optimizer.value import FlowValuePolicy, ValueCoefficientTuner

LINE = "─" * 68


def main() -> None:
    print(f"\n{LINE}")
    print("  FEEDBACK LOOP — Auto-Tuning Value Coefficients")
    print(LINE)

    # ── setup ─────────────────────────────────────────────────────────────
    vp    = FlowValuePolicy.from_presets()
    tuner = ValueCoefficientTuner(vp, boost_factor=1.2, decay_factor=0.95)
    sla   = ValueSLAContract("voip_tenant", min_value_rate_per_sec=100.0)

    print()
    print("  Rule:    voip (SIP/RTP port 5060)")
    print(f"  SLA:     min {sla.min_value_rate_per_sec:.0f} value-units/s")
    print(f"  Start:   coefficient = 100.0")
    print()
    print(f"  {'Obs':>3}  {'Delivered':>10}  {'Eff%':>6}  {'Action':<22}  Coefficient")
    print(f"  {'---':>3}  {'-'*10}  {'-'*6}  {'-'*22}  -----------")

    # Simulate a realistic sequence of delivery observations
    observations = [
        # (delivered_rate, label)
        (48.0,  "SLA violated — burst storm"),
        (55.0,  "SLA violated — still under"),
        (72.0,  "SLA violated — improving"),
        (91.0,  "Acceptable   — no change"),
        (95.0,  "Acceptable   — no change"),
        (100.5, "SLA met      — no change"),
        (99.9,  "Perfect      — decay ×0.95"),
        (99.9,  "Perfect      — decay ×0.95"),
        (99.8,  "Perfect      — decay ×0.95"),
        (95.0,  "Acceptable   — no change"),
        (88.0,  "SLA violated — boost again"),
        (99.6,  "Acceptable   — no change"),
        (99.9,  "Perfect      — decay ×0.95"),
        (99.9,  "Perfect      — decay ×0.95"),
        (99.9,  "Perfect      — decay ×0.95"),
    ]

    for obs_num, (delivered, label) in enumerate(observations, 1):
        new_coeff = tuner.observe_contract("voip", delivered_rate=delivered, contract=sla)
        eff = 100.0 * delivered / sla.min_value_rate_per_sec
        violated = "!" if eff < 90 else " "
        print(
            f"  {obs_num:>3}  {delivered:>10.1f}  {eff:>5.1f}%{violated}  "
            f"{label:<22}  {new_coeff:>9.4f}"
        )

    # ── summary ───────────────────────────────────────────────────────────
    report = tuner.tuning_report()["rules"]["voip"]
    print()
    print("  Summary")
    print(f"    Base coefficient:    {report['base_coefficient']:.4f}")
    print(f"    Final coefficient:   {report['current_coefficient']:.4f}")
    print(f"    Total boosts:        {report['boost_count']}")
    print(f"    Total decays:        {report['decay_count']}")
    print()
    print("  The tuner converged: coefficient rose during violation,")
    print("  then decayed back toward the base once SLA was met.")
    print()

    # ── show overrides are live ────────────────────────────────────────────
    snap = vp.overrides_snapshot()
    print(f"  Live policy overrides: {snap}")
    print()
    tuner.reset("voip")
    snap = vp.overrides_snapshot()
    print(f"  After reset():         {snap}  (back to static YAML value)")
    print()
    print(f"{LINE}\n")


if __name__ == "__main__":
    main()
