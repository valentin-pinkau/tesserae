// ostrom_prices, Spectra chart archetype. Today's hourly electricity spot
// price (gross ct/kWh, Ostrom API) as a line, over a shaded p25-p75 band
// computed per hour-of-day across the prior week, so the eye reads "is now
// cheap or dear vs. the recent norm". The current hour is dotted in accent-1.
//
// With the outlook on, the line continues over the next days: tomorrow's
// real prices once the auction publishes, then a weather-model forecast
// drawn dashed. Each day is captioned with its cheapest WINDOW_HOURS-hour
// window; the cheapest day overall gets an accent-3 pill + shaded window,
// which answers "laundry today or wait?" at a glance.
//
// Every mark must survive a monochrome panel: the renderer maps each pixel
// to the nearest panel colour without dithering, so any light tint turns
// white and every accent turns black. Encodings therefore lean on shape,
// not hue: solid vs dashed line, hatched band, filled vs hollow window bar,
// inverted vs plain caption, open ring for "now". Colour only reinforces.
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

// Diagonal-line fill for the "typical" band. A flat tint would threshold to
// white on a 1-bit panel; thin dark hatching stays visible on any panel
// while still reading as a light area.
function hatchPattern(ctx, color) {
  const tile = document.createElement("canvas");
  tile.width = tile.height = 7;
  const g = tile.getContext("2d");
  g.strokeStyle = color;
  g.lineWidth = 1.25;
  g.beginPath();
  for (const off of [-7, 0, 7]) {
    g.moveTo(off, 7);
    g.lineTo(off + 7, 0);
  }
  g.stroke();
  return ctx.createPattern(tile, "repeat");
}

function weekday(iso) {
  // Noon avoids a DST-midnight edge landing on the previous day.
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en", { weekday: "short" });
}

function fmtWindow(best, hours) {
  if (!best) return "";
  const end = (best.start + hours) % 24;
  return `${String(best.start).padStart(2, "0")}–${String(end).padStart(2, "0")}`;
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

  const todayRaw = Array.isArray(data.today) ? data.today.map(Number) : [];
  const hoursRaw = Array.isArray(data.hours) ? data.hours.map(Number) : [];
  const p25Raw = Array.isArray(data.p25) ? data.p25 : null;
  const p75Raw = Array.isArray(data.p75) ? data.p75 : null;
  const hasBand = Boolean(data.hasBand) && p25Raw && p75Raw;
  const unit = data.unit || "ct/kWh";
  const hasData = todayRaw.length >= 2;
  const windowHours = Number(data.windowHours) || 3;
  const size = ctx?.cell?.size || "md";

  // shadow.host is .cell-content; clientWidth reads the pre-scale virtual
  // cell width, the same number the container-query breakpoints fire on.
  const cellWidth = shadow.host?.clientWidth || 600;
  const showChip = cellWidth > 280;   // "now" price
  const showY = cellWidth > 280;
  const showLegend = cellWidth > 440;

  // Outlook: off at xs (no room for day captions), two extra days at sm.
  const outlookAll = Array.isArray(data.outlook) ? data.outlook : [];
  const outlookOn = opts.outlook !== false && size !== "xs" && outlookAll.length > 0;
  const outlook = outlookOn ? outlookAll.slice(0, size === "sm" ? 2 : outlookAll.length) : [];
  const multiDay = outlook.length > 0;
  const hasForecast = outlook.some((d) => d.kind === "forecast");

  // One slot per hour. Today is laid out 0..23 by its real hour so the
  // following days line up on midnight; today-only mode keeps the server's
  // hour list as-is (identical unless Ostrom skipped an hour).
  const todayByHour = new Map(hoursRaw.map((h, i) => [h, i]));
  const slotsToday = multiDay ? [...Array(24).keys()] : hoursRaw;
  const pick = (arr, h) => {
    const i = todayByHour.get(h);
    const v = i == null || !arr ? null : arr[i];
    return v == null ? null : Number(v);
  };
  const days = [{ date: data.date, kind: "actual", prices: slotsToday.map((h) => pick(todayRaw, h)), best: data.todayBest }, ...outlook];
  const n = days.length * slotsToday.length;
  const dayOf = (i) => Math.floor(i / slotsToday.length);
  const hourOf = (i) => (multiDay ? i % 24 : slotsToday[i]);

  const real = [];
  const forecast = [];
  days.forEach((d) => {
    const isFc = d.kind === "forecast";
    (d.prices || []).slice(0, slotsToday.length).forEach((v) => {
      real.push(isFc ? null : v);
      forecast.push(isFc ? v : null);
    });
  });
  // Bridge the dashed line back to the last real point so the two read as one.
  const firstFc = forecast.findIndex((v) => v != null);
  if (firstFc > 0) forecast[firstFc - 1] = real[firstFc - 1];

  const p25 = hasBand ? Array.from({ length: n }, (_, i) => (dayOf(i) === 0 ? pick(p25Raw, hourOf(i)) : null)) : null;
  const p75 = hasBand ? Array.from({ length: n }, (_, i) => (dayOf(i) === 0 ? pick(p75Raw, hourOf(i)) : null)) : null;

  const todaySlots = days[0].prices;
  const nowIndex = Number.isInteger(data.nowIndex) && data.nowIndex >= 0
    ? (multiDay ? hoursRaw[data.nowIndex] : data.nowIndex)
    : -1;
  const labels = Array.from({ length: n }, (_, i) => String(hourOf(i)).padStart(2, "0"));

  const bestDayIdx = days.findIndex((d) => d.date === data.bestDay);
  const bestDay = bestDayIdx >= 0 ? days[bestDayIdx] : null;
  const dayName = (i) => (i === 0 ? "Today" : weekday(days[i].date));

  const chip = (showChip && data.now != null)
    ? `
      <span class="ep-chip">
        <span class="now"><span class="now-label">Now:</span> ${escapeHtml(fmtPrice(data.now))}<span class="u">${escapeHtml(unit)}</span></span>
      </span>`
    : "";

  // Multi-day legend doubles as the recommendation ("Best: Sun 11–14"); the
  // red now-pip is self-explanatory next to the red "Now:" chip, and the
  // band only covers today, so both keys shrink to fit one row at md.
  const bestKey = multiDay && bestDay?.best
    ? `<span class="ep-key best"><span class="sw best-sw"></span>Best: ${escapeHtml(dayName(bestDayIdx))} ${escapeHtml(fmtWindow(bestDay.best, windowHours))}</span>`
    : "";
  const legend = showLegend
    ? `
      <div class="ep-legend">
        <span class="ep-key"><span class="sw line-sw"></span>${multiDay ? "Price" : "Today"}</span>
        ${hasForecast ? '<span class="ep-key"><span class="sw fc-sw"></span>Forecast</span>' : ""}
        ${hasBand ? `<span class="ep-key"><span class="sw band-sw"></span>${multiDay ? "Typical" : "Typical (7&nbsp;days)"}</span>` : ""}
        ${multiDay ? bestKey : '<span class="ep-key"><span class="sw now-sw"></span>Now</span>'}
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
    .ep-key .fc-sw { height: 0; border-top: 3px dashed var(--accent-4); }
    .ep-key .band-sw {
      background: repeating-linear-gradient(-45deg, var(--text-secondary) 0 1.25px, transparent 1.25px 4.5px);
    }
    .ep-key .best-sw { height: 0.4em; background: var(--accent-3); }
    .ep-key.best { color: var(--accent-3); }
    .ep-key .now-sw {
      width: 0.8em; height: 0.8em; box-sizing: border-box;
      border: 3px solid var(--accent-1); border-radius: 50%; background: var(--surface);
    }
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
  const accent3Soft = getComputedStyle(shadow.host).getPropertyValue("--accent-3-soft").trim()
    || withAlpha(t.accent3, 0.25);

  // Tick fonts scale with the cell so the y values read from across a room
  // (the old bar chart's fixed ~10px was the complaint). y is bumped harder
  // than x since it carries the price numbers the user reads first.
  const yTick = Math.max(15, Math.min(26, Math.round(cellWidth / 38)));
  const xTick = Math.max(10, Math.min(20, Math.round(cellWidth / 60)));
  const labelTick = Math.max(11, Math.min(18, Math.round(cellWidth / 48)));

  // Same gate as the y-axis / chip: too little room at xs for a value pill
  // without it colliding with the line or the axis. Multi-day mode replaces
  // today's min/max pills with one cheapest-window caption per day.
  const showMinMaxLabels = cellWidth > 280 && !multiDay;
  const todayVals = todaySlots.filter((v) => v != null);
  const maxIdx = todaySlots.indexOf(Math.max(...todayVals));
  const minIdx = todaySlots.indexOf(Math.min(...todayVals));

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
      spanGaps: false,
      fill: "+1",
      backgroundColor: hatchPattern(canvas.getContext("2d"), t.textSecondary),
      order: 3,
    });
    datasets.push({
      label: "p25",
      data: p25,
      borderColor: "transparent",
      borderWidth: 0,
      pointRadius: 0,
      tension: 0.3,
      spanGaps: false,
      fill: false,
      order: 3,
    });
  }
  // Real prices on top. The current hour is an open ring (hollow reads as
  // "you are here" even when every accent prints black); in today-only mode
  // the day's cheapest/priciest hour each get a small solid pip next to
  // their pills (now takes priority when it coincides with either).
  const isPip = (i) => showMinMaxLabels && i !== nowIndex && (i === maxIdx || i === minIdx);
  const pointColor = (i) => {
    if (i === nowIndex) return t.accent1;
    if (isPip(i)) return i === maxIdx ? t.accent1 : t.accent3;
    return "transparent";
  };
  // Line weight scales with the cell: the panel thresholds antialiased
  // edges away, so a 2.5px line at lg survives as ~1px on a mono panel.
  const lineW = Math.max(3, Math.round(cellWidth / 280));
  datasets.push({
    label: "Price",
    data: real,
    borderColor: t.accent4,
    borderWidth: lineW,
    tension: 0.3,
    fill: false,
    order: 1,
    pointRadius: real.map((_, i) => {
      if (i === nowIndex) return Math.max(7, lineW * 2.2);
      if (isPip(i)) return 4;
      return 0;
    }),
    pointBackgroundColor: real.map((_, i) => (i === nowIndex ? t.surface : pointColor(i))),
    pointBorderColor: real.map((_, i) => {
      if (i === nowIndex) return t.accent1;
      return pointColor(i) === "transparent" ? "transparent" : t.surface;
    }),
    pointBorderWidth: real.map((_, i) => (i === nowIndex ? lineW + 0.5 : 2)),
  });
  if (hasForecast) {
    datasets.push({
      label: "Forecast",
      data: forecast,
      borderColor: t.accent4,
      borderWidth: lineW,
      borderDash: [lineW * 2.5, lineW * 1.7],
      tension: 0.3,
      fill: false,
      order: 2,
      pointRadius: 0,
    });
  }

  // Y range spans every plotted value so nothing clips; extra headroom for
  // the min/max pills or (multi-day) the per-day captions along the top.
  const span = [...real, ...forecast, ...(hasBand ? [...p25, ...p75] : [])].filter(
    (v) => typeof v === "number" && !Number.isNaN(v)
  );
  const lo = Math.min(...span);
  const hi = Math.max(...span);
  const pad = Math.max(0.5, (hi - lo) * (showMinMaxLabels ? 0.22 : 0.12));
  const topPad = multiDay ? Math.max(1, (hi - lo) * 0.3) : pad;

  // Draws a filled pill with the price next to the min/max points, so the
  // day's cheapest/priciest hour is readable straight off the chart instead
  // of only from the header chip. Max labels sit above its point, min below,
  // so neither collides with the line; x is clamped inside the plot area so
  // an edge-hour extreme doesn't get its pill cut off.
  const pill = (ctx, chartArea, label, x, y, color, textColor) => {
    const pad2 = 5;
    const tw = ctx.measureText(label).width + pad2 * 2;
    const th = labelTick + 6;
    const bx = Math.min(Math.max(x - tw / 2, chartArea.left), chartArea.right - tw);
    if (color) {
      ctx.fillStyle = color;
      ctx.fillRect(bx, y, tw, th);
    }
    ctx.fillStyle = textColor;
    ctx.fillText(label, bx + pad2, y + th / 2 + 0.5);
  };
  const minMaxLabelPlugin = {
    id: "ep_minmax_labels",
    afterDatasetsDraw(chart) {
      if (!showMinMaxLabels) return;
      const { ctx, chartArea, scales } = chart;
      if (!scales.x || !scales.y) return;
      ctx.save();
      ctx.font = `800 ${labelTick}px ${t.fontFamily}`;
      ctx.textBaseline = "middle";
      const th = labelTick + 6;
      // Arrow prefixes tell high from low without relying on pill colour.
      const at = (idx, above, color) => {
        const v = todaySlots[idx];
        if (idx < 0 || v == null) return;
        const x = scales.x.getPixelForValue(idx);
        const y = scales.y.getPixelForValue(v);
        const label = `${above ? "▲" : "▼"} ${fmtPrice(v)}`;
        pill(ctx, chartArea, label, x, above ? y - th - 6 : y + 6, color, t.surface);
      };
      at(maxIdx, true, t.accent1);
      if (minIdx !== maxIdx) at(minIdx, false, t.accent3);
      ctx.restore();
    },
  };

  // Multi-day furniture: dotted midnight dividers, each day's cheapest
  // window as a bar along the bottom edge (solid on the best day, hollow on
  // the rest), and a caption per day along the top with that window's
  // average (inverted on the best day). The best day's window also gets a
  // soft full-height tint, which is a colour-panel bonus only: it drops out
  // on mono panels, where the bar and caption carry the meaning alone.
  const dayPlugin = {
    id: "ep_days",
    beforeDatasetsDraw(chart) {
      if (!multiDay) return;
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      const barH = Math.max(6, Math.round(labelTick * 0.5));
      days.forEach((d, di) => {
        if (!d.best) return;
        const x0 = scales.x.getPixelForValue(di * 24 + d.best.start - 0.5);
        const x1 = scales.x.getPixelForValue(di * 24 + d.best.start + windowHours - 0.5);
        const by = chartArea.bottom - barH;
        if (di === bestDayIdx) {
          ctx.fillStyle = accent3Soft;
          ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
          ctx.fillStyle = t.accent3;
          ctx.fillRect(x0, by, x1 - x0, barH);
        } else {
          ctx.strokeStyle = t.textSecondary;
          ctx.lineWidth = 2;
          ctx.strokeRect(x0 + 1, by + 1, x1 - x0 - 2, barH - 2);
        }
      });
      ctx.strokeStyle = t.textSecondary;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 4]);
      for (let di = 1; di < days.length; di++) {
        const x = Math.round(scales.x.getPixelForValue(di * 24 - 0.5)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
      }
      ctx.restore();
    },
    afterDatasetsDraw(chart) {
      if (!multiDay) return;
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      ctx.font = `800 ${labelTick}px ${t.fontFamily}`;
      ctx.textBaseline = "middle";
      days.forEach((d, di) => {
        if (!d.best) return;
        const x = scales.x.getPixelForValue(di * 24 + 11.5);
        const label = `${d.kind === "forecast" ? "~" : ""}${fmtPrice(d.best.avg)}`;
        const isBest = di === bestDayIdx;
        pill(ctx, chartArea, label, x, chartArea.top + 2, isBest ? t.accent3 : null, isBest ? t.surface : t.textSecondary);
      });
      ctx.restore();
    },
  };

  const __chartT0 = performance.now();
  const chart = new window.Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: [dayPlugin, minMaxLabelPlugin],
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          ticks: multiDay
            ? {
                color: t.textSecondary,
                font: { family: t.fontFamily, weight: 700, size: xTick },
                autoSkip: false,
                maxRotation: 0,
                // Day names centred under each day instead of hour numbers.
                callback: (_v, i) => (i % 24 === 12 ? dayName(dayOf(i)) : ""),
              }
            : {
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
          suggestedMax: hi + topPad,
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
