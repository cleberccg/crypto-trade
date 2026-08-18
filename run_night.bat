@echo off
setlocal EnableExtensions EnableDelayedExpansion

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo ========================================
echo Night Runner - Crypto Project
echo ========================================

if not exist ".venv\Scripts\python.exe" (
  echo [ERRO] Ambiente virtual nao encontrado em .venv\Scripts\python.exe
  exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERRO] Falha ao ativar ambiente virtual.
  exit /b 1
)

echo [INFO] Verificando dependencias...
python -m pip show psutil >nul 2>nul
if errorlevel 1 (
  echo [INFO] Instalando dependencias ausentes...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias.
    exit /b 1
  )
)

echo [INFO] Rodando preflight do night runner...
python night_runner.py --dry-run
if errorlevel 1 (
  echo [ERRO] Preflight falhou. Execucao cancelada.
  exit /b 1
)

echo [INFO] Iniciando execucao noturna completa...
python night_runner.py
if errorlevel 1 (
  echo [FALHA] Night runner finalizado com erros. Verifique logs\night\errors.log e logs\night\night_runner.log
  exit /b 1
)

echo [SUCESSO] Execucao noturna concluida com sucesso.
echo [INFO] Relatorio: optimization\results\night_execution_report.txt
exit /b 0
