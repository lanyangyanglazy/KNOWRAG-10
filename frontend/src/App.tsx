import { useCallback, useEffect, useState } from "react";
import { api, ApiError, friendlyError } from "./api";
import type { FullUser, Stats, VisibleDoc } from "./types";
import { LoginView } from "./components/LoginView";
import { ChatView } from "./components/ChatView";
import { SkeletonLines } from "./components/Skeleton";

export default function App() {
  const [user, setUser] = useState<FullUser | null>(null);
  const [docs, setDocs] = useState<VisibleDoc[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loginMessage, setLoginMessage] = useState("");
  const [loading, setLoading] = useState(true);

  const enter = useCallback(async () => {
    try {
      const data = await api.me();
      setUser(data.user);
      setDocs(data.visible_docs);
      setStats(data.stats);
    } catch (err) {
      if ((err as ApiError)?.status === 401) {
        setUser(null);
      } else {
        setLoginMessage(friendlyError(err));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void enter();
  }, [enter]);

  const handleLogin = useCallback(
    async (username: string) => {
      try {
        await api.login(username);
        await enter();
      } catch (err) {
        setLoginMessage(friendlyError(err));
      }
    },
    [enter]
  );

  const handleLogout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* 忽略登出失败 */
    }
    setUser(null);
    setDocs([]);
    setStats(null);
  }, []);

  const handleSessionExpired = useCallback(() => {
    setUser(null);
    setDocs([]);
    setStats(null);
  }, []);

  if (loading) {
    return (
      <div className="login-shell">
        <div className="login-main">
          <div className="login-panel">
            <div className="panel-head">
              <h2>系统登录</h2>
              <p>正在校验会话…</p>
            </div>
            <div className="panel-body-skeleton">
              <SkeletonLines count={4} widths={["w-60", "w-92", "w-75", "w-88"]} />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!user) {
    return <LoginView onLogin={handleLogin} message={loginMessage} />;
  }

  return (
    <ChatView
      user={user}
      docs={docs}
      stats={stats ?? undefined}
      onLogout={handleLogout}
      onSessionExpired={handleSessionExpired}
    />
  );
}
