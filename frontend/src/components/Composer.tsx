import { useState } from "react";
import type { KeyboardEvent } from "react";
import { isFinePointer } from "../utils";

const EXAMPLES = [
  "差旅住宿费标准是多少？",
  "年休假有几天？",
  "政务服务中心的办公时间？",
  "口令有什么要求？",
];

export function Composer({
  onSend,
  onStop,
  busy,
}: {
  onSend: (q: string) => void;
  onStop: () => void;
  busy: boolean;
}) {
  const [value, setValue] = useState("");

  const submit = () => {
    const v = value.trim();
    if (!v || busy) return;
    onSend(v);
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    if (!isFinePointer()) return; // 触屏设备保留换行，点“发送”提交
    e.preventDefault();
    submit();
  };

  return (
    <div className="composer-wrap">
      <div className="composer-inner">
        <div className="examples-row">
          <span className="examples-label">示例问题</span>
          <div className="examples">
            {EXAMPLES.map((q, i) => (
              <span key={i} style={{ display: "inline-flex", alignItems: "baseline", gap: 10 }}>
                {i > 0 && (
                  <span className="example-sep" aria-hidden="true">
                    |
                  </span>
                )}
                <button
                  type="button"
                  className="example"
                  disabled={busy}
                  onClick={() => onSend(q)}
                >
                  {q}
                </button>
              </span>
            ))}
          </div>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label className="sr-only" htmlFor="question">
            请输入问题
          </label>
          <textarea
            id="question"
            className="question-input"
            rows={3}
            maxLength={500}
            placeholder="请输入问题，例如：差旅住宿费标准是多少？"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="composer-foot">
            <p className="composer-hint">
              Enter 发送，Shift+Enter 换行 · 回答仅供内部参考，请以原文为准
            </p>
            <div className="composer-actions">
              <span className="counter">{`${value.length} / 500`}</span>
              {busy && (
                <button
                  type="button"
                  className="btn btn-outline"
                  onClick={onStop}
                >
                  停止生成
                </button>
              )}
              <button type="submit" className="btn btn-primary" disabled={busy}>
                发送
              </button>
            </div>
          </div>
        </form>
        <p className="audit-note">
          本次会话的提问、命中片段与回答将写入审计日志。
        </p>
      </div>
    </div>
  );
}
