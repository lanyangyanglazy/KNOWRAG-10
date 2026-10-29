import { useState } from "react";
import type { AssistantMessage } from "../types";
import { Markdown } from "../markdown";
import { Citations } from "./Citations";
import { copyText } from "../utils";

// 检索/生成中的骨架屏（流式首字到达前显示，避免空白）
function PendingSkeleton() {
  return (
    <div className="skeleton-lines" aria-hidden="true">
      <span className="skeleton-line w-100" />
      <span className="skeleton-line w-92" />
      <span className="skeleton-line w-78" />
    </div>
  );
}

export function AnswerCard({
  msg,
  onRetry,
}: {
  msg: AssistantMessage;
  onRetry?: (messageId: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  const contentClass = `content${msg.status === "pending" ? " pending" : ""}`;
  const cardClass = `answer-card${
    msg.status === "refusal" ? " is-refusal" : ""
  }${msg.status === "error" ? " is-error" : ""}`;

  const onCopy = () => {
    void copyText(msg.answer).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    });
  };

  const isError = msg.status === "error";

  return (
    <div className={cardClass}>
      {msg.notice && <p className="notice">{msg.notice}</p>}
      {msg.detail && <p className="detail">{msg.detail}</p>}
      <div className={contentClass}>
        {msg.status === "pending" ? (
          <PendingSkeleton />
        ) : (
          <Markdown source={msg.answer} />
        )}
      </div>
      <Citations items={msg.citations} />
      <div className="meta-line">
        <span className="meta-text">{msg.meta}</span>
        <span className="meta-tools">
          {isError && onRetry && (
            <button
              type="button"
              className="link-action"
              onClick={() => onRetry(msg.id)}
            >
              重试
            </button>
          )}
          {msg.canCopy && !isError && (
            <button type="button" className="link-action" onClick={onCopy}>
              {copied ? "已复制" : "复制回答"}
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
