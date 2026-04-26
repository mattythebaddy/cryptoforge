"""Monte Carlo resampling for probability distributions."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class MonteCarloResult:
    simulations: int = 0
    # Percentiles of final equity
    p5_equity: float = 0.0
    p25_equity: float = 0.0
    p50_equity: float = 0.0
    p75_equity: float = 0.0
    p95_equity: float = 0.0
    # Percentiles of max drawdown
    p5_drawdown: float = 0.0
    p25_drawdown: float = 0.0
    p50_drawdown: float = 0.0
    p75_drawdown: float = 0.0
    p95_drawdown: float = 0.0
    # Probabilities
    probability_of_ruin: float = 0.0  # equity < 50% of start
    probability_of_profit: float = 0.0  # equity > start
    # Distribution
    equity_distribution: list[float] = field(default_factory=list)
    drawdown_distribution: list[float] = field(default_factory=list)


class MonteCarloSimulator:
    """Resample trade results to estimate probability distributions."""

    def run(
        self,
        trade_pnls: list[float],
        initial_capital: float = 10000.0,
        num_simulations: int = 1000,
        seed: int = 42,
    ) -> MonteCarloResult:
        """
        Bootstrap resample trade P&Ls and compute equity paths.
        """
        if not trade_pnls:
            return MonteCarloResult()

        rng = np.random.default_rng(seed)
        pnls = np.array(trade_pnls)
        n_trades = len(pnls)

        final_equities = []
        max_drawdowns = []

        for _ in range(num_simulations):
            # Resample with replacement
            sampled = rng.choice(pnls, size=n_trades, replace=True)

            # Build equity curve
            equity = initial_capital
            peak = equity
            max_dd = 0.0

            for pnl in sampled:
                equity += pnl
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

            final_equities.append(equity)
            max_drawdowns.append(max_dd)

        equities = np.array(final_equities)
        drawdowns = np.array(max_drawdowns)

        result = MonteCarloResult(
            simulations=num_simulations,
            p5_equity=float(np.percentile(equities, 5)),
            p25_equity=float(np.percentile(equities, 25)),
            p50_equity=float(np.percentile(equities, 50)),
            p75_equity=float(np.percentile(equities, 75)),
            p95_equity=float(np.percentile(equities, 95)),
            p5_drawdown=float(np.percentile(drawdowns, 5)),
            p25_drawdown=float(np.percentile(drawdowns, 25)),
            p50_drawdown=float(np.percentile(drawdowns, 50)),
            p75_drawdown=float(np.percentile(drawdowns, 75)),
            p95_drawdown=float(np.percentile(drawdowns, 95)),
            probability_of_ruin=float(np.mean(equities < initial_capital * 0.5)),
            probability_of_profit=float(np.mean(equities > initial_capital)),
            equity_distribution=equities.tolist(),
            drawdown_distribution=drawdowns.tolist(),
        )

        log.info(
            "monte_carlo.complete",
            sims=num_simulations,
            median_equity=round(result.p50_equity, 2),
            prob_profit=f"{result.probability_of_profit*100:.1f}%",
            prob_ruin=f"{result.probability_of_ruin*100:.1f}%",
        )

        return result
