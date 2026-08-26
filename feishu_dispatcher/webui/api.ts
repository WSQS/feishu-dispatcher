export class ApiError extends Error {
  readonly status: number;
  readonly payload: any;

  constructor(status: number, payload: any) {
    const code = payload?.error || `http_${status}`;
    const detail = payload?.message ? `：${payload.message}` : "";
    super(`${code}${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function createApiRequest(getToken: () => string) {
  return async function apiRequest<T = any>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const token = getToken().trim();
    if (!token) {
      throw new Error("请输入 http-channel.token");
    }
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(path, { ...options, headers });
    let payload: any = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    return payload as T;
  };
}

export function createApiClient(getToken: () => string) {
  const request = createApiRequest(getToken);

  return {
    request,

    async listProjects(): Promise<any[]> {
      const payload = await request("/api/projects");
      return Array.isArray(payload.items) ? payload.items : [];
    },

    async loadTreeChildren(project: string, path: string): Promise<any[]> {
      const query = new URLSearchParams({ path });
      const payload = await request(
        `/api/projects/${encodeURIComponent(project)}/tree/children?${query}`,
      );
      return Array.isArray(payload.entries) ? payload.entries : [];
    },

    async readFile(project: string, path: string, rev = "work"): Promise<any> {
      const query = new URLSearchParams({ path, rev });
      return request(
        `/api/projects/${encodeURIComponent(project)}/file?${query}`,
      );
    },
  };
}
