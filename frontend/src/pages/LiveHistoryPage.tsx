import { Alert, Box, Button, CircularProgress, Grid2 as Grid, Paper, Stack, TextField, Typography } from "@mui/material";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api/client";
import { SimpleTable } from "../components/SimpleTable";
import { MetricGrid } from "../components/MetricGrid";
import { Row, money, stamp, positionRows, brl } from "../api/live";

export function LiveHistoryPage() {
  const [filter, setFilter] = useState({strategy:"Sma200RegimeGated",symbol:"BNB/USDT",since:"",until:""});
  const [applied, setApplied] = useState(filter);
  const params = new URLSearchParams();
  if(applied.strategy) params.set("strategy",applied.strategy);
  if(applied.symbol) params.set("symbol",applied.symbol);
  if(applied.since) params.set("since",`${applied.since}T00:00:00-03:00`);
  if(applied.until) params.set("until",`${applied.until}T23:59:59-03:00`);
  const query=useQuery({queryKey:["live-history",params.toString()],queryFn:()=>apiGet<{trades:Row[];orders:Row[];at:string}>(`/live/history?${params}`),refetchInterval:15000,refetchIntervalInBackground:true});
  const fx=useQuery({queryKey:["usdt-brl-quote"],queryFn:()=>apiGet<{rate:number|null;at:string|null}>("/live/brl-quote"),refetchInterval:60000});
  const rate=fx.isError ? null : fx.data?.rate ?? null;
  const equivalent=(value:unknown)=>`${money(value)}\n${brl(value == null ? null : Number(value),rate)}`;
  const rawOrders=query.data?.orders || [];
  const knownFees=rawOrders.filter(o=>o.fee_usdt != null);
  const missingFees=rawOrders.length-knownFees.length;
  const feeTotal=knownFees.reduce((sum,o)=>sum+Number(o.fee_usdt),0);
  const orders=rawOrders.map(o=>({
    Fill:o.id,Ordem:o.exchange_order_id,Trade:o.trade_id,Ativo:o.symbol,
    Operação:o.side === "BUY" ? "COMPRA" : "VENDA",Status:o.status,
    Execução:`${Number(o.filled_quantity).toFixed(12)} ${String(o.symbol).split("/")[0]}\nPreço: ${money(o.price)}`,
    "Valor executado":equivalent(o.executed_value_usdt),
    "Comissão Binance":`${o.fee == null ? "Indisponível" : `${o.fee} ${o.fee_currency || "ativo desconhecido"}`}\n${equivalent(o.fee_usdt)}`,
    "Cotação da comissão":o.fee_conversion_price == null || !o.fee_currency ? "Não registrada" : `1 ${o.fee_currency} = ${money(o.fee_conversion_price)}`,
    "Total equivalente":o.side === "BUY" ? `Custo da compra:\n${equivalent(o.buy_cost_equivalent_usdt)}` : `Recebimento da venda:\n${equivalent(o.sell_proceeds_equivalent_usdt)}`,
    "Tarifa de conversão": "Não registrada",
    "Origem da comissão":o.fee_source || "Indisponível",Horário:stamp(o.timestamp)
  }));
  const trades=(query.data?.trades||[]).map(r=>({...positionRows([r])[0],Estratégia:r.strategy_name,Abertura:stamp(r.entry_time),Fechamento:stamp(r.exit_time),"Comissões do trade":equivalent(r.total_fees_usdt)}));
  return <Box><Typography variant="overline" color="primary">MYSQL LIVE / SOMENTE LEITURA</Typography><Typography variant="h4" sx={{mb:2}}>Histórico de operações</Typography><Paper sx={{p:2,mb:2}}><Grid container spacing={2}>
    {(["strategy","symbol","since","until"] as const).map(k=><Grid key={k} size={{xs:12,sm:6,md:3}}><TextField fullWidth size="small" type={k==="since"||k==="until" ? "date":"text"} label={{strategy:"Estratégia (vazio = todas)",symbol:"Ativo (vazio = todos)",since:"Desde · Brasília",until:"Até · Brasília"}[k]} slotProps={{inputLabel:{shrink:true}}} value={filter[k]} onChange={e=>setFilter({...filter,[k]:e.target.value})}/></Grid>)}
    </Grid><Stack direction="row" spacing={2} alignItems="center" sx={{mt:2}}><Button variant="contained" onClick={()=>setApplied({...filter})}>Aplicar filtros</Button><Typography variant="caption" color="text.secondary">Apenas operações reais · até 200 registros por tabela · {stamp(query.data?.at)}</Typography></Stack></Paper>
    {query.isPending && <CircularProgress/>}{query.isError && <Alert severity="error">Histórico indisponível. Nenhum resultado simulado.</Alert>}
    {query.data && <>
      <MetricGrid items={[
        {label:"Comissões Binance · fills exibidos",value:knownFees.length ? equivalent(feeTotal) : "Indisponível",note:missingFees ? `Total parcial: ${missingFees} comissão(ões) indisponível(is).` : `${rawOrders.length} fills exibidos. Não soma novamente as comissões dos trades.`},
        {label:"Tarifas de conversão BRL/USDT",value:"Não registradas",note:"Cotação de equivalência não é uma tarifa cobrada. Não é possível inferir custo ou spread de uma conversão não registrada."}
      ]}/>
      <Typography variant="body2" color="text.secondary" sx={{mt:2}}>R$ é uma equivalência pela cotação atual USDT/BRL ({stamp(fx.data?.at)}), não o câmbio histórico. Compras mostram valor executado + comissão; vendas mostram valor executado − comissão. Se a taxa foi paga em BNB, esses totais são equivalentes econômicos, não um débito ou crédito adicional em USDT. O PnL persistido não é alterado.</Typography>
      <Typography variant="h6" sx={{my:2}}>Trades · OPEN, DUST e encerrados</Typography>{trades.length ? <SimpleTable columns={["Trade","Ativo","Estratégia","Status","Quantidade","Entrada","Custo","Comissões do trade","PnL realizado","Abertura","Fechamento"]} rows={trades}/> : <Alert severity="info">Nenhum trade nos filtros informados.</Alert>}
      <Typography variant="h6" sx={{my:2}}>Custos por transação · ordens e fills</Typography><Typography variant="body2" color="text.secondary" sx={{mb:1}}>Cada linha representa um fill persistido. Um order ID pode ter várias linhas: as comissões são parcelas distintas. Valores ausentes não são tratados como zero. Deslize a tabela para consultar todos os detalhes.</Typography>
      {orders.length ? <SimpleTable columns={["Fill","Ordem","Trade","Ativo","Operação","Status","Execução","Valor executado","Comissão Binance","Cotação da comissão","Total equivalente","Tarifa de conversão","Origem da comissão","Horário"]} rows={orders}/> : <Alert severity="info">Nenhuma execução nos filtros informados.</Alert>}
    </>}
  </Box>;
}
