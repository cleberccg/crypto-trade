from itertools import islice

from optimizer.parameter_grid import ParameterGrid


def main() -> None:
    g = ParameterGrid()

    ema_fast_values = [15, 20, 25]
    ema_slow_values = [45, 50]
    rsi_period_values = [14]
    atr_period_values = [14]
    atr_stop_multiplier_values = [1.5, 2.0, 2.5]
    risk_reward_ratio_values = [2.5, 3.0, 3.18, 3.8]
    score_min_values = [0.6]
    volume_multiplier_min_values = [0.8, 1.0]
    atr_high_threshold_values = [1.0, 1.2]
    volume_low_threshold_values = [0.8, 1.0]

    param_sizes = {
        "ema_fast": len(ema_fast_values),
        "ema_slow": len(ema_slow_values),
        "rsi_period": len(rsi_period_values),
        "atr_period": len(atr_period_values),
        "atr_stop_multiplier": len(atr_stop_multiplier_values),
        "risk_reward_ratio": len(risk_reward_ratio_values),
        "score_min": len(score_min_values),
        "volume_multiplier_min": len(volume_multiplier_min_values),
        "atr_high_threshold": len(atr_high_threshold_values),
        "volume_low_threshold": len(volume_low_threshold_values),
    }

    theoretical_total = 1
    for v in param_sizes.values():
        theoretical_total *= v

    all_items = list(g.combinations(strategy_name="ReversaoNextGenV1"))
    unique_items = len({tuple(sorted(d.items())) for d in all_items})
    effective_500 = sum(1 for _ in islice(g.combinations(limit=500, strategy_name="ReversaoNextGenV1"), 500))

    print("PARAMETER GRID REPORT — ReversaoNextGenV1")
    print("=" * 50)
    print("Parâmetros otimizáveis (quantidade de valores):")
    for k, v in param_sizes.items():
        print(f"- {k}: {v}")

    print(f"Total teórico de combinações: {theoretical_total}")
    print(f"Total após filtros/geração real: {len(all_items)}")
    print(f"Total único após deduplicação: {unique_items}")
    print(f"Total efetivamente enviado ao optimizer (limit=500): {effective_500}")
    print(f"Grid dentro da faixa alvo 500-700? {'SIM' if 500 <= len(all_items) <= 700 else 'NÃO'}")


if __name__ == "__main__":
    main()
