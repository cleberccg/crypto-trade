export interface PaginatedMeta {
  page: number;
  page_size: number;
  total: number;
}

export interface PaginatedResponse<T extends Record<string, unknown>> {
  meta: PaginatedMeta;
  items: T[];
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  role: string;
}
