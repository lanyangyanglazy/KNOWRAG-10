import { useCallback, useEffect, useRef, useState } from "react";
import { api, friendlyError, streamChat } from "../api";
import type { ApiError } from "../api";
import type {
  AssistantMessage,
  ChatEvent,
  ChatMessage,
  UserMessage,
} from "../types";
import { clearLocalHistory, loadHistory, saveHistory } from "../storage";
import { fmtNumber, nowTime } from "../utils";

const REFUSAL_REASON: Record<string, string> = {
  no_visible_docs: "无可见文档",
  no_dense_hits: "未检索到内容",
  low_relevance: "无足够相关依据",
};

let msgSeq = 0;
const nextId = () => `m${++msgSeq}`;

export interface UseChatSession {
  messages: ChatMessage[];
  send: (question: string, opts?: { onSessionExpired?: () => void }) => void;
  stop: () => void;
  clearHistory: () => Promise<boolean>;
  /** 重新提问：复用该条回答对应的问题 */
  retry: (messageId: string) => void;
  busy: boolean;
  streamStatus: string;
  /** 正在从本地恢复历史（恢复期间不写入本地存储） */
  restoring: boolean;
}

export function useChatSession(username: string): UseChatSession {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [streamStatus, setStreamStatus] = useState("");
  const [restoring, setRestoring] = useState(true);

  const controllerRef = useRef<AbortController | null>(null);
  const activeIdRef = useRef<string | null>(null);
  const bufRef = useRef("");
  const frameRef = useRef(0);
  const saveTimerRef = useRef(0);

  // ---------- 本地历史：进入时恢复，并把轮次回填服务端，保证追问仍有上下文 ----------
  useEffect(() => {
    const restored = loadHistory(username);
    if (restored.length) {
      setMessages(restored);
      const turns = restored
        .filter(
          (m): m is AssistantMessage =>
            m.role === "assistant" && !!m.answer && m.status !== "error"
        )
        .map((m) => ({ question: m.question, answer: m.answer }));
      if (turns.length) {
        // 有本地历史 → 保留服务端上下文并回填轮次，让「那报销标准呢？」能续上
        void api.seedHistory(turns).catch(() => {});
      }
    } else {
      // 无本地历史 → 清空服务端上下文，避免出现界面不可见的历史
      void api.clearHistory().catch(() => {});
    }
    setRestoring(false);
  }, [username]);

  // ---------- 本地历史：变更后防抖落盘 ----------
  useEffect(() => {
    if (restoring) return;
    window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      saveHistory(username, messages);
    }, 400);
    return () => window.clearTimeout(saveTimerRef.current);
  }, [messages, username, restoring]);

  const patchAssistant = useCallback(
    (id: string, patch: Partial<AssistantMessage>) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === id && m.role === "assistant" ? { ...m, ...patch } : m
        )
      );
    },
    []
  );

  const paint = useCallback(() => {
    frameRef.current = 0;
    if (activeIdRef.current) {
      patchAssistant(activeIdRef.current, {
        answer: bufRef.current,
        status: "streaming",
      });
    }
  }, [patchAssistant]);

  const flushAnswer = useCallback(() => {
    if (frameRef.current) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
    }
    paint();
  }, [paint]);

  const scheduleAnswer = useCallback(() => {
    if (frameRef.current) return;
    frameRef.current = requestAnimationFrame(paint);
  }, [paint]);

  const stop = useCallback(() => {
    setStreamStatus("正在停止生成");
    controllerRef.current?.abort();
  }, []);

  const clearHistory = useCallback(async (): Promise<boolean> => {
    if (busy) return false;
    if (
      !window.confirm(
        "确定清空当前会话？清空后本页问答与服务端上下文将不可恢复。"
      )
    ) {
      return false;
    }
    try {
      await api.clearHistory();
      clearLocalHistory(username);
      setMessages([]);
      setStreamStatus("会话历史已清空");
      return true;
    } catch (err) {
      window.alert(friendlyError(err));
      return false;
    }
  }, [busy, username]);

  const send = useCallback(
    (
      question: string,
      opts?: { onSessionExpired?: () => void }
    ): void => {
      const q = question.trim();
      if (!q || busy) return;

      setBusy(true);
      setStreamStatus("正在检索并生成回答");

      const userMsg: UserMessage = {
        id: nextId(),
        role: "user",
        text: q,
        time: nowTime(),
      };
      const aId = nextId();
      const aMsg: AssistantMessage = {
        id: aId,
        role: "assistant",
        status: "pending",
        answer: "",
        question: q,
        citations: [],
        meta: "",
        canCopy: false,
      };
      setMessages((prev) => [...prev, userMsg, aMsg]);

      activeIdRef.current = aId;
      bufRef.current = "";

      const controller = new AbortController();
      controllerRef.current = controller;

      let gotToken = false;
      let finalAnswer = "";

      const onEvent = (ev: ChatEvent) => {
        switch (ev.type) {
          case "rewrite":
            patchAssistant(aId, { meta: `检索改写：${ev.to}` });
            break;
          case "citations":
            patchAssistant(aId, { citations: ev.items });
            break;
          case "token":
            gotToken = true;
            bufRef.current += ev.text;
            scheduleAnswer();
            break;
          case "refusal":
            flushAnswer();
            patchAssistant(aId, {
              status: "refusal",
              notice: "未能回答",
              detail: `${ev.message}\n原因：${
                REFUSAL_REASON[ev.reason] ?? ev.reason
              }；可见文档 ${ev.visible_docs} 篇。`,
              answer: "",
              meta: `未调用生成模型 · 拒答 · ${nowTime()}`,
            });
            setStreamStatus("未能回答：没有可依据的文档");
            break;
          case "done":
            finalAnswer = ev.answer || bufRef.current;
            flushAnswer();
            patchAssistant(aId, {
              status: "done",
              answer: finalAnswer,
              meta: `完成 · ${fmtNumber(ev.elapsed_ms)} ms · 引用 ${
                ev.citations.length
              } 段 · ${nowTime()}`,
              canCopy: !!finalAnswer,
            });
            setStreamStatus("回答已完成");
            break;
          case "error":
            flushAnswer();
            patchAssistant(aId, {
              status: "error",
              answer: ev.message,
              meta: `请求异常 · ${nowTime()}`,
            });
            setStreamStatus("回答失败");
            break;
        }
      };

      streamChat(q, controller.signal, onEvent)
        .catch((err: unknown) => {
          if (
            controller.signal.aborted ||
            (err as Error).name === "AbortError"
          ) {
            flushAnswer();
            if (!gotToken && !finalAnswer) {
              patchAssistant(aId, {
                answer: "已取消本次提问。",
                meta: `已停止生成 · ${nowTime()}`,
              });
            } else {
              patchAssistant(aId, {
                meta: `已停止生成 · ${nowTime()}`,
                canCopy: !!bufRef.current,
              });
            }
            setStreamStatus("已停止生成");
          } else if ((err as ApiError)?.status === 401) {
            opts?.onSessionExpired?.();
            setStreamStatus("会话已过期，请重新登录");
          } else {
            flushAnswer();
            patchAssistant(aId, {
              status: "error",
              answer: `请求失败：${friendlyError(err)}`,
              meta: `请求异常 · ${nowTime()}`,
            });
            setStreamStatus("请求失败");
          }
        })
        .finally(() => {
          controllerRef.current = null;
          activeIdRef.current = null;
          bufRef.current = "";
          if (frameRef.current) {
            cancelAnimationFrame(frameRef.current);
            frameRef.current = 0;
          }
          setBusy(false);
        });
    },
    [busy, patchAssistant, flushAnswer, scheduleAnswer]
  );

  // 重试：移除出错的那条回答及其用户提问，再重新提问
  const retry = useCallback(
    (messageId: string) => {
      if (busy) return;
      const target = messages.find(
        (m) => m.id === messageId && m.role === "assistant"
      );
      if (!target || target.role !== "assistant") return;
      const question = target.question;
      if (!question) return;
      setMessages((prev) => {
        const idx = prev.findIndex((m) => m.id === messageId);
        if (idx < 0) return prev;
        const next = prev.slice();
        next.splice(idx, 1);
        // 紧邻其上的用户提问一并移除，避免重试后出现重复提问
        const prevMsg = next[idx - 1];
        if (prevMsg && prevMsg.role === "user" && prevMsg.text === question) {
          next.splice(idx - 1, 1);
        }
        return next;
      });
      // 等 React 提交完状态再发，避免与删除操作竞争
      window.setTimeout(() => send(question), 0);
    },
    [busy, messages, send]
  );

  return {
    messages,
    send,
    stop,
    clearHistory,
    retry,
    busy,
    streamStatus,
    restoring,
  };
}
