export class ApiError extends Error {
    status;
    payload;
    constructor(status, payload) {
        const code = payload?.error || `http_${status}`;
        const detail = payload?.message ? `：${payload.message}` : "";
        super(`${code}${detail}`);
        this.name = "ApiError";
        this.status = status;
        this.payload = payload;
    }
}
export function createApiRequest(getToken) {
    return async function apiRequest(path, options = {}) {
        const token = getToken().trim();
        if (!token) {
            throw new Error("请输入 http-channel.token");
        }
        const headers = new Headers(options.headers || {});
        headers.set("Authorization", `Bearer ${token}`);
        const response = await fetch(path, { ...options, headers });
        let payload = {};
        try {
            payload = await response.json();
        }
        catch (_error) {
            payload = {};
        }
        if (!response.ok) {
            throw new ApiError(response.status, payload);
        }
        return payload;
    };
}
