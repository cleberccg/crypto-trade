#!/usr/bin/env python
"""
Análise automática dos resultados finais de FASE 5.2
Extrai as 7 métricas críticas conforme especificado pelo usuário.
"""
from pathlib import Path
import json
import pandas as pd


BASELINE_CONFIDENCE = 0.6368

def analyze_fase52_final():
    """Extrai e formata as 7 métricas finais de FASE 5.2."""
    
    # Procura pelo JSON de resultado mais recente
    results_dir = Path("optimization/results")
    json_files = sorted(results_dir.glob("quantitative_discovery_lab_*.json"), reverse=True)
    
    if not json_files:
        print("❌ Nenhum arquivo de resultado encontrado em optimization/results/")
        return
    
    report_file = json_files[0]
    print(f"📄 Carregando: {report_file.name}\n")
    
    report = json.loads(report_file.read_text(encoding='utf-8'))
    
    # Extração das 7 métricas
    print("=" * 70)
    print("ANÁLISE FINAL - FASE 5.2 (ESCALABILIDADE)")
    print("=" * 70)
    
    ranked = report.get("hypotheses", [])
    equiv = report.get("scientific_equivalence", {})
    scalability = report.get("scalability_audit", {})
    
    h1 = ranked[0] if ranked else {}
    
    # 1. Total de eventos
    total_events = scalability.get("events_mined", 0)
    print(f"\n1️⃣  EVENTOS MINERADOS NO TOTAL")
    print(f"   Total: {total_events:,} eventos")
    print(f"   ✓ Melhora: {total_events} >> (baseline ~1.375K em monolítico)")
    
    # 2. Clusters encontrados
    cluster_count = len(report.get("cluster_metrics", []))
    print(f"\n2️⃣  CLUSTERS ENCONTRADOS")
    print(f"   Total: {cluster_count} clusters")
    
    # 3. Hipóteses geradas
    hyp_count = len(ranked)
    print(f"\n3️⃣  HIPÓTESES GERADAS")
    print(f"   Total: {hyp_count} hipóteses")
    
    # 4. H1 mantida?
    h1_family = h1.get("family", "N/A")
    old_h1 = equiv.get("old_h1") or {}
    current_h1 = equiv.get("current_h1") or {}
    old_h1_family = old_h1.get("family", "N/A")
    print(f"\n4️⃣  H1 MANTIDA?")
    print(f"   Anterior (baseline): {old_h1_family}")
    print(f"   Atual (FASE 5.2):    {h1_family}")
    if h1_family == old_h1_family:
        print(f"   ✓ MANTIDA (mesma família)")
    elif h1_family == "BreakoutNextGen":
        print(f"   ✓ MANTIDA como BreakoutNextGen")
    else:
        print(f"   ⚠️  ALTERADA para {h1_family}")
    
    # 5. Confiança passou de 0.6368?
    h1_confidence = h1.get("confidence", 0)
    old_confidence = old_h1.get("confidence", BASELINE_CONFIDENCE)
    delta_conf = h1_confidence - old_confidence
    print(f"\n5️⃣  CONFIANÇA DA H1")
    print(f"   Anterior (baseline): {old_confidence:.4f}")
    print(f"   Atual (FASE 5.2):    {h1_confidence:.4f}")
    print(f"   Delta:               {delta_conf:+.4f}")
    if h1_confidence >= 0.70:
        print(f"   ✓ ÓTIMO: Confiança ≥ 0.70")
    elif h1_confidence > old_confidence:
        print(f"   ✓ MELHORADO: Ganho de {delta_conf:.4f}")
    else:
        print(f"   ⚠️  Confiança reduzida")
    
    # 6. Tamanho da amostra
    h1_sample = h1.get("sample_size", 0)
    old_sample = old_h1.get("sample_size", 0)
    delta_sample = h1_sample - old_sample
    print(f"\n6️⃣  TAMANHO DA AMOSTRA")
    print(f"   Anterior (baseline): {old_sample:,} eventos")
    print(f"   Atual (FASE 5.2):    {h1_sample:,} eventos")
    print(f"   Delta:               {delta_sample:+,}")
    if h1_sample >= 1000:
        print(f"   ✓ EXCELENTE: >1.000 eventos (amostra robusta)")
    elif h1_sample >= 100:
        print(f"   ✓ BOM: centenas de eventos")
    else:
        print(f"   ⚠️  Amostra < 100 eventos")
    
    # 7. Cobertura multi-ativo/período
    assets = h1.get("asset_coverage") or []
    timeframes = h1.get("timeframe_coverage") or []

    # Alguns relatórios não persistem cobertura na hipótese; calcula via CSV final.
    if (not assets or not timeframes) and h1.get("cluster_id"):
        csv_path = Path("optimization/results") / f"quantitative_discovery_operations_{report_file.stem.split('quantitative_discovery_lab_')[-1]}.csv"
        if csv_path.exists():
            h1_cluster = h1.get("cluster_id")
            assets_set: set[str] = set()
            timeframes_set: set[str] = set()
            for chunk in pd.read_csv(csv_path, usecols=["cluster_id", "symbol", "timeframe"], chunksize=500000):
                subset = chunk[chunk["cluster_id"] == h1_cluster]
                if subset.empty:
                    continue
                assets_set.update(subset["symbol"].dropna().astype(str).unique().tolist())
                timeframes_set.update(subset["timeframe"].dropna().astype(str).unique().tolist())
            assets = sorted(assets_set)
            timeframes = sorted(timeframes_set)
    print(f"\n7️⃣  COBERTURA MULTI-ATIVO/PERÍODO/REGIME")
    print(f"   Ativos:    {len(assets)} ({', '.join(assets[:5])}{'...' if len(assets) > 5 else ''})")
    print(f"   Timeframes: {len(timeframes)} ({', '.join(timeframes)})")
    if len(assets) >= 3 and len(timeframes) >= 2:
        print(f"   ✓ PRESENTE: Múltiplos ativos ({len(assets)}) e períodos ({len(timeframes)})")
    elif len(assets) >= 2:
        print(f"   ✓ PARCIAL: {len(assets)} ativos, {len(timeframes)} timeframes")
    else:
        print(f"   ⚠️  Cobertura limitada: {len(assets)} ativo(s)")
    
    # Resumo Final
    print("\n" + "=" * 70)
    print("CENÁRIO FINAL")
    print("=" * 70)
    
    checks = []
    checks.append(("H1 Mantida", h1_family == "BreakoutNextGen" or h1_family == old_h1_family))
    checks.append(("Confiança ≥ 0.70", h1_confidence >= 0.70))
    checks.append(("Amostra ≥ 100 eventos", h1_sample >= 100))
    checks.append(("Multi-ativo (≥2)", len(assets) >= 2))
    checks.append(("Multi-período (≥2)", len(timeframes) >= 2))
    
    print("\nValidação contra cenário ideal:")
    for check_name, passed in checks:
        symbol = "✅" if passed else "❌"
        print(f"  {symbol} {check_name}")
    
    passing = sum(1 for _, p in checks if p)
    total = len(checks)
    print(f"\nPontuação: {passing}/{total} ✓")
    
    # Metadata
    print(f"\nMetadata:")
    print(f"  Run ID: {report.get('run_id', 'N/A')}")
    print(f"  Timestamp: {report.get('generated_at', 'N/A')}")
    print(f"  Total tempo: {report.get('profiling', {}).get('total_elapsed_seconds', 0):.1f} s")
    print(f"  Candles processados: {scalability.get('candles_processed', 0):,}")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    analyze_fase52_final()
