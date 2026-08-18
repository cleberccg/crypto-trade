"""Fix duplicate calculate() methods in reversao_nextgen_v1.py"""
content = open('d:/xampp/htdocs/crypto/strategies/reversao_nextgen_v1.py', encoding='utf-8').read()

NEW_CALC = (
    "    # ------------------------------------------------------------------\n"
    "    # Calculation\n"
    "    # ------------------------------------------------------------------\n"
    "\n"
    "    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:\n"
    '        """\n'
    "        Add all indicator columns to a copy of *df*.\n"
    "\n"
    "        Performance: uses an internal cache so that repeated calls with growing\n"
    "        prefix slices (as done by BacktestEngine bar-by-bar) are served in O(1).\n"
    "        The first call on a full dataset triggers one O(n) vectorised pass.\n"
    "\n"
    "        Slow operations replaced:\n"
    "        - pd.qcut per bar  -> expanding().rank(pct=True) + np.where  [O(n) vectorised]\n"
    "        - apply(axis=1)    -> np.where on numpy arrays               [O(n) vectorised]\n"
    "\n"
    "        Columns added: ema_fast, ema_slow, rsi, bb_middle, bb_upper, bb_lower,\n"
    "        bb_percent_b, atr, trend_score, atr_bucket, volume_bucket,\n"
    "        bollinger_position, regime_reversal.\n"
    '        """\n'
    "        self._assert_initialized()\n"
    "        n = len(df)\n"
    "\n"
    "        # --- Cache hit: return pre-computed slice (O(1)) ---\n"
    "        if self._enriched_cache is not None and n <= len(self._enriched_cache):\n"
    "            if n > 0 and df.index[-1] == self._enriched_cache.index[n - 1]:\n"
    "                return self._enriched_cache.iloc[:n]\n"
    "\n"
    "        # --- Full vectorised computation (O(n)) ---\n"
    "        _t0 = time.perf_counter()\n"
    '        logger.debug("%s \u2014 calculate: computing indicators for %d bars", self.name, n)\n'
    "        result = df.copy()\n"
    "\n"
    "        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]\n"
    "        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]\n"
    '        logger.debug("%s \u2014 EMA done (%.3fs)", self.name, time.perf_counter() - _t0)\n'
    "\n"
    "        _t1 = time.perf_counter()\n"
    '        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]\n'
    '        logger.debug("%s \u2014 RSI done (%.3fs)", self.name, time.perf_counter() - _t1)\n'
    "\n"
    "        _t2 = time.perf_counter()\n"
    '        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]\n'
    '        logger.debug("%s \u2014 ATR done (%.3fs)", self.name, time.perf_counter() - _t2)\n'
    "\n"
    "        _t3 = time.perf_counter()\n"
    "        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]\n"
    '        result["bb_middle"] = bb_df["middle"]\n'
    '        result["bb_upper"] = bb_df["upper"]\n'
    '        result["bb_lower"] = bb_df["lower"]\n'
    '        result["bb_percent_b"] = bb_df["percent_b"]\n'
    '        logger.debug("%s \u2014 Bollinger done (%.3fs)", self.name, time.perf_counter() - _t3)\n'
    "\n"
    "        # Trend score (EMA-based)\n"
    "        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]\n"
    "        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]\n"
    '        result["trend_score"] = (\n'
    "            (result[ema_fast_col] - result[ema_slow_col]) / result[ema_slow_col] * 100\n"
    "        )\n"
    "\n"
    "        _t4 = time.perf_counter()\n"
    "        # ATR tertiles: expanding-rank replaces pd.qcut (mathematically equivalent)\n"
    '        _atr_rank = result["atr"].expanding(min_periods=3).rank(pct=True).to_numpy()\n'
    '        result["atr_bucket"] = np.where(\n'
    '            np.isnan(_atr_rank), "mid_atr",\n'
    '            np.where(_atr_rank <= 1 / 3, "low_atr",\n'
    '            np.where(_atr_rank <= 2 / 3, "mid_atr", "high_atr")),\n'
    "        )\n"
    '        logger.debug("%s \u2014 ATR buckets done (%.3fs)", self.name, time.perf_counter() - _t4)\n'
    "\n"
    "        # Volume bucket: fixed thresholds, already O(n)\n"
    '        result["relative_volume"] = result["volume"] / result["volume"].rolling(20).mean()\n'
    '        result["volume_bucket"] = pd.cut(\n'
    '            result["relative_volume"],\n'
    '            bins=[-float("inf"), 0.9, 1.1, float("inf")],\n'
    '            labels=["low_volume", "normal_volume", "high_volume"],\n'
    "            include_lowest=True,\n"
    "        ).astype(str)\n"
    "\n"
    "        _t5 = time.perf_counter()\n"
    "        # Bollinger position: numpy.where replaces apply(axis=1)\n"
    '        _close = result["close"].to_numpy()\n'
    '        _bb_upper = result["bb_upper"].to_numpy()\n'
    '        _bb_lower = result["bb_lower"].to_numpy()\n'
    '        result["bollinger_position"] = np.where(\n'
    '            _close > _bb_upper, "above_upper",\n'
    '            np.where(_close < _bb_lower, "below_lower", "inside_band"),\n'
    "        )\n"
    '        logger.debug("%s \u2014 Bollinger positions done (%.3fs)", self.name, time.perf_counter() - _t5)\n'
    "\n"
    "        # Regime: detect reversal (trend_score changing sign or crossing zero)\n"
    '        result["trend_score_prev"] = result["trend_score"].shift(1)\n'
    '        result["regime_reversal"] = (\n'
    '            (result["trend_score"] * result["trend_score_prev"] < 0)  # Sign change\n'
    "            | (\n"
    '                (result["trend_score"].abs() < 0.2)\n'
    '                & (result["trend_score_prev"].abs() > 0.2)\n'
    "            )  # Entering consolidation\n"
    "        )\n"
    "\n"
    "        _total = time.perf_counter() - _t0\n"
    '        logger.info("%s \u2014 indicators pre-computed: %d bars in %.2fs", self.name, n, _total)\n'
    "\n"
    "        # Cache for subsequent prefix-slice lookups by BacktestEngine\n"
    "        if self._enriched_cache is None or n > len(self._enriched_cache):\n"
    "            self._enriched_cache = result\n"
    "\n"
    "        return result\n"
    "\n"
)

end_marker = "@staticmethod\n    def _get_bollinger_position("
calc_start = content.find(
    "    # ------------------------------------------------------------------\n    # Calculation\n"
)
calc_end = content.find(end_marker, calc_start)

new_content = content[:calc_start] + NEW_CALC + "    " + content[calc_end:]
open('d:/xampp/htdocs/crypto/strategies/reversao_nextgen_v1.py', 'w', encoding='utf-8').write(new_content)
print('Done. Total lines:', new_content.count('\n'))
# Verify only one calculate()
count = new_content.count('    def calculate(')
print(f'calculate() definitions: {count}')
