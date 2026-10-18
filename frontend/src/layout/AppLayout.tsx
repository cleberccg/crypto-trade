import { Button, Stack, Box, Chip, Divider, Drawer, List, ListItemButton, ListItemText, Toolbar, Typography } from "@mui/material";
import { Link, Outlet, useLocation } from "react-router-dom";

import { clearAuth, getRole } from "../api/client";

const drawerWidth = 280;

const navItems = [
  { label: "Dashboard", path: "/" },
  { label: "Execucoes", path: "/executions" },
  { label: "Otimizacoes", path: "/optimizations" },
  { label: "Backtests", path: "/backtests" },
  { label: "Operacoes", path: "/trades" },
  { label: "Sinais", path: "/signals" },
  { label: "Indicadores", path: "/indicators" },
  { label: "Analytics", path: "/analytics" },
  { label: "Validacoes", path: "/validation" },
  { label: "Banco", path: "/database" },
  { label: "Logs", path: "/logs" },
  { label: "Configuracoes", path: "/settings" },
  { label: "Monitor", path: "/monitor" },
  { label: "Observabilidade", path: "/observability" },
  { label: "Jobs", path: "/jobs" },
  { label: "Execution Timeline", path: "/timeline" },
  { label: "Notification Center", path: "/notifications" },
  { label: "Scheduler", path: "/scheduler" },
  { label: "Research Lab", path: "/research" },
  { label: "Research Comparisons", path: "/research/comparisons" },
  { label: "Research Rankings", path: "/research/rankings" },
  { label: "Research Insights", path: "/research/insights" },
  { label: "Research Heatmaps", path: "/research/heatmaps" },
  { label: "Research Reports", path: "/research/reports" },
  { label: "Market Scanner", path: "/scanner" },
  { label: "System Status", path: "/system-status" },
  { label: "Next Phase Readiness", path: "/next-phase" },
  { label: "Execution Manager", path: "/execution-manager" },
  { label: "Execution Heartbeat", path: "/execution-manager/heartbeat" },
  { label: "Execution Watchdog", path: "/execution-manager/watchdog" },
  { label: "Execution Incidents", path: "/execution-manager/incidents" },
  { label: "Execution Replay", path: "/execution-manager/replay" },
  { label: "Execution Performance", path: "/execution-manager/performance" },
  { label: "Execution Comparison", path: "/execution-manager/comparison" }
];

export function AppLayout() {
  const location = useLocation();
  const role = getRole() ?? "read-only";
  if (import.meta.env.VITE_LIVE_MONITOR_ONLY === "1") return (
    <Box sx={{minHeight:"100vh"}}>
      <Box component="header" sx={{borderBottom:"1px solid rgba(255,255,255,.08)",background:"#0a1b24",px:{xs:2,md:4},py:2}}>
        <Stack direction="row" alignItems="center" justifyContent="space-between" gap={2} flexWrap="wrap">
          <Box><Typography variant="h6">Trade CCG <Box component="span" sx={{color:"primary.main"}}>LIVE</Box></Typography><Typography variant="caption" color="text.secondary">Monitoramento da conta Spot</Typography></Box>
          <Stack component="nav" direction="row" spacing={1} flexWrap="wrap">
            <Button component={Link} to="/" variant={location.pathname==="/"?"contained":"text"}>Visão geral</Button>
            <Button component={Link} to="/trades" variant={location.pathname==="/trades"?"contained":"text"}>Operações</Button>
            <Button onClick={()=>{clearAuth();window.location.assign("/");}}>Sair</Button>
          </Stack>
          <Chip size="small" label={role === "read-only" ? "Somente leitura" : "Controle de processos"} color="success" variant="outlined"/>
        </Stack>
      </Box>
      <Box component="main" sx={{maxWidth:1600,mx:"auto",p:{xs:2,md:4}}}><Outlet/></Box>
    </Box>
  );

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <Drawer
        variant="permanent"
        sx={{
          width: drawerWidth,
          flexShrink: 0,
          [`& .MuiDrawer-paper`]: {
            width: drawerWidth,
            boxSizing: "border-box",
            borderRight: "1px solid rgba(255,255,255,0.08)",
            background: "linear-gradient(180deg, rgba(6,22,31,0.95), rgba(12,35,48,0.92))"
          }
        }}
      >
        <Toolbar>
          <Box>
            <Typography variant="h6" sx={{ letterSpacing: 0.5 }}>
              Trade CCG
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Trading Control Center
            </Typography>
            <Box sx={{ mt: 0.6 }}>
              <Chip size="small" label="Admin" color={role === "administrator" ? "primary" : "default"} />
            </Box>
          </Box>
        </Toolbar>
        <Divider />
        <List>
          {navItems.map((item) => (
            <ListItemButton
              key={item.path}
              component={Link}
              to={item.path}
              selected={location.pathname === item.path}
            >
              <ListItemText primary={item.label} />
            </ListItemButton>
          ))}
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: { xs: 2, md: 3 } }}>
        <Outlet />
      </Box>
    </Box>
  );
}
