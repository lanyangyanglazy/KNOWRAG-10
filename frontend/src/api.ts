import type {
  ChatEvent,
  ChunkDetail,
  Citation,
  FullUser,
  PublicUser,
  Stats,
  VisibleDoc,
} from "./types";

// 同源调用：开发期由 Vite 代理 /api 到 FastAPI（127.0.0.1:8000），生产期由后端直接托管。
// Cookie 会话（HttpOnly / SameSite=Lax），必须带 credentials: "include" 才能回传。

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: string };
      detail = data.detail || detail;
    } catch {
      /* 忽略解析失败 */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export const api = {
  listUsers: () => request<{ users: PublicUser[] }>("/api/users"),

  login: (username: string) =>
    request<{ user: FullUser }>("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password: "" }),
    }),

  me: () =>
    request<{ user: FullUser; visible_docs: VisibleDoc[]; stats: Stats }>(
      "/api/me"
    ),

  logout: () => request<{ ok: true }>("/api/logout", { method: "POST" }),

  clearHistory: () =>
    request<{ ok: true }>("/api/history/clear", { method: "POST" }),

  // 回填历史轮次（本地历史恢复后同步服务端上下文，供追问改写使用）
  seedHistory: (turns: { question: string; answer: string }[]) =>
    request<{ ok: true; seeded: number }>("/api/history/seed", {
      method: "POST",
      body: JSON.stringify({ turns }),
    }),

  // 切片原文（服务端已做 can_view 权限判定，不可见时返回 404）
  chunk: (chunkId: string) =>
    request<ChunkDetail>(`/api/chunks/${encodeURIComponent(chunkId)}`),
};

// 会话失效（401）的友好文案
export function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.name === "AbortError") return "已停止生成。";
    if (err.status === 401) return "会话已过期，请重新登录。";
    if (err.status) return err.message || `请求失败（HTTP ${err.status}）。`;
  }
  return "无法连接服务，请确认后端已启动（uvicorn）且网络正常。";
}

// 后端 SSE：逐块读取并解析 `data: {json}\n\n` 帧，按 type 分发给回调。
export async function streamChat(
  question: string,
  signal: AbortSignal,
  onEvent: (ev: ChatEvent) => void
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: string };
      detail = data.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (!res.body) throw new ApiError("后端未返回流式响应");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data:")) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
      } catch {
        /* 跳过无法解析的帧 */
      }
    }
  }
}

export type { Citation };
