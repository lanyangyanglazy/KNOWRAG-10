import React from "react";
import type { ChatMessage } from "../types";
import { AnswerCard } from "./AnswerCard";

function MessageItemImpl({
  msg,
  onRetry,
}: {
  msg: ChatMessage;
  onRetry?: (messageId: string) => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="msg user">
        <div className="msg-label" aria-hidden="true">
          问
        </div>
        <div className="msg-body">{msg.text}</div>
      </div>
    );
  }
  return (
    <div className="msg assistant">
      <div className="msg-label" aria-hidden="true">
        答
      </div>
      <div className="msg-body">
        <AnswerCard msg={msg} onRetry={onRetry} />
      </div>
    </div>
  );
}

// 流式更新只改当前助手消息，用 memo 避免其它消息随每帧重渲染。
export const MessageItem = React.memo(MessageItemImpl);
