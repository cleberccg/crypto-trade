# RUNBOOK OFICIAL - RECUPERACAO POS-REBOOT (WINDOWS)

Versao: 1.0  
Data: 2026-08-06  
Escopo: restauracao completa do ambiente Scientific Paper Live apos reboot do Windows, usando somente componentes ja existentes no projeto.

## Regras de Execucao

1. Execute tudo no diretorio do projeto: `d:\xampp\htdocs\crypto`.
2. Use PowerShell com permissao para iniciar processos Python.
3. Nao pule etapas.
4. Nao avance para a proxima etapa sem validar a etapa atual.
5. Se qualquer gate falhar, siga a secao de troubleshooting antes de continuar.

---

## 1) Pre-requisitos (gate obrigatorio)

### 1.1 Ambiente base

Diretorio:

```powershell
cd d:\xampp\htdocs\crypto
```

Comandos de validacao:

```powershell
python --version
pip check
```

Resultado esperado:

- Python responde sem erro.
- `pip check` sem dependencias quebradas.

Tempo esperado: 10 a 30 segundos.

---

### 1.2 MySQL ativo

Comandos:

```powershell
Get-Service | Where-Object { $_.Name -match 'mysql|mariadb' } | Format-Table -Auto Name,Status,DisplayName
```

```powershell
python -c "from sqlalchemy import text; from database.connection import get_session; s=get_session();
with s as session: print(session.execute(text('SELECT 1')).scalar())"
```

Resultado esperado:

- Servico MySQL/MariaDB com `Status = Running`.
- Query retorna `1`.

Tempo esperado: 10 a 40 segundos.

---

### 1.3 Exchange acessivel

Comando:

```powershell
python -c "import ccxt; ex=ccxt.binance({'enableRateLimit': True}); print(ex.fetch_time())"
```

Resultado esperado:

- Retorno numerico (timestamp) sem excecao.

Tempo esperado: 5 a 30 segundos.

---

### 1.4 Diretorios obrigatorios

Comando:

```powershell
Get-ChildItem -Directory .\optimization, .\optimization\results, .\logs | Select-Object FullName
```

Resultado esperado:

- Diretorios existentes: `optimization`, `optimization\results`, `logs`.

Tempo esperado: 5 a 15 segundos.

---

### 1.5 Arquivos de estado e campanha

Comandos:

```powershell
Get-ChildItem .\optimization\results\paper_live_state__*.json | Select-Object Name,LastWriteTime | Sort-Object LastWriteTime -Descending | Select-Object -First 20
```

```powershell
Get-ChildItem .\optimization\results\paper_specialized_campaign_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name,LastWriteTime
```

Resultado esperado:

- Existem arquivos `paper_live_state__*.json`.
- Existe pelo menos um `paper_specialized_campaign_*.json` recente.

Tempo esperado: 5 a 20 segundos.

---

### 1.6 Configuracao carregavel

Comando:

```powershell
python -c "from config.settings import settings; print(settings.startup_summary())"
```

Resultado esperado:

- Resumo de startup impresso sem excecao.

Tempo esperado: 5 a 20 segundos.

---

### 1.7 Gate final dos pre-requisitos

So continue se TODOS abaixo estiverem OK:

- MySQL running
- Banco acessivel (`SELECT 1`)
- Exchange acessivel
- Diretorios obrigatorios existentes
- Arquivos de estado encontrados
- Configuracao carregada sem erro

Se qualquer item falhar: pare e execute a secao 6 (Troubleshooting).

---

## 2) Ordem oficial de inicializacao

### Variaveis operacionais (definir antes)

Diretorio: `d:\xampp\htdocs\crypto`

1. Descobrir `campaign_id` mais recente (se necessario):

```powershell
$latest = Get-ChildItem .\optimization\results\paper_specialized_campaign_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$campaign = (Get-Content $latest.FullName -Raw | ConvertFrom-Json).scope.campaign_id
$campaign
```

2. Definir variaveis de execucao:

```powershell
$STRATEGY_NAME='ClassicDonchianBreakout'
$STRATEGY_VERSION='v1.0'
$CAMPAIGN_ID=$campaign   # ou valor fixo, ex: 'spc-official-cdb-v1'
```

---

### Etapa 1 - Iniciar market-data-daemon

Comando exato:

```powershell
python .\main.py market-data-daemon --symbols "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT" --timeframes "5m,15m,1h" --polling-interval-seconds 30 --context-delay-seconds 0.2 --batch-size 1000 --retry-count 5 --retry-delay-seconds 2 --bootstrap-days 7 --recent-gap-bars 2000 --report-every-cycles 1 --max-cycles 0 --output-prefix market_data_daemon
```

Parametros:

- Simbolos e timeframes oficiais da campanha.
- `--max-cycles 0` para modo continuo.

Tempo esperado para inicializar: 30 a 180 segundos.

Resultado esperado:

- Processo ativo no terminal.
- Saida com bloco `MARKET DATA DAEMON` e `Summary`.
- Criacao de artefatos em `optimization/results` com prefixo `market_data_daemon`.

Validacao obrigatoria antes de continuar:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main.py market-data-daemon' } | Select-Object ProcessId,CommandLine
```

```powershell
Get-ChildItem .\optimization\results\market_data_daemon*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1 Name,LastWriteTime
```

Gate de passagem:

- Processo encontrado.
- Arquivo de saida recente encontrado.

---

### Etapa 2 - Iniciar paper-live-supervisor

Comando exato:

```powershell
python .\main.py paper-live-supervisor --strategy-name $STRATEGY_NAME --strategy-version $STRATEGY_VERSION --campaign-id $CAMPAIGN_ID --poll-seconds 15 --bootstrap-bars 1500 --bootstrap-replay-bars 350 --min-trades-before-change 0 --supervisor-poll-seconds 10 --stuck-timeout-seconds 600 --startup-grace-seconds 120 --restart-delay-seconds 2 --max-consecutive-restarts 5 --max-supervision-cycles 0 --output-prefix paper_live
```

Parametros:

- `--campaign-id` obrigatorio.
- Contextos serao carregados do relatorio mais recente (comportamento padrao, sem `--no-contexts-from-latest-report`).

Tempo esperado para inicializar: 20 a 120 segundos.

Resultado esperado:

- Supervisor entra em loop continuo.
- Sao criados: `paper_live_supervisor_*.json` e `paper_live_supervisor_audit_*.jsonl`.
- Workers sao iniciados automaticamente, 1 por contexto aprovado.

Validacao obrigatoria antes de continuar:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main.py paper-live-supervisor' } | Select-Object ProcessId,CommandLine
```

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10 --show-contexts
```

```powershell
Get-ChildItem .\optimization\results\paper_live_supervisor_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1 Name,LastWriteTime
```

Gate de passagem:

- Processo do supervisor ativo.
- `check_paper_live_status.py` executa sem erro.
- Existe arquivo de supervisor recente.

---

### Etapa 3 - Validar workers e runtime paper live

Observacao operacional:

- Nao existe comando separado para worker.
- Cada worker e um processo Paper Live iniciado pelo supervisor (entrypoint interno do projeto).

Validacoes obrigatorias:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10 --show-contexts
```

```powershell
Get-ChildItem .\optimization\results\paper_live_state__*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 20 Name,LastWriteTime
```

```powershell
Start-Sleep -Seconds 120
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10
```

Gate de passagem:

- `contexts_total` esperado da campanha (alvo operacional: 12).
- `contexts_active` maior que 0 no aquecimento e evoluindo ate 12.
- `paper_live_state__*.json` com `LastWriteTime` avancando.
- `cycles` crescendo entre duas execucoes.

Tempo esperado para estabilizar apos reboot: 5 a 20 minutos.

---

### Etapa 4 - Validar operational hypothesis runtime

Objetivo:

- Confirmar que o runtime de hipotese operacional foi carregado no estado persistido dos contexts.

Comando de validacao:

```powershell
python -c "import json,glob; files=glob.glob('optimization/results/paper_live_state__*.json');
count=0
for f in files:
 p=json.load(open(f,encoding='utf-8'))
 if isinstance(p.get('hypothesis_config'),dict): count+=1
print({'states':len(files),'with_hypothesis_config':count})"
```

Resultado esperado:

- `with_hypothesis_config` maior que 0 para campanha com hipotese ativa.
- Nenhum erro de parse dos state files.

Gate de passagem:

- Nao ha indicio de perda de hipotese apos reboot/restart.

---

### Etapa 5 - Ativar monitoramento operacional

#### 5.1 Monitor de status paper live

Comando:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10 --show-contexts
```

Tempo: 5 a 30 segundos por execucao.

#### 5.2 Soak monitor horario (read-only)

Comando:

```powershell
python .\soak_monitor.py --campaign-id $CAMPAIGN_ID --results-dir optimization/results --log-path logs/application.log --window-hours 1 --max-context-lag-min 10 --max-restarts 5 --max-error-lines 20 --max-disk-pct 95 --max-ram-pct 95 --max-cpu-pct 98 --output-prefix soak_hourly_report
```

Tempo: 5 a 30 segundos por execucao.

#### 5.3 Cobertura da campanha (se aplicavel)

Comando:

```powershell
python .\main.py paper-campaign-coverage --campaign-id $CAMPAIGN_ID --strategy-name $STRATEGY_NAME --strategy-version $STRATEGY_VERSION --stale-minutes 180 --min-coverage-percent 90 --critical-coverage-percent 75 --output-prefix paper_specialized_campaign_coverage
```

Tempo: 10 a 60 segundos.

#### 5.4 Monitor de trades/log em tempo real (opcional recomendado)

Comando:

```powershell
python .\monitor_live_trades.py --poll-seconds 15 --watch-db --log-path .\logs\application.log
```

Tempo: continuo.

#### 5.5 Dashboard (se aplicavel)

No estado atual da arquitetura nao ha servico web dedicado de dashboard para recovery do paper live.
Use os artefatos oficiais gerados em `optimization/results` e `scientific_dashboard.csv` para visualizacao operacional.

Gate de passagem da etapa 5:

- Comandos de monitoramento executam sem erro.
- Arquivos `soak_hourly_report_*.json/.md` sao gerados.
- Cobertura da campanha retorna summary valido.

---

## 3) Validacao obrigatoria apos cada etapa (regra global)

Nao avance sem cumprir TODOS os checks de validacao da etapa anterior.

Checklist minimo por etapa:

- Processo iniciou
- Comando retornou sem excecao
- Artefato esperado foi gerado
- Estado atualizou no horario esperado
- Nao ha erro critico recorrente no log

Comandos auxiliares de validacao rapida:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | Select-Object ProcessId,Name,CommandLine
```

```powershell
Get-ChildItem .\logs\application.log | Select-Object Name,Length,LastWriteTime
```

```powershell
Get-Content .\logs\application.log -Tail 100
```

---

## 4) Inicializacao completa (visao consolidada)

Sequencia oficial resumida:

1. Confirmar pre-requisitos (secao 1)
2. Iniciar `market-data-daemon`
3. Iniciar `paper-live-supervisor`
4. Permitir spawn dos workers (paper live por contexto)
5. Validar runtime de hipotese operacional nos state files
6. Ativar monitoramento (`check_paper_live_status.py`, `soak_monitor.py`, `paper-campaign-coverage`)
7. Acompanhar ate convergir para estado saudavel

Observacao importante:

- O `paper live` dos contexts e iniciado pelo supervisor automaticamente; nao iniciar instancia manual paralela para os mesmos contexts da campanha.

---

## 5) Validacao final de ambiente recuperado

Execute em ordem:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10 --show-contexts
```

```powershell
python .\soak_monitor.py --campaign-id $CAMPAIGN_ID --window-hours 1 --max-context-lag-min 10 --max-restarts 5 --max-error-lines 20
```

```powershell
python .\main.py paper-campaign-coverage --campaign-id $CAMPAIGN_ID --strategy-name $STRATEGY_NAME --strategy-version $STRATEGY_VERSION
```

### Criterios obrigatorios para declarar RECUPERADO

- `contexts_active = 12`
- `contexts_stale = 0`
- `lag` dentro do limite operacional (<= 10 min na validacao final)
- workers vivos (atividade de ciclo e estado atualizando)
- supervisor saudavel (`supervisor_status` sem falha permanente)
- `paper_live_state` atualizando continuamente
- candles atualizando (daemon ativo e estados progredindo)
- `soak_monitor` em `APROVADO` ou `APROVADO_COM_RESTRICOES`

Se qualquer criterio falhar, ambiente NAO esta recuperado.

---

## 6) Troubleshooting

### 6.1 MySQL indisponivel

Sintomas:

- `SELECT 1` falha
- erros `OperationalError` de banco

Causa provavel:

- servico MySQL parado
- credencial/host incorreto

Diagnostico:

```powershell
Get-Service | Where-Object { $_.Name -match 'mysql|mariadb' } | Format-Table -Auto Name,Status
python -c "from sqlalchemy import text; from database.connection import get_session; s=get_session();
with s as session: print(session.execute(text('SELECT 1')).scalar())"
```

Acao corretiva:

- iniciar servico MySQL
- validar `.env`/config de DB
- repetir teste de conectividade antes de reiniciar supervisor

---

### 6.2 Exchange indisponivel

Sintomas:

- `fetch_time` falha
- erros de rede/exchange no log

Causa provavel:

- sem internet
- bloqueio DNS/firewall
- indisponibilidade temporaria da exchange

Diagnostico:

```powershell
python -c "import ccxt; ex=ccxt.binance({'enableRateLimit': True}); print(ex.fetch_time())"
Get-Content .\logs\application.log -Tail 200
```

Acao corretiva:

- restaurar conectividade de rede
- aguardar normalizacao da exchange
- manter supervisor parado ate recuperar feed estavel

---

### 6.3 Workers nao iniciam

Sintomas:

- `contexts_active` nao sobe
- sem progresso de `cycles`
- state files nao atualizam

Causa provavel:

- supervisor sem contexts validos
- falha na leitura de relatorio de campanha
- erro de inicializacao em worker

Diagnostico:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --show-contexts
Get-ChildItem .\optimization\results\paper_live_supervisor_audit_*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1 Name,LastWriteTime
Get-Content (Get-ChildItem .\optimization\results\paper_live_supervisor_audit_*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName -Tail 100
```

Acao corretiva:

- confirmar campaign_id correto
- reiniciar supervisor
- se necessario, informar `--contexts` explicitamente no comando do supervisor

---

### 6.4 Supervisor parado

Sintomas:

- processo `paper-live-supervisor` ausente
- nenhum novo evento de auditoria

Causa provavel:

- terminal encerrado
- excecao fatal

Diagnostico:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main.py paper-live-supervisor' } | Select-Object ProcessId,CommandLine
Get-Content .\logs\application.log -Tail 200
```

Acao corretiva:

- reiniciar comando da Etapa 2
- manter em terminal dedicado

---

### 6.5 Contextos stale

Sintomas:

- `contexts_stale > 0`
- `lag_max` acima do limite

Causa provavel:

- feed de candles parado
- worker travado
- restart storm

Diagnostico:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --max-stale-min 10 --show-contexts
python .\soak_monitor.py --campaign-id $CAMPAIGN_ID --window-hours 1
```

Acao corretiva:

- validar daemon de mercado
- reiniciar supervisor
- investigar contextos com maior lag no status detalhado

---

### 6.6 Candles nao atualizam

Sintomas:

- `cycles` nao cresce
- `last_open_time` parado

Causa provavel:

- market-data-daemon parado
- falha de ingestao

Diagnostico:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main.py market-data-daemon' } | Select-Object ProcessId,CommandLine
Get-ChildItem .\optimization\results\market_data_daemon*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name,LastWriteTime
```

Acao corretiva:

- reiniciar market-data-daemon
- aguardar aquecimento e revalidar `check_paper_live_status.py`

---

### 6.7 Paper Live parado

Sintomas:

- estados sem update
- supervisor com status interrompido/falha

Causa provavel:

- workers encerrados
- erro recorrente em runtime

Diagnostico:

```powershell
python .\check_paper_live_status.py --campaign-id $CAMPAIGN_ID --show-contexts
Get-Content .\logs\application.log -Tail 300
```

Acao corretiva:

- reiniciar supervisor
- confirmar DB e exchange antes do restart

---

### 6.8 Hipotese operacional nao carregada

Sintomas:

- state sem `hypothesis_config` quando esperado
- divergencia de comportamento apos restart

Causa provavel:

- campanha/contexto sem payload de hipotese persistido
- estado antigo inconsistente

Diagnostico:

```powershell
python -c "import json,glob; files=glob.glob('optimization/results/paper_live_state__*.json');
for f in files[:20]:
 p=json.load(open(f,encoding='utf-8'))
 print(f, isinstance(p.get('hypothesis_config'), dict))"
```

Acao corretiva:

- confirmar artefato de campanha com hipotese aprovada
- reiniciar supervisor com campaign_id correto
- validar novamente os state files apos restart

---

## 7) Checklist final pos-reboot (usar sempre)

Marque cada item como concluido:

- [ ] Estou no diretorio `d:\xampp\htdocs\crypto`
- [ ] Python e dependencias OK (`python --version`, `pip check`)
- [ ] MySQL running
- [ ] Banco acessivel (`SELECT 1`)
- [ ] Exchange acessivel (`ccxt.binance.fetch_time`)
- [ ] Diretorios `optimization/results` e `logs` existentes
- [ ] Arquivos `paper_live_state__*.json` encontrados
- [ ] `market-data-daemon` iniciado e ativo
- [ ] `paper-live-supervisor` iniciado e ativo
- [ ] Workers spawnados e com progresso de ciclos
- [ ] `paper_live_state` atualizando
- [ ] Validacao de hipotese operacional executada
- [ ] `check_paper_live_status.py` executado sem erro
- [ ] `soak_monitor.py` executado sem erro
- [ ] `paper-campaign-coverage` executado (quando aplicavel)
- [ ] `contexts_active = 12`
- [ ] `contexts_stale = 0`
- [ ] `lag` dentro do limite
- [ ] Supervisor saudavel
- [ ] Candles atualizando
- [ ] `soak_monitor` = `APROVADO` ou `APROVADO_COM_RESTRICOES`

Se qualquer item ficar pendente, o ambiente nao deve ser considerado recuperado.
