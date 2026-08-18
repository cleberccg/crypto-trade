import { Dialog, DialogContent, DialogTitle, Typography } from "@mui/material";

interface ExecutionDetailsDialogProps {
  open: boolean;
  onClose: () => void;
  payload: Record<string, unknown> | null;
}

export function ExecutionDetailsDialog({ open, onClose, payload }: ExecutionDetailsDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Detalhes da Execucao</DialogTitle>
      <DialogContent>
        <Typography component="pre" sx={{ whiteSpace: "pre-wrap", margin: 0 }}>
          {payload ? JSON.stringify(payload, null, 2) : "Sem dados"}
        </Typography>
      </DialogContent>
    </Dialog>
  );
}
