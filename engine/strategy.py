"""Strategy scoring and ranking for funding-rate arbitrage.

This module is the *single preferred mutation surface* for autonomous research.
Live execution can call into it, and offline backtests can replay historical
snapshots through the same logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from config import settings
from utils.models import ArbOpportunity, Platform


@dataclass
class StrategyContext:
    """Additional context used to score opportunities.

    All fields are optional and default to conservative values so the strategy
    can be used both in live trading and in lightweight backtests.
    """

    symbol_score: float = 1.0
    pair_score: float = 1.0
    expected_hold_hours: float = 24.0
    spread_persistence_hours: float = 24.0
    execution_risk: float = 0.10
    slippage_bps: float = 5.0
    opportunity_cost_usd: float = 0.0


@dataclass
class StrategyDecision:
    symbol: str
    long_platform: Platform
    short_platform: Platform
    score: float
    expected_net_usd: float
    expected_hold_hours: float
    recommended_size_usd: float
    days_to_breakeven: float
    should_trade: bool
    reasons: List[str]
    metadata: Dict[str, float]


class StrategyPolicy:
    """Default policy for ranking and filtering arbitrage opportunities.

    Keep this file small and easy to mutate. The rest of the research harness
    treats this class as the primary experimental surface.
    """

    def __init__(self):
        self.min_monthly_profit_usd = settings.arb.min_profit_threshold_usd
        self.max_breakeven_days = 7.0
        self.max_execution_risk = 0.35
        self.default_hold_hours = 24.0

    def score_opportunity(
        self,
        opportunity: ArbOpportunity,
        size_usd: float,
        context: Optional[StrategyContext] = None,
    ) -> StrategyDecision:
        context = context or StrategyContext()
        reasons: List[str] = []

        expected_hold_hours = max(1.0, context.expected_hold_hours or self.default_hold_hours)
        expected_hold_days = expected_hold_hours / 24.0

        funding_capture_usd = (opportunity.spread_ann / 100.0 / 365.0) * size_usd * expected_hold_days
        fees_usd = opportunity.estimated_fees_usd
        slippage_usd = size_usd * (context.slippage_bps / 10000.0)
        execution_penalty_usd = funding_capture_usd * context.execution_risk
        persistence_multiplier = min(1.25, max(0.25, context.spread_persistence_hours / expected_hold_hours))

        raw_expected_net = (
            funding_capture_usd * persistence_multiplier
            - fees_usd
            - slippage_usd
            - execution_penalty_usd
            - context.opportunity_cost_usd
        )

        quality_multiplier = max(0.1, context.symbol_score * context.pair_score)
        risk_adjusted_expected_net = raw_expected_net * quality_multiplier
        monthly_expected_usd = risk_adjusted_expected_net * (30.0 / expected_hold_days)

        if opportunity.net_profit_daily_usd > 0:
            days_to_breakeven = fees_usd / max(opportunity.net_profit_daily_usd, 1e-9)
        else:
            days_to_breakeven = 999.0

        score = (
            risk_adjusted_expected_net
            - (context.execution_risk * size_usd * 0.001)
        )

        should_trade = True
        if risk_adjusted_expected_net <= 0:
            should_trade = False
            reasons.append("expected_net_non_positive")
        if monthly_expected_usd < self.min_monthly_profit_usd:
            should_trade = False
            reasons.append("monthly_profit_below_threshold")
        if days_to_breakeven > self.max_breakeven_days:
            should_trade = False
            reasons.append("breakeven_too_slow")
        if context.execution_risk > self.max_execution_risk:
            should_trade = False
            reasons.append("execution_risk_too_high")

        return StrategyDecision(
            symbol=opportunity.symbol,
            long_platform=opportunity.long_platform,
            short_platform=opportunity.short_platform,
            score=score,
            expected_net_usd=risk_adjusted_expected_net,
            expected_hold_hours=expected_hold_hours,
            recommended_size_usd=size_usd,
            days_to_breakeven=days_to_breakeven,
            should_trade=should_trade,
            reasons=reasons,
            metadata={
                "funding_capture_usd": funding_capture_usd,
                "fees_usd": fees_usd,
                "slippage_usd": slippage_usd,
                "execution_penalty_usd": execution_penalty_usd,
                "persistence_multiplier": persistence_multiplier,
                "quality_multiplier": quality_multiplier,
                "monthly_expected_usd": monthly_expected_usd,
            },
        )

    def rank_opportunities(
        self,
        opportunities: Iterable[ArbOpportunity],
        size_usd: float,
        context_map: Optional[Dict[str, StrategyContext]] = None,
    ) -> List[StrategyDecision]:
        context_map = context_map or {}
        decisions = [
            self.score_opportunity(
                opportunity=opp,
                size_usd=size_usd,
                context=context_map.get(opp.symbol),
            )
            for opp in opportunities
        ]
        decisions.sort(key=lambda item: item.score, reverse=True)
        return decisions
