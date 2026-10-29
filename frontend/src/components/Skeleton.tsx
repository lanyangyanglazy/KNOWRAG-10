// 加载骨架屏：用占位块代替空白，弱网下也能看出「正在加载」。
// 纯 CSS 动画，尊重 prefers-reduced-motion（已在 index.css 全局关闭动画）。

export function SkeletonLines({
  count = 3,
  widths,
}: {
  count?: number;
  widths?: string[];
}) {
  const w = widths ?? ["w-100", "w-92", "w-78", "w-85"];
  return (
    <div className="skeleton-lines" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <span
          key={i}
          className={`skeleton-line ${w[i % w.length]}`}
          style={{ animationDelay: `${i * 120}ms` }}
        />
      ))}
    </div>
  );
}

export function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div className="skeleton-rows" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <span key={i} className="skeleton-row" style={{ animationDelay: `${i * 90}ms` }} />
      ))}
    </div>
  );
}
