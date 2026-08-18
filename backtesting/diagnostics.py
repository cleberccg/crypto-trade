"""Backtest diagnostics report generation."""
from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import pandas as pd

from backtesting.engine import BacktestResult
from risk.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy, SignalType
from utils.logger import get_logger

logger = get_logger(__name__)

_RESULTS_DIR = Path(__file__).parent / "results"


class BacktestDiagnosticReporter:
    """Builds and saves detailed diagnostic reports for a backtest run."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._output_dir = output_dir or _RESULTS_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def generate_and_save(
        self,
        strategy: BaseStrategy,
        raw_df: pd.DataFrame,
        enriched_df: pd.DataFrame,
        result: BacktestResult,
        timeframe: str,
    ) -> tuple[Path, Path]:
        report = self._build_report(strategy, raw_df, enriched_df, result, timeframe)

        symbol_slug = result.symbol.replace("/", "")
        txt_path = self._output_dir / f"{result.strategy_name}_{symbol_slug}_diagnostic_report.txt"
        json_path = self._output_dir / f"{result.strategy_name}_{symbol_slug}_diagnostic_report.json"
        generic_txt_path = self._output_dir / "diagnostic_report.txt"
        generic_json_path = self._output_dir / "diagnostic_report.json"

        txt_content = self._render_text(report)
        txt_path.write_text(txt_content, encoding="utf-8")
        generic_txt_path.write_text(txt_content, encoding="utf-8")

        json_content = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        json_path.write_text(json_content, encoding="utf-8")
        generic_json_path.write_text(json_content, encoding="utf-8")

        print("\n" + txt_content)
        logger.info("Diagnostic report saved: %s", txt_path)
        logger.info("Diagnostic JSON saved: %s", json_path)
        return txt_path, json_path

    def _build_report(
        self,
        strategy: BaseStrategy,
        raw_df: pd.DataFrame,
        enriched_df: pd.DataFrame,
        result: BacktestResult,
        timeframe: str,
    ) -> dict:
        warmup = result.config.warmup_bars
        valid_df = (
            enriched_df.iloc[warmup:].copy()
            if len(enriched_df) > warmup
            else enriched_df.iloc[0:0].copy()
        )

        indicator_cols = {
            "EMA20": "ema_20",
            "EMA50": "ema_50",
            "EMA200": "ema_200",
            "RSI": "rsi",
            "ATR": "atr",
            "Volume": "volume",
        }

        if "ema_200" not in valid_df.columns and "close" in valid_df.columns:
            valid_df["ema_200"] = pd.to_numeric(
                valid_df["close"], errors="coerce"
            ).ewm(span=200, adjust=False).mean()

        indicator_stats: dict[str, dict] = {}
        discarded_any_indicator = pd.Series(False, index=valid_df.index)

        for label, col in indicator_cols.items():
            if col not in valid_df.columns:
                indicator_stats[label] = {
                    "present": False,
                    "min": None,
                    "max": None,
                    "mean": None,
                    "nan_count": 0,
                    "invalid_count": 0,
                }
                continue

            s = pd.to_numeric(valid_df[col], errors="coerce")
            nan_count = int(s.isna().sum())
            invalid_count = int((~np.isfinite(s.fillna(np.nan))).sum() - nan_count)
            invalid_mask = s.isna() | ~np.isfinite(s.fillna(np.nan))
            discarded_any_indicator = discarded_any_indicator | invalid_mask

            indicator_stats[label] = {
                "present": True,
                "min": float(s.min()) if s.notna().any() else None,
                "max": float(s.max()) if s.notna().any() else None,
                "mean": float(s.mean()) if s.notna().any() else None,
                "nan_count": nan_count,
                "invalid_count": max(invalid_count, 0),
            }

        discarded_candles = int(discarded_any_indicator.sum()) if len(discarded_any_indicator) else 0

        condition_counts = self._condition_counts(valid_df)
        score_stats = self._score_stats(strategy, valid_df)
        signal_stats = self._signal_stats(strategy, valid_df)
        operations_stats = self._operations_stats(result)
        market_dist = self._market_distribution(valid_df)
        interesting = self._interesting_candles(strategy, valid_df)
        summary = self._summary(result, condition_counts, score_stats, signal_stats)

        start_ts = raw_df.index[0] if len(raw_df) else None
        end_ts = raw_df.index[-1] if len(raw_df) else None
        days = (
            float((end_ts - start_ts).total_seconds() / 86400.0)
            if start_ts is not None and end_ts is not None
            else 0.0
        )

        return {
            "general": {
                "total_candles_loaded": int(len(raw_df)),
                "total_valid_after_warmup": int(max(len(raw_df) - warmup, 0)),
                "first_candle": str(start_ts) if start_ts is not None else None,
                "last_candle": str(end_ts) if end_ts is not None else None,
                "analyzed_days": round(days, 2),
                "timeframe": timeframe,
                "symbol": result.symbol,
                "strategy": result.strategy_name,
                "warmup_bars": warmup,
            },
            "indicators": {
                "stats": indicator_stats,
                "candles_discarded": discarded_candles,
            },
            "conditions": condition_counts,
            "score": score_stats,
            "signals": signal_stats,
            "operations": operations_stats,
            "market_distribution": market_dist,
            "interesting_candles": interesting,
            "final_summary": summary,
            "backtest_metrics": result.metrics.to_dict(),
        }

    def _condition_counts(self, df: pd.DataFrame) -> dict:
        if df.empty:
            return {
                "ema20_gt_ema50": 0,
                "ema50_gt_ema200": 0,
                "price_gt_ema20": 0,
                "rsi_between_55_70": 0,
                "volume_above_mean": 0,
                "atr_above_min": 0,
                "all_conditions_true": 0,
            }

        ema20 = pd.to_numeric(df.get("ema_20"), errors="coerce")
        ema50 = pd.to_numeric(df.get("ema_50"), errors="coerce")
        ema200 = pd.to_numeric(df.get("ema_200"), errors="coerce")
        close = pd.to_numeric(df.get("close"), errors="coerce")
        rsi = pd.to_numeric(df.get("rsi"), errors="coerce")
        volume = pd.to_numeric(df.get("volume"), errors="coerce")
        atr = pd.to_numeric(df.get("atr"), errors="coerce")

        c1 = (ema20 > ema50).fillna(False)
        c2 = (ema50 > ema200).fillna(False)
        c3 = (close > ema20).fillna(False)
        c4 = ((rsi >= 55) & (rsi <= 70)).fillna(False)
        c5 = (volume > volume.mean()).fillna(False)
        c6 = (atr > atr.quantile(0.25)).fillna(False)
        all_true = c1 & c2 & c3 & c4 & c5 & c6

        return {
            "ema20_gt_ema50": int(c1.sum()),
            "ema50_gt_ema200": int(c2.sum()),
            "price_gt_ema20": int(c3.sum()),
            "rsi_between_55_70": int(c4.sum()),
            "volume_above_mean": int(c5.sum()),
            "atr_above_min": int(c6.sum()),
            "all_conditions_true": int(all_true.sum()),
        }

    def _score_stats(self, strategy: BaseStrategy, df: pd.DataFrame) -> dict:
        if df.empty:
            return {
                "min": 0.0,
                "max": 0.0,
                "mean": 0.0,
                "distribution_0_20": 0,
                "distribution_21_40": 0,
                "distribution_41_60": 0,
                "distribution_61_80": 0,
                "distribution_81_100": 0,
                "top_20": [],
            }

        scores = []
        for i in range(1, len(df) + 1):
            try:
                s = float(strategy.score(df.iloc[:i])) * 100.0
            except Exception:
                s = 0.0
            scores.append(s)

        score_series = pd.Series(scores, index=df.index, name="score")

        def _count(lo: float, hi: float) -> int:
            return int(((score_series >= lo) & (score_series <= hi)).sum())

        merged = df.copy()
        merged["score"] = score_series
        top = merged.sort_values("score", ascending=False).head(20)

        top_rows = []
        for ts, row in top.iterrows():
            top_rows.append(
                {
                    "date": str(ts),
                    "price": float(row.get("close", 0.0)),
                    "score": round(float(row["score"]), 2),
                    "ema20": _safe_float(row.get("ema_20")),
                    "ema50": _safe_float(row.get("ema_50")),
                    "ema200": _safe_float(row.get("ema_200")),
                    "rsi": _safe_float(row.get("rsi")),
                    "atr": _safe_float(row.get("atr")),
                    "volume": _safe_float(row.get("volume")),
                }
            )

        return {
            "min": round(float(score_series.min()), 2),
            "max": round(float(score_series.max()), 2),
            "mean": round(float(score_series.mean()), 2),
            "distribution_0_20": _count(0, 20),
            "distribution_21_40": _count(21, 40),
            "distribution_41_60": _count(41, 60),
            "distribution_61_80": _count(61, 80),
            "distribution_81_100": _count(81, 100),
            "top_20": top_rows,
        }

    def _signal_stats(self, strategy: BaseStrategy, df: pd.DataFrame) -> dict:
        buy_total = 0
        sell_total = 0
        buy_discarded = 0
        sell_discarded = 0
        reasons: dict[str, int] = {}

        rm = RiskManager()
        fake_portfolio = 10_000.0
        strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(strategy)

        for i in range(2, len(df) + 1):
            window = df.iloc[:i]
            last = window.iloc[-1]

            try:
                buy_sig = strategy.entry_signal(window)
            except Exception:
                buy_sig = None

            if buy_sig and buy_sig.signal == SignalType.BUY:
                buy_total += 1
                try:
                    rm.evaluate_trade(
                        portfolio_value=fake_portfolio,
                        entry_price=float(last["close"]),
                        stop_loss=buy_sig.stop_loss,
                        take_profit=buy_sig.take_profit,
                        trailing_stop_pct=buy_sig.trailing_stop_pct,
                        strategy_score=buy_sig.score,
                        min_risk_reward_ratio=strategy_rr_min,
                    )
                except Exception as exc:
                    buy_discarded += 1
                    reason = str(exc).split(".")[0].strip() or "rejeitado_por_risco"
                    reasons[reason] = reasons.get(reason, 0) + 1

            try:
                sell_sig = strategy.exit_signal(window, float(last["close"]))
            except Exception:
                sell_sig = None

            if sell_sig and sell_sig.signal == SignalType.SELL:
                sell_total += 1

        return {
            "total_buy_generated": buy_total,
            "total_sell_generated": sell_total,
            "buy_discarded": buy_discarded,
            "sell_discarded": sell_discarded,
            "discard_reasons": reasons,
        }

    def _operations_stats(self, result: BacktestResult) -> dict:
        trades = result.trades
        if not trades:
            return {
                "has_operations": False,
                "message": (
                    "Nenhuma operação foi executada. A estratégia não encontrou nenhuma "
                    "oportunidade compatível com as regras atuais."
                ),
            }

        rows = []
        for t in trades:
            entry_time = pd.to_datetime(t.get("entry_time"), utc=True, errors="coerce")
            exit_time = pd.to_datetime(t.get("exit_time"), utc=True, errors="coerce")
            duration_min = None
            if pd.notna(entry_time) and pd.notna(exit_time):
                duration_min = float((exit_time - entry_time).total_seconds() / 60.0)
            rows.append(
                {
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "pnl": float(t.get("pnl", 0.0)),
                    "duration_min": duration_min,
                }
            )

        df = pd.DataFrame(rows)
        wins = df[df["pnl"] > 0]["pnl"]
        losses = df[df["pnl"] <= 0]["pnl"]

        per_day = _group_count(df["entry_time"], "D")
        per_month = _group_count(df["entry_time"], "M")
        per_hour = _group_count(df["entry_time"].dt.hour)
        per_weekday = _group_count(df["entry_time"].dt.day_name())

        return {
            "has_operations": True,
            "avg_operation_minutes": _safe_float(df["duration_min"].mean()),
            "max_gain": _safe_float(df["pnl"].max()),
            "max_loss": _safe_float(df["pnl"].min()),
            "avg_profit": _safe_float(wins.mean()) if not wins.empty else 0.0,
            "avg_loss": _safe_float(losses.mean()) if not losses.empty else 0.0,
            "operations_per_day": per_day,
            "operations_per_month": per_month,
            "operations_per_hour": per_hour,
            "operations_per_weekday": per_weekday,
        }

    def _market_distribution(self, df: pd.DataFrame) -> dict:
        if df.empty or "ema_50" not in df.columns or "ema_200" not in df.columns:
            return {
                "uptrend_pct": 0.0,
                "downtrend_pct": 0.0,
                "sideways_pct": 0.0,
            }

        ema50 = pd.to_numeric(df["ema_50"], errors="coerce")
        ema200 = pd.to_numeric(df["ema_200"], errors="coerce")

        up = (ema50 > ema200).fillna(False)
        down = (ema50 < ema200).fillna(False)
        side = ~(up | down)
        total = max(len(df), 1)

        return {
            "uptrend_pct": round(float(up.sum()) * 100.0 / total, 2),
            "downtrend_pct": round(float(down.sum()) * 100.0 / total, 2),
            "sideways_pct": round(float(side.sum()) * 100.0 / total, 2),
        }

    def _interesting_candles(self, strategy: BaseStrategy, df: pd.DataFrame) -> list[dict]:
        if df.empty:
            return []

        conditions = self._condition_booleans(df)
        met_count = conditions.sum(axis=1)
        score_values = []
        for i in range(1, len(df) + 1):
            try:
                score_values.append(float(strategy.score(df.iloc[:i])) * 100.0)
            except Exception:
                score_values.append(0.0)

        score_series = pd.Series(score_values, index=df.index)

        ordered = df.copy()
        ordered["conditions_met"] = met_count
        ordered["score"] = score_series
        ordered = ordered.sort_values(["conditions_met", "score"], ascending=False).head(20)

        rows: list[dict] = []
        names = list(conditions.columns)
        for ts, row in ordered.iterrows():
            row_cond = conditions.loc[ts]
            met = [n for n in names if bool(row_cond[n])]
            missing = [n for n in names if not bool(row_cond[n])]
            rows.append(
                {
                    "date": str(ts),
                    "price": _safe_float(row.get("close")),
                    "score": _safe_float(row.get("score")),
                    "ema20": _safe_float(row.get("ema_20")),
                    "ema50": _safe_float(row.get("ema_50")),
                    "ema200": _safe_float(row.get("ema_200")),
                    "rsi": _safe_float(row.get("rsi")),
                    "atr": _safe_float(row.get("atr")),
                    "volume": _safe_float(row.get("volume")),
                    "conditions_met": met,
                    "conditions_missing": missing,
                }
            )
        return rows

    def _condition_booleans(self, df: pd.DataFrame) -> pd.DataFrame:
        ema20 = pd.to_numeric(df.get("ema_20"), errors="coerce")
        ema50 = pd.to_numeric(df.get("ema_50"), errors="coerce")
        ema200 = pd.to_numeric(df.get("ema_200"), errors="coerce")
        close = pd.to_numeric(df.get("close"), errors="coerce")
        rsi = pd.to_numeric(df.get("rsi"), errors="coerce")
        volume = pd.to_numeric(df.get("volume"), errors="coerce")
        atr = pd.to_numeric(df.get("atr"), errors="coerce")

        return pd.DataFrame(
            {
                "EMA20 > EMA50": (ema20 > ema50).fillna(False),
                "EMA50 > EMA200": (ema50 > ema200).fillna(False),
                "Preco > EMA20": (close > ema20).fillna(False),
                "RSI entre 55 e 70": ((rsi >= 55) & (rsi <= 70)).fillna(False),
                "Volume acima da media": (volume > volume.mean()).fillna(False),
                "ATR acima do minimo": (atr > atr.quantile(0.25)).fillna(False),
            },
            index=df.index,
        )

    def _summary(
        self,
        result: BacktestResult,
        condition_counts: dict,
        score_stats: dict,
        signal_stats: dict,
    ) -> dict:
        if not result.trades:
            reasons = sorted(
                signal_stats.get("discard_reasons", {}).items(),
                key=lambda x: x[1],
                reverse=True,
            )[:2]
            top_reasons = [name for name, _ in reasons] or ["Sem motivo dominante identificado"]
            return {
                "candles_analyzed": int(len(result.equity_curve)),
                "all_conditions_true": int(condition_counts.get("all_conditions_true", 0)),
                "max_score": score_stats.get("max", 0.0),
                "score_needed": 80,
                "main_reason_no_ops": top_reasons,
            }

        recommendation = (
            "Manter monitoramento e validar robustez em outros periodos."
            if result.metrics.profit_factor >= 1
            else "Revisar filtros e risco para melhorar consistencia."
        )
        return {
            "operational": True,
            "profit_factor": result.metrics.profit_factor,
            "win_rate": result.metrics.win_rate,
            "recommendation": recommendation,
        }

    def _render_text(self, report: dict) -> str:
        g = report["general"]
        i = report["indicators"]
        c = report["conditions"]
        s = report["score"]
        sig = report["signals"]
        o = report["operations"]
        m = report["market_distribution"]
        summary = report["final_summary"]

        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("DIAGNOSTICO COMPLETO DO BACKTEST")
        lines.append("=" * 60)
        lines.append("\n1. ESTATISTICAS GERAIS")
        lines.append(f"- Candles carregados: {g['total_candles_loaded']}")
        lines.append(f"- Candles validos (apos warm-up): {g['total_valid_after_warmup']}")
        lines.append(f"- Primeiro candle: {g['first_candle']}")
        lines.append(f"- Ultimo candle: {g['last_candle']}")
        lines.append(f"- Dias analisados: {g['analyzed_days']}")
        lines.append(f"- Timeframe: {g['timeframe']}")
        lines.append(f"- Simbolo: {g['symbol']}")
        lines.append(f"- Estrategia: {g['strategy']}")

        lines.append("\n2. INDICADORES")
        for name, st in i["stats"].items():
            if not st["present"]:
                lines.append(f"- {name}: nao disponivel")
                continue
            lines.append(
                f"- {name}: min={_fmt(st['min'])} max={_fmt(st['max'])} media={_fmt(st['mean'])} "
                f"NaN={st['nan_count']} invalidos={st['invalid_count']}"
            )
        lines.append(f"- Candles descartados: {i['candles_discarded']}")

        lines.append("\n3. ESTATISTICAS DAS CONDICOES")
        lines.append(f"- EMA20 > EMA50: {c['ema20_gt_ema50']} vezes")
        lines.append(f"- EMA50 > EMA200: {c['ema50_gt_ema200']} vezes")
        lines.append(f"- Preco > EMA20: {c['price_gt_ema20']} vezes")
        lines.append(f"- RSI entre 55 e 70: {c['rsi_between_55_70']} vezes")
        lines.append(f"- Volume acima da media: {c['volume_above_mean']} vezes")
        lines.append(f"- ATR acima do minimo: {c['atr_above_min']} vezes")
        lines.append(f"- Todas as condicoes verdadeiras: {c['all_conditions_true']} vezes")

        lines.append("\n4. SCORE")
        lines.append(f"- Min: {_fmt(s['min'])}")
        lines.append(f"- Max: {_fmt(s['max'])}")
        lines.append(f"- Media: {_fmt(s['mean'])}")
        lines.append(
            "- Distribuicao: "
            f"0-20={s['distribution_0_20']} "
            f"21-40={s['distribution_21_40']} "
            f"41-60={s['distribution_41_60']} "
            f"61-80={s['distribution_61_80']} "
            f"81-100={s['distribution_81_100']}"
        )
        lines.append("- Top 20 candles por score:")
        for row in s["top_20"]:
            lines.append(
                f"  {row['date']} price={_fmt(row['price'])} score={_fmt(row['score'])} "
                f"ema20={_fmt(row['ema20'])} ema50={_fmt(row['ema50'])} ema200={_fmt(row['ema200'])} "
                f"rsi={_fmt(row['rsi'])} atr={_fmt(row['atr'])} vol={_fmt(row['volume'])}"
            )

        lines.append("\n5. SINAIS")
        lines.append(f"- Total BUY gerados: {sig['total_buy_generated']}")
        lines.append(f"- Total SELL gerados: {sig['total_sell_generated']}")
        lines.append(f"- BUY descartados: {sig['buy_discarded']}")
        lines.append(f"- SELL descartados: {sig['sell_discarded']}")
        lines.append("- Motivos de descarte:")
        if sig["discard_reasons"]:
            for reason, qty in sorted(sig["discard_reasons"].items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {reason}: {qty}")
        else:
            lines.append("  Nenhum")

        lines.append("\n6. OPERACOES")
        if not o.get("has_operations"):
            lines.append(o["message"])
        else:
            lines.append(f"- Tempo medio da operacao (min): {_fmt(o['avg_operation_minutes'])}")
            lines.append(f"- Maior ganho: {_fmt(o['max_gain'])}")
            lines.append(f"- Maior perda: {_fmt(o['max_loss'])}")
            lines.append(f"- Lucro medio: {_fmt(o['avg_profit'])}")
            lines.append(f"- Perda media: {_fmt(o['avg_loss'])}")
            lines.append(f"- Operacoes por dia: {o['operations_per_day']}")
            lines.append(f"- Operacoes por mes: {o['operations_per_month']}")
            lines.append(f"- Operacoes por horario: {o['operations_per_hour']}")
            lines.append(f"- Operacoes por dia da semana: {o['operations_per_weekday']}")

        lines.append("\n7. DISTRIBUICAO DO MERCADO")
        lines.append(f"- Alta: {m['uptrend_pct']}%")
        lines.append(f"- Baixa: {m['downtrend_pct']}%")
        lines.append(f"- Lateral: {m['sideways_pct']}%")

        lines.append("\n8. CANDLES INTERESSANTES")
        for row in report["interesting_candles"]:
            lines.append(
                f"- {row['date']} price={_fmt(row['price'])} score={_fmt(row['score'])} ema20={_fmt(row['ema20'])} "
                f"ema50={_fmt(row['ema50'])} ema200={_fmt(row['ema200'])} rsi={_fmt(row['rsi'])} "
                f"atr={_fmt(row['atr'])} vol={_fmt(row['volume'])}"
            )
            lines.append(
                f"  condicoes atendidas: {', '.join(row['conditions_met']) if row['conditions_met'] else 'nenhuma'}"
            )
            lines.append(
                f"  condicoes faltantes: {', '.join(row['conditions_missing']) if row['conditions_missing'] else 'nenhuma'}"
            )

        lines.append("\n9. RESUMO FINAL")
        if summary.get("operational"):
            lines.append("Resumo: Estrategia operacional.")
            lines.append(f"Profit Factor: {_fmt(summary.get('profit_factor'))}")
            lines.append(f"Win Rate: {_fmt(summary.get('win_rate'))}")
            lines.append(f"Recomendacao: {summary.get('recommendation')}")
        else:
            lines.append("Resumo:")
            lines.append(f"Candles analisados: {summary.get('candles_analyzed')}")
            lines.append(f"Todas as condicoes ocorreram: {summary.get('all_conditions_true')} vezes")
            lines.append(f"Maior score: {summary.get('max_score')}")
            lines.append(f"Score necessario: {summary.get('score_needed')}")
            lines.append("Principal motivo para ausencia de operacoes:")
            for reason in summary.get("main_reason_no_ops", []):
                lines.append(f"- {reason}")

        lines.append("\n10. ARQUIVOS")
        lines.append("- diagnostic_report.txt")
        lines.append("- diagnostic_report.json")
        lines.append("=" * 60)
        return "\n".join(lines)


def _safe_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        val = float(v)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    except Exception:
        return None


def _fmt(v: object) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.4f}"
    except Exception:
        return str(v)


def _group_count(series: pd.Series, freq_or_none: str | None = None) -> dict[str, int]:
    s = series.dropna()
    if s.empty:
        return {}
    if freq_or_none:
        grouped = s.dt.to_period(freq_or_none).astype(str).value_counts().sort_index()
    else:
        grouped = s.value_counts().sort_index()
    return {str(k): int(v) for k, v in grouped.items()}
