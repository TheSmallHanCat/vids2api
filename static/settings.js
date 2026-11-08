/* System settings: independent loading, validated writes and effective-value feedback. */
const settingsDefinitions = {
  generation: { url: "/api/config/generation", fields: { image_timeout: "cfg-image-timeout", speech_timeout: "cfg-speech-timeout", video_timeout: "cfg-video-timeout", max_retries: "cfg-max-retries" } },
  pool: { url: "/api/config/pool", fields: { call_mode: "cfg-call-mode", error_ban_threshold: "cfg-error-ban", keepalive_interval: "cfg-keepalive-interval", quota_preflight: "cfg-quota-preflight", keepalive_enabled: "cfg-keepalive-enabled" } },
  login: { url: "/api/config/login", fields: { auto_relogin_enabled: "cfg-auto-relogin", relogin_cooldown_seconds: "cfg-relogin-cooldown", login_timeout_seconds: "cfg-login-timeout" } },
  proxy: { url: "/api/config/proxy", fields: { enabled: "cfg-proxy-enabled", proxy_url: "cfg-proxy-url" } },
  cache: { url: "/api/config/cache", fields: { enabled: "cfg-cache-enabled", cache_timeout: "cfg-cache-timeout", cache_base_url: "cfg-cache-base-url" } },
  extension: { url: "/api/config/extension", fields: { enabled: "cfg-ext-enabled", push_token: "cfg-ext-token", connection_url: "cfg-ext-url", auto_enable_on_update: "cfg-ext-auto-enable" } },
  debug: { url: "/api/config/debug", fields: { enabled: "cfg-debug-enabled", log_requests: "cfg-debug-requests", log_responses: "cfg-debug-responses", mask_token: "cfg-debug-mask" } },
  admin: { url: "/api/admin/config", fields: { username: "cfg-admin-username" } },
};
const settingsState = {};

const settingsMessage = (kind, message, error = false) => {
  const state = settingsState[kind];
  state.message.textContent = I18n.t(message);
  state.message.classList.toggle("text-destructive", error);
  state.message.classList.toggle("text-muted-foreground", !error);
  state.retry.hidden = !error;
};

const settingsBusy = (kind, busy) => {
  const state = settingsState[kind];
  state.busy = busy;
  state.card.setAttribute("aria-busy", String(busy));
  state.card.querySelectorAll("input, select, button").forEach((node) => {
    node.disabled = node === state.retry ? busy : busy || !state.loaded;
  });
};

const settingsErrorMessage = (data, status) => {
  if (data?.error?.message) return data.error.message;
  if (typeof data?.detail === "string") return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg).join("；");
  return I18n.t("请求失败（HTTP {status}）", { status });
};

const settingsRequest = async (url, body) => {
  const response = await apiRequest(url, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response) throw new Error(I18n.t("登录已失效，请重新登录"));
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(settingsErrorMessage(data, response.status));
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(I18n.t("服务器返回了无效配置"));
  return data;
};

const applySettings = (kind, data) => {
  const fields = settingsDefinitions[kind].fields;
  if (Object.keys(fields).some((key) => !(key in data))) throw new Error(I18n.t("配置数据不完整，请重新加载"));
  Object.entries(fields).forEach(([key, id]) => {
    const node = document.getElementById(id);
    if (node.type === "checkbox") node.checked = data[key];
    else node.value = data[key];
  });
  if (kind === "pool") {
    const key = document.getElementById("cfg-yescaptcha-key");
    key.value = "";
    key.placeholder = data.yescaptcha_api_key_set ? I18n.t("已配置（输入新值可更换）") : "";
  }
  if (kind === "cache") document.getElementById("cfg-cache-effective-url").textContent = data.media_url_prefix || "—";
  if (kind === "admin") document.getElementById("api-key-hint").textContent = data.api_key_hint || "—";
};

const loadSettingsGroup = async (kind) => {
  const state = settingsState[kind];
  if (state.busy) return;
  settingsBusy(kind, true);
  settingsMessage(kind, "正在加载配置…");
  try {
    applySettings(kind, await settingsRequest(settingsDefinitions[kind].url));
    state.loaded = true;
    settingsMessage(kind, "");
  } catch (error) {
    state.loaded = false;
    settingsMessage(kind, error.message || "配置加载失败，请重试", true);
  } finally {
    settingsBusy(kind, false);
  }
};

const loadSettings = () => Promise.allSettled(Object.keys(settingsDefinitions).map(loadSettingsGroup));

const settingsValidity = (nodes) => {
  for (const node of nodes) {
    node.setCustomValidity("");
    if (!node.reportValidity()) return false;
  }
  return true;
};

const collectSettings = (kind) => {
  const body = {};
  Object.entries(settingsDefinitions[kind].fields).forEach(([key, id]) => {
    const node = document.getElementById(id);
    if (node.readOnly) return;
    body[key] = node.type === "checkbox" ? node.checked : node.type === "number" ? node.valueAsNumber : node.value.trim();
  });
  if (kind === "pool") {
    const key = document.getElementById("cfg-yescaptcha-key").value.trim();
    if (key) body.yescaptcha_api_key = key;
  }
  if (kind === "extension" && !body.push_token) delete body.push_token;
  if (kind === "extension" && body.push_token && !/^[!-~]+$/.test(body.push_token)) throw new Error(I18n.t("推送令牌须使用非空白 ASCII 字符"));
  if (kind === "cache" && body.cache_base_url) {
    let address;
    try { address = new URL(body.cache_base_url); } catch { throw new Error(I18n.t("请输入有效的 HTTP(S) 访问地址")); }
    if (!["http:", "https:"].includes(address.protocol) || address.username || address.password || address.search || address.hash) {
      throw new Error(I18n.t("访问地址须为不含认证信息、查询参数或片段的 HTTP(S) 地址"));
    }
  }
  if (kind === "proxy" && body.enabled && !body.proxy_url) throw new Error(I18n.t("启用代理时必须填写代理地址"));
  return body;
};

const saveSettingsGroup = async (kind) => {
  const state = settingsState[kind];
  if (state.busy || !state.loaded) return;
  if (!settingsValidity(state.card.querySelectorAll("input, select"))) return;
  let body;
  try { body = collectSettings(kind); } catch (error) { settingsMessage(kind, error.message, true); return; }
  settingsBusy(kind, true);
  settingsMessage(kind, "正在保存…");
  try {
    applySettings(kind, await settingsRequest(settingsDefinitions[kind].url, body));
    settingsMessage(kind, "已保存并生效");
  } catch (error) {
    settingsMessage(kind, error.message || "保存失败", true);
  } finally {
    settingsBusy(kind, false);
  }
};

const regenerateExtToken = async () => {
  const state = settingsState.extension;
  if (state.busy || !state.loaded || !confirm(I18n.t("重置后旧令牌立即失效，已配置的扩展需要更新令牌。继续？"))) return;
  settingsBusy("extension", true);
  try {
    applySettings("extension", await settingsRequest(settingsDefinitions.extension.url, { regenerate: true }));
    settingsMessage("extension", "令牌已重置");
  } catch (error) {
    settingsMessage("extension", error.message, true);
  } finally {
    settingsBusy("extension", false);
  }
};

const copySettingsValue = async (id) => {
  const node = document.getElementById(id);
  if (!node.value) return;
  try {
    await navigator.clipboard.writeText(node.value);
    showToast(I18n.t("已复制到剪贴板"), "success", 1600);
  } catch {
    node.focus();
    node.select();
    showToast(I18n.t("已选中文本，请复制"), "info");
  }
};
const copyExtToken = () => copySettingsValue("cfg-ext-token");
const copyExtUrl = () => copySettingsValue("cfg-ext-url");

const saveAdminCredentials = async () => {
  const state = settingsState.admin;
  if (state.busy || !state.loaded) return;
  const ids = ["cfg-admin-username", "cfg-admin-old-password", "cfg-admin-password", "cfg-admin-confirm-password"];
  const nodes = ids.map((id) => document.getElementById(id));
  if (!settingsValidity(nodes)) return;
  if (nodes[2].value !== nodes[3].value) {
    nodes[3].setCustomValidity(I18n.t("两次输入的新密码不一致"));
    nodes[3].reportValidity();
    return;
  }
  const body = { username: nodes[0].value.trim(), old_password: nodes[1].value, password: nodes[2].value, confirm_password: nodes[3].value };
  settingsBusy("admin", true);
  try {
    await settingsRequest("/api/admin/change-password", body);
    nodes.slice(1).forEach((node) => { node.value = ""; });
    settingsMessage("admin", "管理凭据已更新，请重新登录");
    window.location.replace("/login");
  } catch (error) {
    settingsMessage("admin", error.message, true);
    settingsBusy("admin", false);
  }
};

const saveApiKey = async () => {
  const state = settingsState.admin;
  const input = document.getElementById("cfg-api-key");
  if (state.busy || !state.loaded || !settingsValidity([input])) return;
  if (!/^[!-~]+$/.test(input.value)) { settingsMessage("admin", I18n.t("API Key 须使用非空白 ASCII 字符"), true); return; }
  settingsBusy("admin", true);
  try {
    await settingsRequest("/api/admin/apikey", { api_key: input.value });
    applySettings("admin", await settingsRequest(settingsDefinitions.admin.url));
    input.value = "";
    settingsMessage("admin", "API Key 已更新");
  } catch (error) {
    settingsMessage("admin", error.message, true);
  } finally {
    settingsBusy("admin", false);
  }
};

Object.entries(settingsDefinitions).forEach(([kind, definition]) => {
  const card = document.getElementById(Object.values(definition.fields)[0]).closest(".p-5");
  card.dataset.settingsGroup = kind;
  const footer = document.createElement("div");
  footer.className = "flex items-center gap-3 mt-3 text-sm";
  const message = document.createElement("span");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "underline text-xs whitespace-nowrap";
  retry.textContent = I18n.t("重新加载");
  retry.setAttribute("data-i18n", "重新加载");
  retry.hidden = true;
  retry.addEventListener("click", () => loadSettingsGroup(kind));
  footer.append(message, retry);
  card.append(footer);
  settingsState[kind] = { card, message, retry, loaded: false, busy: false };
  settingsBusy(kind, false);
  card.querySelectorAll("input, select").forEach((node) => node.addEventListener("input", () => node.setCustomValidity("")));
});
document.querySelectorAll(".save-btn[data-save]").forEach((button) => button.addEventListener("click", () => saveSettingsGroup(button.dataset.save)));
