"""Research services package placeholder for next-phase activation."""

from research.services.strategy_discovery_pipeline import DiscoveryPilotPlan, DiscoveryWeights, run_strategy_discovery_pipeline
from research.services.scientific_robustness_validation import (
    ScientificRobustnessValidationConfig,
    ScientificRobustnessValidationService,
)
from research.services.execution_framework_optimization import (
	ExecutionFrameworkOptimizationConfig,
	ExecutionFrameworkOptimizationService,
)
from research.services.trade_outcome_controlled_implementation import (
    TradeOutcomeControlledImplementationConfig,
    TradeOutcomeControlledImplementationService,
)

__all__ = [
	"DiscoveryPilotPlan",
	"DiscoveryWeights",
	"run_strategy_discovery_pipeline",
	"ExecutionFrameworkOptimizationConfig",
	"ExecutionFrameworkOptimizationService",
	"ScientificRobustnessValidationConfig",
	"ScientificRobustnessValidationService",
	"TradeOutcomeControlledImplementationConfig",
	"TradeOutcomeControlledImplementationService",
]
