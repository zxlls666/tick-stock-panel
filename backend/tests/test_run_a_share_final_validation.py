from __future__ import annotations

import polars as pl

from scripts.run_a_share_final_validation import build_bias_waterfall, build_final_verdicts


def test_final_verdict_requires_every_bias_reduction_gate() -> None:
    gates = pl.DataFrame({
        "strategy": [
            "abnormal_turnover_monthly_raw",
            "abnormal_turnover_monthly_size_neutral",
            "reversal_monthly",
        ],
        "phase2_direction_positive": [True, True, False],
        "phase2_excess_positive": [True, True, False],
        "phase2_ir_positive": [True, True, False],
        "extreme_recovery_positive": [True, True, False],
        "cost_20bps_positive": [True, True, True],
        "cost_40bps_positive": [True, False, True],
        "subperiod_gate": [True, True, True],
        "parameter_gate": [True, True, True],
        "capacity_10m_gate": [True, True, True],
        "year_contribution_gate": [True, True, True],
        "unknowns_disclosed": [True, True, True],
    })

    verdicts = build_final_verdicts(gates)

    assert verdicts.select("strategy", "verdict").sort("strategy").rows() == [
        ("abnormal_turnover_monthly_raw", "bias_reduced_candidate"),
        ("abnormal_turnover_monthly_size_neutral", "pause"),
        ("reversal_monthly", "pause"),
    ]


def test_bias_waterfall_uses_frozen_stage_sources() -> None:
    phase1 = {
        "legacy": [{
            "strategy": "abnormal_turnover_monthly_raw",
            "total_return": 2.3,
            "benchmarks": {"000985.SH": {"excess_total_return": 2.2, "information_ratio": 1.5}},
        }],
        "strict_pit": [{
            "strategy": "abnormal_turnover_monthly_raw",
            "total_return": 2.0,
            "benchmarks": {"000985.SH": {"excess_total_return": 1.9, "information_ratio": 1.4}},
        }],
    }
    phase2 = pl.DataFrame({
        "strategy": ["abnormal_turnover_monthly_raw"] * 2,
        "stage": ["pit_st", "delisted_recovery_70"],
        "total_return": [1.3, 1.2],
        "excess_total_return": [1.25, 1.15],
        "information_ratio": [1.1, 1.0],
    })
    phase3 = pl.DataFrame({
        "strategy": ["abnormal_turnover_monthly_raw"],
        "scenario": ["adv_1pct_eta_0.50_queue_conservative"],
        "initial_capital": [10_000_000.0],
        "total_return": [0.8],
        "excess_total_return": [0.75],
        "information_ratio": [0.9],
    })

    waterfall = build_bias_waterfall(phase1, phase2, phase3)

    assert waterfall.select("stage", "total_return").rows() == [
        ("legacy", 2.3),
        ("strict_pit", 2.0),
        ("pit_st", 1.3),
        ("delisted_recovery_70", 1.2),
        ("capacity_10m_conservative", 0.8),
    ]
