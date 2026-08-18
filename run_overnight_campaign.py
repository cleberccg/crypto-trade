#!/usr/bin/env python
"""
Script para executar a Campanha Noturna com prioridade dinâmica.

Esta campanha implementa as melhorias solicitadas em tarefa.txt:
1. Verifica IMPLEMENTATION_PENDING primeiro (19 estratégias)
2. Processa-as completamente antes de nova pesquisa
3. Para automaticamente às 09:00
4. Busca 3 PAPER_CANDIDATE em vez de apenas 1
5. Completa o pipeline de qualquer estratégia já iniciada antes de parar
"""
import sys
import subprocess
from datetime import datetime

def run_overnight_campaign():
    """Execute overnight campaign with intelligent prioritization."""
    
    print("\n" + "="*70)
    print("CAMPANHA NOTURNA - DESCOBERTA INTELIGENTE DE ESTRATÉGIAS")
    print("="*70)
    print(f"Hora de início: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nConfigurações:")
    print("  - Prioridade 1: Processar 19 estratégias em IMPLEMENTATION_PENDING")
    print("  - Prioridade 2: Quando zerar pendências, ativar Fase 14 (pesquisa)")
    print("  - Alvo: Encontrar 3 estratégias PAPER_CANDIDATE")
    print("  - Parada automática: 09:00 (horário local)")
    print("  - Resiliência: Se estratégia começar, terminar todo o pipeline antes de parar")
    print("="*70 + "\n")
    
    cmd = [
        sys.executable,
        "main.py",
        "overnight-campaign",
        "--symbol", "BTC/USDT",
        "--timeframe", "5m",
        "--batch-size", "50",  # Allow many strategies
        "--target-paper-candidates", "3",  # Find 3 candidates instead of 1
        "--campaign-end-hour", "9",  # Stop at 09:00
        "--campaign-max-seconds", "32400",  # 9 hours
        "--window-days", "120",
        "--capital", "10000.0",
        "--optimizer-max-combinations", "15",
        "--optimizer-workers", "1",
        "--probe-max-combinations", "8",
        "--probe-top-n", "3",
        "--paper-candidate-min-trades", "100",
        "--paper-candidate-min-profit-factor", "1.10",
        "--paper-experiment-review-window-days", "14",
        "--output-prefix", f"overnight_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    ]
    
    print(f"Executando: {' '.join(cmd)}\n")
    
    result = subprocess.run(cmd, cwd=".")
    
    if result.returncode == 0:
        print("\n" + "="*70)
        print("CAMPANHA CONCLUÍDA COM SUCESSO")
        print("="*70)
    else:
        print("\n" + "="*70)
        print(f"ERRO: Campanha terminou com código {result.returncode}")
        print("="*70)
        sys.exit(result.returncode)

if __name__ == "__main__":
    run_overnight_campaign()
