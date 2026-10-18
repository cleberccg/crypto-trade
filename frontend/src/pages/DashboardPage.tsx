import { Alert, Box, Button, Chip, CircularProgress, Grid2 as Grid, Paper, Stack, Typography } from "@mui/material";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api/client";
import { MetricGrid } from "../components/MetricGrid";
import { PlatformControls } from "../components/PlatformControls";
import { SimpleTable } from "../components/SimpleTable";
import { LiveSnapshot, money, stamp, positionRows, brl } from "../api/live";

export function DashboardPage() {
  const query = useQuery({queryKey: ["live-dashboard"], queryFn: () => apiGet<LiveSnapshot>("/dashboard"), refetchInterval: 15000, refetchIntervalInBackground: true});
  const fx = useQuery({queryKey:["usdt-brl-quote"],queryFn:()=>apiGet<{rate:number|null;at:string|null;source:string;status:string}>("/live/brl-quote"),refetchInterval:60000});
  const data = query.data;
  if (!data && query.isPending) return <CircularProgress aria-label="Carregando dados LIVE" />;
  if (!data) return <Alert severity="error">Não foi possível consultar o ambiente LIVE. Nenhum valor simulado será exibido.</Alert>;
  const pnl = data.capital.realized_pnl;
  const outcome = pnl == null ? "Indisponível" : pnl < 0 ? "PREJUÍZO" : pnl > 0 ? "LUCRO" : "NEUTRO";
  const rate = fx.isError ? null : fx.data?.rate ?? null;
  const wallet = data.balances?.USDT;
  const walletUsdt = wallet ? Number(wallet.free) + Number(wallet.locked) : null;
  const metrics = [
    {label:"Saldo da carteira USDT em R$",value:brl(walletUsdt,rate),note:`Livre + bloqueado: ${money(walletUsdt)}. Outros ativos separados.`},
    {label:pnl != null && pnl < 0 ? "Prejuízo realizado em R$" : "Resultado realizado em R$",value:brl(pnl,rate),tone:pnl == null ? undefined : pnl < 0 ? "error.main" : "success.main",note:"Equivalente pela cotação atual; não é o câmbio da data do trade."},
    {label: "USDT livre na conta", value: `${money(data.capital.usdt_free)} / ${brl(data.capital.usdt_free,rate)}`, note: `Binance · ${stamp(data.sources.binance.at)}`},
    {label: "Comprometido em OPEN", value: money(data.capital.committed_open), note: "Custo de entrada das posições do contexto"},
    {label: "Capital acompanhado · custo", value: money(data.capital.tracked_cost_capital), note: "USDT da conta + OPEN · exclui DUST e ativos externos"},
    {label: "Custo residual DUST", value: money(data.capital.dust_cost, 6), note: "Separado do capital ativo · não é valor de mercado"},
    {label: "PnL realizado · contexto", value: `${money(pnl, 6)} · ${outcome}`, tone: pnl == null ? undefined : pnl < 0 ? "error.main" : "success.main", note: "Somado uma vez por trade · inclui parcelas realizadas DUST"},
    {label: "PnL não realizado", value: "Indisponível", note: "Sem marcação a mercado confiável para esta projeção"},
    {label: "Posições OPEN", value: data.open_positions ? `${data.open_positions.length} / ${data.open_limit}` : "Indisponível", note: "DUST não ocupa posição ativa"},
    {label: "Posições DUST", value: data.dust_positions?.length ?? "Indisponível", note: "Resíduos preservados com o trade original"}
  ];
  const currentActivity = (kind: "worker" | "daemon") => {
    const at = data.activity[`${kind}_at`];
    return Boolean(at && Date.now() - Date.parse(at) < 90000);
  };
  return <Box className="fade-in">
    <Stack direction={{xs:"column",md:"row"}} justifyContent="space-between" gap={2} sx={{mb:3}}>
      <Box><Typography variant="overline" color="primary">BINANCE SPOT MAINNET / MONITORAMENTO</Typography><Typography variant="h4">Operação LIVE</Typography><Typography color="text.secondary">{data.context.strategy} {data.context.version} · {data.context.symbol} · {data.context.timeframe}</Typography></Box>
      <Stack alignItems={{xs:"flex-start",md:"flex-end"}} spacing={1}><Button startIcon={<RefreshRounded/>} variant="outlined" onClick={() => query.refetch()} disabled={query.isFetching}>Atualizar agora</Button><Typography variant="caption" color="text.secondary">Atualização automática · 15s · Brasília<br/>{stamp(data.updated_at)}</Typography></Stack>
    </Stack>
    <PlatformControls/>
    {query.isError && <Alert severity="warning" sx={{mb:2}}>A última atualização falhou. Os dados abaixo são da consulta anterior: {stamp(data.updated_at)}.</Alert>}
    {data.alerts.map((alert) => <Alert key={alert} severity="warning" sx={{mb:1}}>{alert}</Alert>)}
    <Paper sx={{p:2,mb:3}}><Stack direction="row" gap={1} flexWrap="wrap">
      <Chip label="LIVE · ordens reais no worker" color="warning"/>
      {(["worker", "daemon"] as const).map(kind => <Chip key={kind} color={data.processes[kind].length===1 && currentActivity(kind) ? "success" : "error"} variant="outlined" label={`${kind === "worker" ? "Worker" : "Market-data"} · PID ${data.processes[kind].map(p=>p.pid).join(", ") || "ausente"} · ${currentActivity(kind) ? "atividade recente" : "sem atividade recente"}`}/>)}
      <Chip label={`MySQL · ${data.sources.mysql.status === "connected" ? "conectado" : "indisponível"}`} variant="outlined"/>
      <Chip label={`Binance · ${data.sources.binance.status === "connected" ? "consultada" : "indisponível"}`} variant="outlined"/>
    </Stack><Typography variant="caption" display="block" color="text.secondary" sx={{mt:1}}>Worker: {stamp(data.activity.worker_at)} · Daemon: {stamp(data.activity.daemon_at)} · MySQL: {stamp(data.sources.mysql.at)}</Typography></Paper>
    <MetricGrid items={metrics}/>
    <Typography variant="caption" display="block" color="text.secondary" sx={{mt:1}}>Conversão indicativa: 1 USDT = {rate == null ? "Indisponivel" : `R$ ${rate.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:6})}`} - {fx.data?.source ?? "Binance Spot USDT/BRL"} - consulta {stamp(fx.data?.at)}. Valores em USDT permanecem a referência da operação.</Typography>
    <Typography variant="caption" display="block" color="text.secondary" sx={{mt:1,mb:3}}>{data.scope_note}</Typography>
    <Grid container spacing={2}>
      <Grid size={{xs:12,lg:8}}><Paper sx={{p:2.5}}><Typography variant="h6" sx={{mb:2}}>Posições ativas · OPEN</Typography>{data.open_positions?.length ? <SimpleTable columns={["Trade","Ativo","Quantidade","Entrada","Custo","PnL realizado","Status"]} rows={positionRows(data.open_positions)}/> : <Typography color="text.secondary">{data.open_positions ? "Nenhuma posição ativa neste contexto." : "Indisponível"}</Typography>}</Paper>
      <Paper sx={{p:2.5,mt:2,borderLeft:"3px solid #ffc857"}}><Typography variant="h6" sx={{mb:2}}>Resíduos preservados · DUST</Typography><SimpleTable columns={["Trade","Ativo","Quantidade","Entrada","Custo","PnL realizado","Status"]} rows={positionRows(data.dust_positions || [])}/><Typography variant="caption" color="text.secondary">Não representam uma posição ativa ou uma venda concluída. PnL realizado permanece no trade.</Typography></Paper></Grid>
      <Grid size={{xs:12,lg:4}}><Paper sx={{p:2.5}}><Typography variant="h6" sx={{mb:2}}>Estratégia e dados</Typography>
        <Typography variant="caption" color="text.secondary">Última decisão registrada · {stamp(data.activity.decision_at)}</Typography><Typography variant="h5" sx={{mb:2}}>{data.activity.decision === "HOLD_NO_ACTIVE_POSITION" ? "HOLD - sem posicao ativa" : data.activity.decision?.replace(/_/g, " ") ?? "Indisponivel"}</Typography>
        <Typography variant="body2">Último sinal: {data.activity.signal?.signal ?? "Indisponível"}</Typography><Typography variant="body2" color="text.secondary">{data.activity.signal?.reason ?? "Nenhuma avaliação de sinal registrada nesta execução."}</Typography><Typography variant="caption" color="text.secondary">Horario da avaliacao: {stamp(data.activity.signal_at)}</Typography>
        <Typography variant="body2" sx={{mt:2}}>Candle fechado disponível: {stamp(data.last_closed_candle_available)}</Typography><Typography variant="body2">Última avaliação persistida de posição: {stamp(data.last_evaluated_position_candle)}</Typography><Typography variant="caption" color="text.secondary">Disponibilidade de candle não comprova processamento pelo worker. Indicadores: indisponíveis nesta fonte; não recalculados.</Typography>
      </Paper><Paper sx={{p:2.5,mt:2}}><Typography variant="h6">Monitoramento</Typography><Typography variant="body2" sx={{mt:1}}>Erros recentes: {data.activity.errors.length}</Typography>{data.activity.errors.map((e,i)=><Typography key={i} color="error" variant="body2">{stamp(e.at)} · {e.source}: {e.message}</Typography>)}<Typography variant="body2" sx={{mt:1}}>Ordens BNB/USDT abertas na Binance: {data.binance_open_orders?.length ?? "Indisponivel"}</Typography><Typography variant="body2" sx={{mt:1}}>BNB livre / bloqueado: {data.balances?.BNB ? `${data.balances.BNB.free} / ${data.balances.BNB.locked}` : "Indisponivel"}</Typography><Typography variant="caption" color="text.secondary">Saldo da conta; inclui ativos nao atribuidos ao bot. Consulta: {stamp(data.sources.binance.at)}</Typography><Typography variant="body2" sx={{mt:1}}>Posições legadas preservadas: {data.preserved_positions?.length ?? "Indisponível"}</Typography><Typography variant="caption" color="text.secondary">Fora do contexto atual; não somadas a OPEN ou ao capital acima.</Typography></Paper></Grid>
    </Grid>
  </Box>;
}
