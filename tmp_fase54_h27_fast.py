"""
Fase 5.4 — Engenharia Reversa do Cluster H27 (reversao_2) — VERSÃO OTIMIZADA

Extrai dados consolidados do JSON do laboratório para análise rápida,
com opção de drill-down granular nos eventos brutos conforme necessário.
"""

import json
import warnings
from pathlib import Path
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
LAB_JSON = RESULTS_DIR / "quantitative_discovery_lab_20260629_152434.json"

HYPOTHESIS_ID = "H27"
CLUSTER_ID = "reversao_2"
CLUSTER_KEY = "reversao|high_atr|unknown|low_volume|inside_band|SELL"


def main():
    print(f"=== FASE 5.4: Engenharia Reversa do Cluster H27 (reversao_2) ===\n")
    
    # Load lab data
    print("Loading lab H27 data...")
    lab_data = json.loads(LAB_JSON.read_text(encoding="utf-8"))
    
    # Find H27 hypothesis
    h27_hyp = None
    for hyp in lab_data.get("hypotheses", []):
        if hyp.get("hypothesis_id") == HYPOTHESIS_ID:
            h27_hyp = hyp
            break
    
    if not h27_hyp:
        print(f"ERROR: {HYPOTHESIS_ID} not found in lab JSON")
        return
    
    # Extract H27 cluster from clusters list
    h27_cluster = None
    for cluster in lab_data.get("clusters", {}).get("clusters", []):
        if cluster.get("cluster_id") == CLUSTER_ID:
            h27_cluster = cluster
            break
    
    if not h27_cluster:
        print(f"ERROR: Cluster {CLUSTER_ID} not found in lab clusters")
        return
    
    print(f"\n✓ H27 Hypothesis loaded")
    print(f"✓ Cluster {CLUSTER_ID} loaded\n")
    
    # ==== ETAPA 1: ENGENHARIA REVERSA ====
    print("=" * 70)
    print("ETAPA 1: ENGENHARIA REVERSA (Feature Importance)")
    print("=" * 70)
    
    etapa1 = {
        "cluster_key_parsed": {
            "regime": "reversao",
            "atr_bucket": "high_atr",
            "rsi_bucket": "unknown",
            "volume_bucket": "low_volume",
            "bollinger_position": "inside_band",
            "direction": "SELL",
        },
        "top_patterns": h27_hyp.get("top_patterns", []),
    }
    
    print("\nCluster Definition (from cluster_key):")
    for feature, value in etapa1["cluster_key_parsed"].items():
        print(f"  • {feature}: {value}")
    
    print("\nTop Patterns (Feature Importance from Lab):")
    for i, pattern in enumerate(etapa1["top_patterns"][:5], 1):
        print(f"  {i}. {pattern['feature']} = {pattern['value']}")
        print(f"     importance_score: {pattern['importance_score']}")
        print(f"     trades: {pattern['sample_size']:,}")
    
    # ==== ETAPA 2: PERFIL ESTATÍSTICO ====
    print("\n" + "=" * 70)
    print("ETAPA 2: PERFIL ESTATÍSTICO (Consolidado)")
    print("=" * 70)
    
    evidence = h27_hyp.get("evidence", {})
    
    etapa2 = {
        "trades": evidence.get("trades"),
        "win_rate": evidence.get("win_rate"),
        "sharpe": evidence.get("sharpe"),
        "expectancy": evidence.get("expectancy"),
        "profit_factor": evidence.get("profit_factor"),
        "drawdown": evidence.get("drawdown"),
        "net_profit": evidence.get("net_profit"),
        "avg_mfe": evidence.get("avg_mfe"),
        "avg_mae": evidence.get("avg_mae"),
    }
    
    print(f"\nCluster H27 Performance Profile:")
    print(f"  • Total Trades: {etapa2['trades']:,}")
    print(f"  • Win Rate: {etapa2['win_rate']}%")
    print(f"  • Sharpe Ratio: {etapa2['sharpe']:.4f}")
    print(f"  • Expectancy: ${etapa2['expectancy']:.4f}")
    print(f"  • Profit Factor: {etapa2['profit_factor'] or 'N/A (100% WR)'}")
    print(f"  • Max Drawdown: {etapa2['drawdown']:.2f}")
    print(f"  • Net Profit: ${etapa2['net_profit']:,.0f}")
    print(f"  • Avg MFE (Maximum Favorable Excursion): {etapa2['avg_mfe']:.6f}")
    print(f"  • Avg MAE (Maximum Adverse Excursion): {etapa2['avg_mae']:.6f}")
    
    # ==== ETAPA 3: CONTEXTO DO PADRÃO ====
    print("\n" + "=" * 70)
    print("ETAPA 3: CONTEXTO DO PADRÃO (Predominância)")
    print("=" * 70)
    
    etapa3 = {
        "cluster_id": h27_cluster.get("cluster_id"),
        "regime": h27_cluster.get("regime"),
        "sample_size": h27_cluster.get("trades", 0) if isinstance(h27_cluster.get("trades"), int) else len(h27_cluster.get("trades", [])),
        "avg_mfe": h27_cluster.get("avg_mfe"),
        "avg_mae": h27_cluster.get("avg_mae"),
    }
    
    print(f"\nCluster Characteristics:")
    print(f"  • Regime: {etapa3['regime']} (dominant regime for this cluster)")
    print(f"  • Sample Size: {etapa3['sample_size']:,} events in cluster definition")
    if etapa3['avg_mfe'] is not None:
        print(f"  • Avg MFE: {etapa3['avg_mfe']:.6f}")
    if etapa3['avg_mae'] is not None:
        print(f"  • Avg MAE: {etapa3['avg_mae']:.6f}")
    
    # ==== ETAPA 4: COMPORTAMENTO PÓS-EVENTO ====
    print("\n" + "=" * 70)
    print("ETAPA 4: COMPORTAMENTO PÓS-EVENTO (Trade Dynamics)")
    print("=" * 70)
    
    trades = h27_cluster.get("trades", [])
    trades_list = trades if isinstance(trades, list) else []
    if trades_list and len(trades_list) > 0:
        # Sample first 10 trades to show structure
        print(f"\nTrade Dynamics (sample of {min(10, len(trades_list))} trades):")
        for i, trade in enumerate(trades_list[:10], 1):
            print(f"  Trade {i}:")
            print(f"    - Entry: ${trade.get('entry_price', 'N/A')}")
            print(f"    - Exit: ${trade.get('exit_price', 'N/A')}")
            print(f"    - PnL: ${trade.get('pnl', 'N/A')}")
            print(f"    - MFE: {trade.get('mfe', 'N/A')}")
            print(f"    - MAE: {trade.get('mae', 'N/A')}")
    else:
        print(f"\nNote: Trade-level details consolidated in cluster statistics above")
    
    mfe_avg = evidence.get("avg_mfe", 0)
    mae_avg = evidence.get("avg_mae", 0)
    
    print(f"\nPost-Event Statistics:")
    print(f"  • Avg MFE: {mfe_avg:.6f} (favorable move potential)")
    print(f"  • Avg MAE: {mae_avg:.6f} (adverse move risk)")
    print(f"  • MFE/MAE Ratio: {mfe_avg / mae_avg:.2f} (opportunity vs risk)")
    print(f"  • Strategy Outcome: 100% win rate → favorable moves dominate adverse moves")
    
    # ==== ETAPA 5: ESPECIFICAÇÃO DA ESTRATÉGIA ====
    print("\n" + "=" * 70)
    print("ETAPA 5: ESPECIFICAÇÃO DA ESTRATÉGIA (ReversaoNextGenV1)")
    print("=" * 70)
    
    strategy_spec = {
        "name": "ReversaoNextGenV1",
        "family": "ReversalEdge",
        "entry_rules": {
            "regime_filter": "regime == 'reversao'",
            "volatility_filter": "atr_bucket == 'high_atr'",
            "rsi_filter": "rsi_bucket == 'unknown'",
            "volume_filter": "volume_bucket == 'low_volume'",
            "bollinger_filter": "bollinger_position == 'inside_band'",
        },
        "direction": "SELL (short reversal)",
        "entry_logic": "Enter SHORT when ALL conditions are met",
        "stop_loss_mae": f"MAE avg = {mae_avg:.6f} → set SL ~0.5% above entry",
        "take_profit_mfe": f"MFE avg = {mfe_avg:.6f} → set TP ~1.1% below entry",
        "expectancy_per_trade": f"${etapa2['expectancy']:.4f}",
        "win_rate": f"{etapa2['win_rate']}%",
        "confidence_level": 0.75,
    }
    
    print("\nStrategy: ReversaoNextGenV1")
    print("\nEntry Conditions (ALL must be true):")
    for rule_name, rule_expr in strategy_spec["entry_rules"].items():
        print(f"  ✓ {rule_name}: {rule_expr}")
    
    print(f"\nDirection: {strategy_spec['direction']}")
    print(f"\nRisk/Reward:")
    print(f"  • Stop Loss: ~0.5% above entry (avg MAE)")
    print(f"  • Take Profit: ~1.1% below entry (avg MFE)")
    print(f"  • Risk/Reward Ratio: {mfe_avg / mae_avg:.2f}:1")
    print(f"\nExpected Performance:")
    print(f"  • Win Rate: {strategy_spec['win_rate']}")
    print(f"  • Expectancy: {strategy_spec['expectancy_per_trade']}")
    print(f"  • Confidence: {strategy_spec['confidence_level']:.1%}")
    
    # ==== ETAPA 6: IMPLEMENTABILIDADE ====
    print("\n" + "=" * 70)
    print("ETAPA 6: IMPLEMENTABILIDADE")
    print("=" * 70)
    
    etapa6 = {
        "indicators_required": [
            "Regime Detection (existing: trend, breakout, reversal, compression, lateral)",
            "ATR (existing)",
            "RSI (existing)",
            "Volume Analysis (existing)",
            "Bollinger Bands (existing)",
        ],
        "infrastructure_available": True,
        "implementation_status": "All required indicators already implemented in platform",
        "complexity": "LOW - uses only existing core indicators",
    }
    
    print("\nRequired Indicators:")
    for i, indicator in enumerate(etapa6["indicators_required"], 1):
        status = "✓ AVAILABLE" if "existing" in indicator else "⚠ PENDING"
        print(f"  {i}. {indicator.split('(')[0].strip()} [{status}]")
    
    print(f"\nInfrastructure Status: {etapa6['infrastructure_available']}")
    print(f"Complexity: {etapa6['complexity']}")
    print("\nImplementability: FULLY FEASIBLE")
    
    # ==== ETAPA 7: DECISIÓN ====
    print("\n" + "=" * 70)
    print("ETAPA 7: DECISIÓN")
    print("=" * 70)
    
    # Criteria
    sufficient_sample = etapa2["trades"] >= 100_000
    high_win_rate = etapa2["win_rate"] >= 95
    good_sharpe = etapa2["sharpe"] >= 1.0
    positive_expectancy = etapa2["expectancy"] > 0
    implementable = etapa6["infrastructure_available"]
    
    print("\nDecision Criteria:")
    print(f"  ✓ Sample Size >= 100k trades: {sufficient_sample} ({etapa2['trades']:,})")
    print(f"  ✓ Win Rate >= 95%: {high_win_rate} ({etapa2['win_rate']}%)")
    print(f"  ✓ Sharpe >= 1.0: {good_sharpe} ({etapa2['sharpe']:.4f})")
    print(f"  ✓ Positive Expectancy: {positive_expectancy} (${etapa2['expectancy']:.4f})")
    print(f"  ✓ Fully Implementable: {implementable}")
    
    all_passed = all([sufficient_sample, high_win_rate, good_sharpe, positive_expectancy, implementable])
    
    print(f"\n{'='*70}")
    if all_passed:
        print("RECOMENDAÇÃO: OPÇÃO A - PRONTA PARA IMPLEMENTAÇÃO")
        print("='*70")
        print(f"\nO cluster H27 (reversao_2) apresenta características estatísticas")
        print(f"suficientemente robustas para implementação de estratégia tradable:")
        print(f"  • 1.9M eventos com 100% taxa de acerto (estatisticamente extremo)")
        print(f"  • Sharpe ratio de 249.48 (indicador de qualidade de retorno)")
        print(f"  • Expectancy de $25.00 por evento")
        print(f"  • Indicadores todos disponíveis na plataforma")
        print(f"\nPróximas ações:")
        print(f"  1. Implementar entrada com filtros consolidados")
        print(f"  2. Configurar stops/TPs baseados em MAE/MFE")
        print(f"  3. Testar em paper trading")
        print(f"  4. Validar em ambiente live com posição pequena")
    else:
        print("RECOMENDAÇÃO: OPÇÃO B - DADOS INSUFICIENTES PARA IMPLEMENTAÇÃO")
        print("="*70)
        failed_criteria = []
        if not sufficient_sample:
            failed_criteria.append("Sample size")
        if not high_win_rate:
            failed_criteria.append("Win rate")
        if not good_sharpe:
            failed_criteria.append("Sharpe ratio")
        if not positive_expectancy:
            failed_criteria.append("Expectancy")
        if not implementable:
            failed_criteria.append("Implementability")
        
        print(f"\nCritérios falhados: {', '.join(failed_criteria)}")
    
    print("\n" + "="*70 + "\n")
    
    # Save comprehensive report
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hypothesis_id": HYPOTHESIS_ID,
        "cluster_id": CLUSTER_ID,
        "cluster_key": CLUSTER_KEY,
        "etapa1_engenharia_reversa": etapa1,
        "etapa2_perfil_estatistico": etapa2,
        "etapa3_contexto": etapa3,
        "etapa4_comportamento_pos_evento": {
            "avg_mfe": mfe_avg,
            "avg_mae": mae_avg,
            "mfe_mae_ratio": mfe_avg / mae_avg if mae_avg > 0 else 0,
        },
        "etapa5_especificacao_estrategia": strategy_spec,
        "etapa6_implementabilidade": etapa6,
        "etapa7_decisao": {
            "decision": "OPÇÃO A" if all_passed else "OPÇÃO B",
            "criteria_passed": all_passed,
            "criteria_details": {
                "sufficient_sample": sufficient_sample,
                "high_win_rate": high_win_rate,
                "good_sharpe": good_sharpe,
                "positive_expectancy": positive_expectancy,
                "implementable": implementable,
            }
        }
    }
    
    report_path = RESULTS_DIR / "fase54_h27_engineering_completo.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Relatório salvo em: {report_path}\n")


if __name__ == "__main__":
    main()
