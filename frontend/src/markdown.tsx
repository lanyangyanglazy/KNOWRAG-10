import React from "react";

// Markdown → React 节点。全部走 React 的受控元素与文本节点，不使用 innerHTML，
// 因此模型输出中的任何 HTML 都不会被当作标签解析（XSS 安全）。
// 链接仅放行 http(s) 与站内相对路径，并加 rel="noopener noreferrer"。

function safeUrl(url: string): boolean {
  return /^(https?:\/\/|\/)/i.test(url);
}

const INLINE_RE =
  /(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\*([^*]+)\*)|(\[([^\]]+)\]\((https?:\/\/[^)\s]+|[^)\s]+)\))/g;

function renderInline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  // 必须用独立实例：renderInline 会递归（处理 **加粗** 内部的行内语法），
  // 若共用带 g 标志的模块级正则，递归里的 lastIndex=0 会把外层循环重置回起点，
  // 导致外层反复匹配同一段文本 → 死循环卡死主线程。
  // 注意必须带上 "g" 标志，否则 exec 永远从 0 开始匹配，同样会死循环。
  const re = new RegExp(INLINE_RE.source, "g");
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) {
      out.push(<strong key={key++}>{renderInline(m[2])}</strong>);
    } else if (m[3]) {
      out.push(<code key={key++}>{m[4]}</code>);
    } else if (m[5]) {
      out.push(<em key={key++}>{renderInline(m[6])}</em>);
    } else if (m[7]) {
      const url = m[9];
      if (safeUrl(url)) {
        out.push(
          <a
            key={key++}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {m[8]}
          </a>
        );
      } else {
        out.push(m[8]);
      }
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function splitRow(row: string): string[] {
  let s = row.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

const HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;

// 块级解析：标题 / 列表 / 引用 / 代码块 / 表格 / 分隔线 / 段落（段落内保留原换行 <br>）。
export function renderMarkdown(src: string): React.ReactNode[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }

    const fence = line.match(/^```(.*)$/);
    if (fence) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // 跳过结束 ```
      blocks.push(
        <pre key={key++}>
          <code>{buf.join("\n")}</code>
        </pre>
      );
      continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = Math.min(h[1].length, 6);
      const Tag = HEADINGS[lvl - 1] as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key++}>{renderInline(h[2].trim())}</Tag>);
      i++;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      blocks.push(<hr key={key++} />);
      i++;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={key++}>{renderMarkdown(buf.join("\n"))}</blockquote>
      );
      continue;
    }

    const isTableHead =
      line.includes("|") &&
      i + 1 < lines.length &&
      lines[i + 1].includes("-") &&
      /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]);
    if (isTableHead) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (
        i < lines.length &&
        lines[i].includes("|") &&
        lines[i].trim()
      ) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      blocks.push(
        <table key={key++}>
          <thead>
            <tr>
              {header.map((c, ci) => (
                <th key={ci}>{renderInline(c)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {r.map((c, ci) => (
                  <td key={ci}>{renderInline(c)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      blocks.push(
        <ul key={key++}>
          {items.map((it, ii) => (
            <li key={ii}>{renderInline(it)}</li>
          ))}
        </ul>
      );
      continue;
    }

    if (/^\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+[.)]\s+/, ""));
        i++;
      }
      blocks.push(
        <ol key={key++}>
          {items.map((it, ii) => (
            <li key={ii}>{renderInline(it)}</li>
          ))}
        </ol>
      );
      continue;
    }

    // 段落：聚合连续普通行，行内以 <br> 保留原换行
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```/.test(lines[i]) &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !/^>\s?/.test(lines[i]) &&
      !/^[-*]\s+/.test(lines[i]) &&
      !/^\d+[.)]\s+/.test(lines[i]) &&
      !(
        lines[i].includes("|") &&
        i + 1 < lines.length &&
        lines[i + 1].includes("-") &&
        /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])
      )
    ) {
      para.push(lines[i]);
      i++;
    }
    if (para.length) {
      blocks.push(
        <p key={key++}>
          {para.map((ln, idx) => (
            <React.Fragment key={idx}>
              {idx > 0 && <br />}
              {renderInline(ln)}
            </React.Fragment>
          ))}
        </p>
      );
    }
  }

  return blocks;
}

export function Markdown({ source }: { source: string }) {
  return <>{renderMarkdown(source)}</>;
}
