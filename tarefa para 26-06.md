# TAREFA - Desenvolver Dashboard Web Profissional para a Plataforma de Trading

## Objetivo

Criar um Dashboard Web moderno para monitorar, controlar e analisar toda a plataforma de Trading Quantitativo.

Este NÃO será apenas um painel administrativo.

O objetivo é criar uma plataforma semelhante às utilizadas por fundos quantitativos, permitindo visualizar em tempo real a saúde do sistema, resultados, otimizações, backtests, estratégias, logs e operações.

---

# IMPORTANTE

Não alterar nenhuma regra de negócio existente.

Não modificar:

* Strategy
* Optimizer
* Risk Manager
* Backtest Engine
* Banco de Dados
* Persistência

O Dashboard deverá consumir apenas APIs públicas do sistema.

Caso ainda não existam APIs suficientes, criá-las utilizando FastAPI.

Nunca acessar o banco diretamente pelo Frontend.

Toda comunicação deverá ocorrer via API REST e WebSocket.

---

# Arquitetura

Frontend

↓

FastAPI REST

↓

Services

↓

Repositories

↓

SQLite / PostgreSQL

---

# Tecnologias

Frontend

* React
* TypeScript
* Vite
* Material UI
* TanStack Query
* React Router
* Recharts
* React Hook Form

Backend

* FastAPI
* Pydantic
* SQLAlchemy
* WebSocket
* Background Tasks

---

# Layout

Menu lateral responsivo.

Tema escuro profissional.

Layout semelhante a plataformas financeiras.

Dashboard totalmente responsivo.

---

# Tela 1 - Dashboard Principal

Mostrar:

Status do sistema

ONLINE

OFFLINE

Modo

Paper

Live

Estratégia ativa

Símbolo

Timeframe

Capital atual

Capital inicial

Lucro diário

Lucro semanal

Lucro mensal

Drawdown

Profit Factor

Sharpe

Expectância

Número de trades

Sinais gerados

Backtests executados

Otimizações executadas

CPU

RAM

Tempo de execução

Última atualização

---

# Tela 2 - Execuções

Lista de todas as execuções.

Mostrar:

Execution ID

Data

Tipo

Status

Duração

Workers

Estratégia

Símbolo

Timeframe

Ao clicar:

Abrir detalhes completos.

---

# Tela 3 - Otimizações

Mostrar:

Progresso em tempo real

Combinações executadas

Combinações restantes

Tempo estimado

Melhor Profit Factor

Melhor configuração

Ranking Top 100

Gráfico de evolução

Filtros.

---

# Tela 4 - Backtests

Lista completa.

Ao abrir:

Curva de patrimônio

Drawdown

Distribuição dos trades

Lucro acumulado

Profit Factor

Sharpe

Expectância

Histórico de operações.

---

# Tela 5 - Operações

Tabela completa.

Filtros:

Período

Ativo

Estratégia

Resultado

Paper

Live

Colunas:

Entrada

Saída

Stop

Take

RR

Lucro

Tempo

Motivo da saída

Score

---

# Tela 6 - Sinais

Mostrar TODOS os sinais.

Inclusive os rejeitados.

Filtros:

BUY

SELL

Aceitos

Rejeitados

Mostrar motivo da rejeição.

---

# Tela 7 - Analytics

Gráficos:

Profit Factor por ativo

Profit Factor por timeframe

Win Rate

Drawdown

Sharpe

Expectância

EMA mais lucrativa

RR mais lucrativo

Heatmap por horário

Heatmap por dia da semana

Distribuição dos trades

Resultados por estratégia

Resultados por versão

---

# Tela 8 - Banco de Dados

Permitir visualizar:

optimization_runs

optimization_results_history

backtest_runs

trade_history

signal_snapshots

indicator_snapshots

validation_runs

execution_sessions

execution_checkpoints

Permitir filtros.

Somente leitura.

---

# Tela 9 - Logs

Logs em tempo real.

Filtros:

INFO

WARNING

ERROR

DEBUG

Busca.

Download.

---

# Tela 10 - Configurações

Permitir alterar:

Ativo

Timeframe

Workers

Modo

Paper

Live

Sem editar o .env manualmente.

---

# Tela 11 - Monitor em Tempo Real

Atualização via WebSocket.

Mostrar:

Preço

Posições abertas

Lucro atual

Capital

Último sinal

Status do robô

Uso de CPU

Uso de memória

Tempo online

---

# APIs

Criar endpoints para:

Dashboard

Backtests

Optimizer

Validation

Trades

Signals

Indicators

Strategies

Logs

Execution Sessions

Analytics

Configurações

Todos documentados automaticamente pelo FastAPI.

---

# Segurança

Preparar autenticação JWT.

Perfis:

Administrador

Somente leitura

Operador

Mesmo que inicialmente exista apenas um usuário.

---

# Qualidade

Aplicar:

SOLID

Clean Architecture

Repository Pattern

Service Layer

DTOs

Type Hints

Tratamento de exceções

Paginação

Ordenação

Filtros

---

# Documentação

Atualizar:

docs/task_details.txt

docs/checklist.md

Criar:

docs/frontend_architecture.md

Documentar:

Estrutura do Frontend

Estrutura da API

Fluxo dos dados

Rotas

Componentes

---

# Objetivo Final

Construir uma plataforma profissional de pesquisa quantitativa, permitindo acompanhar todo o ciclo de vida do robô:

Download de dados

↓

Backtests

↓

Otimizações

↓

Validação

↓

Paper Trading

↓

Live Trading

↓

Relatórios

↓

Analytics

↓

Monitoramento em tempo real

O Dashboard deve ser modular, preparado para crescimento futuro e capaz de acompanhar milhões de registros sem necessidade de reestruturação da arquitetura.
