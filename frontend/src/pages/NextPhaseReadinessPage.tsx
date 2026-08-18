import { Box, Alert, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function NextPhaseReadinessPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Next Phase Readiness</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        Modo seguro ativo: esta tela apenas prepara ativacao para amanha. Nenhuma automacao foi habilitada.
      </Alert>
      <DataTablePage
        title="Readiness Checks"
        endpoint="/next-phase/readiness"
        columns={["id", "component", "status", "details", "activation_required", "checked_at"]}
      />
      <Box sx={{ mt: 3 }}>
        <DataTablePage
          title="Activation Plan (Dry Run)"
          endpoint="/next-phase/activation-plan"
          columns={["id", "name", "description", "enabled", "dry_run_only", "updated_at"]}
        />
      </Box>
    </Box>
  );
}
