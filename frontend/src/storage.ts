import type { ChatMessage } from "./types";

// 本地历史：按用户名分桶存放（避免共用终端上的账号互相看到对方的问答）。
// 只存本机 localStorage，不涉及跨设备同步。

const PREFIX = "rag.chat.v1";
const MAX_MESSAGES = 40;

export function historyKey(username: string): string {
  return `${PREFIX}.${username}`;
}

export function loadHistory(username: string): ChatMessage[] {
  try {
    const raw = window.localStorage.getItem(historyKey(username));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(-MAX_MESSAGES) as ChatMessage[];
  } catch {
    // 隐私模式 / 配额异常 / 数据损坏：静默降级为「无历史」
    return [];
  }
}

export function saveHistory(username: string, messages: ChatMessage[]): void {
  try {
    window.localStorage.setItem(
      historyKey(username),
      JSON.stringify(messages.slice(-MAX_MESSAGES))
    );
  } catch {
    // 配额超限：不影响主流程
  }
}

export function clearLocalHistory(username: string): void {
  try {
    window.localStorage.removeItem(historyKey(username));
  } catch {
    /* ignore */
  }
}
