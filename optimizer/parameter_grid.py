"""Parameter grid generation for strategy optimization."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable


@dataclass(frozen=True)
class ParameterGrid:
    """Defines the optimizer search space."""

    ema_fast_min: int = 5
    ema_fast_max: int = 30
    ema_fast_step: int = 1
    ema_mid_min: int = 20
    ema_mid_max: int = 80
    ema_mid_step: int = 1
    ema_trend_min: int = 100
    ema_trend_max: int = 300
    ema_trend_step: int = 10
    rsi_min_min: int = 40
    rsi_min_max: int = 60
    rsi_min_step: int = 1
    rsi_max_min: int = 60
    rsi_max_max: int = 80
    rsi_max_step: int = 1
    atr_stop_min: float = 1.0
    atr_stop_max: float = 4.0
    atr_stop_step: float = 0.25
    rr_min: float = 1.2
    rr_max: float = 4.0
    rr_step: float = 0.2
    score_min: int = 50
    score_max: int = 95
    score_step: int = 5
    volume_min: float = 0.8
    volume_max: float = 2.0
    volume_step: float = 0.1
    breakout_window_min: int = 12
    breakout_window_max: int = 40
    breakout_window_step: int = 2
    breakout_buffer_min: float = 0.00025
    breakout_buffer_max: float = 0.002
    breakout_buffer_step: float = 0.00025
    sample_seed: int = 13

    def _frange(self, start: float, stop: float, step: float) -> list[float]:
        values: list[float] = []
        current = start
        while current <= stop + 1e-9:
            values.append(round(current, 10))
            current += step
        return values

    def ema_fast_values(self) -> list[int]:
        return list(range(self.ema_fast_min, self.ema_fast_max + 1, self.ema_fast_step))

    def ema_mid_values(self) -> list[int]:
        return list(range(self.ema_mid_min, self.ema_mid_max + 1, self.ema_mid_step))

    def ema_trend_values(self) -> list[int]:
        return list(range(self.ema_trend_min, self.ema_trend_max + 1, self.ema_trend_step))

    def rsi_min_values(self) -> list[int]:
        return list(range(self.rsi_min_min, self.rsi_min_max + 1, self.rsi_min_step))

    def rsi_max_values(self) -> list[int]:
        return list(range(self.rsi_max_min, self.rsi_max_max + 1, self.rsi_max_step))

    def atr_stop_values(self) -> list[float]:
        return self._frange(self.atr_stop_min, self.atr_stop_max, self.atr_stop_step)

    def rr_values(self) -> list[float]:
        return self._frange(self.rr_min, self.rr_max, self.rr_step)

    def score_values(self) -> list[int]:
        return list(range(self.score_min, self.score_max + 1, self.score_step))

    def volume_values(self) -> list[float]:
        return self._frange(self.volume_min, self.volume_max, self.volume_step)

    def breakout_window_values(self) -> list[int]:
        return list(range(self.breakout_window_min, self.breakout_window_max + 1, self.breakout_window_step))

    def breakout_buffer_values(self) -> list[float]:
        return self._frange(self.breakout_buffer_min, self.breakout_buffer_max, self.breakout_buffer_step)

    def _trend_combinations(self, limit: int | None = None) -> Iterable[dict[str, float | int]]:
        ema_fast = self.ema_fast_values()
        ema_mid = self.ema_mid_values()
        ema_trend = self.ema_trend_values()
        rsi_min = self.rsi_min_values()
        rsi_max = self.rsi_max_values()
        atr_stop = self.atr_stop_values()
        rr = self.rr_values()
        score = self.score_values()
        volume = self.volume_values()

        value_sets = [ema_fast, ema_mid, ema_trend, rsi_min, rsi_max, atr_stop, rr, score, volume]
        lengths = [len(values) for values in value_sets]
        if any(length == 0 for length in lengths):
            return []

        total_space = 1
        for length in lengths:
            total_space *= length

        target = total_space if limit is None else min(limit, total_space)
        if target <= 0:
            return []

        strides = [1, 7, 13, 17, 19, 23, 29, 31, 37]
        offsets = [self.sample_seed % length for length in lengths]

        def _generator() -> Iterable[dict[str, float | int]]:
            seen: set[tuple[int, ...]] = set()
            index = 0
            while len(seen) < target and index < total_space * 2:
                combo_indexes = []
                for position, length in enumerate(lengths):
                    combo_indexes.append((offsets[position] + index * strides[position]) % length)

                key = tuple(combo_indexes)
                if key not in seen:
                    seen.add(key)
                    yield {
                        "ema_fast": ema_fast[key[0]],
                        "ema_mid": ema_mid[key[1]],
                        "ema_trend": ema_trend[key[2]],
                        "rsi_min": rsi_min[key[3]],
                        "rsi_max": rsi_max[key[4]],
                        "atr_stop_multiplier": atr_stop[key[5]],
                        "risk_reward_ratio": rr[key[6]],
                        "score_min": score[key[7]],
                        "volume_multiplier": volume[key[8]],
                    }
                index += 1

        return _generator()

    def _mean_reversion_combinations(self, limit: int | None = None) -> Iterable[dict[str, float | int]]:
        ema_fast = self.ema_fast_values()
        ema_mid = self.ema_mid_values()
        rsi_min = self.rsi_min_values()
        rsi_max = self.rsi_max_values()
        atr_stop = self.atr_stop_values()
        rr = self.rr_values()
        score = self.score_values()
        volume = self.volume_values()

        value_sets = [ema_fast, ema_mid, rsi_min, rsi_max, atr_stop, rr, score, volume]
        lengths = [len(values) for values in value_sets]
        if any(length == 0 for length in lengths):
            return []

        total_space = 1
        for length in lengths:
            total_space *= length

        target = total_space if limit is None else min(limit, total_space)
        if target <= 0:
            return []

        strides = [1, 7, 13, 17, 23, 29, 31, 37]
        offsets = [self.sample_seed % length for length in lengths]

        def _generator() -> Iterable[dict[str, float | int]]:
            seen: set[tuple[int, ...]] = set()
            index = 0
            while len(seen) < target and index < total_space * 2:
                combo_indexes = []
                for position, length in enumerate(lengths):
                    combo_indexes.append((offsets[position] + index * strides[position]) % length)

                key = tuple(combo_indexes)
                if key not in seen:
                    seen.add(key)
                    yield {
                        "ema_fast": ema_fast[key[0]],
                        "ema_mid": ema_mid[key[1]],
                        "rsi_min": rsi_min[key[2]],
                        "rsi_max": rsi_max[key[3]],
                        "atr_stop_multiplier": atr_stop[key[4]],
                        "risk_reward_ratio": rr[key[5]],
                        "score_min": score[key[6]],
                        "volume_multiplier": volume[key[7]],
                    }
                index += 1

        return _generator()

    def _breakout_combinations(self, limit: int | None = None) -> Iterable[dict[str, float | int]]:
        ema_fast = self.ema_fast_values()
        ema_mid = self.ema_mid_values()
        rsi_min = self.rsi_min_values()
        rsi_max = self.rsi_max_values()
        atr_stop = self.atr_stop_values()
        rr = self.rr_values()
        score = self.score_values()
        volume = self.volume_values()
        breakout_window = self.breakout_window_values()
        breakout_buffer = self.breakout_buffer_values()

        value_sets = [ema_fast, ema_mid, rsi_min, rsi_max, atr_stop, rr, score, volume, breakout_window, breakout_buffer]
        lengths = [len(values) for values in value_sets]
        if any(length == 0 for length in lengths):
            return []

        total_space = 1
        for length in lengths:
            total_space *= length

        target = total_space if limit is None else min(limit, total_space)
        if target <= 0:
            return []

        strides = [1, 7, 13, 17, 19, 23, 29, 31, 37, 41]
        offsets = [self.sample_seed % length for length in lengths]

        def _generator() -> Iterable[dict[str, float | int]]:
            seen: set[tuple[int, ...]] = set()
            index = 0
            while len(seen) < target and index < total_space * 2:
                combo_indexes = []
                for position, length in enumerate(lengths):
                    combo_indexes.append((offsets[position] + index * strides[position]) % length)

                key = tuple(combo_indexes)
                if key not in seen:
                    seen.add(key)
                    yield {
                        "ema_fast": ema_fast[key[0]],
                        "ema_mid": ema_mid[key[1]],
                        "rsi_min": rsi_min[key[2]],
                        "rsi_max": rsi_max[key[3]],
                        "atr_stop_multiplier": atr_stop[key[4]],
                        "risk_reward_ratio": rr[key[5]],
                        "score_min": score[key[6]],
                        "volume_multiplier": volume[key[7]],
                        "breakout_window": breakout_window[key[8]],
                        "breakout_buffer": breakout_buffer[key[9]],
                    }
                index += 1

        return _generator()

    def _reversao_nextgen_combinations(self, limit: int | None = None) -> Iterable[dict[str, float | int]]:
        """Parameter combinations for ReversaoNextGenV1 strategy (reversal edge detection)."""
        ema_fast_values = [15, 20, 25]
        ema_slow_values = [45, 50]
        rsi_period_values = [14]  # Fixed
        atr_period_values = [14]  # Fixed
        atr_stop_multiplier_values = [1.5, 2.0, 2.5]
        risk_reward_ratio_values = [2.5, 3.0, 3.18, 3.8]
        score_min_values = [0.6]
        volume_multiplier_min_values = [0.8, 1.0]
        atr_high_threshold_values = [1.0, 1.2]
        volume_low_threshold_values = [0.8, 1.0]

        all_combinations = list(
            product(
                ema_fast_values,
                ema_slow_values,
                rsi_period_values,
                atr_period_values,
                atr_stop_multiplier_values,
                risk_reward_ratio_values,
                score_min_values,
                volume_multiplier_min_values,
                atr_high_threshold_values,
                volume_low_threshold_values,
            )
        )
        total_space = len(all_combinations)
        target = total_space if limit is None else min(limit, total_space)
        if target <= 0:
            return []

        # Coprime step guarantees full-cycle deterministic permutation over total_space.
        start = self.sample_seed % total_space
        step = max(1, total_space - 1)

        def _generator() -> Iterable[dict[str, float | int]]:
            for idx in range(target):
                pick = (start + idx * step) % total_space
                combo = all_combinations[pick]
                yield {
                    "ema_fast": combo[0],
                    "ema_slow": combo[1],
                    "rsi_period": combo[2],
                    "atr_period": combo[3],
                    "atr_stop_multiplier": combo[4],
                    "risk_reward_ratio": combo[5],
                    "score_min": combo[6],
                    "volume_multiplier_min": combo[7],
                    "atr_high_threshold": combo[8],
                    "volume_low_threshold": combo[9],
                }

        return _generator()

    def _supertrend_combinations(self, limit: int | None = None) -> Iterable[dict[str, float | int]]:
        """Compact SuperTrend search space for controlled phase pipelines."""
        atr_period_values = [7, 10, 14]
        atr_multiplier_values = [2.0, 2.5, 3.0]
        trend_confirmation_values = [1, 2, 3]
        stop_atr_multiplier_values = [1.5, 2.0, 2.5]
        take_profit_pct_values = [0.0, 0.01, 0.02]
        risk_reward_ratio_values = [1.5, 2.0, 2.5, 3.0]
        score_min_values = [0, 30, 50]

        all_combinations = list(
            product(
                atr_period_values,
                atr_multiplier_values,
                trend_confirmation_values,
                stop_atr_multiplier_values,
                take_profit_pct_values,
                risk_reward_ratio_values,
                score_min_values,
            )
        )
        total_space = len(all_combinations)
        target = total_space if limit is None else min(limit, total_space)
        if target <= 0:
            return []

        start = self.sample_seed % total_space
        step = max(1, total_space - 1)

        def _generator() -> Iterable[dict[str, float | int]]:
            for idx in range(target):
                pick = (start + idx * step) % total_space
                combo = all_combinations[pick]
                yield {
                    "atr_period": combo[0],
                    "atr_multiplier": combo[1],
                    "trend_confirmation": combo[2],
                    "stop_atr_multiplier": combo[3],
                    "take_profit_pct": combo[4],
                    "risk_reward_ratio": combo[5],
                    "score_min": combo[6],
                }

        return _generator()

    def combinations(self, limit: int | None = None, strategy_name: str = "TrendV1") -> Iterable[dict[str, float | int]]:
        normalized = strategy_name.strip().lower().replace("_", "")
        if normalized in {"reversaonextgenv1", "reversaov1", "h27"}:
            return self._reversao_nextgen_combinations(limit=limit)
        if normalized in {"supertrendv1", "supertrend"}:
            return self._supertrend_combinations(limit=limit)
        if normalized in {"meanreversionv1", "mrv1"}:
            return self._mean_reversion_combinations(limit=limit)
        if normalized in {"breakoutv1", "brk1"}:
            return self._breakout_combinations(limit=limit)
        return self._trend_combinations(limit=limit)
