import { Box, Chip, Divider, Drawer, List, ListItemButton, ListItemText, Toolbar, Typography } from "@mui/material";
import { Link, Outlet, useLocation } from "react-router-dom";

import { getRole } from "../api/client";

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
              Quant Nexus
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Trading Control Center
            </Typography>
            <Box sx={{ mt: 0.6 }}>
              <Chip size="small" label={`Role: ${role}`} color={role === "administrator" ? "primary" : "default"} />
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
