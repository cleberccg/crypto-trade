export type Row = Record<string, unknown>;
export interface LiveSnapshot {
  updated_at: string; mode: string; read_only: boolean;
  context: {strategy: string; version: string; symbol: string; timeframe: string};
  processes: {worker: Array<{pid: number}>; daemon: Array<{pid: number}>};
  activity: {worker_at: string | null; daemon_at: string | null; decision: string | null; decision_at: string | null; signal: {signal:string; reason:string} | null; signal_at:string|null; errors: Array<{at:string;source:string;message:string}>};
  sources: {mysql:{status:string;at:string|null};binance:{status:string;at:string|null}};
  capital: {usdt_free:number|null;committed_open:number|null;dust_cost:number|null;tracked_cost_capital:number|null;realized_pnl:number|null};
  open_limit:number;open_positions:Row[]|null;dust_positions:Row[]|null;preserved_positions:Row[]|null;
  last_closed_candle_available:string|null;last_evaluated_position_candle:string|null;
  balances:Record<string,{free:string;locked:string}>|null;binance_open_orders:Row[]|null;alerts:string[];scope_note:string;
}
export const money = (value: unknown, precision=8) => value == null ? "Indisponível" : `${Number(value).toLocaleString("pt-BR", {minimumFractionDigits:precision, maximumFractionDigits:precision})} USDT`;
export const stamp = (value: unknown) => value ? new Date(String(value)).toLocaleString("pt-BR", {timeZone:"America/Sao_Paulo"}) : "Indisponível";
export const positionRows = (rows: Row[]) => rows.map(r=>({Trade:r.id,Ativo:r.symbol,Quantidade:Number(r.quantity).toFixed(12),Entrada:money(r.entry_price),Custo:money(Number(r.quantity)*Number(r.entry_price),6),"PnL realizado":r.pnl == null ? "Indisponível" : `${money(r.pnl,6)} · ${Number(r.pnl)<0 ? "PREJUÍZO" : Number(r.pnl)>0 ? "LUCRO" : "NEUTRO"}`,Status:r.status}));


export const brl = (amount: number | null | undefined, rate: number | null | undefined) =>
  amount == null || rate == null ? "Indisponível" : (amount * rate).toLocaleString("pt-BR", {style:"currency", currency:"BRL"});
