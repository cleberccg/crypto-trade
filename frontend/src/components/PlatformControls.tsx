import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Box, Button, Chip, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, Paper, Stack, Typography } from "@mui/material";
import PlayArrowRounded from "@mui/icons-material/PlayArrowRounded";
import { apiGet, apiPost, getRole, clearAuth, AUTH_REQUIRED_EVENT } from "../api/client";

interface PlatformStatus {
  processes:{worker:Array<{pid:number}>;daemon:Array<{pid:number}>};
  starting:Partial<Record<"worker"|"daemon",boolean>>;
  daemon_activity_at:string|null;
  worker_command:string;
}
export function PlatformControls(){
  const role=getRole();
  const permitted=role==="administrator";
  const cache=useQueryClient();
  const [confirmOpen,setConfirmOpen]=useState(false);
  const [confirmed,setConfirmed]=useState(false);
  const [message,setMessage]=useState("");
  const status=useQuery({queryKey:["platform-status"],queryFn:()=>apiGet<PlatformStatus>("/platform/status"),refetchInterval:5000});
  const start=useMutation({mutationFn:(component:"worker"|"daemon")=>apiPost<{status:string;component:string}>("/platform/start",{component,confirm_real_orders:component==="worker"&&confirmed}),onSuccess:(response)=>{
    setMessage(response.status==="already_running"?"O serviço já está em execução; nenhuma duplicata foi criada.":"Solicitação aceita. Aguarde a inicialização e acompanhe o PID e a atividade abaixo.");
    setConfirmOpen(false);setConfirmed(false);
    cache.invalidateQueries({queryKey:["platform-status"]});cache.invalidateQueries({queryKey:["live-dashboard"]});
  }});
  const worker=status.data?.processes.worker||[];
  const daemon=status.data?.processes.daemon||[];
  const daemonRecent=Boolean(status.data?.daemon_activity_at&&Date.now()-Date.parse(status.data.daemon_activity_at)<90000);
  const running=(part:"worker"|"daemon")=>(status.data?.processes[part].length||0)>0;
  const disabled=(part:"worker"|"daemon")=>!permitted||!status.data||status.isError||start.isPending||running(part)||status.data?.starting[part];
  return <Paper sx={{p:2.5,mb:3,border:"1px solid rgba(0,209,160,.22)"}}>
    <Stack direction={{xs:"column",md:"row"}} gap={2} justifyContent="space-between" alignItems={{xs:"stretch",md:"center"}}>
      <Box><Typography variant="h6">Retomar a plataforma</Typography><Typography variant="body2" color="text.secondary">Após reiniciar o computador: inicie os candles, aguarde a atividade do daemon e então inicie o LIVE.</Typography><Typography variant="caption" color="text.secondary">Abrir o painel não inicia negociações. Cada serviço abre sua própria janela PowerShell.</Typography></Box>
      <Stack direction={{xs:"column",sm:"row"}} gap={1}>
        {running("daemon") ? <Chip color="success" variant="outlined" label={`Candles ativos · PID ${daemon.map(p=>p.pid).join(", ")}`} /> : <Button startIcon={<PlayArrowRounded/>} variant="outlined" disabled={disabled("daemon")} onClick={()=>{setMessage("");start.mutate("daemon");}}>{status.data?.starting.daemon?"Candles iniciando…":"Iniciar download de candles"}</Button>}
        {running("worker") ? <Chip color="success" variant="outlined" label={`LIVE ativo · PID ${worker.map(p=>p.pid).join(", ")}`} /> : <Button startIcon={<PlayArrowRounded/>} color="warning" variant="contained" disabled={disabled("worker")||daemon.length!==1||!daemonRecent} onClick={()=>{setConfirmed(false);start.reset();setConfirmOpen(true);}}>{status.data?.starting.worker?"LIVE iniciando…":"Iniciar worker LIVE"}</Button>}
      </Stack>
    </Stack>
    {running("worker")&&running("daemon")&&<Typography variant="body2" color="success.main" sx={{mt:2}}>A plataforma já está em execução. Não é necessário iniciar os serviços novamente.</Typography>}
    {!permitted&&<Alert severity="info" sx={{mt:2}} action={<Button color="inherit" onClick={()=>{clearAuth();cache.clear();window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));}}>Entrar como admin</Button>}>Entre com o usuário admin para controlar os serviços.</Alert>}
    {permitted&&!running("worker")&&!daemonRecent&&<Typography variant="body2" color="warning.main" sx={{mt:1}}>O LIVE será habilitado quando houver exatamente um daemon com atividade recente.</Typography>}
    {message&&<Alert severity="success" sx={{mt:2}}>{message}</Alert>}
    {(start.isError||status.isError)&&<Alert severity="error" sx={{mt:2}}>{start.isError?start.error.message:"Não foi possível consultar o status dos serviços."}</Alert>}
    <Dialog open={confirmOpen} onClose={()=>{if(!start.isPending)setConfirmOpen(false);}} maxWidth="md" fullWidth>
      <DialogTitle>Iniciar LIVE na Binance Spot MAINNET?</DialogTitle>
      <DialogContent><Alert severity="warning" sx={{mb:2}}>O worker poderá enviar ordens reais automaticamente conforme a estratégia. Esta ação não executa uma compra ou venda manual.</Alert><Typography sx={{mb:1}}>Antes de iniciar, o sistema verificará Binance, MySQL e JSON por auditoria somente leitura. Divergências ou intenções pendentes bloqueiam o início.</Typography><Box component="pre" sx={{whiteSpace:"pre-wrap",overflowWrap:"anywhere",fontSize:12,p:2,bgcolor:"background.default"}}>{status.data?.worker_command}</Box><FormControlLabel control={<Checkbox checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} disabled={start.isPending}/>} label="Autorizo iniciar o worker LIVE com ordens reais habilitadas."/>{start.isError&&<Alert severity="error">{start.error.message}</Alert>}</DialogContent>
      <DialogActions><Button disabled={start.isPending} onClick={()=>setConfirmOpen(false)}>Cancelar</Button><Button color="warning" variant="contained" disabled={!confirmed||start.isPending} onClick={()=>start.mutate("worker")}>{start.isPending?"Auditando e iniciando…":"Auditar e iniciar LIVE"}</Button></DialogActions>
    </Dialog>
  </Paper>;
}
