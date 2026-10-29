const esbuild = require("esbuild");
const path = require("path");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const entry = path.resolve("src/markdown.tsx");
esbuild.buildSync({
  entryPoints: [entry],
  bundle: true,
  format: "cjs",
  platform: "node",
  jsx: "automatic",
  outfile: path.resolve("test/.md_bundle.cjs"),
  logLevel: "error",
});
const mod = require(path.resolve("test/.md_bundle.cjs"));

const samples = {
  "纯文本": "普通文本",
  "加粗(关键)": "- **北京** 每人每天 500 元",
  "斜体+代码": "这是 *斜体* 与 `代码` 混排",
  "标题": "## 差旅住宿费标准",
  "表格": "| 城市 | 标准 |\n| --- | --- |\n| 北京 | 500 |\n| 上海 | 480 |",
  "无序列表": "- 甲\n- 乙",
  "有序列表": "1. 第一\n2. 第二",
  "引用": "> 注：以最新文件为准。",
  "代码块": "```python\nprint('示例')\n```",
  "链接": "参考 [政务服务中心](https://gov.example.gov.cn) 与 [内部页](/page)",
  "危险链接": "[恶意](javascript:alert(1))",
  "XSS尝试": "<script>alert(1)</script> 和 <img src=x onerror=alert(1)>",
  "组合": "## 标题\n\n根据办法：\n\n- 北京 **500** 元\n- 上海 480 元\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n> 注\n\n```py\nprint(1)\n```",
};

let fail = 0;
for (const [k, s] of Object.entries(samples)) {
  const t0 = Date.now();
  try {
    const html = renderToStaticMarkup(React.createElement(mod.Markdown, { source: s }));
    const ms = Date.now() - t0;
    const tag = (html.match(/<(\w+)[ >]/g) || []).map((x) => x.replace(/[< >]/g, "")).filter((x) => !["div","span","p","br","strong","em","code","a","h1","h2","h3","h4","ul","ol","li","blockquote","pre","table","thead","tbody","tr","th","td","hr"].includes(x));
    // 只检测未转义的真实标签/事件属性/危险协议；&lt;script&gt; 这类转义文本是安全的
    const danger = /<script|<img[^>]+onerror|href="javascript:/i.test(html);
    console.log(`${danger ? "!! " : "ok "} ${k}: ${html.length} 字符 / ${ms}ms${danger ? "  <<< 危险内容未净化!" : ""}`);
    if (danger) { fail++; console.log("     " + html.slice(0, 200)); }
  } catch (e) {
    fail++;
    console.log(`ERR ${k}: ${e.message}`);
  }
}
console.log(fail ? `\n失败 ${fail} 项` : "\n全部通过");
process.exit(fail ? 1 : 0);
