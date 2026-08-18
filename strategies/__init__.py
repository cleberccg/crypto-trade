"""
Strategies Package.
"""
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.factory import create_strategy
from strategies.families import QuantStrategy
from strategies.breakout_v1 import BreakoutV1Strategy
from strategies.classic_catalog_strategies import (
	ClassicATRBreakoutStrategy,
	ClassicBollingerReversalStrategy,
	ClassicDonchianBreakoutStrategy,
	ClassicDualMomentumStrategy,
	ClassicEMACrossoverStrategy,
	ClassicKeltnerChannelStrategy,
	ClassicMACDTrendStrategy,
	ClassicRSIMeanReversionStrategy,
	ClassicSMACrossoverStrategy,
	ClassicVWAPReversionStrategy,
)
from strategies.mean_reversion_v1 import MeanReversionV1Strategy
from strategies.registry import (
	family_comparison_snapshot,
	list_registered_strategies,
	list_strategy_families,
)
from strategies.trade_outcome_nextgen_v1 import TradeOutcomeNextGenV1Strategy
from strategies.trade_outcome_nextgen_v1_1 import TradeOutcomeNextGenV11Strategy
from strategies.supertrend_v1 import SuperTrendV1Strategy
from strategies.trend_v1 import TrendV1Strategy
from strategies.trend_v2 import TrendV2Strategy

__all__ = [
	"BaseStrategy",
	"SignalType",
	"StrategySignal",
	"QuantStrategy",
	"TrendV1Strategy",
	"TrendV2Strategy",
	"BreakoutV1Strategy",
	"ClassicEMACrossoverStrategy",
	"ClassicSMACrossoverStrategy",
	"ClassicMACDTrendStrategy",
	"ClassicRSIMeanReversionStrategy",
	"ClassicBollingerReversalStrategy",
	"ClassicDonchianBreakoutStrategy",
	"ClassicATRBreakoutStrategy",
	"ClassicVWAPReversionStrategy",
	"ClassicKeltnerChannelStrategy",
	"ClassicDualMomentumStrategy",
	"MeanReversionV1Strategy",
	"TradeOutcomeNextGenV1Strategy",
	"TradeOutcomeNextGenV11Strategy",
	"SuperTrendV1Strategy",
	"create_strategy",
	"list_registered_strategies",
	"list_strategy_families",
	"family_comparison_snapshot",
]
