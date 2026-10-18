# Dashboard LIVE local - monitoramento e inicializacao controlada

Frontend aproveitado: `frontend/`, React 18 + Vite + TypeScript + Material UI + React Query.
Backend aproveitado: FastAPI `webapi.app:app`, autenticação JWT e credenciais de acesso existentes.
Não iniciar outra arquitetura ou os mocks para monitorar a conta LIVE.

## Abrir e iniciar

Abra **http://127.0.0.1:5173/**. Use o usuário/senha da API já configurados.
Use somente `admin`, com a senha configurada em `API_PASSWORD` (padrão local: `admin`).
Isso é somente para localhost; não exponha essas credenciais padrão na rede.

```powershell
Set-Location 'D:\xampp\htdocs\crypto'
.\scripts\start_live_dashboard.ps1
```

O script abre duas janelas PowerShell externas e verifica portas antes de iniciar:
- Backend: `127.0.0.1:8000`, com `API_DASHBOARD_READONLY=1`, sem bootstrap/migração.
- Frontend: `127.0.0.1:5173`, com `VITE_LIVE_MONITOR_ONLY=1`; proxy local `/api` para a API.

Para iniciar apenas uma parte, use `-Component Backend` ou `-Component Frontend`.
Para parar apenas o dashboard, pressione **Ctrl+C nas janelas “Crypto Dashboard - Backend” e “Crypto Dashboard - Frontend”**.
Não use as janelas do worker LIVE nem do market-data daemon. Não é necessário reiniciar o bot.
O script não para nenhum serviço existente. Porta ocupada por outro serviço bloqueia o lançamento.

Comandos equivalentes (cada um em sua janela externa):

```powershell
# Backend, na raiz
$env:API_DASHBOARD_READONLY='1'
C:\Python313\python.exe -B -m uvicorn webapi.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

```powershell
# Frontend
Set-Location 'D:\xampp\htdocs\crypto\frontend'
$env:VITE_LIVE_MONITOR_ONLY='1'
npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

## Fontes e escopo

- MySQL `trades` e `orders`, com `is_paper=0`, consultas parametrizadas SELECT e transação READ ONLY.
- Estado existente `optimization/results/live_positions.json`, apenas lido.
- Processos Python e atividade recente em `logs/application.log`; o shell pai não conta como worker/daemon.
- Binance Spot MAINNET pelo cliente isolado `BinanceSpotReadOnlyAuditClient`, apenas GET account/openOrders, sem redirecionamentos. O cliente de negociação não é utilizado. Cache de 30 segundos.
- Projeção padrão: Sma200RegimeGated v1, BNB/USDT, 4h. Histórico permite outros filtros LIVE.
- API atualiza a interface a cada 15 segundos. Cada fonte tem horário próprio. Saldo indisponível não reutiliza valor antigo como atual.
- MySQL ingere horários UTC; interface exibe Brasília. Logs operacionais são lidos como horário de Brasília.
- Custo OPEN = quantidade restante × preço de entrada. DUST é separado e não ocupa posição OPEN.
- Capital acompanhado = saldo USDT da conta + custo OPEN do contexto. Não é patrimônio marcado a mercado e não atribui ownership de depósitos USDT. BNB não rastreado e ETH legado não são incorporados ao capital do contexto.
- PnL realizado vem de `trades.pnl`, somado uma vez por trade OPEN/DUST/CLOSED; não se somam fills novamente.
- Histórico limitado aos 200 registros mais recentes por tabela/filtro. Trades usam entrada/saída; ordens usam timestamp de execução. Limpar filtros de estratégia/ativo permite consultar operações LIVE antigas.
- A tabela de ordens representa linhas/fills persistidos. Um exchange_order_id pode aparecer em vários fills.
- Candle disponível no banco não significa candle processado pelo worker. Última avaliação persistida de posição e último sinal registrado são apresentados separadamente.

## Limites explícitos

PnL não realizado e indicadores sem fonte LIVE inequívoca aparecem **Indisponível**. Não são recalculados no dashboard.
O schema atual de sinais/indicadores não separa confiavelmente PAPER/LIVE; o painel não mistura esse histórico com os sinais do worker LIVE.
Conexão Binance indica a última consulta autenticada, não uma conexão contínua.
As credenciais existentes podem ter permissão de escrita: proteção GET/SELECT é de aplicação/transação, não de credencial.

## Segurança

Modo monitor publica saude sem DSN, login JWT, consultas financeiras somente leitura e inicio de processos restrito ao admin.
Nao publica mocks, configuracao, ordens manuais, cancelamento, bootstrap ou migracao. Os controles iniciam apenas os dois comandos fixos aprovados.
API e Vite vinculados apenas a loopback, com autenticação preservada, CORS local e validação de Host.
Chaves, secrets, DATABASE_URL, conteúdo do .env e URLs assinadas não são enviados ao navegador.
Erros de fontes têm mensagens genéricas; logs brutos não são publicados.

## Validação

- Build: `$env:VITE_LIVE_MONITOR_ONLY='1'; npm.cmd --prefix frontend run build`.
- Testes `tests/test_live_dashboard.py` cobrem autenticação, ausência de escrita, SQL LIVE, separação DUST/legado, cache, falhas, horários e processos.
- Regressão inclui webapi, auditoria, CLI LIVE, ciclo LIVE, serviço, ranking e executor.
- Validação visual: `C:\Python313\python.exe -B scripts/verify_live_dashboard.py` usa Chrome headless com perfil temporário isolado; não acessa perfis pessoais. Faz login pela UI, verifica atualização automática, desktop e mobile e salva capturas em `optimization/results/live_dashboard_*.png`.
- O navegador integrado não iniciou nesta sessão por falha de permissões do sandbox Windows; foi utilizado Chrome local isolado para a inspeção visual.

## Arquivos desta entrega

Backend: `webapi/app.py`, `webapi/live_monitor.py`, `webapi/live_routes.py`, `webapi/routers.py`.
Frontend: `frontend/vite.config.ts`, `frontend/src/App.tsx`, `frontend/src/api/client.ts`, `frontend/src/api/live.ts`,
`frontend/src/components/MetricGrid.tsx`, `frontend/src/components/SimpleTable.tsx`, `frontend/src/layout/AppLayout.tsx`,
`frontend/src/pages/DashboardPage.tsx`, `frontend/src/pages/LiveHistoryPage.tsx`, `frontend/src/pages/LoginPage.tsx`, `frontend/src/styles.css`.
Validação/operação: `tests/test_live_dashboard.py`, `scripts/start_live_dashboard.ps1`, `scripts/verify_live_dashboard.py`, este documento.

A duplicação preexistente das rotas de observabilidade HTTP/WebSocket foi removida, preservando o contrato completo e corrigindo a verificação JWT do WebSocket legado. Essas rotas legadas não são montadas no modo monitor.

## Valores em reais

O painel apresenta o saldo USDT da carteira (livre + bloqueado) em R$ e o equivalente do PnL realizado em R$.
A cotação é do mercado público Binance USDT/BRL, via GET fixo `/api/v3/ticker/price?symbol=USDTBRL`, sem credenciais.
A API autenticada expõe `/api/v1/live/brl-quote`, com cache de 60 segundos, origem e horário da consulta.
A conversão serve apenas para apresentação. Não altera valores USDT, contabilidade, estratégia ou dimensionamento.
O equivalente do prejuízo usa a cotação atual, não o câmbio histórico da execução. Outros ativos e DUST permanecem separados.
Se a cotação falhar, os valores convertidos ficam indisponíveis; não se reutiliza uma cotação vencida como atual.

## Custos por transação

O histórico mostra o valor executado de cada fill (quantidade efetivamente executada × preço),
a comissão original e equivalente USDT/R$, a cotação registrada da comissão e a origem dessa informação.
Para compra, apresenta custo equivalente incluindo comissão; para venda, recebimento equivalente descontando comissão.
Comissões pagas em BNB são equivalentes econômicos, não um débito/crédito adicional em USDT.
A tarifa separada de conversão BRL/USDT não consta nos registros existentes: aparece como não registrada, nunca como zero.
A cotação usada para expressar valores em R$ é a atual. A cotação persistida para converter a comissão para USDT é histórica e aparece em coluna própria.
Cada linha é um fill: um order ID pode ter várias parcelas. O total de comissões no card soma apenas os fills exibidos, sinalizando valores faltantes; não é somado de novo ao PnL ou às taxas do trade.
Implementação de apresentação em `webapi/transaction_costs.py`, sem alteração da contabilidade financeira.

## Retomada após reiniciar o Windows

1. Crie um atalho para `D:\xampp\htdocs\crypto\iniciar_dashboard.bat` (botão direito → Enviar para → Área de trabalho).
2. Dê dois cliques no atalho. O launcher inicia/reutiliza apenas backend e frontend, espera localhost responder e abre o navegador padrão.
3. Entre com o único usuário `admin`, usando a senha existente em `API_PASSWORD` (padrão local: `admin`).
4. No bloco **Retomar a plataforma**, clique **Iniciar download de candles**. Aguarde aparecer o PID e a atividade recente do daemon.
5. Clique **Iniciar worker LIVE**, confira o comando e marque a autorização de ordens reais. Clique **Auditar e iniciar LIVE**.
6. Se a auditoria bloquear, nenhum worker novo é iniciado. O motivo aparece no painel; não há reparos, sinais forçados ou rollback automático.

Os botões ficam desabilitados quando o serviço já existe, está iniciando ou o perfil não tem permissão. O LIVE também exige exatamente um daemon com atividade recente.
Não é necessário executar scripts manualmente em cada reboot. Abrir o `.bat` nunca inicia trading automaticamente.
Cada componente é iniciado em uma janela PowerShell externa própria. Para esta tarefa não foram adicionados botões para parar, matar processos ou executar ordens.

A proteção contra duplicatas usa lock na API e mutex do Windows mantido durante toda a execução de cada componente. O launch também confere os processos existentes antes de chamar Python.
Parâmetros do worker e daemon são fixos e preservam os comandos existentes; o navegador não fornece caminhos, argumentos ou comandos arbitrários.
O processo filho carrega o `.env` original, sem herdar overrides acidentais de paper/testnet/SQLite do terminal. O arquivo não é modificado.

A auditoria reutiliza o cliente isolado de quatro rotas GET e consultas MySQL SELECT/READ ONLY. Históricos maiores são divididos em janelas adjacentes de até 24h, conforme os limites Binance.
O primeiro início pelo painel cobre desde a abertura persistida do contexto BNB; os seguintes usam a última auditoria válida com sobreposição de cinco minutos. Lacunas acima de 90 dias, referência temporal inválida, intenção pendente ou divergência exigem revisão manual.
O ownership externo documentado continua 0,00030331 BNB; discrepâncias bloqueiam em vez de atribuir saldo ao bot.
`optimization/results/platform_last_audit.json` contém somente o horário da auditoria válida para cobertura futura. Não altera positions JSON, trades ou valores financeiros.

Novos arquivos: `iniciar_dashboard.bat`, `scripts/open_live_dashboard.ps1`, `scripts/run_platform_component.ps1`, `webapi/platform_control.py`, `frontend/src/components/PlatformControls.tsx`, `tests/test_platform_control.py`.


### Conta pessoal

A plataforma aceita somente o usuário `admin`, com a senha existente em `API_PASSWORD` (padrão local: `admin`). Os logins viewer/operator e suas sessões antigas são rejeitados. Todos os controles do painel ficam disponíveis ao admin; confirmação de ordens reais e auditoria de início continuam obrigatórias.
