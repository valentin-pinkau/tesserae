// ostrom_prices, Spectra chart archetype. Today's hourly electricity spot
// price (gross ct/kWh, Ostrom API) as a line, over a shaded p25-p75 band
// computed per hour-of-day across the prior week, so the eye reads "is now
// cheap or dear vs. the recent norm". The current hour is dotted in accent-1.
// Renders once, statically, then gets screenshotted, no motion, no fetch.
//
// Built directly on Chart.js rather than the spectra-chart helpers because a
// fill band between two series isn't something barChart/lineChart expose; we
// still pull all colours from tokens() so the cell theme drives the palette.

import { tokens } from "../../static/spectra-chart.js";

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  return Number(v).toFixed(1);
}

// Hex / rgb(a) → rgba with a new alpha. Inlined (same logic spectra-chart.js
// uses internally) so the direct-chart path doesn't depend on it.
function withAlpha(color, alpha) {
  if (typeof color !== "string") return color;
  if (color.startsWith("#") && color.length === 7) {
    const r = parseInt(color.slice(1, 3), 16);
    const g = parseInt(color.slice(3, 5), 16);
    const b = parseInt(color.slice(5, 7), 16);
    if (!Number.isNaN(r)) return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  const m = color.match(
    /^\s*rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)/
  );
  if (m) return `rgba(${m[1]}, ${m[2]}, ${m[3]}, ${alpha})`;
  return color;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const opts = ctx?.cell?.options || {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const title = String(opts.title || "").trim() || "Electricity";

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="ostrom_prices">
        <div class="w-title"><i class="ph-bold ph-warning-circle" style="color:var(--accent-1)"></i><h3>${escapeHtml(title)}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const today = Array.isArray(data.today) ? data.today.map(Number) : [];
  const labels = Array.isArray(data.labels) ? data.labels.map(String) : [];
  const p25 = Array.isArray(data.p25) ? data.p25.map((v) => (v == null ? null : Number(v))) : null;
  const p75 = Array.isArray(data.p75) ? data.p75.map((v) => (v == null ? null : Number(v))) : null;
  const hasBand = Boolean(data.hasBand) && p25 && p75;
  const nowIndex = Number.isInteger(data.nowIndex) ? data.nowIndex : -1;
  const unit = data.unit || "ct/kWh";
  const hasData = today.length >= 2;

  // shadow.host is .cell-content; clientWidth reads the pre-scale virtual
  // cell width, the same number the container-query breakpoints fire on.
  const cellWidth = shadow.host?.clientWidth || 600;
  const showChip = cellWidth > 280;   // "now" price
  const showY = cellWidth > 280;
  const showLegend = cellWidth > 440;

  // Min/max used to live here too, but they're now labelled directly on the
  // chart at the day's high/low point, so the chip stays just the current
  // price and never crowds the title.
  const chip = (showChip && data.now != null)
    ? `
      <span class="ep-chip">
        <span class="now"><span class="now-label">Now:</span> ${escapeHtml(fmtPrice(data.now))}<span class="u">${escapeHtml(unit)}</span></span>
      </span>`
    : "";

  const legend = showLegend
    ? `
      <div class="ep-legend">
        <span class="ep-key"><span class="sw line-sw"></span>Today</span>
        ${hasBand ? '<span class="ep-key"><span class="sw band-sw"></span>Typical (7&nbsp;days)</span>' : ""}
        <span class="ep-key"><i class="ph-bold ph-circle-fill now-sw"></i>Now</span>
      </div>`
    : "";

  const layout = `
    .ep-body { gap: var(--space-2); }
    .ep-chip {
      margin-left: auto;
      flex: 0 0 auto;
      white-space: nowrap;
      display: inline-flex;
      align-items: baseline;
      gap: var(--space-2);
      font-size: var(--fs-label);
      font-weight: var(--fw-bold);
      letter-spacing: var(--ls-label);
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    .ep-chip .now { color: var(--accent-1); }
    .ep-chip .now-label { color: var(--text-muted); font-weight: var(--fw-bold); }
    .ep-chip .now .u { font-size: 0.72em; margin-left: 0.15em; color: var(--text-muted); }
    .ep-chart { flex: 1 1 auto; min-height: 0; position: relative; }
    .ep-legend {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-3) var(--space-4);
      align-items: center;
      flex: 0 0 auto;
    }
    .ep-key {
      display: inline-flex;
      align-items: center;
      gap: 0.45em;
      font-size: var(--fs-caption);
      font-weight: var(--fw-bold);
      color: var(--text-secondary);
      letter-spacing: var(--ls-label);
    }
    .ep-key .sw { width: 0.95em; height: 0.95em; display: inline-block; }
    .ep-key .line-sw { height: 0; border-top: 3px solid var(--accent-4); }
    .ep-key .band-sw { background: var(--accent-5); opacity: 0.28; }
    .ep-key .now-sw { font-size: 0.75em; color: var(--accent-1); }
    @container (max-width: 280px) {
      .ep-chip { display: none; }
    }
  `;

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w" data-widget="ostrom_prices">
      <div class="w-title">
        <i class="ph-bold ph-lightning" style="color:var(--accent-4)"></i>
        <h3>${escapeHtml(title)}</h3>
        ${chip}
      </div>
      <div class="w-body chart-body ep-body">
        <div class="ep-chart">
          ${hasData ? "<canvas></canvas>" : '<p class="u-muted">No price data.</p>'}
        </div>
        ${legend}
      </div>
    </div>`;

  if (!hasData) return;
  const canvas = shadow.querySelector("canvas");
  if (!canvas || !window.Chart) return;
  const t = tokens(shadow.host);

  // Tick fonts scale with the cell so the y values read from across a room
  // (the old bar chart's fixed ~10px was the complaint). y is bumped harder
  // than x since it carries the price numbers the user reads first.
  const yTick = Math.max(15, Math.min(26, Math.round(cellWidth / 38)));
  const xTick = Math.max(10, Math.min(20, Math.round(cellWidth / 60)));
  const labelTick = Math.max(11, Math.min(18, Math.round(cellWidth / 48)));

  // Same gate as the y-axis / chip: too little room at xs for a value pill
  // without it colliding with the line or the axis.
  const showMinMaxLabels = cellWidth > 280;
  const maxIdx = today.indexOf(Math.max(...today));
  const minIdx = today.indexOf(Math.min(...today));

  const datasets = [];
  // Band drawn behind (higher order = painted first). p75 (upper) fills down
  // to the immediately-following p25 (lower) dataset via fill:"+1".
  if (hasBand) {
    datasets.push({
      label: "p75",
      data: p75,
      borderColor: "transparent",
      borderWidth: 0,
      pointRadius: 0,
      tension: 0.3,
      spanGaps: true,
      fill: "+1",
      backgroundColor: withAlpha(t.accent5, 0.16),
      order: 3,
    });
    datasets.push({
      label: "p25",
      data: p25,
      borderColor: "transparent",
      borderWidth: 0,
      pointRadius: 0,
      tension: 0.3,
      spanGaps: true,
      fill: false,
      order: 3,
    });
  }
  // Today's line, on top. Current hour gets an accent-1 pip; the day's
  // cheapest/priciest hour each get a smaller pip in the chip's lo/hi
  // colours (now takes visual priority when it coincides with either).
  const pointColor = (i) => {
    if (i === nowIndex) return t.accent1;
    if (showMinMaxLabels && i === maxIdx) return t.accent1;
    if (showMinMaxLabels && i === minIdx) return t.accent3;
    return "transparent";
  };
  datasets.push({
    label: "Today",
    data: today,
    borderColor: t.accent4,
    borderWidth: 3,
    tension: 0.3,
    fill: false,
    order: 1,
    pointRadius: today.map((_, i) => {
      if (i === nowIndex) return 5;
      if (showMinMaxLabels && (i === maxIdx || i === minIdx)) return 4;
      return 0;
    }),
    pointBackgroundColor: today.map((_, i) => pointColor(i)),
    pointBorderColor: today.map((_, i) => (pointColor(i) === "transparent" ? "transparent" : t.surface)),
    pointBorderWidth: 2,
  });

  // Y range spans today + the band so nothing clips; extra headroom when the
  // min/max value pills are on so they don't collide with the axis or plot edge.
  const span = [...today, ...(hasBand ? [...p25, ...p75] : [])].filter(
    (v) => typeof v === "number" && !Number.isNaN(v)
  );
  const lo = Math.min(...span);
  const hi = Math.max(...span);
  const pad = Math.max(0.5, (hi - lo) * (showMinMaxLabels ? 0.22 : 0.12));

  // Draws a filled pill with the price next to the min/max points, so the
  // day's cheapest/priciest hour is readable straight off the chart instead
  // of only from the header chip. Max labels sit above its point, min below,
  // so neither collides with the line; x is clamped inside the plot area so
  // an edge-hour extreme doesn't get its pill cut off.
  const minMaxLabelPlugin = {
    id: "ep_minmax_labels",
    afterDatasetsDraw(chart) {
      if (!showMinMaxLabels) return;
      const { ctx, chartArea, scales } = chart;
      const xScale = scales.x;
      const yScale = scales.y;
      if (!xScale || !yScale) return;
      ctx.save();
      ctx.font = `800 ${labelTick}px ${t.fontFamily}`;
      ctx.textBaseline = "middle";
      const draw = (idx, value, color, above) => {
        if (idx < 0 || value == null) return;
        const x = xScale.getPixelForValue(idx);
        const y = yScale.getPixelForValue(value);
        const label = fmtPrice(value);
        const pad2 = 5;
        const tw = ctx.measureText(label).width + pad2 * 2;
        const th = labelTick + 6;
        const bx = Math.min(Math.max(x - tw / 2, chartArea.left), chartArea.right - tw);
        const by = above ? y - th - 6 : y + 6;
        ctx.fillStyle = color;
        ctx.fillRect(bx, by, tw, th);
        ctx.fillStyle = t.surface;
        ctx.fillText(label, bx + pad2, by + th / 2 + 0.5);
      };
      draw(maxIdx, today[maxIdx], t.accent1, true);
      if (minIdx !== maxIdx) draw(minIdx, today[minIdx], t.accent3, false);
      ctx.restore();
    },
  };

  const __chartT0 = performance.now();
  const chart = new window.Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: [minMaxLabelPlugin],
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          ticks: {
            color: t.textSecondary,
            font: { family: t.fontFamily, weight: 700, size: xTick },
            autoSkip: true,
            maxRotation: 0,
            autoSkipPadding: 8,
          },
          grid: { display: false },
          border: { display: false },
        },
        y: {
          display: showY,
          suggestedMin: lo - pad,
          suggestedMax: hi + pad,
          ticks: {
            color: t.textSecondary,
            font: { family: t.fontFamily, weight: 700, size: yTick },
            maxTicksLimit: 5,
            padding: 4,
          },
          grid: { color: withAlpha(t.textPrimary, 0.08), drawTicks: false },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
    },
  });
  console.log(`[ostrom_prices] new Chart() took ${(performance.now() - __chartT0).toFixed(2)}ms`);
  canvas._chart = chart;
}
