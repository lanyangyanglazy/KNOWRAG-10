import { useEffect, useRef, useState } from "react";
import { api, friendlyError } from "../api";
import type { ChunkDetail, Citation } from "../types";
import { Badge } from "./Badge";
import { cleanPreview, copyText } from "../utils";

// ---------- 原文弹窗 ----------
export function ChunkModal({
  chunkId,
  onClose,
}: {
  chunkId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ChunkDetail | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!chunkId) {
      setDetail(null);
      setErr("");
      return;
    }
    let alive = true;
    setLoading(true);
    setErr("");
    api
      .chunk(chunkId)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setErr(friendlyError(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [chunkId]);

  // Esc 关闭 + 焦点移入面板
  useEffect(() => {
    if (!chunkId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    panelRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [chunkId, onClose]);

  if (!chunkId) return null;

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chunk-modal-title"
        tabIndex={-1}
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3 id="chunk-modal-title" className="modal-title">
            {detail ? detail.title : "引用原文"}
          </h3>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </div>

        <div className="modal-body">
          {loading && (
            <div className="skeleton-lines" aria-hidden="true">
              <span className="skeleton-line w-90" />
              <span className="skeleton-line w-100" />
              <span className="skeleton-line w-75" />
              <span className="skeleton-line w-85" />
            </div>
          )}
          {!loading && err && <p className="modal-error">{err}</p>}
          {!loading && !err && detail && (
            <>
              <div className="modal-meta">
                <Badge classification={detail.classification} />
                <span>{detail.dept}</span>
                <span>文号 {detail.doc_id}</span>
                <span>生效 {detail.effective_date || "—"}</span>
              </div>
              <pre className="chunk-text">{detail.text}</pre>
            </>
          )}
        </div>

        {!loading && !err && detail && (
          <div className="modal-foot">
            <button
              type="button"
              className="btn btn-outline btn-sm"
              onClick={() => void copyText(detail.text)}
            >
              复制原文
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={onClose}
            >
              关闭
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- 引用卡 ----------
function CitationItem({
  item,
  onOpenSource,
}: {
  item: Citation;
  onOpenSource: (chunkId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const preview = cleanPreview(item.preview);

  const onCopy = () => {
    const text = [
      `【${item.title}】`,
      `文号：${item.doc_id}`,
      `部门：${item.dept}`,
      `密级：${item.classification}`,
      `生效日期：${item.effective_date || "—"}`,
      item.chunk_id ? `切片：${item.chunk_id}` : "",
      `原文：${preview}`,
    ]
      .filter(Boolean)
      .join("\n");
    void copyText(text).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    });
  };

  return (
    <div className={`cite-item${expanded ? " is-expanded" : ""}`}>
      <div className="cite-index">[{item.rank}]</div>
      <div className="cite-body">
        <div className="cite-title-row">
          <span className="cite-title">{item.title}</span>
          <Badge classification={item.classification} />
        </div>
        <div className="cite-meta">
          {`${item.dept} · 文号 ${item.doc_id} · 生效日期 ${
            item.effective_date || "—"
          }`}
        </div>
        <div className="cite-preview">{preview}</div>

        <div className="cite-actions">
          <button
            type="button"
            className="link-action"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
          >
            {expanded ? "收起" : "展开"}
          </button>
          {item.chunk_id && (
            <button
              type="button"
              className="link-action"
              onClick={() => onOpenSource(item.chunk_id as string)}
            >
              查看原文
            </button>
          )}
          <button type="button" className="link-action" onClick={onCopy}>
            {copied ? "已复制" : "复制引用"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Citations({ items }: { items: Citation[] }) {
  const [openChunk, setOpenChunk] = useState<string | null>(null);
  if (!items.length) return null;
  return (
    <>
      <div className="citations">
        <div className="cite-head">
          <span>引用依据</span>
          <span className="cite-count">{`共 ${items.length} 段`}</span>
        </div>
        <div className="cite-list">
          {items.map((it) => (
            <CitationItem key={it.rank} item={it} onOpenSource={setOpenChunk} />
          ))}
        </div>
      </div>
      <ChunkModal chunkId={openChunk} onClose={() => setOpenChunk(null)} />
    </>
  );
}
