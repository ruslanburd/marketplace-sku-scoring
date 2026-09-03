"""
Portfolio implementation of the marketplace SKU scoring model.

The production workflow prepares 90-day marketplace metrics in the DWH/Python
pipeline. This module focuses only on the analytical scoring layer:

    raw metrics
        -> normalization
        -> reliability adjustment
        -> business weights
        -> metric contributions
        -> total SKU score (0-100)

The metric set below reflects the Ozon version of the model. Some marketplace
implementations use a slightly different set of metrics depending on source
data availability.

This file is intentionally simplified and anonymized for portfolio use.
Company-specific data access, SQL, API calls, file paths, and operational
reporting logic are excluded.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd


ReliabilityBase = Literal["orders", "opens", "views", "none"]


@dataclass(frozen=True)
class MetricRule:
    """Configuration for one component of the SKU score."""

    weight: float
    low_score: float
    high_score: float
    reliability_base: ReliabilityBase = "none"
    use_missing_value_plug: bool = False


# Reliability shrinks volatile rate metrics when the observation count is low.
RELIABILITY_K = 10.0

# The score is designed to sum to 100 points before reliability adjustments.
TOTAL_WEIGHT = 100.0


def _p95(series: pd.Series, *, metric_name: str) -> float:
    """Return the 95th percentile and fail clearly if it cannot be calculated."""
    clean = pd.to_numeric(series, errors="coerce").dropna()

    if clean.empty:
        raise ValueError(
            f"Cannot calculate the 95th-percentile threshold for '{metric_name}': "
            "no valid observations."
        )

    return float(clean.quantile(0.95))


def build_metric_rules(df: pd.DataFrame) -> dict[str, MetricRule]:
    """
    Build scoring rules from business weights and empirical thresholds.

    Two threshold approaches are used:

    1. Distribution-based thresholds
       Revenue, profitability, CR, CTR and search position use the 95th
       percentile to keep extreme observations from dominating the score.

    2. Business-defined thresholds
       Return rate, buyout rate, cancellation rate, rating, review-count
       magnitude and demand stability use interpretable business boundaries.

    Search position is an inverted metric: a lower position is better.
    This is represented by low_score > high_score, so the same normalization
    formula can be used without special-case scoring logic.
    """

    required = {
        "revenue",
        "margin",
        "CR",
        "CTR",
        "stability",
        "avr_pos",
        "return_rate",
        "buyout_rate",
        "cancel_rate",
        "rating",
        "feedback_digits",
        "order_cnt",
        "opens",
        "views",
    }
    missing = required.difference(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    revenue_p95 = _p95(
        df.loc[df["revenue"] > 0, "revenue"],
        metric_name="revenue",
    )

    margin_p95 = _p95(
        df.loc[(df["revenue"] > 0) & (df["margin"] != -1), "margin"],
        metric_name="margin",
    )

    cr_p95 = _p95(
        df.loc[df["opens"] >= 10, "CR"],
        metric_name="CR",
    )

    ctr_p95 = _p95(
        df.loc[df["views"] >= 10, "CTR"],
        metric_name="CTR",
    )

    search_position_p95 = _p95(
        df.loc[df["avr_pos"] > 0, "avr_pos"],
        metric_name="avr_pos",
    )

    rules = {
        "revenue": MetricRule(
            weight=15,
            low_score=0,
            high_score=revenue_p95,
        ),
        "margin": MetricRule(
            weight=25,
            low_score=0,
            high_score=margin_p95,
            use_missing_value_plug=True,
        ),
        "CR": MetricRule(
            weight=10,
            low_score=0,
            high_score=cr_p95,
            reliability_base="opens",
            use_missing_value_plug=True,
        ),
        "CTR": MetricRule(
            weight=10,
            low_score=0,
            high_score=ctr_p95,
            reliability_base="views",
        ),
        "stability": MetricRule(
            weight=15,
            low_score=0,
            high_score=1,
        ),
        "avr_pos": MetricRule(
            weight=5,
            low_score=search_position_p95,
            high_score=1,
            use_missing_value_plug=True,
        ),
        "return_rate": MetricRule(
            weight=7,
            low_score=0.15,
            high_score=0,
            reliability_base="orders",
            use_missing_value_plug=True,
        ),
        "buyout_rate": MetricRule(
            weight=5,
            low_score=0.50,
            high_score=1,
            reliability_base="orders",
            use_missing_value_plug=True,
        ),
        "cancel_rate": MetricRule(
            weight=3,
            low_score=0.30,
            high_score=0,
            reliability_base="orders",
            use_missing_value_plug=True,
        ),
        "rating": MetricRule(
            weight=3,
            low_score=3,
            high_score=5,
        ),
        "feedback_digits": MetricRule(
            weight=2,
            low_score=1,
            high_score=3,
        ),
    }

    total_weight = sum(rule.weight for rule in rules.values())
    if not np.isclose(total_weight, TOTAL_WEIGHT):
        raise ValueError(
            f"Metric weights must sum to {TOTAL_WEIGHT:g}; got {total_weight:g}."
        )

    return rules


def normalize_metric(
    values: np.ndarray,
    low_score: float,
    high_score: float,
) -> np.ndarray:
    """
    Map a raw metric to a 0-1 scale.

    The same formula supports both:
      - metrics where higher is better: low_score < high_score;
      - metrics where lower is better: low_score > high_score.

    Values outside the scoring range are clipped to [0, 1].
    """
    denominator = high_score - low_score

    if np.isclose(denominator, 0):
        raise ValueError(
            f"Normalization range is zero: low_score={low_score}, "
            f"high_score={high_score}."
        )

    normalized = (values - low_score) / denominator
    return np.clip(normalized, 0.0, 1.0)


def reliability_factor(sample_size: np.ndarray) -> np.ndarray:
    """
    Shrink low-volume observations using N / (N + K).

    With K=10:
      N=2   -> 0.17
      N=8   -> 0.44
      N=100 -> 0.91

    This prevents rates calculated from a handful of observations from having
    the same influence as rates supported by substantial traffic or order data.
    """
    sample_size = np.asarray(sample_size, dtype=float)
    sample_size = np.clip(sample_size, 0.0, None)
    return sample_size / (sample_size + RELIABILITY_K)


def _get_reliability(
    df: pd.DataFrame,
    base: ReliabilityBase,
) -> np.ndarray:
    """Return the reliability array for the requested evidence base."""
    if base == "orders":
        return reliability_factor(df["order_cnt"].to_numpy(dtype=float))

    if base == "opens":
        return reliability_factor(df["opens"].to_numpy(dtype=float))

    if base == "views":
        return reliability_factor(df["views"].to_numpy(dtype=float))

    return np.ones(len(df), dtype=float)


def score_skus(
    df: pd.DataFrame,
    *,
    include_intermediate_columns: bool = True,
) -> pd.DataFrame:
    """
    Calculate a 0-100 score for each marketplace listing.

    Parameters
    ----------
    df:
        Prepared 90-day SKU-level dataset. Each row represents one marketplace
        listing and must contain the columns required by `build_metric_rules`.

        Missing-value convention used by the production model:
        -1 marks an unavailable metric for metrics configured with
        `use_missing_value_plug=True`.

    include_intermediate_columns:
        If True, keep `<metric>_norm` and `<metric>_reliability` columns.
        These columns are useful for model explainability and portfolio demos.

    Returns
    -------
    pandas.DataFrame
        Original data plus per-metric score contributions and `total_score`.
    """
    scored = df.copy()
    rules = build_metric_rules(scored)
    total_score = np.zeros(len(scored), dtype=float)

    for metric, rule in rules.items():
        values = pd.to_numeric(scored[metric], errors="coerce").fillna(0).to_numpy(
            dtype=float
        )

        normalized = normalize_metric(
            values,
            low_score=rule.low_score,
            high_score=rule.high_score,
        )

        reliability = _get_reliability(scored, rule.reliability_base).copy()

        # In the production model, -1 is used as a plug for unavailable values
        # in selected metrics. Such observations contribute zero points.
        if rule.use_missing_value_plug:
            reliability[values == -1] = 0.0

        metric_score = normalized * reliability * rule.weight

        if include_intermediate_columns:
            scored[f"{metric}_norm"] = normalized
            scored[f"{metric}_reliability"] = reliability

        scored[f"{metric}_score"] = metric_score
        total_score += metric_score

    scored["total_score"] = total_score
    return scored


def select_portfolio_output(scored: pd.DataFrame) -> pd.DataFrame:
    """Return a compact explainability-oriented result for review or export."""
    base_columns = [
        column
        for column in [
            "sku",
            "days_in_stock",
            "revenue",
            "profit",
            "margin",
            "buyout_cnt",
            "cancel_rate",
            "buyout_rate",
            "return_rate",
            "stability",
            "rating",
            "feedback_digits",
            "views",
            "opens",
            "order_cnt",
            "CR",
            "CTR",
            "avr_pos",
        ]
        if column in scored.columns
    ]

    explainability_columns = [
        column
        for column in scored.columns
        if column.endswith("_norm")
        or column.endswith("_reliability")
        or column.endswith("_score")
    ]

    # `total_score` also ends with `_score`, so avoid adding it twice.
    explainability_columns = [
        column for column in explainability_columns if column != "total_score"
    ]

    return scored[
        base_columns + explainability_columns + ["total_score"]
    ].sort_values("total_score", ascending=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate marketplace SKU scores from a prepared 90-day dataset."
    )
    parser.add_argument(
        "input_csv",
        type=Path,
        help="CSV with prepared SKU-level marketplace metrics.",
    )
    parser.add_argument(
        "output_csv",
        type=Path,
        help="Path for the scored CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    df = pd.read_csv(args.input_csv)
    scored = score_skus(df, include_intermediate_columns=True)
    output = select_portfolio_output(scored)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output_csv, index=False)

    print(
        f"Scored {len(output):,} marketplace listings. "
        f"Saved to: {args.output_csv}"
    )


if __name__ == "__main__":
    main()
