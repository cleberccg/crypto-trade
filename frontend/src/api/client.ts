const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
const TOKEN_STORAGE_KEY = "dashboard_auth_token";
const ROLE_STORAGE_KEY = "dashboard_auth_role";
export const AUTH_REQUIRED_EVENT = "dashboard:auth-required";

let token: string | null = localStorage.getItem(TOKEN_STORAGE_KEY);
let role: string | null = localStorage.getItem(ROLE_STORAGE_KEY);

export const setToken = (newToken: string | null) => {
  token = newToken;
  if (newToken) {
    localStorage.setItem(TOKEN_STORAGE_KEY, newToken);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
};

export const setRole = (newRole: string | null) => {
  role = newRole;
  if (newRole) {
    localStorage.setItem(ROLE_STORAGE_KEY, newRole);
  } else {
    localStorage.removeItem(ROLE_STORAGE_KEY);
  }
};

export const getRole = (): string | null => role;

export const isAuthenticated = (): boolean => Boolean(token);

export const clearAuth = (): void => {
  setToken(null);
  setRole(null);
};

function handleUnauthorized(): void {
  clearAuth();
  window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
}

export const getApiBaseUrl = (): string => API_BASE_URL;

export const getAuthHeaders = (): Record<string, string> => {
  return token ? { Authorization: `Bearer ${token}` } : {};
};

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined
  });
  if (response.status === 401) {
    handleUnauthorized();
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(body)
  });
  if (response.status === 401) {
    handleUnauthorized();
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(body)
  });
  if (response.status === 401) {
    handleUnauthorized();
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function wsUrl(path: string): string {
  const base = API_BASE_URL.replace("http://", "ws://").replace("https://", "wss://");
  return `${base}${path}`;
}

export function wsUrlWithAuth(path: string): string {
  const baseUrl = wsUrl(path);
  if (!token) {
    return baseUrl;
  }

  const separator = baseUrl.includes("?") ? "&" : "?";
  return `${baseUrl}${separator}token=${encodeURIComponent(token)}`;
}
