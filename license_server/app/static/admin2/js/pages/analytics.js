/* 数据分析 — 真实 API 版（/admin2/api/analytics） */
(function () {
  "use strict";
  const { $, $$, fmt, esc } = App;

  let range = { days: 30, from: null, to: null };

  const planNames = { d1: "天卡", m30: "月卡", h180: "半年卡", y365: "年卡", other: "其他" };
  const planColors = App.PLAN_COLORS;

  function renderKPI(totals, label) {
    $("#kpiGrid").innerHTML = `
      <div class="kpi">
        <div class="kpi-label">区间新增设备</div>
        <div class="kpi-value">${fmt.num(totals.new_devices)}</div>
        <div class="kpi-foot"><span>首次激活口径，日均 ${Math.round(totals.new_devices / Math.max(1, totals.days))}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">区间兑换次数</div>
        <div class="kpi-value">${fmt.num(totals.redeem_total)}</div>
        <div class="kpi-foot"><span>日均 ${Math.round(totals.redeem_total / Math.max(1, totals.days))}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">日均 DAU</div>
        <div class="kpi-value">${fmt.num(totals.dau_avg)}</div>
        <div class="kpi-foot"><span>北京日切去重</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">DAU 峰值</div>
        <div class="kpi-value">${fmt.num(totals.dau_peak)}</div>
        <div class="kpi-foot"><span>区间内最高单日</span></div>
      </div>`;
    $("#rangeLabel").textContent = label;
  }

  function renderCharts(rows) {
    $("#planLegend").innerHTML = Object.keys(planNames).map(k =>
      `<span class="lg"><i style="background:${planColors[k]}"></i>${planNames[k]}</span>`).join("");

    Charts.stackedBars($("#planBars"), rows.map(d => d.date.slice(5)),
      rows.map(d => ({ label: d.date, parts: { d1: d.d1, m30: d.m30, h180: d.h180, y365: d.y365, other: d.other } })),
      planColors, { partNames: planNames, totalLine: true, tipTitle: lb => lb + "（北京时间）" });

    Charts.line($("#trendChart"), rows.map(d => d.date.slice(5)), [
      { name: "新增设备", color: "#4d7cfe", values: rows.map(d => d.new_devices) },
      { name: "DAU", color: "#38bdf8", values: rows.map(d => d.dau), dash: true, area: false },
    ], { tipTitle: lb => lb + "（北京时间）" });
  }

  function renderTable(rows) {
    const rowHtml = d => `<tr>
      <td class="num">${esc(d.date)}</td>
      <td class="num"><b>${d.new_devices}</b></td>
      <td class="num">${d.first}</td>
      <td class="num">${d.renew}</td>
      <td class="num" style="color:${planColors.d1}">${d.d1}</td>
      <td class="num" style="color:${planColors.m30}">${d.m30}</td>
      <td class="num" style="color:${planColors.h180}">${d.h180}</td>
      <td class="num" style="color:${planColors.y365}">${d.y365}</td>
      <td class="num" style="color:${planColors.other}">${d.other}</td>
      <td class="num">${d.dau}</td>
    </tr>`;

    $("#dailyTable").innerHTML = rows.length === 0
      ? App.emptyState("区间内没有数据", "调整日期范围后重试")
      : `<table class="table">
          <thead><tr>
            <th>日期</th><th>新增设备</th><th>首次激活</th><th>续费</th>
            <th>天卡</th><th>月卡</th><th>半年卡</th><th>年卡</th><th>其他</th><th>DAU</th>
          </tr></thead>
          <tbody>${rows.map(rowHtml).join("")}</tbody>
        </table>`;
  }

  const pager = App.Paginator({ footEl: "#dailyFoot", pageSize: 15, onChange: () => load() });

  async function load() {
    $("#dailyTable").innerHTML = App.loadingState("加载统计数据…");
    try {
      const data = await App.API.get("/admin2/api/analytics", {
        days: range.from && range.to ? undefined : range.days,
        date_from: range.from || undefined, date_to: range.to || undefined,
      });
      renderKPI(data.totals, data.totals.days ? `共 ${data.totals.days} 天` : "");
      renderCharts(data.rows);
      renderTable(data.rows);
      pager.set(data.rows.length, true);
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#dailyTable").innerHTML = App.errorState(err, "reloadAnalytics");
      App.toast(err.message || "加载失败", "error");
    }
  }
  window.reloadAnalytics = () => load();

  $$("#rangeSeg button").forEach(b => b.addEventListener("click", () => {
    $$("#rangeSeg button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    range.days = parseInt(b.dataset.days, 10);
    range.from = range.to = null;
    $("#dateFrom").value = ""; $("#dateTo").value = "";
    load();
  }));

  function applyCustom() {
    const f = $("#dateFrom").value, t = $("#dateTo").value;
    if (!f || !t) { App.toast("请选择开始与结束日期", "warn"); return; }
    if (f > t) { App.toast("开始日期不能晚于结束日期", "error"); return; }
    range.from = f; range.to = t;
    $$("#rangeSeg button").forEach(x => x.classList.remove("on"));
    load();
  }
  $("#btnApplyRange").addEventListener("click", applyCustom);

  $("#btnExport").addEventListener("click", () => {
    const params = new URLSearchParams({ scope: "daily" });
    if (range.from && range.to) { params.set("date_from", range.from); params.set("date_to", range.to); }
    else params.set("days", range.days);
    window.location.href = "/admin2/api/export/csv?" + params.toString();
  });

  load();
})();
