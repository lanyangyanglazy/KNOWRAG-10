import type { FullUser, Stats, VisibleDoc } from "../types";
import { useChatSession } from "../hooks/useChat";
import { Rail } from "./Rail";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";

export function ChatView({
  user,
  docs,
  stats,
  onLogout,
  onSessionExpired,
}: {
  user: FullUser;
  docs: VisibleDoc[];
  stats?: Stats;
  onLogout: () => void;
  onSessionExpired: () => void;
}) {
  const {
    messages,
    send,
    stop,
    clearHistory,
    retry,
    busy,
    streamStatus,
  } = useChatSession(user.username);

  const handleSend = (q: string) => send(q, { onSessionExpired });

  return (
    <div id="chat-view" className="app-shell">
      <a className="skip-link" href="#chat-main">
        跳到主要内容
      </a>
      <div className="gov-strip" aria-hidden="true" />

      <header className="masthead masthead-app">
        <div className="masthead-inner">
          <div className="brand">
            <div className="brand-mark brand-mark-sm" aria-hidden="true">
              政务
            </div>
            <div className="brand-text">
              <h1 className="app-title">政务知识问答平台</h1>
            </div>
          </div>
          <div className="masthead-actions">
            <div className="identity">
              <span className="identity-name">{user.display_name}</span>
              <span className="identity-meta">{`${user.dept} · ${user.role} · 最高密级 ${user.max_classification}`}</span>
            </div>
            <button
              type="button"
              className="btn btn-outline btn-sm"
              onClick={onLogout}
            >
              退出登录
            </button>
          </div>
        </div>
      </header>

      <div className="statusbar">
        <div className="statusbar-inner">
          <nav className="breadcrumb" aria-label="当前位置">
            首页 <span aria-hidden="true">/</span> 知识问答
          </nav>
          {import.meta.env.DEV && (
            <p className="system-status">
              开发调试模式 · 内部运行指标已在交付版脱敏（load_issues: {stats?.load_issues?.length ?? 0}）
            </p>
          )}
        </div>
      </div>

      <div className="workspace">
        <Rail user={user} docs={docs} loading={!docs.length} />
        <main id="chat-main" className="chat-main">
          <div className="chat-head">
            <div>
              <h2>知识问答</h2>
              <p>仅基于您有权查看的制度文件回答；没有依据时直接拒答。</p>
            </div>
            <div className="chat-head-actions">
              <p className="mobile-identity">{`${user.display_name} · ${user.dept} · 最高密级 ${user.max_classification} · 可见文档 ${docs.length} 篇`}</p>
              <button
                type="button"
                className="btn btn-outline btn-sm print-btn"
                onClick={() => window.print()}
              >
                打印
              </button>
              <button
                type="button"
                className="btn btn-outline btn-sm"
                disabled={busy}
                onClick={() => void clearHistory()}
              >
                清空会话
              </button>
            </div>
          </div>

          <MessageList
            messages={messages}
            busy={busy}
            streamStatus={streamStatus}
            onRetry={retry}
          />
          <Composer onSend={handleSend} onStop={stop} busy={busy} />
        </main>
      </div>
    </div>
  );
}
