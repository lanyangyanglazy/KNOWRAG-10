import { useEffect, useState } from "react";
import { api, friendlyError } from "../api";
import type { PublicUser } from "../types";
import { SkeletonRows } from "./Skeleton";

export function LoginView({
  onLogin,
  message,
}: {
  onLogin: (username: string) => void;
  message: string;
}) {
  const [users, setUsers] = useState<PublicUser[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listUsers()
      .then((d) => setUsers(d.users))
      .catch((e) => setErr(friendlyError(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div id="login-view" className="login-shell">
      <a className="skip-link" href="#login-main">
        跳到主要内容
      </a>
      <div className="gov-strip" aria-hidden="true" />

      <header className="masthead">
        <div className="masthead-inner">
          <div className="brand">
            <div className="brand-mark" aria-hidden="true">
              政务
            </div>
            <div className="brand-text">
              <h1>政务知识问答平台</h1>
              <p>内部演示系统 · 检索增强问答</p>
            </div>
          </div>
          <p className="masthead-note">演示环境 · 请勿录入真实涉密信息</p>
        </div>
      </header>

      <main id="login-main" className="login-main">
        <section className="login-panel" aria-labelledby="login-title">
          <div className="panel-head">
            <h2 id="login-title">系统登录</h2>
            <p>
              请选择演示账号。系统将按账号所属部门与最高密级裁剪可检索文档。
            </p>
          </div>
          {message && (
            <p className="panel-alert" role="alert">
              {message}
            </p>
          )}

          <div className="login-body">
            <section className="login-intro" aria-labelledby="intro-title">
              <h3 id="intro-title">系统说明</h3>
              <p>
                本系统用于内部制度文件的检索问答。回答仅基于检索到的原文片段，并附引用出处；没有可依据的内容时直接拒答。
              </p>
              <dl className="rule-list">
                <div>
                  <dt>访问控制</dt>
                  <dd>密级不高于账号上限，且仅限本部门文档；公开文档全员可见。</dd>
                </div>
                <div>
                  <dt>检索原则</dt>
                  <dd>权限校验在检索之前完成，越权内容不会进入候选集。</dd>
                </div>
                <div>
                  <dt>审计留痕</dt>
                  <dd>每次问答记录用户、问题、命中片段、回答与模型版本。</dd>
                </div>
              </dl>
            </section>

            <section className="login-accounts" aria-labelledby="accounts-title">
              <div className="section-title">
                <h3 id="accounts-title">演示账号</h3>
                <span className="section-count">
                  {users.length ? `共 ${users.length} 个` : ""}
                </span>
              </div>
              <div className="table-wrap">
                <table className="account-table">
                  <caption className="sr-only">
                    演示账号列表，选择账号进入系统
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">姓名</th>
                      <th scope="col">部门</th>
                      <th scope="col">职务</th>
                      <th scope="col" className="col-action">
                        操作
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {loading ? (
                      <tr className="skeleton-cell">
                        <td colSpan={4}>
                          <SkeletonRows count={4} />
                        </td>
                      </tr>
                    ) : err ? (
                      <tr>
                        <td colSpan={4}>{err}</td>
                      </tr>
                    ) : (
                      users.map((u) => (
                        <tr key={u.username}>
                          <td data-label="姓名">{u.display_name}</td>
                          <td data-label="部门">{u.dept}</td>
                          <td data-label="职务">{u.role}</td>
                          <td className="col-action">
                            <button
                              type="button"
                              className="btn btn-primary"
                              onClick={() => onLogin(u.username)}
                            >
                              进入系统
                            </button>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
              <p className="table-note">
                进入系统后，左侧会列出该账号实际可见的文档清单。
              </p>
            </section>
          </div>

          <p className="notice-block">
            演示环境：账号与文档均为虚构，不涉及真实政务数据。
          </p>
        </section>
        <p className="login-footnote">
          政务知识问答平台 · v0.1.0 · 本地化部署，数据不出内网
        </p>
      </main>

      <footer className="site-footer">
        <div className="site-footer-inner">
          <span>仅限内部演示使用</span>
          <span>技术支持：信息中心</span>
        </div>
      </footer>
    </div>
  );
}
