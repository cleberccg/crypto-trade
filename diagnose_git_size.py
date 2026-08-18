#!/usr/bin/env python3
"""Diagnóstico de tamanho do repositório Git."""

import os
import subprocess
import json
from pathlib import Path

repo_path = r'd:\xampp\htdocs\crypto'
os.chdir(repo_path)

print("=" * 80)
print("DIAGNÓSTICO DE TAMANHO DO REPOSITÓRIO GIT")
print("=" * 80)

# 1. Verificar últimos commits
print("\n[1] ÚLTIMOS 20 COMMITS:")
print("-" * 80)
try:
    result = subprocess.run(['git', 'log', '--oneline', '-20'], capture_output=True, text=True)
    print(result.stdout if result.stdout else "Erro ao executar git log")
except Exception as e:
    print(f"Erro: {e}")

# 2. Status do repositório
print("\n[2] STATUS DO REPOSITÓRIO:")
print("-" * 80)
try:
    result = subprocess.run(['git', 'status', '-s'], capture_output=True, text=True)
    lines = result.stdout.split('\n') if result.stdout else []
    lines = [l for l in lines if l.strip()]
    print(f"Total de arquivos modificados/não rastreados: {len(lines)}")
    if lines:
        print("Exemplos dos primeiros 10:")
        for line in lines[:10]:
            print(f"  {line}")
except Exception as e:
    print(f"Erro: {e}")

# 3. Verificar tamanho dos objetos Git
print("\n[3] TAMANHO DOS OBJETOS GIT:")
print("-" * 80)
try:
    result = subprocess.run(['git', 'count-objects', '-vH'], capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    else:
        print("Nenhuma saída")
except Exception as e:
    print(f"Erro: {e}")

# 4. HTTP postBuffer
print("\n[4] CONFIGURAÇÃO HTTP.POSTBUFFER:")
print("-" * 80)
try:
    result = subprocess.run(['git', 'config', '--get', 'http.postBuffer'], capture_output=True, text=True)
    buffer_size = result.stdout.strip()
    if buffer_size:
        buffer_mb = int(buffer_size) / (1024 * 1024)
        print(f"Valor configurado: {buffer_size} bytes ({buffer_mb:.2f} MB)")
    else:
        print("Não configurado (padrão: 1 MB)")
except Exception as e:
    print(f"Erro: {e}")

# 5. Analisar arquivos grandes no working directory
print("\n[5] MAIORES ARQUIVOS NO WORKING DIRECTORY:")
print("-" * 80)
all_files = []
for root, dirs, files in os.walk(repo_path):
    # Ignorar .git
    if '.git' in dirs:
        dirs.remove('.git')
    
    for file in files:
        filepath = os.path.join(root, file)
        try:
            size = os.path.getsize(filepath)
            all_files.append((size, filepath))
        except:
            pass

all_files.sort(reverse=True)
total_size = sum(f[0] for f in all_files)

print(f"Total de arquivos: {len(all_files)}")
print(f"Tamanho total: {total_size / (1024**2):.2f} MB")
print(f"\nTop 20 maiores arquivos:")
for size, filepath in all_files[:20]:
    rel_path = os.path.relpath(filepath, repo_path)
    size_mb = size / (1024**2)
    print(f"  {size_mb:7.2f} MB  {rel_path}")

# 6. Contar arquivos em optimization/results
print("\n[6] ANÁLISE - DIRETÓRIO optimization/results/:")
print("-" * 80)
opt_results = Path(repo_path) / 'optimization' / 'results'
if opt_results.exists():
    opt_files = list(opt_results.glob('*'))
    opt_size = sum(f.stat().st_size for f in opt_files if f.is_file())
    print(f"Quantidade de arquivos: {len(opt_files)}")
    print(f"Tamanho total: {opt_size / (1024**2):.2f} MB")
    
    # Separar por tipo
    json_files = [f for f in opt_files if f.suffix == '.json']
    md_files = [f for f in opt_files if f.suffix == '.md']
    csv_files = [f for f in opt_files if f.suffix == '.csv']
    
    print(f"  - Arquivos JSON: {len(json_files)}")
    print(f"  - Arquivos MD: {len(md_files)}")
    print(f"  - Arquivos CSV: {len(csv_files)}")

print("\n" + "=" * 80)
print("FIM DO DIAGNÓSTICO")
print("=" * 80)
