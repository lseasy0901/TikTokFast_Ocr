/* ============================================================
   LiveLens Admin 2.0 — 轻量 SVG 图表（原型，无外部依赖）
   line/area 折线面积图 · 堆叠柱状图 · 环形图
   ============================================================ */

(function () {
  "use strict";
  const Charts = (window.Charts = {});
  const NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function niceMax(v) {
    if (v <= 5) return 5;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / pow;
    const step = n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * pow;
  }

  function attachTip(container) {
    let tip = container.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      container.appendChild(tip);
    }
    return tip;
  }

  /* ------------------------------------------------------------
     折线 / 面积图
     series: [{ name, color, values:number[], area?:bool, dash?:bool }]
     labels: string[]（x 轴，可稀疏标注）
     ------------------------------------------------------------ */
  Charts.line = function (container, labels, series, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const W = opts.width || 720, H = opts.height || 260;
    const pad = { l: 42, r: 14, t: 14, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const n = labels.length;

    const maxV = niceMax(Math.max(1, ...series.flatMap((s) => s.values)) * 1.08);
    const x = (i) => pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
    const y = (v) => pad.t + ih - (v / maxV) * ih;

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
    svg.style.height = opts.cssHeight || "260px";

    // 网格 + y 轴标签
    for (let g = 0; g <= 4; g++) {
      const gv = (maxV / 4) * g;
      const gy = y(gv);
      svg.appendChild(svgEl("line", { x1: pad.l, y1: gy, x2: W - pad.r, y2: gy, stroke: "#1c2434", "stroke-width": 1 }));
      const t = svgEl("text", { x: pad.l - 8, y: gy + 4, "text-anchor": "end", "font-size": 10.5, fill: "#626d82" });
      t.textContent = Math.round(gv);
      svg.appendChild(t);
    }

    // x 轴标签（最多 ~8 个）
    const labelStep = Math.max(1, Math.ceil(n / 8));
    labels.forEach((lb, i) => {
      if (i % labelStep !== 0 && i !== n - 1) return;
      const t = svgEl("text", { x: x(i), y: H - 8, "text-anchor": "middle", "font-size": 10.5, fill: "#626d82" });
      t.textContent = lb;
      svg.appendChild(t);
    });

    // 面积 + 线
    for (const s of series) {
      const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
      if (s.area !== false) {
        svg.appendChild(svgEl("polygon", {
          points: `${pad.l},${y(0)} ${pts} ${x(n - 1)},${y(0)}`,
          fill: s.color, opacity: 0.1,
        }));
      }
      svg.appendChild(svgEl("polyline", {
        points: pts, fill: "none", stroke: s.color, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": round(),
        "stroke-dasharray": s.dash ? "5 4" : "none",
      }));
    }
    function round() { return "round"; }

    // hover 参考线 + 交互
    const hoverLine = svgEl("line", { y1: pad.t, y2: pad.t + ih, stroke: "#3b4a66", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.appendChild(hoverLine);
    const dots = series.map((s) => {
      const c = svgEl("circle", { r: 3.5, fill: s.color, stroke: "#10151f", "stroke-width": 2, visibility: "hidden" });
      svg.appendChild(c);
      return c;
    });

    const tip = attachTip(container);
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const relX = ((e.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((relX - pad.l) / iw) * (n - 1))));
      hoverLine.setAttribute("x1", x(i)); hoverLine.setAttribute("x2", x(i));
      hoverLine.setAttribute("visibility", "visible");
      series.forEach((s, si) => {
        dots[si].setAttribute("cx", x(i)); dots[si].setAttribute("cy", y(s.values[i]));
        dots[si].setAttribute("visibility", "visible");
      });
      tip.style.display = "block";
      const px = (x(i) / W) * rect.width;
      tip.style.left = Math.min(rect.width - 150, Math.max(0, px + 10)) + "px";
      tip.style.top = "6px";
      tip.innerHTML = `<div class="tt-date">${opts.tipTitle ? opts.tipTitle(labels[i]) : labels[i]}</div>` +
        series.map((s) => `<div class="tt-row"><span class="tt-dot" style="background:${s.color}"></span>${s.name}<b>${(s.values[i] == null ? "—" : s.values[i].toLocaleString("zh-CN"))}${s.unit || ""}</b></div>`).join("");
    });
    svg.addEventListener("mouseleave", () => {
      hoverLine.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      tip.style.display = "none";
    });

    container.appendChild(svg);
  };

  /* ------------------------------------------------------------
     堆叠柱状图
     stacks: [{ label, parts: { planKey: value } }]
     planColors: { key: color }
     ------------------------------------------------------------ */
  Charts.stackedBars = function (container, labels, stacks, planColors, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const W = opts.width || 720, H = opts.height || 260;
    const pad = { l: 42, r: 14, t: 14, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const n = stacks.length;
    const totals = stacks.map((s) => Object.values(s.parts).reduce((a, b) => a + b, 0));
    const maxV = niceMax(Math.max(1, ...totals) * 1.08);
    const slot = iw / n;
    const bw = Math.max(3, Math.min(26, slot * 0.52));

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
    svg.style.height = opts.cssHeight || "260px";

    for (let g = 0; g <= 4; g++) {
      const gv = (maxV / 4) * g;
      const gy = pad.t + ih - (gv / maxV) * ih;
      svg.appendChild(svgEl("line", { x1: pad.l, y1: gy, x2: W - pad.r, y2: gy, stroke: "#1c2434", "stroke-width": 1 }));
      const t = svgEl("text", { x: pad.l - 8, y: gy + 4, "text-anchor": "end", "font-size": 10.5, fill: "#626d82" });
      t.textContent = Math.round(gv);
      svg.appendChild(t);
    }

    const labelStep = Math.max(1, Math.ceil(n / 8));
    stacks.forEach((st, i) => {
      const cx = pad.l + slot * i + slot / 2;
      let acc = 0;
      for (const key of Object.keys(st.parts)) {
        const v = st.parts[key];
        if (!v) { continue; }
        const h = (v / maxV) * ih;
        const yTop = pad.t + ih - acc - h;
        svg.appendChild(svgEl("rect", {
          x: cx - bw / 2, y: yTop, width: bw, height: Math.max(0.5, h),
          rx: Math.min(3, bw / 4), fill: planColors[key] || "#626d82", opacity: 0.92,
        }));
        acc += h;
      }
      if (i % labelStep === 0 || i === n - 1) {
        const t = svgEl("text", { x: cx, y: H - 8, "text-anchor": "middle", "font-size": 10.5, fill: "#626d82" });
        t.textContent = labels[i];
        svg.appendChild(t);
      }
    });

    const tip = attachTip(container);
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const relX = ((e.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.floor(((relX - pad.l) / iw) * n)));
      const st = stacks[i];
      tip.style.display = "block";
      const px = ((pad.l + slot * i + slot / 2) / W) * rect.width;
      tip.style.left = Math.min(rect.width - 150, Math.max(0, px + 10)) + "px";
      tip.style.top = "6px";
      tip.innerHTML = `<div class="tt-date">${opts.tipTitle ? opts.tipTitle(labels[i]) : labels[i]}</div>` +
        Object.keys(st.parts).map((k) =>
          `<div class="tt-row"><span class="tt-dot" style="background:${planColors[k] || "#888"}"></span>${opts.partNames ? opts.partNames[k] || k : k}<b>${st.parts[k].toLocaleString("zh-CN")}</b></div>`
        ).join("") + (opts.totalLine ? `<div class="tt-row" style="border-top:1px solid #2b3549;margin-top:4px;padding-top:4px">合计<b>${totals[i].toLocaleString("zh-CN")}</b></div>` : "");
    });
    svg.addEventListener("mouseleave", () => (tip.style.display = "none"));

    container.appendChild(svg);
  };

  /* ------------------------------------------------------------
     环形图
     slices: [{ key, label, value, color }]
     ------------------------------------------------------------ */
  Charts.donut = function (container, slices, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const size = opts.size || 190, r = size / 2 - 12, cx = size / 2, cy = size / 2;
    const total = slices.reduce((a, s) => a + s.value, 0);
    const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size });

    svg.appendChild(svgEl("circle", { cx, cy, r, fill: "none", stroke: "#1c2434", "stroke-width": 18 }));

    if (total > 0) {
      let a0 = -Math.PI / 2;
      for (const s of slices) {
        if (!s.value) continue;
        const frac = s.value / total;
        const a1 = a0 + frac * Math.PI * 2;
        const large = frac > 0.5 ? 1 : 0;
        const p0 = [cx + r * Math.cos(a0), cy + r * Math.sin(a0)];
        const p1 = [cx + r * Math.cos(a1), cy + r * Math.sin(a1)];
        svg.appendChild(svgEl("path", {
          d: `M ${p0[0]} ${p0[1]} A ${r} ${r} 0 ${large} 1 ${p1[0]} ${p1[1]}`,
          fill: "none", stroke: s.color, "stroke-width": 18, "stroke-linecap": "butt",
        }));
        a0 = a1;
      }
    }

    const t1 = svgEl("text", { x: cx, y: cy - 4, "text-anchor": "middle", "font-size": 20, "font-weight": 700, fill: "#e8ecf4" });
    t1.textContent = total.toLocaleString("zh-CN");
    const t2 = svgEl("text", { x: cx, y: cy + 15, "text-anchor": "middle", "font-size": 10.5, fill: "#626d82" });
    t2.textContent = opts.centerLabel || "合计";
    svg.appendChild(t1); svg.appendChild(t2);
    container.appendChild(svg);
  };
})();
