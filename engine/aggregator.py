"""
Funding Rate Aggregator.
Collects funding rates from all connected platforms,
normalises them, and identifies cross-platform spreads.
"""

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import settings
from connectors.base import BaseConnector
from utils.logger import get_logger
from utils.models import ArbOpportunity, FundingRate, Platform
from utils.crypto_list import TOP_200_SYMBOLS

log = get_logger("aggregator")


class FundingRateAggregator:
    """
    Aggregates funding rates across all platforms and detects
    arbitrage opportunities based on annualised spread thresholds.
    """

    def __init__(self, connectors: List[BaseConnector]):
        self._connectors = {c.platform: c for c in connectors}
        # symbol -> {platform -> FundingRate}
        self._rates: Dict[str, Dict[Platform, FundingRate]] = defaultdict(dict)
        self._last_update: Optional[datetime] = None

    @property
    def rates(self) -> Dict[str, Dict[Platform, FundingRate]]:
        return dict(self._rates)

    @property
    def last_update(self) -> Optional[datetime]:
        return self._last_update

    async def refresh_all_rates(self) -> Dict[str, Dict[Platform, FundingRate]]:
        """
        Fetch funding rates from all platforms concurrently.
        Returns the full rates map: symbol -> {platform -> FundingRate}.
        """
        self._rates.clear()

        # Fetch from all platforms in parallel
        tasks = {
            platform: asyncio.create_task(connector.get_all_funding_rates())
            for platform, connector in self._connectors.items()
        }

        results: Dict[Platform, List[FundingRate]] = {}
        for platform, task in tasks.items():
            try:
                rates = await task
                results[platform] = rates
                log.info(f"Fetched {len(rates)} rates from {platform.value}")
            except Exception as e:
                log.error(f"Error fetching rates from {platform.value}: {e}")
                results[platform] = []

        # Merge into unified map
        for platform, rate_list in results.items():
            for fr in rate_list:
                self._rates[fr.symbol][platform] = fr

        self._last_update = datetime.utcnow()

        # Filter to only top-200 symbols
        top_200 = set(TOP_200_SYMBOLS)
        filtered = {
            sym: platforms
            for sym, platforms in self._rates.items()
            if sym in top_200
        }
        self._rates = defaultdict(dict, filtered)

        log.info(
            f"Aggregated rates for {len(self._rates)} symbols "
            f"across {len(self._connectors)} platforms"
        )
        return dict(self._rates)

    async def find_opportunities(
        self,
        entry_threshold_pct: float = None,
        notional_usd: float = None,
    ) -> List[ArbOpportunity]:
        """
        Scan aggregated rates for pairs where the annualised funding
        rate spread exceeds the entry threshold.

        For each symbol, we want:
          - Platform with the HIGHEST annualised rate -> go SHORT there
            (you receive funding)
          - Platform with the LOWEST annualised rate  -> go LONG there
            (you pay less / receive if negative)

        Only returns opportunities where:
          spread_ann (%) > entry_threshold_pct

        Args:
            entry_threshold_pct: Minimum annualised spread to qualify.
            notional_usd: Estimated position size per leg for profit/fee
                          calculation. If None, uses min_balance * position_size%.
        """
        if entry_threshold_pct is None:
            entry_threshold_pct = settings.arb.entry_rate_diff_pct

        if notional_usd is None:
            notional_usd = settings.arb.min_balance_usd * (settings.arb.position_size_pct / 100)

        opportunities: List[ArbOpportunity] = []

        for symbol, platform_rates in self._rates.items():
            if len(platform_rates) < 2:
                continue  # need at least 2 platforms

            # Sort by annualised rate
            sorted_rates = sorted(
                platform_rates.items(),
                key=lambda x: x[1].rate_annualised,
            )

            lowest_platform, lowest_rate = sorted_rates[0]
            highest_platform, highest_rate = sorted_rates[-1]

            # Spread in percentage points
            spread_ann = (highest_rate.rate_annualised - lowest_rate.rate_annualised) * 100

            if spread_ann >= entry_threshold_pct:
                # Estimate fees for both legs using actual connector methods
                long_conn = self._connectors.get(lowest_platform)
                short_conn = self._connectors.get(highest_platform)

                fee_long = notional_usd * 0.001  # fallback
                fee_short = notional_usd * 0.001

                if long_conn and short_conn:
                    try:
                        fee_long, fee_short = await asyncio.gather(
                            long_conn.estimate_fees(symbol, notional_usd),
                            short_conn.estimate_fees(symbol, notional_usd),
                        )
                    except Exception:
                        fee_long = notional_usd * 0.001
                        fee_short = notional_usd * 0.001

                total_fees = fee_long + fee_short
                daily_profit = (spread_ann / 100 / 365) * notional_usd
                net_daily = daily_profit - (total_fees / 30)  # amortise fees over ~30 days

                opp = ArbOpportunity(
                    symbol=symbol,
                    long_platform=lowest_platform,
                    short_platform=highest_platform,
                    long_rate_ann=lowest_rate.rate_annualised * 100,
                    short_rate_ann=highest_rate.rate_annualised * 100,
                    spread_ann=spread_ann,
                    estimated_profit_daily_usd=daily_profit,
                    estimated_fees_usd=total_fees,
                    net_profit_daily_usd=net_daily,
                )
                opportunities.append(opp)

        # Sort by spread descending
        opportunities.sort(key=lambda o: o.spread_ann, reverse=True)
        return opportunities

    def find_exit_candidates(
        self,
        open_positions: List,  # List[Position]
        exit_threshold_pct: float = None,
    ) -> List:
        """
        Check open positions: if the spread has narrowed below the exit
        threshold, flag them for closure.
        """
        if exit_threshold_pct is None:
            exit_threshold_pct = settings.arb.exit_rate_diff_pct

        candidates = []
        for pos in open_positions:
            sym_rates = self._rates.get(pos.symbol, {})
            long_rate = sym_rates.get(pos.long_platform)
            short_rate = sym_rates.get(pos.short_platform)

            if long_rate is None or short_rate is None:
                log.warning(
                    f"Cannot evaluate exit for {pos.symbol}: "
                    f"missing rate on {pos.long_platform} or {pos.short_platform}"
                )
                continue

            current_spread = (short_rate.rate_annualised - long_rate.rate_annualised) * 100

            if current_spread <= exit_threshold_pct:
                log.info(
                    f"EXIT signal: {pos.symbol} spread narrowed to {current_spread:.2f}% "
                    f"(threshold {exit_threshold_pct}%)"
                )
                candidates.append(pos)

        return candidates

    def get_rate_summary(self) -> List[dict]:
        """Return a flat list suitable for dashboard rendering."""
        rows = []
        for symbol, platform_rates in sorted(self._rates.items()):
            for platform, fr in sorted(platform_rates.items(), key=lambda x: x[0].value):
                rows.append({
                    "symbol": symbol,
                    "platform": platform.value,
                    "rate_hourly_pct": fr.rate_hourly * 100,
                    "rate_ann_pct": fr.rate_annualised * 100,
                    "timestamp": fr.timestamp.isoformat(),
                })
        return rows
