import { Box, Button, Paper, Stack, TextField, Typography } from "@mui/material";
import { useState } from "react";

import { apiPost, setRole, setToken } from "../api/client";
import type { LoginResponse } from "../api/types";

interface LoginPageProps {
  onAuthenticated: () => void;
}

export function LoginPage({ onAuthenticated }: LoginPageProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>("");

  const submit = async () => {
    try {
      const response = await apiPost<LoginResponse>("/auth/login", {
        username,
        password
      });
      setToken(response.access_token);
      setRole(response.role ?? "read-only");
      onAuthenticated();
    } catch {
      setError("Falha de autenticação");
    }
  };

  return (
    <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center", p: 2 }}>
      <Paper sx={{ width: "100%", maxWidth: 420, p: 3 }}>
        <Typography variant="h5" sx={{ mb: 2 }}>
          Acesso ao Dashboard
        </Typography>
        <Stack spacing={2}>
          <TextField autoComplete="username" label="Usuário" value={username} onChange={(e) => setUsername(e.target.value)} />
          <TextField autoComplete="current-password" label="Senha" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          <Button variant="contained" onClick={submit}>
            Entrar
          </Button>
          {error ? <Typography color="error">{error}</Typography> : null}
        </Stack>
      </Paper>
    </Box>
  );
}
