/* vids2api i18n: zh-CN is the source locale; catalogs live in /static/locales. */
(function () {
  "use strict";

  const STORAGE_KEY = "vids2api.language";
  const SUPPORTED = ["zh-CN", "en"];
  const PAGE_CATALOGS = {
    login: ["common", "login"],
    manage: ["common", "manage"],
    test: ["common", "test"],
    "api-guide": ["common"],
  };

  let catalogs = {};
  let locale = localStorage.getItem(STORAGE_KEY) || "zh-CN";
  if (!SUPPORTED.includes(locale)) locale = "zh-CN";

  function detectPage() {
    const node = document.querySelector("[data-i18n-page]");
    return node ? node.getAttribute("data-i18n-page") : "manage";
  }

  async function loadCatalogs() {
    const names = PAGE_CATALOGS[detectPage()] || ["common"];
    catalogs = {};
    await Promise.all(
      names.map((name) =>
        fetch(`/static/locales/${name}.json`)
          .then((r) => (r.ok ? r.json() : {}))
          .then((data) => {
            catalogs[name] = data[locale] || {};
          })
          .catch(() => {
            catalogs[name] = {};
          })
      )
    );
  }

  function lookup(source) {
    for (const name of Object.keys(catalogs)) {
      const value = catalogs[name][source];
      if (typeof value === "string") return value;
    }
    return source;
  }

  const I18n = {
    get locale() {
      return locale;
    },
    t(source, params) {
      let text = lookup(source);
      if (params) {
        Object.keys(params).forEach((key) => {
          text = text.split(`{${key}}`).join(String(params[key]));
        });
      }
      return text;
    },
    apply(root) {
      const scope = root || document;
      scope.querySelectorAll("[data-i18n]").forEach((node) => {
        const key = node.getAttribute("data-i18n") || node.textContent.trim();
        if (!key) return;
        node.textContent = I18n.t(key);
      });
      scope.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
        const key = node.getAttribute("data-i18n-placeholder");
        if (key) node.placeholder = I18n.t(key);
      });
      scope.querySelectorAll("[data-i18n-title]").forEach((node) => {
        const key = node.getAttribute("data-i18n-title");
        if (key) node.title = I18n.t(key);
      });
      scope.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
        node.setAttribute("aria-label", this.t(node.getAttribute("data-i18n-aria-label")));
      });
    },
    headers() {
      return { "Accept-Language": locale };
    },
    setLocale(next) {
      if (!SUPPORTED.includes(next)) return;
      locale = next;
      localStorage.setItem(STORAGE_KEY, next);
      window.location.reload();
    },
    async init() {
      await loadCatalogs();
      I18n.apply(document);
    },
  };

  window.I18n = I18n;
  document.addEventListener("DOMContentLoaded", () => {
    I18n.init();
  });
  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY && event.newValue && event.newValue !== locale) {
      window.location.reload();
    }
  });
})();
