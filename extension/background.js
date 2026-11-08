// Vids2API Pool Pusher: collect the signed-in Google session and push it to
// an vids2api server. Runs in the extension service worker.

const REQUIRED_COOKIES = [
  "SID",
  "HSID",
  "SSID",
  "SAPISID",
  "__Secure-1PSID",
  "__Secure-3PSID",
];

const EDITOR_PATH_RE = /^\/videos\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]+)\/edit/;
const VIDS_HOME = "https://docs.google.com/videos/?authuser=0";
const VIDS_NEW = "https://vids.new";

function getSettings() {
  return chrome.storage.local
    .get({ serverUrl: "", pushToken: "", remark: "" })
    .then((stored) => ({
      serverUrl: (stored.serverUrl || "").trim().replace(/\/+$/, ""),
      pushToken: (stored.pushToken || "").trim(),
      remark: (stored.remark || "").trim(),
    }));
}

async function collectCookies() {
  const rows = await chrome.cookies.getAll({ url: "https://docs.google.com" });
  const jar = {};
  for (const row of rows) {
    jar[row.name] = true;
  }
  const missing = REQUIRED_COOKIES.filter((name) => !jar[name]);
  if (missing.length) {
    throw new Error(chrome.i18n.getMessage("errNotSignedIn", [missing.join(", ")]));
  }
  return rows.map((row) => ({
    name: row.name,
    value: row.value,
    domain: row.domain,
    path: row.path,
    secure: row.secure,
    httpOnly: row.httpOnly,
    sameSite: row.sameSite === "no_restriction" ? "none" : row.sameSite,
    expirationDate: row.expirationDate,
  }));
}

async function findEditorFromHome() {
  const response = await fetch(VIDS_HOME, { credentials: "include" });
  if (!response.ok) {
    throw new Error(chrome.i18n.getMessage("errHomeFetch", [String(response.status)]));
  }
  const html = await response.text();
  const pattern = /href="(\/videos\/(?:u\/\d+\/)?d\/[A-Za-z0-9_-]+\/edit[^"]*)"/g;
  let match = pattern.exec(html);
  while (match) {
    const address = new URL(match[1], "https://docs.google.com");
    if (EDITOR_PATH_RE.test(address.pathname)) {
      return `${address.origin}${address.pathname}?authuser=0`;
    }
    match = pattern.exec(html);
  }
  return null;
}

function waitForEditorUrl(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      clearTimeout(timer);
      resolve(value);
    };
    const onUpdated = (updatedTabId, changeInfo, tab) => {
      if (updatedTabId !== tabId || !changeInfo.url) return;
      try {
        const address = new URL(changeInfo.url);
        if (address.hostname === "docs.google.com" && EDITOR_PATH_RE.test(address.pathname)) {
          finish(`${address.origin}${address.pathname}?authuser=0`);
        }
      } catch (error) {
        reject(error);
      }
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

async function createBlankDocument() {
  const tab = await chrome.tabs.create({ url: VIDS_NEW, active: false });
  try {
    const editor = await waitForEditorUrl(tab.id, 60000);
    return editor;
  } finally {
    try {
      await chrome.tabs.remove(tab.id);
    } catch (error) {
      // The tab may already be gone.
    }
  }
}

async function ensureServerPermission(serverUrl) {
  const origin = new URL(serverUrl).origin + "/*";
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) {
    throw new Error(chrome.i18n.getMessage("errPermission"));
  }
}

async function pushAccount() {
  const settings = await getSettings();
  if (!settings.serverUrl || !settings.pushToken) {
    throw new Error(chrome.i18n.getMessage("errNotConfigured"));
  }
  await ensureServerPermission(settings.serverUrl);

  const cookies = await collectCookies();
  let editorUrl = await findEditorFromHome();
  if (!editorUrl) {
    editorUrl = await createBlankDocument();
  }
  if (!editorUrl) {
    throw new Error(chrome.i18n.getMessage("errNoDocument"));
  }

  const response = await fetch(`${settings.serverUrl}/api/extension/push`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Push-Token": settings.pushToken,
    },
    body: JSON.stringify({
      cookies,
      editor_url: editorUrl,
      remark: settings.remark,
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      payload && payload.error && payload.error.message
        ? payload.error.message
        : chrome.i18n.getMessage("errPush", [String(response.status)]);
    throw new Error(message);
  }
  return payload;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "pushAccount") {
    pushAccount()
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
    return true;
  }
  return false;
});
