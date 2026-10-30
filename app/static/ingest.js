(() => {
  const LEVEL_NAME = { 0: "公开", 1: "内部", 2: "秘密" };
  const LEVEL_CLASS = { 0: "lv0", 1: "lv1", 2: "lv2" };
  const STATUS_NAME = {
    uploaded: "已上传", scanning: "扫描中", parsing: "解析中",
    needs_review: "待复核", embedding: "向量化中", ready: "就绪",
    failed: "失败", quarantined: "已隔离",
    draft: "草稿", active: "已激活", inactive: "已下线", deleting: "清理中",
  };

  const $ = (id) => document.getElementById(id);
  let me = null;
  let currentVersion = null;
  let currentDoc = null;
  let currentTables = [];

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const levelName = (level) =>
    LEVEL_NAME[level] ?? (typeof level === "string" ? level : "未定级");
  const badge = (level) =>
    `<span class="badge ${LEVEL_CLASS[level] || "lv2"}">${esc(levelName(level))}</span>`;
  const fmtTime = (iso) => {
    if (!iso) return "—";
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? String(iso)
      : date.toLocaleString("zh-CN", { hour12: false });
  };
  const rangeStart = (range) => {
    const match = /^([A-Z]+)(\d+):/.exec(String(range || ""));
    return match ? Number(match[2]) : 1;
  };

  // FastAPI 校错时 detail 是数组；拼成可读中文，避免界面显示 [object Object]
  function formatDetail(detail) {
    if (Array.isArray(detail)) {
      return detail.map((entry) => {
        if (entry == null) return "";
        if (typeof entry !== "object") return String(entry);
        const loc = Array.isArray(entry.loc)
          ? entry.loc.filter((part) => part !== "body").join(".")
          : "";
        const msg = entry.msg || entry.message || "";
        if (loc && msg) return `${loc}：${msg}`;
        if (msg) return String(msg);
        if (loc) return loc;
        try { return JSON.stringify(entry); } catch (_) { return ""; }
      }).filter(Boolean).join("；");
    }
    if (detail && typeof detail === "object") {
      return detail.msg || detail.message || JSON.stringify(detail);
    }
    return detail ? String(detail) : "";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
    if (!response.ok) {
      const detail = data ? formatDetail(data.detail || data.code) : "";
      const error = new Error(detail || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = data && data.code;
      throw error;
    }
    return data;
  }

  // 提示条位于 #ingest-view 内；视图隐藏时先切过去，否则提示不可见
  function revealIngestView() {
    if (!$("ingest-view").classList.contains("hidden")) return;
    $("login-view").classList.add("hidden");
    $("chat-view").classList.add("hidden");
    $("ingest-view").classList.remove("hidden");
  }

  function showAlert(message, ok = false) {
    revealIngestView();
    const el = $("ingest-alert");
    el.textContent = message;
    el.classList.toggle("ok", ok);
    el.classList.remove("hidden");
  }
  function clearAlert() { $("ingest-alert").classList.add("hidden"); }

  // 视图切换

  async function open() {
    clearAlert();
    // 每次进入文档库都重置复核/回源状态，避免复用上一次残留的复核表单
    $("ingest-review-panel").classList.add("hidden");
    $("ingest-source-panel").classList.add("hidden");
    currentVersion = null;
    currentDoc = null;
    currentTables = [];
    try {
      me = await api("/api/me");
    } catch (err) {
      if (err.status === 401) return location.reload();
      return showAlert(err.message || "无法进入文档库");
    }
    $("ingest-me-name").textContent = me.user.display_name;
    $("ingest-me-meta").textContent =
      `${me.user.dept} · 最高密级 ${me.user.max_classification}`;
    $("login-view").classList.add("hidden");
    $("chat-view").classList.add("hidden");
    $("ingest-view").classList.remove("hidden");
    try { await loadLibrary(); } catch (err) { showAlert(err.message || "加载文档库失败"); }
  }

  function close() {
    $("ingest-view").classList.add("hidden");
    $("ingest-review-panel").classList.add("hidden");
    $("ingest-source-panel").classList.add("hidden");
    $("chat-view").classList.remove("hidden");
  }

  async function logout() {
    try { await api("/api/logout", { method: "POST" }); } catch (_) {}
    location.reload();
  }

  // 上传与任务

  const UPLOAD_ERRORS = {
    unsupported_type: "不支持的文件类型（仅 PDF/Word/Excel/Markdown）",
    too_large: "文件超过大小限制",
    duplicate_content: "该文件内容已存在，未重复入库",
    queue_full: "任务队列已满，请稍后重试",
    document_inactive: "目标文档已下线，不能追加版本",
    quarantined: "文件未通过安全扫描，已隔离",
  };
  const uploadError = (err) => UPLOAD_ERRORS[err.code] || err.message || "上传失败";

  async function submitUpload(event) {
    event.preventDefault();
    clearAlert();
    const file = $("ingest-file").files[0];
    if (!file) return showAlert("请先选择文件。");
    const form = new FormData();
    form.append("file", file);
    form.append("classification", $("ingest-classification").value);
    const effectiveDate = $("ingest-effective-date").value;
    if (effectiveDate) form.append("effective_date", effectiveDate);
    const target = $("ingest-version-target").value;
    const url = target
      ? `/api/documents/${encodeURIComponent(target)}/versions`
      : "/api/documents";
    $("ingest-submit").disabled = true;
    $("ingest-job-status").textContent = "正在上传…";
    try {
      const result = await api(url, { method: "POST", body: form });
      $("ingest-job-status").textContent = `任务 #${result.job_id} 已排队`;
      await followJob(result.job_id);
      event.target.reset();
      $("ingest-version-target").value = "";
      await loadLibrary();
      showAlert(`上传完成，任务 #${result.job_id} 已结束。`, true);
    } catch (err) {
      showAlert(uploadError(err));
    } finally {
      $("ingest-submit").disabled = false;
    }
  }

  async function followJob(jobId) {
    const progress = $("ingest-progress");
    progress.classList.remove("hidden");
    progress.value = 0;
    for (let i = 0; i < 400; i++) {
      const job = await api(`/api/jobs/${jobId}`);
      const label = { queued: "排队中", running: "执行中", done: "完成", failed: "失败" }[job.status] || job.status;
      $("ingest-job-status").textContent =
        `任务 #${jobId} · ${label} · 第 ${job.attempts} 次尝试`;
      progress.value = job.status === "done" ? 100 : Math.min(90, 10 + job.attempts * 20);
      if (job.status === "done") {
        progress.classList.add("hidden");
        return job;
      }
      if (job.status === "failed") {
        progress.classList.add("hidden");
        throw Object.assign(new Error(job.last_error || "任务失败"), { code: "job_failed" });
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    progress.classList.add("hidden");
    throw Object.assign(new Error("任务仍在执行，请稍后刷新文档库"), { code: "job_timeout" });
  }

  // 文档库

  async function loadLibrary() {
    const data = await api("/api/documents?page_size=100");
    const docs = data.items || [];
    $("ingest-library-empty").classList.toggle("hidden", docs.length > 0);
    $("ingest-library-body").innerHTML = docs.map((doc) => {
      // 已下线/清理中的文档不能追加版本或再次下线，只保留复核（及管理员删除）
      const closed = doc.status === "inactive" || doc.status === "deleting";
      const actions = [
        `<button type="button" class="link-action" data-action="review">复核</button>`,
      ];
      if (!closed) {
        actions.push(
          `<button type="button" class="link-action" data-action="version">新版本</button>`,
          `<button type="button" class="link-action" data-action="deactivate">下线</button>`,
        );
      }
      if (me && (me.user.roles || []).includes("admin")) {
        actions.push(`<button type="button" class="link-action" data-action="delete">删除</button>`);
      }
      return `<tr data-doc-id="${esc(doc.id)}">
        <td class="ingest-title-cell">${esc(doc.title)}</td>
        <td>${esc(doc.dept)}</td>
        <td>${badge(doc.classification)}</td>
        <td>${esc(STATUS_NAME[doc.version_status] || doc.version_status || "—")}</td>
        <td>v${esc(doc.version_no ?? "—")}</td>
        <td>${esc(fmtTime(doc.updated_at))}</td>
        <td class="ingest-row-actions">${actions.join("")}</td>
      </tr>`;
    }).join("");
    fillVersionTargets(docs);
    $("ingest-status").textContent = `共 ${data.total} 篇可见文档`;
  }

  function fillVersionTargets(docs) {
    const select = $("ingest-version-target");
    const previous = select.value;
    select.innerHTML = `<option value="">新建文档</option>` + docs
      .filter((doc) => doc.status !== "inactive" && doc.status !== "deleting")
      .map((doc) => `<option value="${esc(doc.id)}">${esc(doc.title)}（v${esc(doc.version_no ?? "—")}）</option>`)
      .join("");
    if ([...select.options].some((option) => option.value === previous)) {
      select.value = previous;
    }
  }

  function bindLibraryActions() {
    $("ingest-library-body").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      const row = event.target.closest("tr[data-doc-id]");
      if (!button || !row) return;
      const docId = row.getAttribute("data-doc-id");
      const action = button.getAttribute("data-action");
      if (action === "review") return openReview(docId);
      if (action === "version") {
        $("ingest-version-target").value = docId;
        $("ingest-file").focus();
        return showAlert("已选择追加版本，请挑选文件后提交。", true);
      }
      if (action === "deactivate") {
        if (!window.confirm("下线后该文档立即不可检索，确定继续？")) return;
        try {
          await api(`/api/documents/${encodeURIComponent(docId)}/deactivate`, { method: "POST" });
          await loadLibrary();
          showAlert("已下线。", true);
        } catch (err) { showAlert(err.message || "下线失败"); }
        return;
      }
      if (action === "delete") {
        if (!window.confirm("删除为逻辑删除，切片将异步清理，确定继续？")) return;
        try {
          await api(`/api/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
          await loadLibrary();
          showAlert("已提交删除。", true);
        } catch (err) { showAlert(err.message || "删除失败"); }
      }
    });
  }

  // 复核台

  async function openReview(docId) {
    clearAlert();
    try {
      const detail = await api(`/api/documents/${encodeURIComponent(docId)}`);
      const versions = detail.versions || [];
      if (!versions.length) return showAlert("该文档还没有版本。");
      currentVersion = versions[0].id;
      const data = await api(`/api/versions/${encodeURIComponent(currentVersion)}/review`);
      renderReview(data);
    } catch (err) {
      showAlert(err.message || "无法打开复核台");
    }
  }

  function renderReview(data) {
    currentDoc = data.document;
    currentTables = data.tables || [];
    const panel = $("ingest-review-panel");
    panel.classList.remove("hidden");
    const openItems = data.open_items || [];
    const items = openItems.map((item) =>
      `<li>${esc(item.kind)}${item.reason ? `：${esc(item.reason)}` : ""}</li>`).join("");
    $("ingest-review-summary").innerHTML = `
      <div class="ingest-review-head">
        <div>
          <h3 class="ingest-review-title">${esc(currentDoc.title)}</h3>
          <p class="ingest-hint">${esc(currentDoc.dept)} · 解析器 ${esc(data.parser)}
            · 状态 ${esc(STATUS_NAME[data.status] || data.status)} · 待办 ${openItems.length} 项
            ${currentDoc.classification === 2 ? " · 秘密级需双人确认" : ""}</p>
        </div>
        <button type="button" class="btn btn-outline btn-sm" id="ingest-close-review-btn">收起</button>
      </div>
      ${items ? `<ul class="ingest-open-items">${items}</ul>` : ""}`;
    $("ingest-close-review-btn").addEventListener("click", () => panel.classList.add("hidden"));

    $("ingest-review-tables").innerHTML = currentTables.length
      ? currentTables.map((table, index) => tableCard(table, index)).join("")
      : `<p class="ingest-hint">该版本没有表格，将按正文切片。</p>`;
    bindGrid();

    $("ingest-review-title").value = currentDoc.title || "";
    $("ingest-review-classification").value = String(currentDoc.classification ?? 1);
    $("ingest-review-effective-date").value = currentDoc.effective_date || "";
    renderReviewActions(data);
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function tableCard(table, index) {
    const start = rangeStart(table.range);
    const selected = new Set(table.header_rows || []);
    const rows = (table.grid || []).map((cells, offset) => {
      const absolute = start + offset;
      const isHeader = selected.has(absolute);
      return `<tr data-row="${absolute}" class="${isHeader ? "is-header" : ""}">
        <td class="ingest-pick"><input type="checkbox" ${isHeader ? "checked" : ""}></td>
        <th scope="row">${absolute}</th>
        ${cells.map((cell) => `<td>${esc(cell)}</td>`).join("")}
      </tr>`;
    }).join("");
    const candidates = (table.candidates || []).map((candidate) => {
      const startRow = Number(candidate.start_row);
      const depth = Number(candidate.depth);
      return `<button type="button" class="btn btn-outline btn-sm ingest-candidate"
        data-start="${startRow}" data-depth="${depth}">
        候选 ${startRow}–${startRow + depth - 1}（${Math.round(Number(candidate.confidence) * 100)}%）
      </button>`;
    }).join("");
    return `<article class="ingest-table-card" data-table-key="${esc(table.table_key)}" data-index="${index}">
      <header class="ingest-table-card-head">
        <strong>${esc(table.sheet || "工作表")} · ${esc(table.range)}</strong>
        <span class="ingest-hint">置信度 ${Math.round(Number(table.confidence) * 100)}% · ${esc(table.source)}</span>
        ${(table.flags || []).map((flag) => `<span class="ingest-flag">${esc(flag)}</span>`).join("")}
      </header>
      ${candidates ? `<div class="ingest-candidates">${candidates}</div>` : ""}
      <div class="ingest-table-wrap">
        <table class="ingest-table ingest-grid-table">
          <thead><tr><th>表头</th><th>行</th>${(table.columns || []).map((column) =>
            `<th>${esc(column.name)}${column.unit ? `（${esc(column.unit)}）` : ""}</th>`).join("")}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <label class="ingest-field">列名（逗号分隔，顺序与表格一致）
        <input type="text" data-role="columns"
               value="${esc((table.columns || []).map((column) => column.name).join(", "))}">
      </label>
    </article>`;
  }

  function bindGrid() {
    const host = $("ingest-review-tables");
    host.onclick = (event) => {
      const candidate = event.target.closest(".ingest-candidate");
      if (candidate) {
        const card = candidate.closest(".ingest-table-card");
        const start = Number(candidate.dataset.start);
        const depth = Number(candidate.dataset.depth);
        card.querySelectorAll("tbody tr").forEach((row) => {
          const absolute = Number(row.dataset.row);
          const on = absolute >= start && absolute < start + depth;
          row.querySelector("input[type=checkbox]").checked = on;
          row.classList.toggle("is-header", on);
        });
        return;
      }
      const row = event.target.closest("tbody tr");
      if (!row) return;
      const box = row.querySelector("input[type=checkbox]");
      if (event.target !== box) box.checked = !box.checked;
      row.classList.toggle("is-header", box.checked);
    };
    host.onchange = (event) => {
      if (!event.target.matches("tbody input[type=checkbox]")) return;
      const row = event.target.closest("tr");
      row.classList.toggle("is-header", event.target.checked);
    };
  }

  function renderReviewActions(data) {
    const roles = (me && me.user.roles) || [];
    const canReview = roles.includes("reviewer") || roles.includes("admin");
    const actionable = canReview && ["needs_review", "failed"].includes(data.status);
    const openItems = data.open_items || [];
    // 降密后文档密级已不是 2，但服务端仍要求对挂起的密级复核项再投一票
    const secretItem = openItems.some((item) =>
      item.kind === "classification" && item.payload &&
      (item.payload.reason === "secret_two_person" ||
        item.payload.reason === "secret_downgrade"));
    const secretPending = actionable && (currentDoc.classification === 2 || secretItem);
    // 服务端禁止 waive 任何密级复核项
    const hasOpenClassification = openItems.some((item) => item.kind === "classification");
    $("ingest-review-actions").innerHTML = `
      <button type="button" class="btn btn-primary btn-sm" id="ingest-confirm-btn"
        ${actionable ? "" : "disabled"}>确认结构</button>
      ${secretPending ? `<button type="button" class="btn btn-outline btn-sm" id="ingest-approve-btn">秘密级二次确认</button>` : ""}
      <label class="ingest-inline"><input type="checkbox" id="ingest-review-template"> 保存为版式模板</label>
      <span class="ingest-waive">
        <label class="ingest-inline" for="ingest-waive-reason">waive 理由</label>
        <input type="text" id="ingest-waive-reason" placeholder="填写后申请豁免"
               aria-label="waive 理由">
        <button type="button" class="btn btn-outline btn-sm" id="ingest-waive-btn"
          ${actionable && currentDoc.classification !== 2 && !hasOpenClassification ? "" : "disabled"}>申请豁免</button>
      </span>`;
    $("ingest-confirm-btn").addEventListener("click", submitConfirm);
    if ($("ingest-approve-btn")) $("ingest-approve-btn").addEventListener("click", submitApprove);
    if ($("ingest-waive-btn")) $("ingest-waive-btn").addEventListener("click", submitWaive);
  }

  function collectConfirmPayload() {
    const cards = [...$("ingest-review-tables").querySelectorAll(".ingest-table-card")];
    const tables = cards.map((card) => {
      const source = currentTables[Number(card.dataset.index)] || {};
      const headerRows = [...card.querySelectorAll("tbody tr")]
        .filter((row) => row.querySelector("input[type=checkbox]").checked)
        .map((row) => Number(row.dataset.row));
      const names = card.querySelector('[data-role="columns"]').value
        .split(/[,，、]/).map((part) => part.trim()).filter(Boolean);
      const columns = names.map((name, index) => {
        const base = (source.columns || [])[index] || {};
        return {
          key: base.key || `c${index + 1}`,
          name,
          dtype: base.dtype || "string",
          unit: base.unit || null,
        };
      });
      return {
        table_key: card.dataset.tableKey,
        header_rows: headerRows,
        columns,
        footer_rows: source.footer_rows || [],
      };
    });
    return {
      tables,
      meta: {
        title: $("ingest-review-title").value.trim() || null,
        classification: Number($("ingest-review-classification").value),
        effective_date: $("ingest-review-effective-date").value || null,
      },
      apply_template: $("ingest-review-template").checked,
    };
  }

  async function submitConfirm() {
    clearAlert();
    const payload = collectConfirmPayload();
    if (payload.tables.some((table) => table.header_rows.length === 0)) {
      return showAlert("每张表至少要勾选一行表头。");
    }
    const button = $("ingest-confirm-btn");
    button.disabled = true;
    try {
      const result = await api(`/api/versions/${encodeURIComponent(currentVersion)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await loadLibrary();
      if (result.status === "embedding") {
        showAlert("结构已确认，已入队向量化。", true);
        $("ingest-review-panel").classList.add("hidden");
      } else {
        showAlert(`已记录，仍有 ${result.open_items ?? 0} 项待复核。`, true);
        await openReview(currentDoc.id);
      }
    } catch (err) {
      showAlert(err.message || "确认失败");
    } finally {
      button.disabled = false;
    }
  }

  async function submitApprove() {
    if (!window.confirm("确认以当前账号作为第二名复核人？")) return;
    try {
      const result = await api(`/api/versions/${encodeURIComponent(currentVersion)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: "approve" }),
      });
      await loadLibrary();
      if (result.status === "embedding") {
        showAlert("双人确认完成，已入队向量化。", true);
        $("ingest-review-panel").classList.add("hidden");
      } else {
        showAlert(`仍需 ${result.pending_approvals ?? 1} 人确认。`, true);
        await openReview(currentDoc.id);
      }
    } catch (err) {
      showAlert(err.message || "二次确认失败");
    }
  }

  async function submitWaive() {
    const reason = $("ingest-waive-reason").value.trim();
    if (!reason) return showAlert("waive 必须填写理由。");
    try {
      const result = await api(`/api/versions/${encodeURIComponent(currentVersion)}/waive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      });
      await loadLibrary();
      if (result.status === "embedding") {
        showAlert("已豁免并开始向量化。", true);
        $("ingest-review-panel").classList.add("hidden");
      } else {
        showAlert(`已豁免，仍有 ${result.open_items ?? 0} 项待复核。`, true);
        await openReview(currentDoc.id);
      }
    } catch (err) {
      showAlert(err.message || "豁免失败");
    }
  }

  // 引用回源

  async function showSource(chunkId) {
    clearAlert();
    try {
      const data = await api(`/api/chunks/${encodeURIComponent(chunkId)}/source`);
      const locator = data.locator || {};
      const parts = [
        locator.sheet ? `工作表 ${locator.sheet}` : "",
        locator.cell_range ? `区域 ${locator.cell_range}` : "",
        locator.row_range ? `行 ${locator.row_range}` : "",
        locator.page ? `第 ${locator.page} 页` : "",
        Array.isArray(locator.heading_path) && locator.heading_path.length
          ? `章节 ${locator.heading_path.join(" > ")}` : "",
      ].filter(Boolean);
      $("ingest-source-body").innerHTML = `
        <p class="ingest-hint">${esc(data.filename)} · ${esc(locator.chunk_type || "正文")}</p>
        <p class="ingest-source-locator">${esc(parts.join(" · ") || "该切片没有可定位的结构信息")}</p>
        <pre class="ingest-source-content">${esc(data.content)}</pre>`;
      // 统一经 open() 进入文档库：会重置复核台状态，避免残留上一次的复核表单
      await open();
      $("ingest-source-panel").classList.remove("hidden");
      $("ingest-source-panel").scrollIntoView({ block: "start", behavior: "smooth" });
    } catch (err) {
      if (err.status === 401) return location.reload();
      showAlert(err.message || "无法定位原文");
    }
  }

  // 初始化

  function init() {
    $("open-ingest-btn").addEventListener("click", open);
    $("back-chat-btn").addEventListener("click", close);
    $("ingest-logout-btn").addEventListener("click", logout);
    $("ingest-refresh-btn").addEventListener("click", () => {
      loadLibrary().catch((err) => showAlert(err.message || "刷新失败"));
    });
    $("ingest-upload-form").addEventListener("submit", submitUpload);
    $("ingest-source-close-btn").addEventListener("click", () => {
      $("ingest-source-panel").classList.add("hidden");
    });
    bindLibraryActions();
    document.addEventListener("click", (event) => {
      const item = event.target.closest(".cite-item[data-chunk-id]");
      if (!item) return;
      const chunkId = item.getAttribute("data-chunk-id");
      if (chunkId) showSource(chunkId);
    });
  }

  window.IngestUI = { open, close, showSource };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
