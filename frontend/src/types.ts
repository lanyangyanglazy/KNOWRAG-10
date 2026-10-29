// 与后端 app/server.py 的接口契约对齐（FastAPI，Cookie 会话）。

export interface PublicUser {
  username: string;
  display_name: string;
  dept: string;
  role: string;
  // 注意：登录前接口（/api/users）刻意不返回 max_classification，避免泄露权限。
}

export interface FullUser extends PublicUser {
  max_level: number;
  max_classification: string;
}

export interface VisibleDoc {
  title: string;
  dept: string;
  classification: string;
  effective_date?: string;
}

export interface Stats {
  llm_model: string;
  embed_model: string;
  docs: number;
  chunks: number;
  max_distance: number;
}

export interface Citation {
  rank: number;
  title: string;
  classification: string;
  dept: string;
  doc_id: string;
  effective_date?: string;
  preview: string;
  chunk_id?: string;
  source?: string;
}

// /api/chunks/{chunk_id} 返回的切片原文（权限已在服务端校验）
export interface ChunkDetail {
  chunk_id: string;
  doc_id: string;
  title: string;
  dept: string;
  classification: string;
  effective_date: string;
  source: string;
  text: string;
}

// ---------- SSE 流式事件（后端 _sse 帧的 JSON 体） ----------
export type ChatEvent =
  | { type: "rewrite"; to: string }
  | { type: "citations"; items: Citation[] }
  | { type: "token"; text: string }
  | { type: "refusal"; reason: string; message: string; visible_docs: number }
  | { type: "done"; answer: string; elapsed_ms: number; citations: Citation[] }
  | { type: "error"; message: string };

export type MsgStatus = "pending" | "streaming" | "done" | "refusal" | "error";

export interface UserMessage {
  id: string;
  role: "user";
  text: string;
  time: string;
}

export interface AssistantMessage {
  id: string;
  role: "assistant";
  status: MsgStatus;
  answer: string; // Markdown 原文，由 Markdown 组件渲染
  question: string; // 产生该回答的问题（用于「重试」）
  notice?: string;
  detail?: string;
  citations: Citation[];
  meta: string;
  canCopy: boolean;
}

export type ChatMessage = UserMessage | AssistantMessage;
