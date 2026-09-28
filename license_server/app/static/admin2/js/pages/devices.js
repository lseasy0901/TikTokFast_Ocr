/* 设备授权 — 真实 API 版（/admin2/api/devices*） */
(function () {
  "use strict";
  const { $, $$, icon, fmt, esc } = App;

  let filters = { q: "", state: "", sort: "remain_asc" };

  function kpiHtml(counts) {
    const active = counts.active, expiring = counts.expiring, expired = counts.expired, total = counts.total;
    return `
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--success-soft);color:var(--success)">${icon("check")}</div>
        <div class="kpi-label">有效授权设备</div>
        <div class="kpi-value">${fmt.num(active)}</div>
        <div class="kpi-foot"><span>含 7 日内到期 ${expiring} 台</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--warning-soft);color:var(--warning)">${icon("alert")}</div>
        <div class="kpi-label">7 日内到期</div>
        <div class="kpi-value">${fmt.num(expiring)}</div>
        <div class="kpi-foot"><span>建议提前触达续费</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--danger-soft);color:var(--danger)">${icon("ban")}</div>
        <div class="kpi-label">已过期</div>
        <div class="kpi-value">${fmt.num(expired)}</div>
        <div class="kpi-foot"><span>过期设备仍计入 DAU（口径）</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--primary-soft);color:var(--primary-hover)">${icon("users")}</div>
        <div class="kpi-label">累计去重设备</div>
        <div class="kpi-value">${fmt.num(total)}</div>
        <div class="kpi-foot"><span>与 DAU 表累计口径一致</span></div>
      </div>`;
  }

  function rowHtml(d) {
    return `<tr>
      <td class="mono">${esc(d.device_id)}
        <button class="copy-btn" title="复制设备 ID" data-copy="${esc(d.device_id)}">${icon("copy")}</button></td>
      <td>${fmt.stateBadge(d.state)}</td>
      <td>${App.meter(d)}</td>
      <td class="num text-2">${esc(d.first_activation_bj || "—")}</td>
      <td class="num">${esc(d.expires_at_bj || "—")}</td>
      <td class="num text-2">${esc(d.last_seen_bj || "—")}</td>
      <td class="num">${d.redemptions}</td>
      <td>${fmt.planBadge(d.latest_plan_key, d.latest_plan_name)}</td>
      <td><button class="btn btn-ghost btn-sm" data-detail="${esc(d.device_id)}">详情</button></td>
    </tr>`;
  }

  const pager = App.Paginator({ footEl: "#devFoot", pageSize: 10, onChange: () => load(true) });

  async function load(keepPage) {
    $("#devTable").innerHTML = App.loadingState("加载设备列表…");
    try {
      const data = await App.API.get("/admin2/api/devices", {
        state: filters.state || undefined, q: filters.q || undefined,
        sort: filters.sort, page: keepPage ? pager.page() : 1, page_size: 10,
      });
      $("#kpiGrid").innerHTML = kpiHtml(data.counts);
      $("#devTable").innerHTML = data.items.length === 0
        ? App.emptyState("没有匹配的设备", "调整状态筛选或搜索关键词")
        : `<table class="table">
            <thead><tr>
              <th>设备 ID</th><th>状态</th><th>授权余量</th><th>首次激活</th>
              <th>到期时间</th><th>最近活跃</th><th>兑换次数</th><th>最新套餐</th><th style="width:80px">操作</th>
            </tr></thead>
            <tbody>${data.items.map(rowHtml).join("")}</tbody>
          </table>`;
      pager.set(data.total, true);
      bindRows();
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#devTable").innerHTML = App.errorState(err, "reloadDevices");
      App.toast(err.message || "加载失败", "error");
    }
  }
  window.reloadDevices = () => load();

  function bindRows() {
    $$("#devTable [data-copy]").forEach(b =>
      b.addEventListener("click", () => App.copyText(b.getAttribute("data-copy"), "设备 ID 已复制")));
    $$("#devTable [data-detail]").forEach(b =>
      b.addEventListener("click", () => openDevice(b.getAttribute("data-detail"))));
  }

  async function openDevice(id) {
    $("#drawerTitle").textContent = id;
    $("#drawerBody").innerHTML = App.loadingState("加载设备详情…");
    App.openDrawer("devDrawer");
    try {
      const d = await App.API.get("/admin2/api/devices/" + encodeURIComponent(id));
      const hist = (d.history || []).slice().reverse().map(h => `
        <div class="tl-item ${h.type === "renewal" ? "tl-renew" : ""}">
          <div class="tl-title">${h.type === "first" ? "首次激活" : "续费"} · ${esc(h.plan_name)}</div>
          <div class="tl-meta">${esc(h.time_bj || "")} · ${esc(h.result)} · 指纹 ${esc(h.hash8)}</div>
        </div>`).join("");
      $("#drawerBody").innerHTML = `
        <div class="drawer-sec">
          <h4>授权状态</h4>
          <dl class="kv">
            <dt>状态</dt><dd>${fmt.stateBadge(d.state)}</dd>
            <dt>当前到期</dt><dd>${esc(d.expires_at_bj || "—")}</dd>
            <dt>剩余</dt><dd>${d.remain_days == null ? "—" : d.remain_days < 0 ? "已过期 " + (-d.remain_days) + " 天" : "剩余 " + d.remain_days + " 天"}</dd>
            <dt>首次激活</dt><dd>${esc(d.first_activation_bj || "—")}</dd>
            <dt>激活时间</dt><dd>${esc(d.activated_at_bj || "—（未发生过过期重置，激活时间以首次激活为准）")}</dd>
            <dt>最近活跃</dt><dd>${esc(d.last_seen_bj || "—")}</dd>
            <dt>兑换次数</dt><dd>${d.redemptions} 次</dd>
            <dt>功能授权</dt><dd>${d.features.length ? d.features.map(f => `<span class="badge no-dot badge-muted" style="font-size:11px;padding:2px 8px">${esc(f)}</span>`).join(" ") : "—"}</dd>
          </dl>
        </div>
        <div class="drawer-sec">
          <h4>兑换 / 授权时间线</h4>
          <div class="timeline">${hist || '<div class="text-dim">暂无记录</div>'}</div>
        </div>
        <div class="notice notice-info">${icon("info")}
          <div>「累加 / 重置」语义与 redemption_service 状态机一致；历史续费在事件日志表上线前无法区分，标记为不可回溯。</div></div>`;
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#drawerBody").innerHTML = App.errorState(err);
    }
  }
  $("#drawerClose").addEventListener("click", () => App.closeDrawer("devDrawer"));
  $("#devDrawerOverlay").addEventListener("click", () => App.closeDrawer("devDrawer"));

  let searchTimer;
  $("#searchInput").addEventListener("input", e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filters.q = e.target.value.trim(); load(); }, 250);
  });
  $("#stateFilter").addEventListener("change", e => { filters.state = e.target.value; load(); });
  $("#sortSel").addEventListener("change", e => { filters.sort = e.target.value; load(); });

  $("#btnExport").addEventListener("click", () => {
    const params = new URLSearchParams({ scope: "devices" });
    if (filters.state) params.set("state", filters.state);
    if (filters.q) params.set("q", filters.q);
    window.location.href = "/admin2/api/export/csv?" + params.toString();
  });

  load();
})();
