/* Admin request history: metadata pages, on-demand details and local media. */
const logView = {
  page: 0, size: 20, total: 0, rows: [], active: false, busy: false,
  timer: null, sequence: 0, controller: null,
  detailSequence: 0, detailController: null, detailCache: new Map(), returnFocus: null,
  previewSequence: 0,
};
const logTypes = { image: "图片", video: "视频", speech: "语音" };
const logStates = { completed: "已完成", processing: "处理中", failed: "失败", unknown: "结果待确认" };
const logStatusText = {
  pending: "等待处理", selecting_account: "正在选择账号", processing: "处理中",
  completed: "已完成", failed: "生成失败", download_failed: "媒体下载失败",
  cancelled: "请求已取消", unknown: "结果待确认", no_available_accounts: "暂无可用账号",
  no_eligible_account: "暂无可用账号", internal_error: "内部服务错误",
};

const logDate = (value) => {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return ["-", ""];
  return [date.toLocaleDateString(I18n.locale), date.toLocaleTimeString(I18n.locale, { hour12: false })];
};
const logDuration = (value) => value == null || !Number.isFinite(Number(value)) ? "-" : `${Number(value).toFixed(1)}s`;
const logStateLabel = (log) => I18n.t(log.state === "processing" && log.status_code === 202 ? "已受理" : (logStates[log.state] || "处理中"));
const logStateBadge = (log) => {
  const state = Object.hasOwn(logStates, log.state) ? log.state : "processing";
  return `<span class="log-status log-status--${state}">${esc(logStateLabel(log))}</span>`;
};

const setLogFeedback = (message = "", error = false) => {
  const node = document.getElementById("logsFeedback");
  node.textContent = message;
  node.classList.toggle("hidden", !message);
  node.classList.toggle("text-destructive", error);
};

const scheduleLogRefresh = () => {
  clearTimeout(logView.timer);
  logView.timer = null;
  if (logView.active && !document.hidden && document.getElementById("logsAutoRefresh").checked) {
    logView.timer = setTimeout(() => loadLogs(), 15000);
  }
};
const setLogsActive = (active) => {
  logView.active = active;
  clearTimeout(logView.timer);
  logView.timer = null;
  if (active) loadLogs();
};
const toggleLogAutoRefresh = () => scheduleLogRefresh();

const renderLogPage = () => {
  const pages = Math.max(1, Math.ceil(logView.total / logView.size));
  const start = logView.total ? logView.page * logView.size + 1 : 0;
  document.getElementById("logsRange").textContent = I18n.t("共 {total} 条 · 显示 {start}–{end} 条", {
    total: logView.total, start, end: Math.min(logView.total, logView.page * logView.size + logView.rows.length),
  });
  document.getElementById("logsPage").textContent = `${logView.page + 1} / ${pages}`;
  document.getElementById("logsPrevBtn").disabled = logView.busy || logView.page === 0;
  document.getElementById("logsNextBtn").disabled = logView.busy || logView.page + 1 >= pages;
  document.getElementById("logsRefreshBtn").disabled = logView.busy;
};

const renderLogs = () => {
  const body = document.getElementById("logTableBody");
  if (!logView.rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="text-center py-12 text-muted-foreground">${esc(I18n.t(logView.total ? "暂无日志" : "没有符合条件的日志"))}</td></tr>`;
    return;
  }
  body.innerHTML = logView.rows.map((log) => {
    const [date, time] = logDate(log.created_at);
    const generation = log.generation_id || "";
    const shortId = generation.length > 29 ? `${generation.slice(0, 17)}…${generation.slice(-8)}` : generation;
    const percentage = Math.max(0, Math.min(99, Number(log.progress) || 0));
    const showProgress = log.state === "processing" && log.status_code !== 202;
    const status = log.status_code === 202 && log.state === "processing"
      ? I18n.t("请求已返回，生成结果以回执为准")
      : I18n.t(logStatusText[log.status_text] || log.status_text || "等待处理");
    const account = log.account_id == null ? I18n.t("未分配账号") : (log.account_email || I18n.t("已删除账号"));
    return `<tr>
      <td><div class="font-mono text-xs">${esc(time)}</div><div class="log-subtext">${esc(date)}</div></td>
      <td><span class="capability-badge">${esc(I18n.t(logTypes[log.operation] || log.operation || "-"))}</span></td>
      <td><div class="truncate text-xs" title="${esc(account)}">${esc(account)}</div><div class="log-subtext">${log.account_id == null ? "—" : `#${esc(log.account_id)}`}</div></td>
      <td title="${esc(status)}">${logStateBadge(log)}<span class="log-subtext ml-2 font-mono">${log.status_code ? `HTTP ${esc(log.status_code)}` : `${percentage}%`}</span>
        ${showProgress ? `<div class="log-progress" role="progressbar" aria-label="${esc(I18n.t("进度"))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percentage}"><span style="width:${percentage}%"></span></div>` : `<div class="log-subtext truncate">${esc(status)}</div>`}
      </td>
      <td class="font-mono text-xs">${esc(logDuration(log.duration))}</td>
      <td><div class="font-mono text-xs truncate" title="${esc(generation)}">${esc(shortId || "—")}</div><div class="log-subtext">#${esc(log.id)}</div></td>
      <td class="text-right"><button class="icon-btn" data-log-detail="${esc(log.id)}" title="${esc(I18n.t("详情"))}" aria-label="${esc(I18n.t("详情"))}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/></svg></button></td>
    </tr>`;
  }).join("");
};

async function loadLogs() {
  clearTimeout(logView.timer);
  logView.controller?.abort();
  const controller = new AbortController();
  logView.controller = controller;
  const sequence = ++logView.sequence;
  logView.busy = true;
  renderLogPage();
  setLogFeedback(I18n.t("正在加载日志…"));
  const params = new URLSearchParams({
    limit: logView.size, offset: logView.page * logView.size,
    query: document.getElementById("logsQuery").value.trim(),
    operation: document.getElementById("logsOperation").value,
    status: document.getElementById("logsStatus").value,
    days: document.getElementById("logsDays").value,
  });
  try {
    const response = await apiRequest(`/api/logs?${params}`, { signal: controller.signal });
    if (!response) return;
    if (!response.ok) throw new Error("logs_unavailable");
    const data = await response.json();
    if (sequence !== logView.sequence) return;
    logView.total = Number(data.total) || 0;
    if (logView.page > 0 && logView.page * logView.size >= logView.total) {
      logView.page = Math.max(0, Math.ceil(logView.total / logView.size) - 1);
      return loadLogs();
    }
    logView.rows = data.data || [];
    for (const log of logView.rows) {
      const cached = logView.detailCache.get(log.id);
      if (cached && cached.updated_at !== log.updated_at) logView.detailCache.delete(log.id);
    }
    renderLogs();
    setLogFeedback();
  } catch (error) {
    if (error.name !== "AbortError" && sequence === logView.sequence) {
      setLogFeedback(I18n.t("日志加载失败，请点击刷新重试"), true);
      if (!logView.rows.length) document.getElementById("logTableBody").innerHTML = "";
    }
  } finally {
    if (sequence === logView.sequence) {
      logView.busy = false;
      renderLogPage();
      scheduleLogRefresh();
    }
  }
}

const logPretty = (text) => {
  if (!text) return "—";
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return String(text); }
};
const stopLogPreview = () => {
  ++logView.previewSequence;
  document.querySelectorAll("#logDetailContent video,#logDetailContent audio").forEach((media) => {
    media.pause(); media.removeAttribute("src"); media.load();
  });
};
const closeLogDetail = () => {
  ++logView.detailSequence;
  logView.detailController?.abort();
  stopLogPreview();
  document.documentElement.classList.remove("log-modal-open");
  if (logView.returnFocus?.isConnected) logView.returnFocus.focus({ preventScroll: true });
};

const renderLogDetail = (log) => {
  const item = (label, value, wide = false) => `<div${wide ? ' class="log-detail-wide"' : ""}><dt>${esc(I18n.t(label))}</dt><dd>${esc(value)}</dd></div>`;
  const [date, time] = logDate(log.created_at);
  const [updatedDate, updatedTime] = logDate(log.updated_at);
  document.getElementById("logDetailContent").innerHTML = `
    <div class="flex flex-wrap items-center gap-2">${logStateBadge(log)}<span class="text-xs font-mono text-muted-foreground">${log.status_code ? `HTTP ${esc(log.status_code)}` : ""} · #${esc(log.id)}</span></div>
    <dl class="log-detail-grid">
      ${item("生成类型", I18n.t(logTypes[log.operation] || log.operation || "-"))}
      ${item("耗时", logDuration(log.duration))}
      ${item("时间", `${date} ${time}`)}${item("最后更新", `${updatedDate} ${updatedTime}`)}
      ${item("账号", log.account_id == null ? I18n.t("未分配账号") : `${log.account_email || I18n.t("已删除账号")} · #${log.account_id}`, true)}
      ${item("Generation ID", log.generation_id || "—", true)}
      ${item("处理信息", I18n.t(logStatusText[log.status_text] || log.status_text || "等待处理"), true)}
    </dl>
    ${log.status_code === 202 ? `<p class="text-xs text-muted-foreground">${esc(I18n.t("202 表示本次请求已返回，生成结果以回执为准。请勿盲目更换幂等键重复提交。"))}</p>` : ""}
    ${log.generation_id ? `<section class="rounded-lg border border-border p-3 space-y-3"><div class="flex flex-wrap items-center justify-between gap-2"><span class="text-sm font-medium">${esc(I18n.t("生成结果"))}</span><div class="flex items-center gap-3"><button type="button" id="logPreviewBtn" class="rounded-md border border-input h-8 px-3 text-xs">${esc(I18n.t("预览结果"))}</button><a class="text-xs underline" href="/api/logs/${encodeURIComponent(log.id)}/content" download>${esc(I18n.t("下载结果"))}</a></div></div><p class="text-xs text-muted-foreground">${esc(I18n.t("点击后读取本地生成文件，不会重新提交生成请求。"))}</p><div id="logPreview" class="hidden"></div></section>` : ""}
    <details open><summary class="cursor-pointer text-sm font-medium">${esc(I18n.t("请求体"))}</summary><pre class="log-payload">${esc(logPretty(log.request_body))}</pre></details>
    <details><summary class="cursor-pointer text-sm font-medium">${esc(I18n.t("响应体"))}</summary><pre class="log-payload">${esc(logPretty(log.response_body))}</pre></details>`;
  document.getElementById("logPreviewBtn")?.addEventListener("click", () => loadLogPreview(log));
};

async function showLogDetail(logId) {
  if (document.getElementById("logModal").classList.contains("hidden")) logView.returnFocus = document.activeElement;
  stopLogPreview();
  logView.detailController?.abort();
  const controller = new AbortController();
  logView.detailController = controller;
  const sequence = ++logView.detailSequence;
  document.getElementById("logDetailContent").textContent = I18n.t("正在加载详情…");
  openModal("logModal");
  document.documentElement.classList.add("log-modal-open");
  document.getElementById("logDetailContent").scrollTop = 0;
  document.getElementById("logDetailClose").focus({ preventScroll: true });
  const cached = logView.detailCache.get(logId);
  if (cached) { renderLogDetail(cached); return; }
  try {
    const response = await apiRequest(`/api/logs/${logId}`, { signal: controller.signal });
    if (!response || sequence !== logView.detailSequence) return;
    if (!response.ok) throw new Error(response.status === 404 ? "not_found" : "unavailable");
    const log = await response.json();
    if (sequence !== logView.detailSequence) return;
    logView.detailCache.set(logId, log);
    if (logView.detailCache.size > 50) logView.detailCache.delete(logView.detailCache.keys().next().value);
    renderLogDetail(log);
  } catch (error) {
    if (error.name === "AbortError" || sequence !== logView.detailSequence) return;
    document.getElementById("logDetailContent").innerHTML = `<p class="text-sm text-destructive">${esc(I18n.t(error.message === "not_found" ? "该日志已不存在，请刷新列表" : "详情加载失败，请重试"))}</p><button id="logDetailRetry" class="rounded-md border border-input h-8 px-3 text-xs">${esc(I18n.t("重新加载"))}</button>`;
    document.getElementById("logDetailRetry").addEventListener("click", () => showLogDetail(logId));
  }
}

const loadLogPreview = (log) => {
  const target = document.getElementById("logPreview");
  stopLogPreview();
  const sequence = logView.previewSequence;
  target.classList.remove("hidden");
  target.textContent = I18n.t("正在加载媒体…");
  const media = document.createElement(log.operation === "image" ? "img" : log.operation === "speech" ? "audio" : "video");
  media.className = "log-preview-media";
  if (log.operation === "image") media.alt = I18n.t("生成结果");
  else { media.controls = true; media.preload = "metadata"; }
  const event = log.operation === "image" ? "load" : "loadedmetadata";
  media.addEventListener(event, () => { if (target.isConnected && sequence === logView.previewSequence) target.replaceChildren(media); }, { once: true });
  media.addEventListener("error", () => { if (target.isConnected && sequence === logView.previewSequence) target.textContent = I18n.t("生成结果尚未完成、已过期或不可用"); }, { once: true });
  media.src = `/api/logs/${encodeURIComponent(log.id)}/content`;
  // Media elements must be attached for reliable metadata loading in all browsers.
  target.appendChild(media);
};

async function clearLogs() {
  if (!confirm(I18n.t("确认清空全部请求日志？"))) return;
  const button = document.getElementById("logsClearBtn");
  button.disabled = true;
  try {
    const response = await apiRequest("/api/logs", { method: "DELETE" });
    if (!response?.ok) throw new Error("clear_failed");
    logView.detailCache.clear();
    logView.page = 0;
    showToast(I18n.t("日志已清空"), "success");
    await loadLogs();
  } catch { showToast(I18n.t("清空失败，请重试"), "error"); }
  finally { button.disabled = false; }
}

const applyLogFilters = () => { logView.page = 0; loadLogs(); };
document.getElementById("logsFilters").addEventListener("submit", (event) => { event.preventDefault(); applyLogFilters(); });
["logsOperation", "logsStatus", "logsDays"].forEach((id) => document.getElementById(id).addEventListener("change", applyLogFilters));
document.getElementById("logsResetBtn").addEventListener("click", () => { document.getElementById("logsFilters").reset(); applyLogFilters(); });
document.getElementById("logsPageSize").addEventListener("change", (event) => { logView.size = Number(event.target.value); applyLogFilters(); });
document.getElementById("logsPrevBtn").addEventListener("click", () => { if (!logView.busy && logView.page > 0) { --logView.page; loadLogs(); } });
document.getElementById("logsNextBtn").addEventListener("click", () => { if (!logView.busy) { ++logView.page; loadLogs(); } });
document.getElementById("logTableBody").addEventListener("click", (event) => {
  const button = event.target.closest("[data-log-detail]");
  if (button) showLogDetail(Number(button.dataset.logDetail));
});
document.addEventListener("visibilitychange", () => { clearTimeout(logView.timer); if (!document.hidden && logView.active && document.getElementById("logsAutoRefresh").checked) loadLogs(); });
window.addEventListener("pagehide", () => { clearTimeout(logView.timer); logView.controller?.abort(); logView.detailController?.abort(); });
document.getElementById("logModal").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { closeModal("logModal"); return; }
  if (event.key !== "Tab") return;
  const nodes = [...event.currentTarget.querySelectorAll("button:not(:disabled),a[href],summary,video,audio")].filter((node) => node.getBoundingClientRect().height > 0);
  if (!nodes.length) return;
  const first = nodes[0], last = nodes[nodes.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
