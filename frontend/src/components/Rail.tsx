import type { FullUser, VisibleDoc } from "../types";
import { Badge } from "./Badge";
import { SkeletonRows } from "./Skeleton";

export function Rail({
  user,
  docs,
  loading,
}: {
  user: FullUser;
  docs: VisibleDoc[];
  loading?: boolean;
}) {
  return (
    <aside className="rail" aria-label="身份与可见范围">
      <section className="rail-section">
        <h2 className="rail-title">当前身份</h2>
        <dl className="identity-list">
          <div>
            <dt>用户</dt>
            <dd>{user.display_name}</dd>
          </div>
          <div>
            <dt>部门</dt>
            <dd>{user.dept}</dd>
          </div>
          <div>
            <dt>职务</dt>
            <dd>{user.role}</dd>
          </div>
          <div>
            <dt>最高密级</dt>
            <dd>
              <Badge classification={user.max_classification} />
            </dd>
          </div>
        </dl>
      </section>

      <section className="rail-section">
        <div className="rail-title-row">
          <h2 className="rail-title">可见文档</h2>
          <span className="count">{`${docs.length} 篇`}</span>
        </div>
        <p className="rail-note">
          检索前按密级与部门裁剪，越权文档不会进入候选集。
        </p>
        <ul className="doc-list">
          {loading ? (
            <li>
              <SkeletonRows count={3} />
            </li>
          ) : docs.length ? (
            docs.map((d, i) => (
              <li key={i}>
                <div className="doc-title">{d.title}</div>
                <div className="doc-meta">
                  <span>{d.dept}</span>
                  <span>{d.effective_date ?? ""}</span>
                  <Badge classification={d.classification} />
                </div>
              </li>
            ))
          ) : (
            <li>
              <div className="doc-meta">无可见文档</div>
            </li>
          )}
        </ul>
      </section>

      <section className="rail-section rail-section-last">
        <h2 className="rail-title">安全说明</h2>
        <p className="rail-note">
          回答仅供内部参考，请以原文为准。所有问答写入审计日志，包含命中片段的密级信息。
        </p>
      </section>
    </aside>
  );
}
