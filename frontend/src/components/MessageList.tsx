import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { MessageItem } from "./MessageItem";

function EmptyState() {
  return (
    <div className="empty-state">
      <div className="empty-rule" aria-hidden="true" />
      <h3>请输入您的问题</h3>
      <p>系统会在您有权查看的制度文件中检索，并给出带出处的回答。</p>
      <ul className="empty-list">
        <li>回答仅基于检索到的原文，不编造。</li>
        <li>超出权限或知识库范围时，将直接拒答。</li>
        <li>每次问答均写入审计日志，可追溯。</li>
      </ul>
    </div>
  );
}

export function MessageList({
  messages,
  busy,
  streamStatus,
  onRetry,
}: {
  messages: ChatMessage[];
  busy: boolean;
  streamStatus: string;
  onRetry?: (messageId: string) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const isNearBottom = (threshold = 80) => {
    const b = boxRef.current;
    if (!b) return true;
    return b.scrollHeight - b.scrollTop - b.clientHeight < threshold;
  };

  const scrollBottom = () => {
    const b = boxRef.current;
    if (b) {
      b.scrollTop = b.scrollHeight;
      setShowScrollBtn(false);
    }
  };

  // 新消息或流式内容更新时：原本在底部则跟随，否则提示回到底部
  useEffect(() => {
    if (nearBottomRef.current) scrollBottom();
    else setShowScrollBtn(true);
  }, [messages]);

  const onScroll = () => {
    const b = boxRef.current;
    if (!b) return;
    nearBottomRef.current = isNearBottom();
    setShowScrollBtn(!nearBottomRef.current);
  };

  return (
    <div className="messages-wrap">
      <div
        id="messages"
        className="messages"
        role="log"
        aria-live="polite"
        aria-busy={busy}
        ref={boxRef}
        onScroll={onScroll}
      >
        {messages.length ? (
          messages.map((m) => (
            <MessageItem key={m.id} msg={m} onRetry={onRetry} />
          ))
        ) : (
          <EmptyState />
        )}
      </div>
      {showScrollBtn && (
        <button
          type="button"
          className="scroll-bottom"
          aria-label="回到底部"
          onClick={scrollBottom}
        >
          回到底部
        </button>
      )}
      <div className="sr-only" aria-live="polite">
        {streamStatus}
      </div>
    </div>
  );
}
