// weather_today, Spectra chart archetype. Temperature, rain probability and
// UV index drawn as three overlaid lines across the current calendar day
// (00:00–24:00). Built for monochrome e-ink first: the three series are told
// apart by STROKE PATTERN (not colour), so they stay legible once the panel
// quantises everything to grey —
//   • Temp  → solid line, left y-axis (°C)
//   • Rain  → dashed line, implicit 0–100 % axis
//   • UV    → dotted line, right y-axis
// A faint vertical rule marks the current hour. Renders once, statically.

import { tokens } from "../../static/spectra-chart.js";

// One config per series drives both the chart datasets and the legend
// swatches. The lines are told apart by STROKE PATTERN (solid / dashed /
// dotted) — that alone survives greyscale — so every marker is a plain small
// dot; shape-coding the points on top was redundant.
// dash is a Chart.js/SVG stroke-dasharray ([] = solid).
const SERIES = {
  temp: { dash: [], point: "circle", color: "accent1", tint: "accent-1" },
  rain: { dash: [7, 5], point: "circle", color: "accent4", tint: "accent-4" },
  uv: { dash: [2, 3], point: "circle", color: "accent2", tint: "accent-2" },
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Hex / rgb / rgba color string → rgba with new alpha. Same logic
// spectra-chart.js uses internally, inlined here so we don't depend on it
// for the mixed-axis path (matches weather_hourly).
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

// A small inline-SVG legend swatch that reproduces the series' stroke
// pattern + marker shape, so the key reads correctly in greyscale (a plain
// colour dot would be ambiguous on a mono panel).
function legendSwatch(dash) {
  const line = `<line x1="1" y1="5" x2="23" y2="5" stroke="currentColor" stroke-width="2"${
    dash.length ? ` stroke-dasharray="${dash.join(",")}"` : ""
  }/>`;
  const marker = `<circle cx="12" cy="5" r="2.4" fill="currentColor"/>`;
  return `<svg class="ls" viewBox="0 0 24 10" width="20" height="9" aria-hidden="true">${line}${marker}</svg>`;
}

// Draw a series' marker shape on a canvas context, matching the SVG legend
// swatches so the same shape identifies each line everywhere it appears.
function drawMarker(ctx, shape, x, y, r, color) {
  ctx.save();
  ctx.fillStyle = color;
  ctx.beginPath();
  if (shape === "rectRot") {
    ctx.moveTo(x, y - r);
    ctx.lineTo(x + r, y);
    ctx.lineTo(x, y + r);
    ctx.lineTo(x - r, y);
    ctx.closePath();
  } else if (shape === "triangle") {
    ctx.moveTo(x, y - r);
    ctx.lineTo(x + r * 0.95, y + r * 0.8);
    ctx.lineTo(x - r * 0.95, y + r * 0.8);
    ctx.closePath();
  } else {
    ctx.arc(x, y, r, 0, Math.PI * 2);
  }
  ctx.fill();
  ctx.restore();
}

function fmtUv(v) {
  const n = Number(v);
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

// Indices of the min and max of a numeric array (first occurrence wins).
function extremaIndices(vals) {
  let minI = 0;
  let maxI = 0;
  for (let i = 1; i < vals.length; i++) {
    if (vals[i] < vals[minI]) minI = i;
    if (vals[i] > vals[maxI]) maxI = i;
  }
  return { minI, maxI };
}

// Custom Chart.js plugin: mark the min/max points of the labelled series with
// the series' marker shape + a value label placed above (max) or below (min),
// clamped inside the plot area. ``marks`` = [{axisId, shape, index, value,
// text, dir(-1 above / +1 below), color}].
function extremaPlugin(marks, font) {
  return {
    id: "tsr-extrema",
    afterDatasetsDraw(chart) {
      const x = chart.scales?.x;
      const area = chart.chartArea;
      if (!x || !area || !marks.length) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = font;
      ctx.textBaseline = "middle";
      for (const m of marks) {
        const y = chart.scales?.[m.axisId];
        if (!y) continue;
        const px = x.getPixelForValue(m.index);
        const py = y.getPixelForValue(m.value);
        if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
        drawMarker(ctx, m.shape, px, py, 3.4, m.color);
        // Keep the label from spilling past the left/right edges.
        let align = "center";
        if (px < area.left + 24) align = "left";
        else if (px > area.right - 24) align = "right";
        ctx.textAlign = align;
        const gap = 12;
        const ly = Math.min(
          area.bottom - 8,
          Math.max(area.top + 8, py + m.dir * gap)
        );
        ctx.fillStyle = m.color;
        ctx.fillText(m.text, px, ly);
      }
      ctx.restore();
    },
  };
}

// Custom Chart.js plugin: draw a single faint vertical rule at the current
// hour so "now" reads at a glance without hijacking a data series. Skipped
// when nowIndex is null (the current hour couldn't be located in the day).
function nowLinePlugin(nowIndex, color) {
  return {
    id: "tsr-now-line",
    afterDatasetsDraw(chart) {
      if (nowIndex == null) return;
      const x = chart.scales?.x;
      const area = chart.chartArea;
      if (!x || !area) return;
      const px = x.getPixelForValue(nowIndex);
      if (!Number.isFinite(px)) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="weather_today">
        <div class="w-title"><i class="ph-bold ph-warning-circle" style="color:var(--accent-1)"></i><h3>Today</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const label = data.place || data.label || "";
  const hours = Array.isArray(data.hours) ? data.hours.map((h) => h || "") : [];
  const temps = Array.isArray(data.temps) ? data.temps.map((v) => Number(v)) : [];
  const rain = Array.isArray(data.rain) ? data.rain.map((v) => Number(v) || 0) : [];
  const uv = Array.isArray(data.uv) ? data.uv.map((v) => Number(v) || 0) : [];
  const nowIndex = Number.isInteger(data.nowIndex) ? data.nowIndex : null;

  const n = Math.min(hours.length, temps.length, rain.length, uv.length);
  const labels = hours.slice(0, n);
  const tempVals = temps.slice(0, n);
  const rainVals = rain.slice(0, n);
  const uvVals = uv.slice(0, n);
  const hasData = tempVals.length >= 2;

  // Cell width drives the tick font clamp + what fits. shadow.host is
  // .cell-content (transform-scaled), but clientWidth reads the pre-scale
  // layout width — the same number the container-query breakpoints use.
  const cellWidth = shadow.host?.clientWidth || 600;
  const showAxes = cellWidth > 280; // y-axis ticks/titles need room; drop at xs
  const showAxisTitles = cellWidth > 380; // "°C" / "UV" side titles from sm up
  const showExtrema = cellWidth > 380; // min/max on-graph labels from md up

  const showLegend = cellWidth > 280;
  const legend = showLegend
    ? `
      <div class="hr-legend">
        <span class="hr-key hr-key--temp">${legendSwatch(SERIES.temp.dash)}Temp °C</span>
        <span class="hr-key hr-key--rain">${legendSwatch(SERIES.rain.dash)}Rain %</span>
        <span class="hr-key hr-key--uv">${legendSwatch(SERIES.uv.dash)}UV</span>
      </div>`
    : "";

  const layout = `
    .hr-body { gap: var(--space-2); }
    .hr-chart {
      flex: 1 1 auto;
      min-height: 0;
      position: relative;
    }
    .hr-legend {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1) var(--space-3);
      align-items: center;
      flex: 0 0 auto;
    }
    .hr-key {
      display: inline-flex;
      align-items: center;
      gap: 0.3em;
      font-size: calc(var(--fs-caption) * 0.8);
      font-weight: var(--fw-bold);
      color: var(--text-secondary);
      letter-spacing: var(--ls-label);
    }
    .hr-key .ls { flex: 0 0 auto; }
    .hr-key--temp .ls { color: var(--accent-1); }
    .hr-key--rain .ls { color: var(--accent-4); }
    .hr-key--uv .ls { color: var(--accent-2); }
    /* now readings pulled up into the title bar, where the eye already
       lands when it reads the location. */
    .hr-range {
      margin-left: auto;
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--fs-label);
      font-weight: var(--fw-bold);
      letter-spacing: var(--ls-label);
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    .hr-range .now-lbl {
      color: var(--text-muted);
      font-weight: var(--fw-black);
      text-transform: var(--label-transform, uppercase);
    }
    .hr-range .temp { color: var(--accent-1); }
    .hr-range .rain { color: var(--accent-4); }
    .hr-range .uv { color: var(--accent-2); }
    .hr-range .sep { color: var(--text-muted); opacity: 0.5; }
    @container (max-width: 280px) {
      .hr-legend { display: none; }
      .hr-range { display: none; }
    }
  `;

  const parts = [];
  if (data.nowTemp != null) parts.push(`<span class="temp">${escapeHtml(Math.round(Number(data.nowTemp)))}°</span>`);
  if (data.nowRain != null) parts.push(`<span class="rain">${escapeHtml(Math.round(Number(data.nowRain)))}%</span>`);
  if (data.nowUv != null) parts.push(`<span class="uv">UV ${escapeHtml(Number(data.nowUv))}</span>`);
  const rangeChip = parts.length
    ? `<span class="hr-range">${['<span class="now-lbl">Now</span>', ...parts].join('<span class="sep">·</span>')}</span>`
    : "";

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w" data-widget="weather_today">
      <div class="w-title">
        <i class="ph-bold ph-sun-horizon" style="color:var(--accent-2)"></i>
        <h3>${escapeHtml(label || "Today")}</h3>
        ${rangeChip}
      </div>
      <div class="w-body hr-body">
        <div class="hr-chart">
          ${hasData ? '<canvas></canvas>' : '<p class="u-muted">No hourly data.</p>'}
        </div>
        ${legend}
      </div>
    </div>`;

  if (!hasData) return;
  const canvas = shadow.querySelector("canvas");
  if (!canvas || !window.Chart) return;
  const t = tokens(shadow.host);

  // Per-point radius: a marker on the current hour for each line, zero
  // elsewhere. All-zero (invisible) when nowIndex is null.
  const nowMarker = (accent, point) => ({
    pointStyle: point,
    pointRadius: tempVals.map((_, i) => (i === nowIndex ? 3 : 0)),
    pointBackgroundColor: tempVals.map((_, i) => (i === nowIndex ? accent : "transparent")),
    pointBorderColor: tempVals.map((_, i) => (i === nowIndex ? accent : "transparent")),
  });

  const tempSpan = Math.max(0.1, Math.max(...tempVals) - Math.min(...tempVals));
  // UV index is a standardised 0–11+ scale, so anchor the axis to a fixed
  // rounded ceiling (multiple of 3 → clean 0/3/6/9/12 ticks) rather than the
  // day's peak; that keeps the absolute UV level readable and avoids the
  // 10-vs-11 tick crowding a tight max produces.
  const uvMax = Math.max(12, Math.ceil(Math.max(...uvVals) / 3) * 3);

  const datasets = [
    {
      type: "line",
      label: "Temp",
      data: tempVals,
      borderColor: t.accent1,
      backgroundColor: withAlpha(t.accent1, 0.1),
      borderWidth: 3,
      borderDash: SERIES.temp.dash,
      tension: 0.3,
      fill: "origin",
      yAxisID: "yTemp",
      order: 1,
      ...nowMarker(t.accent1, SERIES.temp.point),
    },
    {
      type: "line",
      label: "Rain",
      data: rainVals,
      borderColor: t.accent4,
      backgroundColor: "transparent",
      borderWidth: 2.5,
      borderDash: SERIES.rain.dash,
      tension: 0.3,
      fill: false,
      yAxisID: "yRain",
      order: 2,
      ...nowMarker(t.accent4, SERIES.rain.point),
    },
    {
      type: "line",
      label: "UV",
      data: uvVals,
      borderColor: t.accent2,
      backgroundColor: "transparent",
      borderWidth: 2.5,
      borderDash: SERIES.uv.dash,
      tension: 0.3,
      fill: false,
      yAxisID: "yUv",
      order: 3,
      ...nowMarker(t.accent2, SERIES.uv.point),
    },
  ];

  // Axis tick size clamps against cell width so wide cells get legible
  // numbers while cramped cells fall back without overflowing.
  const cqBase = Math.max(8, Math.min(28, Math.round(cellWidth / 60)));
  const tickFontSize = Math.max(10, Math.min(20, cqBase));
  // The y-axis numbers are the reference readout, so size them up (and bold)
  // and paint them in the darkest text token for maximum legibility.
  const yTickFont = { family: t.fontFamily, weight: 800, size: Math.max(13, Math.min(28, cqBase + 5)) };
  const titleFont = { family: t.fontFamily, weight: 800, size: Math.max(9, tickFontSize - 2) };

  // Label the min/max of the temperature and UV lines directly on the graph.
  // UV's minimum is 0 for most of the night, so we only label it when it's
  // above zero — a "0" pinned to the baseline is noise the flat dotted line
  // already conveys.
  const tE = extremaIndices(tempVals);
  const uE = extremaIndices(uvVals);
  const extremaFont = `700 ${Math.max(9, tickFontSize - 2)}px ${t.fontFamily}`;
  const marks = showExtrema
    ? [
        { axisId: "yTemp", shape: SERIES.temp.point, index: tE.maxI, value: tempVals[tE.maxI], text: `${Math.round(tempVals[tE.maxI])}°`, dir: -1, color: t.accent1 },
        { axisId: "yTemp", shape: SERIES.temp.point, index: tE.minI, value: tempVals[tE.minI], text: `${Math.round(tempVals[tE.minI])}°`, dir: 1, color: t.accent1 },
        { axisId: "yUv", shape: SERIES.uv.point, index: uE.maxI, value: uvVals[uE.maxI], text: `UV ${fmtUv(uvVals[uE.maxI])}`, dir: -1, color: t.accent2 },
        ...(uvVals[uE.minI] > 0
          ? [{ axisId: "yUv", shape: SERIES.uv.point, index: uE.minI, value: uvVals[uE.minI], text: `UV ${fmtUv(uvVals[uE.minI])}`, dir: 1, color: t.accent2 }]
          : []),
      ]
    : [];

  const chart = new window.Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      // Extra top/bottom room so the max/min value labels sit clear of the
      // plot edges instead of clipping.
      layout: { padding: { top: showExtrema ? 16 : 4, bottom: showExtrema ? 6 : 0 } },
      scales: {
        x: {
          ticks: {
            color: t.textSecondary,
            font: { family: t.fontFamily, weight: 700, size: tickFontSize },
            autoSkip: true,
            maxRotation: 0,
            autoSkipPadding: 8,
          },
          grid: { display: false },
          border: { display: false },
        },
        yTemp: {
          position: "left",
          display: showAxes,
          beginAtZero: false,
          suggestedMin: Math.min(...tempVals) - tempSpan * 0.18,
          suggestedMax: Math.max(...tempVals) + tempSpan * 0.05,
          ticks: {
            color: t.textPrimary,
            font: yTickFont,
            maxTicksLimit: 5,
            callback: (v) => `${Math.round(v)}°`,
          },
          title: {
            display: showAxisTitles,
            text: "TEMP °C",
            color: t.textMuted,
            font: titleFont,
          },
          grid: { display: false },
          border: { display: false },
        },
        yRain: {
          position: "right",
          display: false,
          min: 0,
          max: 100,
          grid: { display: false },
          border: { display: false },
        },
        yUv: {
          position: "right",
          display: showAxes,
          min: 0,
          max: uvMax,
          ticks: {
            color: t.textPrimary,
            font: yTickFont,
            stepSize: 3,
            callback: (v) => `${Math.round(v)}`,
          },
          title: {
            display: showAxisTitles,
            text: "UV",
            color: t.textMuted,
            font: titleFont,
          },
          grid: { display: false },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
    },
    plugins: [
      nowLinePlugin(nowIndex, withAlpha(t.textPrimary, 0.25)),
      extremaPlugin(marks, extremaFont),
    ],
  });
  canvas._chart = chart;
}
