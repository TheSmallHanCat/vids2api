// Popup logic: settings persistence, i18n binding and push triggering.

const FIELDS = ["serverUrl", "pushToken", "remark"];

function localize() {
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.getAttribute("data-i18n");
    const text = chrome.i18n.getMessage(key);
    if (text) {
      node.textContent = text;
    }
  });
}

function loadSettings() {
  chrome.storage.local
    .get({ serverUrl: "", pushToken: "", remark: "" })
    .then((stored) => {
      for (const field of FIELDS) {
        document.getElementById(field).value = stored[field] || "";
      }
    });
}

function persistSettings() {
  const update = {};
  for (const field of FIELDS) {
    update[field] = document.getElementById(field).value.trim();
  }
  return chrome.storage.local.set(update).then(() => update);
}

function setStatus(text, isError) {
  const node = document.getElementById("status");
  node.textContent = text;
  node.classList.toggle("error", Boolean(isError));
}

document.getElementById("push").addEventListener("click", async () => {
  const button = document.getElementById("push");
  button.disabled = true;
  setStatus(chrome.i18n.getMessage("statusWorking"), false);
  try {
    const settings = await persistSettings();
    if (!settings.serverUrl || !settings.pushToken) {
      throw new Error(chrome.i18n.getMessage("errNotConfigured"));
    }
    const reply = await chrome.runtime.sendMessage({ type: "pushAccount" });
    if (!reply) {
      throw new Error(chrome.i18n.getMessage("errNoReply"));
    }
    if (!reply.ok) {
      throw new Error(reply.error);
    }
    const payload = reply.payload || {};
    setStatus(
      chrome.i18n.getMessage(
        "statusDone",
        [payload.action || "?", String(payload.account_id ?? "?")]
      ),
      false
    );
  } catch (error) {
    setStatus(String(error.message || error), true);
  } finally {
    button.disabled = false;
  }
});

localize();
loadSettings();
