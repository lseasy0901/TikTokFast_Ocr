/* 兑换记录 — 真实 API 版（/admin2/api/redemptions） */
(function () {
  "use strict";
  const { $, $$, icon, fmt, esc } = App;

  let filters = { type: "", plan: "", q: "", from: "", to: "" };
  let counts = { total: 0, first: 0, renew: 0 };

  function renderSeg() {
    const items = [["", "全部流水", counts.total], ["first", "首次激活", counts.first], ["renewal", "续费", counts.renew]];
    $("#typeSeg").innerHTML = items.map(([v, label, cnt]) =>
      `<button data-v="${v}" class="${filters.type === v ? "on" : ""}">${label}<span class="cnt">${cnt}</span></button>`).join("");
    $$("#typeSeg button").forEach(b => b.addEventListener("click", () => {
      filters.type = b.dataset.v; renderSeg(); load();
    }));
  }

  function kpiHtml() {
    return `
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--primary-soft);color:var(--primary-hover)">${icon("swap")}</div>
        <div class="kpi-label">兑换总次数</div>
        <div class="kpi-value">${fmt.num(counts.total)}</div>
        <div class="kpi-foot"><span>每张卡密仅可兑换一次</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--info-soft);color:var(--info)">${icon("plus")}</div>
        <div class="kpi-label">首次激活</div>
        <div class="kpi-value">${fmt.num(counts.first)}</div>
        <div class="kpi-foot"><span>= 新增设备数（口径）</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:rgba(139,92,246,.14);color:#b79bff">${icon("clock")}</div>
        <div class="kpi-label">老设备续费</div>
        <div class="kpi-value">${fmt.num(counts.renew)}</div>
        <div class="kpi-foot"><span>含到期重置与累加（历史不可细分）</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--success-soft);color:var(--success)">${icon("users")}</div>
        <div class="kpi-label">统计区间</div>
        <div class="kpi-value" style="font-size:18px;line-height:2.2">${filters.from || filters.to ? esc((filters.from || "起") + " 至 " + (filters.to || "今")) : "全部历史"}</div>
        <div class="kpi-foot"><span>北京时间</span></div>
      </div>`;
  }

  function rowHtml(r) {
    return `<tr>
      <td class="num text-2">${esc(r.redeemed_at_bj)}</td>
      <td>${fmt.typeBadge(r.type)}</td>
      <td class="mono">${esc(r.hash8)}</td>
      <td>${fmt.planBadge(r.plan_key, r.plan_name)}</td>
      <td class="mono">${esc(r.device_id || "—")}</td>
      <td class="text-2">${esc(r.result)}</td>
      <td><a class="btn btn-ghost btn-sm" href="/admin2/devices">查看设备</a></td>
    </tr>`;
  }

  const pager = App.Paginator({ footEl: "#rdFoot", pageSize: 12, onChange: () => load(true) });

  async function load(keepPage) {
    $("#rdTable").innerHTML = App.loadingState("加载兑换流水…");
    try {
      const data = await App.API.get("/admin2/api/redemptions", {
        type_: filters.type || undefined, plan: filters.plan || undefined, q: filters.q || undefined,
        date_from: filters.from || undefined, date_to: filters.to || undefined,
        page: keepPage ? pager.page() : 1, page_size: 12,
      });
      counts = data.counts;
      $("#kpiGrid").innerHTML = kpiHtml();
      renderSeg();
      $("#rdTable").innerHTML = data.items.length === 0
        ? App.emptyState("该筛选条件下没有兑换记录", "放宽日期范围或清除筛选")
        : `<table class="table">
            <thead><tr>
              <th>兑换时间（北京时间）</th><th>类型</th><th>KEY 指纹</th><th>套餐</th>
              <th>设备 ID</th><th>授权变更</th><th style="width:90px">操作</th>
            </tr></thead>
            <tbody>${data.items.map(rowHtml).join("")}</tbody>
          </table>`;
      pager.set(data.total, true);
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#rdTable").innerHTML = App.errorState(err, "reloadRedemptions");
      App.toast(err.message || "加载失败", "error");
    }
  }
  window.reloadRedemptions = () => load();

  function resetFilters() {
    filters = { type: "", plan: "", q: "", from: "", to: "" };
    $("#dateFrom").value = ""; $("#dateTo").value = ""; $("#quickRange").value = "";
    $("#planFilter").value = ""; $("#searchInput").value = "";
    load();
  }

  $("#planFilter").addEventListener("change", e => { filters.plan = e.target.value; load(); });
  let searchTimer;
  $("#searchInput").addEventListener("input", e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filters.q = e.target.value.trim(); load(); }, 250);
  });
  $("#dateFrom").addEventListener("change", e => { filters.from = e.target.value; load(); });
  $("#dateTo").addEventListener("change", e => { filters.to = e.target.value; load(); });
  $("#quickRange").addEventListener("change", e => {
    const v = e.target.value;
    if (!v) { filters.from = ""; filters.to = ""; }
    else {
      const to = new Date(); const from = new Date(to.getTime() - (parseInt(v, 10) - 1) * 86400000);
      const p = x => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
      filters.from = p(from); filters.to = p(to);
      $("#dateFrom").value = filters.from; $("#dateTo").value = filters.to;
    }
    load();
  });

  $("#btnExport").addEventListener("click", () => {
    const params = new URLSearchParams({ scope: "redemptions" });
    if (filters.type) params.set("type_", filters.type);
    if (filters.plan) params.set("plan", filters.plan);
    if (filters.q) params.set("q", filters.q);
    if (filters.from) params.set("date_from", filters.from);
    if (filters.to) params.set("date_to", filters.to);
    window.location.href = "/admin2/api/export/csv?" + params.toString();
  });

  load();
})();
