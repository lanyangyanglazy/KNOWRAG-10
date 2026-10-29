// 通用工具函数

export function nowTime(): string {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export function fmtNumber(n: number): string {
  return new Intl.NumberFormat("zh-CN").format(n);
}

export function isFinePointer(): boolean {
  return window.matchMedia("(pointer: fine)").matches;
}

// 引用预览来自切分后的原文，去掉标题井号、加粗和行内代码等 Markdown 记号
export function cleanPreview(text: string): string {
  return String(text || "")
    .replace(/(^|\s)#{1,6}\s+/g, " ")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    /* 降级到 execCommand */
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } catch {
    /* 忽略复制失败 */
  }
  ta.remove();
}
