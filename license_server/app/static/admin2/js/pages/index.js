/* 数据总览 — 真实 API 版（/admin2/api/overview） */
(function () {
  "use strict";
  const { $, $$, icon, fmt, esc } = App;
  const $chart = $("#trendChart");

  let cached = null;      // overview 全量数据（今日/趋势等）
  let trendDays = 30;

  function delta(cur, prev) {
    if (!prev) return '<span class="delta-up">—</span><span>较昨日</span>';
    const d = ((cur - prev) / prev) * 100;
    const cls = d >= 0 ? "delta-up" : "delta-down";
    return `<span class="${cls}">${d >= 0 ? "+" : ""}${d.toFixed(1)}%</span><span>较昨日</span>`;
  }

  function render(data) {
    const t = data.today, y = data.yesterday, dv = data.devices;
    $("#kpiGrid").innerHTML = `
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--primary-soft);color:var(--primary-hover)">${icon("plus")}</div>
        <div class="kpi-label">${icon("device").replace("<svg", '<svg width="14" height="14"')} 今日新增设备</div>
        <div class="kpi-value">${fmt.num(t ? t.new_devices : 0)}</div>
        <div class="kpi-foot">${t ? delta(t.new_devices, y ? y.new_devices : 0) : ""}</div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--info-soft);color:var(--info)">${icon("chart")}</div>
        <div class="kpi-label">今日 DAU（活跃设备）</div>
        <div class="kpi-value">${fmt.num(t ? t.dau : 0)}</div>
        <div class="kpi-foot">${t ? delta(t.dau, y ? y.dau : 0) : ""}</div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:var(--success-soft);color:var(--success)">${icon("swap")}</div>
        <div class="kpi-label">今日兑换次数</div>
        <div class="kpi-value">${fmt.num(t ? t.first + t.renew : 0)}</div>
        <div class="kpi-foot">${t ? delta(t.first + t.renew, y ? y.first + y.renew : 0) : ""}</div>
      </div>
      <div class="kpi">
        <div class="kpi-ico" style="background:rgba(139,92,246,.14);color:#b79bff">${icon("users")}</div>
        <div class="kpi-label">有效授权设备</div>
        <div class="kpi-value">${fmt.num(dv.active)}</div>
        <div class="kpi-foot"><span>累计设备 ${fmt.num(dv.total)} 台</span></div>
      </div>`;

    // 套餐卡片（今日兑换，按套餐；来自趋势最后一行）
    const plans = t ? { d1: t.d1, m30: t.m30, h180: t.h180, y365: t.y365, other: t.other } : {};
    const total = Object.values(plans).reduce((a, b) => a + b, 0);
    const names = { d1: "天卡", m30: "月卡", h180: "半年卡", y365: "年卡", other: "其他" };
    const days = { d1: 1, m30: 30, h180: 180, y365: 365 };
    $("#planGrid").innerHTML = Object.keys(names).map((k) => {
      const v = plans[k] || 0;
      const pct = total ? Math.round(v / total * 100) : 0;
      const sub = days[k] ? `· ${days[k]} 天` : "· 非预设时长";
      return `<div class="plan-card">
        <div class="p-head">${fmt.planDot(k)} ${names[k]} <span class="text-dim">${sub}</span></div>
        <div class="plan-value">${v}<span style="font-size:13px;color:var(--text-3);font-weight:400"> 次/今日</span></div>
        <div class="plan-sub">近 30 天 ${(data.plan_totals_window || {})[k] || 0} 次</div>
        <div class="plan-track"><div class="plan-fill" style="width:${pct}%;background:${App.PLAN_COLORS[k]}"></div></div>
      </div>`;
    }).join("");

    renderTrend();
    renderDonut(data.plan_totals_window || {});
    renderRecent(data.recent_redemptions || []);
    renderAlerts(data.expiring_soon || [], dv);
  }

  function renderTrend() {
    const rows = (cached.trend || []).slice(-trendDays);
    Charts.line($chart, rows.map(r => r.date.slice(5)), [
      { name: "新增设备", color: "#4d7cfe", values: rows.map(r => r.new_devices) },
      { name: "DAU", color: "#38bdf8", values: rows.map(r => r.dau), dash: true, area: false },
    ], { tipTitle: (lb) => lb + "（北京时间）" });
  }

  function renderDonut(totals) {
    const names = { d1: "天卡", m30: "月卡", h180: "半年卡", y365: "年卡", other: "其他" };
    const slices = Object.keys(names).map(k => ({ key: k, label: names[k], value: totals[k] || 0, color: App.PLAN_COLORS[k] }));
    const total = slices.reduce((a, s) => a + s.value, 0);
    Charts.donut($("#planDonut"), slices, { centerLabel: "近30天兑换" });
    $("#planLegend").innerHTML = slices.map(s =>
      `<span class="lg"><i style="background:${s.color}"></i>${s.label}<b style="color:var(--text-1)">${s.value}</b><span class="text-dim">${total ? Math.round(s.value / total * 100) : 0}%</span></span>`).join("");
  }

  function renderRecent(items) {
    if (!items.length) {
      $("#recentRedeem").innerHTML = App.emptyState("还没有兑换记录", "卡密被成功兑换后会出现在这里");
      return;
    }
    $("#recentRedeem").innerHTML = `<table class="table">
      <thead><tr><th>时间</th><th>类型</th><th>套餐</th><th>设备</th><th>KEY 指纹</th></tr></thead>
      <tbody>${items.map(r => `<tr>
        <td class="num text-2">${esc(fmt.short(r.redeemed_at_bj))}</td>
        <td>${fmt.typeBadge(r.type)}</td>
        <td>${fmt.planBadge(r.plan_key, r.plan_name)}</td>
        <td class="mono">${esc(r.device_id || "—")}</td>
        <td class="mono text-dim">${esc(r.hash8)}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  }

  function renderAlerts(expiring, dv) {
    let html = expiring.map(d => `
      <div class="alert-row">
        ${icon("alert").replace("<svg", '<svg width="15" height="15" style="color:var(--warning)"')}
        <span class="a-device">${esc(d.device_id)}</span>
        <span class="badge badge-warning">${d.remain_days === 0 ? "今日到期" : "剩余 " + d.remain_days + " 天"}</span>
        <div class="a-right"><span>到期 ${esc(d.expires_at_bj || "")}</span></div>
      </div>`).join("");
    html += `<div class="alert-row">
        ${icon("key").replace("<svg", '<svg width="15" height="15" style="color:var(--info)"')}
        <span>7 日内到期设备</span>
        <span class="badge badge-warning">${dv.expiring} 台</span>
        <div class="a-right"><a href="/admin2/devices" class="btn btn-ghost btn-sm">去处理 →</a></div>
      </div>
      <div class="alert-row">
        ${icon("key").replace("<svg", '<svg width="15" height="15" style="color:var(--info)"')}
        <span>卡密库存（未使用）</span>
        <span class="badge badge-info">${dv.total ? "见卡密管理" : "0 枚"}</span>
        <div class="a-right"><a href="/admin2/keys" class="btn btn-ghost btn-sm">去补充 →</a></div>
      </div>`;
    $("#alertList").innerHTML = html;
  }

  async function load() {
    $("#kpiGrid").innerHTML = `<div class="kpi" style="grid-column:1/-1">${App.loadingState("加载总览数据…")}</div>`;
    try {
      cached = await App.API.get("/admin2/api/overview", { days: 30 });
      render(cached);
    } catch (err) {
      if (err.code === "unauthorized") return;
      $("#kpiGrid").innerHTML = `<div class="kpi" style="grid-column:1/-1">${App.errorState(err, "load")}</div>`;
      App.toast(err.message || "加载失败", "error");
    }
  }
  window.load = load;

  $$("#trendRange button").forEach(b => b.addEventListener("click", () => {
    $$("#trendRange button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    trendDays = parseInt(b.dataset.days, 10);
    renderTrend();
  }));
  $("#btnRefresh").addEventListener("click", () => { load(); App.toast("已刷新", "success", 1500); });

  load();
})();
