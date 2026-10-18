# Runbook de implantação LIVE — transição OPEN → DUST

**Estado:** proposta operacional; não autoriza implantação. Executar somente após aprovação explícita e janela aprovada. Não alterar estratégia, RiskManager, PositionSizer ou `.env`.

## Pré-condições

1. Revisar o diff e a versão exata do código validado; confirmar que nenhuma alteração não relacionada será implantada. Executar as suítes isoladas aprovadas e arquivar seus resultados.
2. Confirmar uma única instância do worker LIVE para a conta/contexto, usando inspeção somente leitura em PowerShell externo:

   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main.py live' } | Select-Object ProcessId, CommandLine
   ```

   Se houver zero ou mais de um worker, interromper o procedimento e investigar. Não abrir um segundo worker.
3. Confirmar o saldo atual, ordens abertas e fills com credencial Binance dedicada **Enable Reading**, com negociação e saques desabilitados e restrição de IP. Não usar a chave LIVE de negociação para essa auditoria. Nunca registrar chave, segredo ou assinatura em terminal/log/chat. Se não for possível provar essas permissões, **não fazer consulta autenticada**: no painel Binance, conferir manualmente Spot Wallet → BNB, ordens abertas de BNB/USDT e histórico de trades/fills; não clicar em comprar, vender ou cancelar. Registrar somente horário e quantidades necessárias. Comparar o total BNB à soma de OPEN+DUST live; toda sobra permanece não atribuída, salvo ownership externo documentado separadamente.
4. Fazer backup verificável do banco e dos arquivos `optimization/results/live_positions.json` e `optimization/results/live_positions.json.bak`. Usar o procedimento aprovado de backup MySQL, armazenamento protegido com timestamp e checksum; não colocar senha em argumento de processo. Registrar também, via SELECT, trade 3081, ordens/fills relacionados e saldo observado. Não sobrescrever os originais.
5. Confirmar o schema sem DDL: `trades.status` deve aceitar `DUST` (schema observado: `VARCHAR(10) NOT NULL`) e não ter ENUM/CHECK restritivo. Não executar migração.

## Implantação autorizada

6. Somente na janela aprovada, solicitar parada controlada na janela externa do único worker e encerrá-lo com Ctrl+C. Confirmar que terminou e que nenhum outro worker LIVE está ativo. Não usar `Stop-Process`, emergency-close ou encerramento abrupto.
7. Implantar atomicamente o pacote validado, incluindo este runbook. Manter a versão de código anterior arquivada, mas não a iniciar isoladamente após o primeiro estado DUST.
8. Iniciar **um** worker em um PowerShell externo, usando os parâmetros congelados e aprovados para a conta. Exemplo para o contexto validado (a execução exige a autorização LIVE explícita):

   ```powershell
   Set-Location D:\xampp\htdocs\crypto
   & C:\Python313\python.exe main.py live --symbol BNB/USDT --timeframe 4h --strategy-name Sma200RegimeGated --strategy-version v1 --poll-seconds 15 --bootstrap-bars 1500 --bootstrap-replay-bars 350 --output-prefix live --enable-real-orders
   ```

## Reconciliação e aceite

9. Acompanhar a inicialização e comparar trade 3081 com o snapshot pré-implantação. Só aceitar `OPEN → DUST` se os filtros vigentes justificarem o estado e a reconciliação cobrir a quantidade agregada do bot. Confirmar por SELECT e observação de ordens/fills: mesmo trade ID e quantidade residual; status DUST; nenhum fill/SELL criado pela classificação; `exit_price`, `exit_time` e `exit_reason` inalterados; PnL e fees realizados preservados. Não executar UPDATE manual.
10. Conferir que o JSON mantém o ownership do resíduo, que posições DUST não contam como ativas nem CLOSED, e que uma eventual sobra de saldo Binance permanece **não atribuída** (não completar ownership com BNB externo). Conferir USDT livre, capital comprometido apenas em OPEN, custo residual separado e fees sem dupla contabilização. Não adotar diferenças automaticamente.
11. Se houver divergência de saldo, ownership, PnL, fees, estado persistido, ordem aberta ou comportamento inesperado: parar novas decisões LIVE de forma controlada conforme autorização operacional, preservar banco/JSON/logs e reconciliar manualmente. Não vender, apagar, zerar, marcar CLOSED nem recriar trade para “corrigir” a divergência.

## Contingência e rollback

- **Rollback de código antigo sozinho não é seguro após gravar DUST.** O código anterior pode interpretar somente OPEN/CLOSED e deixar de carregar/proteger o resíduo.
- O padrão seguro em falha é manter o worker parado e preservar o código compatível com DUST, o banco e os dois arquivos de estado. Preparar uma correção forward que continue reconhecendo DUST e passe por revisão/testes antes de reiniciar.
- Restaurar conjuntamente código, banco e JSON do backup pré-deploy somente se for comprovado que, desde o snapshot, não houve ordem, fill nem outra escrita operacional. Fazer essa restauração apenas com aprovação e procedimento de recuperação do DBA; não apagar histórico nem liberar ownership por SQL manual.
- Se houve qualquer atividade posterior, **não** restaurar o banco inteiro nem voltar ao código antigo. Reconstruir o estado compatível a partir de ordens/fills e registros preservados, revisar a contabilidade e somente então autorizar uma versão que mantenha suporte a DUST.

## Evidências desta validação

- Trade 3081 e schema foram conferidos por consultas somente leitura; nenhuma escrita em LIVE, ordem, parada de worker ou migração foi realizada.
- Os testes de execução usam mocks, SQLite temporário e filesystem temporário. Os valores históricos de saldo externo não substituem uma confirmação contemporânea da Binance.
- A consulta autenticada Binance não foi feita: o cliente atual não impõe somente GET e pode carregar credenciais com permissão de negociação. Até haver uma chave de leitura comprovadamente sem negociação/saques e um caminho tecnicamente restrito, usar conferência manual segura.