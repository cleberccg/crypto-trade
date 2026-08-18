import { createTheme } from "@mui/material";

export const theme = createTheme({
  palette: {
    mode: "dark",
    primary: { main: "#00d1a0" },
    secondary: { main: "#ffb86b" },
    background: {
      default: "#07131a",
      paper: "#10222b"
    },
    success: { main: "#3ddc97" },
    error: { main: "#ff5f72" },
    warning: { main: "#ffc857" }
  },
  typography: {
    fontFamily: '"Space Grotesk", "IBM Plex Sans", sans-serif',
    h4: { fontWeight: 700 },
    h5: { fontWeight: 600 },
    h6: { fontWeight: 600 }
  },
  shape: { borderRadius: 14 },
  components: {
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: "linear-gradient(160deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01))"
        }
      }
    }
  }
});
