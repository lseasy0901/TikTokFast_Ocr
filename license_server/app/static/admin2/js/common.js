/* ============================================================
   LiveLens Admin 2.0 — 前端公共层（真实数据版，阶段 D）
   外壳由服务端模板渲染；本文件提供：图标 / 格式化 / Toast / 弹窗 /
   分页 / API 封装（401 跳登录、CSRF 头）/ 空状态 / XSS 转义。
   无任何 MOCK 数据；接口失败显式报错，绝不静默回退演示数据。
   ============================================================ */

(function () {
  "use strict";
  const App = (window.App = {});
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  App.$ = $; App.$$ = $$;

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  App.el = el;

  /* ---------- XSS 转义（所有服务端字符串插入 DOM 前必须经过） ---------- */
  App.esc = function (v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  /* ---------- 图标库 ---------- */
  const I = {
    chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>',
    key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M11 12L21 2"/><path d="M16 7l3 3"/></svg>',
    device: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
    swap: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 6h18"/><path d="M7 22l-4-4 4-4"/><path d="M21 18H3"/></svg>',
    analytics: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="3" width="4" height="18" rx="1"/></svg>',
    archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9"/><path d="M10 13h4"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/></svg>',
    inbox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5 5h14l3 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6l3-7z"/></svg>',
    ban: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M5.5 5.5l13 13"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
    users: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M17.5 14.4a6.5 6.5 0 0 1 4 5.6"/></svg>',
    spinner: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3a9 9 0 1 0 9 9"/></svg>',
  };
  App.icon = (name) => I[name] || "";

  /* 图标注入：元素上 data-icon="name" */
  App.injectIcons = function (root) {
    $$("[data-icon]", root || document).forEach((n) => {
      n.insertAdjacentHTML("afterbegin", App.icon(n.getAttribute("data-icon")));
    });
  };

  /* ---------- API 封装 ---------- */
  function csrfToken() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  App.API = {
    async request(path, opts) {
      opts = opts || {};
      const init = {
        method: opts.method || "GET",
        headers: {},
        credentials: "same-origin",
      };
      if (opts.body !== undefined) {
        init.method = opts.method || "POST";
        init.headers["Content-Type"] = "application/json";
        init.headers["X-CSRF-Token"] = csrfToken();
        init.body = JSON.stringify(opts.body);
      }
      let resp;
      try {
        resp = await fetch(path, init);
      } catch (e) {
        throw { code: "network", message: "网络错误：无法连接服务器，请检查网络后重试" };
      }
      if (resp.status === 401) {
        location.href = "/admin2/login?expired=1";
        throw { code: "unauthorized", message: "登录已失效，正在跳转登录页…" };
      }
      if (resp.status === 204) return null;
      let data = null;
      try { data = await resp.json(); } catch (e) { /* 非 JSON 错误页 */ }
      if (!resp.ok) {
        const msg = (data && data.detail) ? String(data.detail)
          : `服务器错误（HTTP ${resp.status}）`;
        throw { code: resp.status, message: msg };
      }
      return data;
    },
    get(path, params) {
      const qs = params ? "?" + new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""))
      ).toString() : "";
      return this.request(path + qs);
    },
    post(path, body) { return this.request(path, { method: "POST", body }); },
  };

  /* ---------- Toast ---------- */
  App.toast = function (msg, type, timeout) {
    type = type || "info";
    const stack = $("#toastStack") || document.body;
    const icons = { success: "check", error: "alert", warn: "alert", info: "info" };
    const t = el(`<div class="toast toast-${type}">${App.icon(icons[type] || "info")}<div class="t-msg">${App.esc(msg)}</div></div>`);
    stack.appendChild(t);
    setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 260); }, timeout || 3200);
  };

  /* ---------- 弹窗 / 抽屉 ---------- */
  App.openModal = (id) => { const m = document.getElementById(id); if (m) m.classList.add("open"); };
  App.closeModal = (id) => { const m = document.getElementById(id); if (m) m.classList.remove("open"); };
  App.bindModals = function (root) {
    $$("[data-close-modal]", root).forEach((b) =>
      b.addEventListener("click", () => App.closeModal(b.getAttribute("data-close-modal"))));
    $$(".modal-overlay", root).forEach((ov) =>
      ov.addEventListener("mousedown", (e) => { if (e.target === ov) ov.classList.remove("open"); }));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") $$(".modal-overlay.open").forEach((ov) => ov.classList.remove("open"));
    });
  };
  App.openDrawer = function (id) {
    document.getElementById(id).classList.add("open");
    document.getElementById(id + "Overlay").classList.add("open");
  };
  App.closeDrawer = function (id) {
    document.getElementById(id).classList.remove("open");
    document.getElementById(id + "Overlay").classList.remove("open");
  };

  /* ---------- 复制 ---------- */
  App.copyText = async function (text, okMsg) {
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
    }
    App.toast(okMsg || "已复制到剪贴板", "success", 1800);
  };

  /* ---------- 分页 ---------- */
  App.Paginator = function (opts) {
    const state = { page: 1, pageSize: opts.pageSize || 10, total: 0 };
    const footEl = typeof opts.footEl === "string" ? $(opts.footEl) : opts.footEl;
    function render() {
      const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
      state.page = Math.min(state.page, pages);
      const from = state.total === 0 ? 0 : (state.page - 1) * state.pageSize + 1;
      const to = Math.min(state.total, state.page * state.pageSize);
      let btns = "";
      const win = [];
      for (let p = 1; p <= pages; p++) {
        if (p === 1 || p === pages || Math.abs(p - state.page) <= 1) win.push(p);
      }
      let last = 0;
      for (const p of win) {
        if (last && p - last > 1) btns += `<button disabled>…</button>`;
        btns += `<button class="${p === state.page ? "on" : ""}" data-p="${p}">${p}</button>`;
        last = p;
      }
      footEl.innerHTML = `
        <div class="page-info">显示 ${from}–${to} 条，共 ${state.total} 条</div>
        <div class="pager">
          <button data-p="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""}>‹</button>
          ${btns}
          <button data-p="${state.page + 1}" ${state.page >= pages ? "disabled" : ""}>›</button>
        </div>`;
      $$("button[data-p]", footEl).forEach((b) =>
        b.addEventListener("click", () => {
          const p = parseInt(b.getAttribute("data-p"), 10);
          if (p >= 1 && p <= pages && p !== state.page) { state.page = p; render(); opts.onChange(state.page); }
        }));
    }
    return {
      set(total, keepPage) { state.total = total; if (!keepPage) state.page = 1; render(); },
      setPageSize(ps) { state.pageSize = ps; },
      page: () => state.page,
      pageSize: () => state.pageSize,
      render,
    };
  };

  /* ---------- 状态 / 套餐 徽章 ---------- */
  const PLAN_COLORS = { d1: "#38bdf8", m30: "#4d7cfe", h180: "#8b5cf6", y365: "#2dd4a7", other: "#9aa5b8" };
  App.fmt = {
    num(n) { return (n == null ? 0 : n).toLocaleString("zh-CN"); },
    dt(s) { return s || "—"; },
    short(s) { return s ? s.slice(5, 16) : "—"; },
    esc: App.esc,
    stateBadge(state) {
      const map = {
        active: ["badge-success", "有效授权"], expiring: ["badge-warning", "即将到期"],
        expired: ["badge-danger", "已过期"], unused: ["badge-info", "未使用"],
        redeemed: ["badge-primary", "已兑换"], revoked: ["badge-muted", "已吊销"],
        new: ["badge-info", "新建授权"], extend: ["badge-violet", "有效期累加"],
        reset: ["badge-warning", "过期重置"], unknown: ["badge-muted", "不可回溯"],
      };
      const [cls, label] = map[state] || ["badge-muted", state];
      return `<span class="badge ${cls}">${label}</span>`;
    },
    typeBadge(type) {
      return type === "first"
        ? '<span class="badge badge-info">首次激活</span>'
        : '<span class="badge badge-violet">续费</span>';
    },
    planBadge(planKey, planName) {
      const color = PLAN_COLORS[planKey] || "#9aa5b8";
      return `<span class="badge no-dot" style="color:${color};background:${color}1f">${App.esc(planName || planKey)}</span>`;
    },
    planDot(planKey) {
      return `<span class="plan-dot" style="background:${PLAN_COLORS[planKey] || "#9aa5b8"}"></span>`;
    },
  };
  App.PLAN_COLORS = PLAN_COLORS;

  /* ---------- 空状态 / 加载 / 错误 ---------- */
  App.emptyState = function (title, hint, actionHtml) {
    return `<div class="empty">
      <div class="e-ico">${App.icon("inbox")}</div>
      <div class="e-title">${App.esc(title)}</div>
      <div class="e-hint">${App.esc(hint || "")}</div>
      ${actionHtml || ""}
    </div>`;
  };
  App.loadingState = function (label) {
    return `<div class="empty"><div class="e-ico" style="color:var(--primary)">${App.icon("spinner")}</div>
      <div class="e-title" style="font-weight:400;color:var(--text-2)">${App.esc(label || "加载中…")}</div></div>`;
  };
  App.errorState = function (err, retryFnName) {
    const msg = err && err.message ? err.message : String(err);
    const retry = retryFnName ? `<div class="e-action"><button class="btn" onclick="${retryFnName}()">重试</button></div>` : "";
    return `<div class="empty"><div class="e-ico" style="color:var(--danger)">${App.icon("alert")}</div>
      <div class="e-title">加载失败</div><div class="e-hint">${App.esc(msg)}</div>${retry}</div>`;
  };

  /* ---------- 授权余量 ---------- */
  App.meter = function (dev) {
    const remain = dev.remain_days;
    let pct = 0, color = "var(--danger)";
    if (remain != null) {
      pct = Math.max(0, Math.min(100, Math.round((remain / 30) * 100)));
      color = remain < 0 ? "var(--danger)" : remain <= 7 ? "var(--warning)" : "var(--success)";
    }
    const label = remain == null ? "—" : remain < 0 ? `已过期 ${-remain} 天` : remain === 0 ? "今日到期" : `剩余 ${remain} 天`;
    return `<div class="meter"><div class="m-track"><div class="m-fill" style="width:${pct}%;background:${color}"></div></div><div class="m-label">${label}</div></div>`;
  };

  /* ---------- 外壳行为（服务端已渲染 DOM） ---------- */
  document.addEventListener("DOMContentLoaded", function () {
    App.injectIcons(document);
    const sidebar = $("#sidebar"), backdrop = $("#sidebarBackdrop");
    const menuBtn = $("#menuBtn");
    if (menuBtn) menuBtn.addEventListener("click", () => { sidebar.classList.add("open"); backdrop.classList.add("show"); });
    if (backdrop) backdrop.addEventListener("click", () => { sidebar.classList.remove("open"); backdrop.classList.remove("show"); });

    const clock = $("#bjClock");
    if (clock) {
      const tick = () => {
        const now = new Date();
        const p = (x) => String(x).padStart(2, "0");
        clock.textContent = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())} 北京时间`;
      };
      tick(); setInterval(tick, 1000);
    }
    App.bindModals(document);
  });
})();
