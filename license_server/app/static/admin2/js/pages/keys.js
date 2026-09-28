/* 卡密管理 — 真实 API 版（/admin2/api/keys*） */
(function () {
  "use strict";
  const { $, $$, icon, fmt, esc } = App;

  let filters = { state: "", plan: "", q: "" };
  let pendingRevokeId = null;

  /* ---------- 状态分段 ---------- */
  function renderSeg(counts) {
    const items = [
      ["", "全部", counts.total], ["unused", "未使用", counts.unused],
      ["redeemed", "已兑换", counts.redeemed], ["revoked", "已吊销", counts.revoked],
    ];
    $("#stateSeg").innerHTML = items.map(([v, label, cnt]) =>
      `<button data-v="${v}" class="${filters.state === v ? "on" : ""}">${label}<span class="cnt">${cnt}</span></button>`).join("");
    $$("#stateSeg button").forEach(b => b.addEventListener("click", () => {
      filters.state = b.dataset.v; renderSeg(counts); load();
    }));
  }

  function featureHtml(features) {
    return features && features.length
      ? features.map(f => `<span class="badge no-dot badge-muted" style="font-size:11px;padding:2px 8px">${esc(f)}</span>`).join(" ")
      : '<span class="text-dim">—</span>';
  }

  function rowHtml(l) {
    const canRevoke = l.state === "unused";
    return `<tr>
      <td class="num text-dim">#${l.id}</td>
      <td class="mono" title="${esc(l.hash)}">${esc(l.hash.slice(0, 16))}…
        <button class="copy-btn" title="复制完整指纹" data-copy="${esc(l.hash)}">${icon("copy")}</button></td>
      <td>${fmt.planBadge(l.plan_key, l.plan_name)}</td>
      <td>${featureHtml(l.features)}</td>
      <td>${fmt.stateBadge(l.state)}</td>
      <td class="num text-2">${esc(l.created_at_bj || "—")}</td>
      <td class="num text-2">${esc(l.redeemed_at_bj || "—")}</td>
      <td class="mono">${l.device_id ? esc(l.device_id) : '<span class="text-dim">—</span>'}</td>
      <td><div class="row-actions">
        <button class="btn btn-ghost btn-sm" data-detail="${l.id}">详情</button>
        ${canRevoke ? `<button class="btn btn-ghost btn-sm" style="color:var(--danger)" data-revoke="${l.id}">吊销</button>` : ""}
      </div></td>
    </tr>`;
  }

  const pager = App.Paginator({ footEl: "#keysFoot", pageSize: 10, onChange: () => load(true) });

  async function load(keepPage) {
    $("#keysTable").innerHTML = App.loadingState("加载卡密列表…");
    try {
      const data = await App.API.get("/admin2/api/keys", {
        state: filters.state || undefined, plan: filters.plan || undefined,
        q: filters.q || undefined, page: keepPage ? pager.page() : 1, page_size: 10,
      });
      renderSeg(data.counts);
      $("#keysTable").innerHTML = data.items.length === 0
        ? App.emptyState("没有匹配的卡密", "调整筛选条件，或清除搜索关键词")
        : `<table class="table">
            <thead><tr>
              <th>ID</th><th>KEY 指纹 (SHA-256)</th><th>套餐</th><th>功能标签</th>
              <th>状态</th><th>创建时间</th><th>兑换时间</th><th>关联设备</th><th style="width:130px">操作</th>
            </tr></thead>
            <tbody>${data.items.map(rowHtml).join("")}</tbody>
          </table>`;
      pager.set(data.total, true);
      bindRows();
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#keysTable").innerHTML = App.errorState(err, "reloadKeys");
      App.toast(err.message || "加载失败", "error");
    }
  }
  window.reloadKeys = () => load();

  function bindRows() {
    $$("#keysTable [data-copy]").forEach(b =>
      b.addEventListener("click", () => App.copyText(b.getAttribute("data-copy"), "指纹已复制")));
    $$("#keysTable [data-detail]").forEach(b =>
      b.addEventListener("click", () => showLic(parseInt(b.getAttribute("data-detail"), 10))));
    $$("#keysTable [data-revoke]").forEach(b =>
      b.addEventListener("click", () => askRevoke(parseInt(b.getAttribute("data-revoke"), 10))));
  }

  /* ---------- 详情 ---------- */
  async function showLic(id) {
    $("#licModalId").textContent = "#" + id;
    $("#licModalBody").innerHTML = App.loadingState("加载详情…");
    App.openModal("licModal");
    try {
      const data = await App.API.get("/admin2/api/keys", { q: String(id), page: 1, page_size: 1 });
      const l = data.items.find(x => x.id === id);
      if (!l) { $("#licModalBody").innerHTML = App.emptyState("未找到该卡密", ""); return; }
      $("#licModalBody").innerHTML = `
        <dl class="kv">
          <dt>指纹</dt><dd class="mono">${esc(l.hash)}</dd>
          <dt>套餐</dt><dd>${fmt.planBadge(l.plan_key, l.plan_name)} <span class="text-dim">（duration_days = ${l.duration_days}）</span></dd>
          <dt>状态</dt><dd>${fmt.stateBadge(l.state)}</dd>
          <dt>功能标签</dt><dd>${featureHtml(l.features)}</dd>
          <dt>创建时间</dt><dd>${esc(l.created_at_bj || "—")} <span class="text-dim">北京时间</span></dd>
          <dt>兑换时间</dt><dd>${l.redeemed_at_bj ? esc(l.redeemed_at_bj) + ' <span class="text-dim">北京时间</span>' : "—"}</dd>
          <dt>关联设备</dt><dd>${l.device_id ? `<span class="mono">${esc(l.device_id)}</span>` : "—"}</dd>
        </dl>
        <div class="notice notice-info" style="margin-top:16px;margin-bottom:0">${icon("info")}
          <div>明文 Key 无法从指纹反推；「KEY 反向查询」是明文 → SHA-256 → 指纹方向的服务端检索。</div></div>`;
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#licModalBody").innerHTML = App.errorState(err);
    }
  }

  /* ---------- 吊销 ---------- */
  function askRevoke(id) {
    pendingRevokeId = id;
    $("#revokeMsg").innerHTML = `确定吊销卡密 <b>#${id}</b>？吊销后该 Key 将无法被兑换。`;
    App.openModal("revokeModal");
  }
  $("#btnRevokeConfirm").addEventListener("click", async () => {
    try {
      const r = await App.API.post("/admin2/api/keys/revoke", { license_id: pendingRevokeId });
      App.toast(`卡密 #${r.id} 已吊销`, "success");
      App.closeModal("revokeModal");
      load(true);
    } catch (err) {
      if (err.code === "unauthorized") return;
      App.toast(err.message || "吊销失败", "error");
    }
  });

  /* ---------- 批量生成 ---------- */
  $("#btnGenerate").addEventListener("click", () => App.openModal("genModal"));
  $("#btnGenConfirm").addEventListener("click", async () => {
    const days = parseInt($("#genPlan").value, 10);
    const qty = parseInt($("#genQty").value, 10) || 0;
    if (qty < 1 || qty > 1000) { App.toast("数量必须是 1–1000 之间的整数", "error"); return; }
    const btn = $("#btnGenConfirm");
    btn.disabled = true; btn.textContent = "生成中…";
    try {
      const r = await App.API.post("/admin2/api/keys/generate", {
        duration_days: days, quantity: qty, features: $("#genFeatures").value.trim() || null,
      });
      $("#genResultMeta").textContent = `已生成 ${r.count} 个「${r.plan_name}」卡密 · 整批单事务写入`;
      $("#genResultArea").value = r.plaintext_keys.join("\n");
      App.closeModal("genModal");
      App.openModal("genResultModal");
      load();
    } catch (err) {
      if (err.code === "unauthorized") return;
      App.toast(err.message || "生成失败", "error");
    } finally {
      btn.disabled = false; btn.textContent = "生成";
    }
  });
  $("#btnCopyKeys").addEventListener("click", () => App.copyText($("#genResultArea").value, "已复制全部明文 Key"));
  $("#btnGenDone").addEventListener("click", () => {
    App.closeModal("genResultModal");
    $("#genResultArea").value = "";
  });

  /* ---------- KEY 反查 ---------- */
  async function doLookup() {
    const raw = $("#lookupInput").value.trim();
    if (!raw) { App.toast("请先粘贴明文 Key", "warn"); return; }
    const box = $("#lookupResult");
    box.innerHTML = App.loadingState("正在检索…");
    try {
      const r = await App.API.post("/admin2/api/keys/lookup", { license_key: raw });
      if (r.status === "invalid_input") { box.innerHTML = App.emptyState("输入为空", "请粘贴有效的明文 Key"); return; }
      if (r.status === "not_found") {
        box.innerHTML = `<div class="lookup-result">
          <div class="hash-arrow">${icon("key").replace("<svg", '<svg width="13" height="13"')} SHA-256（按此前缀查询 key_hash）：<span class="mono">${esc(r.hash8)}</span></div>
          ${App.emptyState("未找到匹配的卡密", "该 Key 的指纹在库中不存在：Key 不存在、输入有误或记录已被删除。")}</div>`;
        return;
      }
      const l = r.license, d = r.device;
      const statusText = { unused: "未使用 · 可分发", redeemed: "已兑换", revoked: "已吊销" }[r.status] || r.status;
      box.innerHTML = `<div class="lookup-result">
        <div class="hash-arrow">${icon("key").replace("<svg", '<svg width="13" height="13"')} SHA-256：<span class="mono">${esc(r.hash8)}</span></div>
        <div class="notice ${r.status === "unused" ? "notice-info" : "notice-warn"}" style="margin:10px 0 12px">${icon(r.status === "unused" ? "info" : "check")}
          <div>命中卡密 <b>#${l.id}</b> —— 状态 <b>${esc(statusText)}</b>${r.status === "redeemed" && d ? `，当前绑定设备 <b class="mono">${esc(d.device_id)}</b>` : ""}${r.status === "redeemed" && !d ? "，但<strong>未找到关联设备记录</strong>（异常数据，请联系排查）" : ""}</div></div>
        <table class="table" style="min-width:0"><tbody>
          <tr><td class="text-dim" style="width:110px">指纹</td><td class="mono">${esc(l.hash ? l.hash.slice(0, 16) + "…" : "")}</td></tr>
          <tr><td class="text-dim">套餐</td><td>${fmt.planBadge(l.plan_key, l.plan_name)} <span class="text-dim">（${l.duration_days} 天）</span></td></tr>
          <tr><td class="text-dim">状态</td><td>${fmt.stateBadge(l.state)}</td></tr>
          <tr><td class="text-dim">创建时间</td><td>${esc(l.created_at_bj || "—")}</td></tr>
          <tr><td class="text-dim">兑换时间</td><td>${esc(l.redeemed_at_bj || "—")}</td></tr>
          <tr><td class="text-dim">关联设备</td><td>${d ? `<span class="mono">${esc(d.device_id)}</span>（${{ active: "有效授权", expiring: "即将到期", expired: "已过期" }[d.state] || esc(d.state)}，到期 ${esc(d.expires_at_bj || "—")}）` : "—"}</td></tr>
        </tbody></table></div>`;
    } catch (err) {
      if (err.code === "unauthorized") return;
      box.innerHTML = App.errorState(err);
      App.toast(err.message || "查询失败", "error");
    }
  }
  $("#btnLookup").addEventListener("click", doLookup);
  $("#lookupInput").addEventListener("keydown", e => { if (e.key === "Enter") doLookup(); });

  /* ---------- 筛选 / 导出 ---------- */
  $("#planFilter").addEventListener("change", e => { filters.plan = e.target.value; load(); });
  let searchTimer;
  $("#searchInput").addEventListener("input", e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filters.q = e.target.value.trim(); load(); }, 250);
  });
  $("#btnExport").addEventListener("click", () => {
    const params = new URLSearchParams({ scope: "keys" });
    if (filters.state) params.set("state", filters.state.toUpperCase());
    if (filters.plan) params.set("plan", filters.plan);
    if (filters.q) params.set("q", filters.q);
    window.location.href = "/admin2/api/export/csv?" + params.toString();
  });

  load();
})();
