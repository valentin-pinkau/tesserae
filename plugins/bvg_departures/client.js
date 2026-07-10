// bvg_departures, Spectra list archetype.
//
// Two stacked groups (S-Bahn on top, Bus below), each its own zebra
// list. One row per upcoming departure: a line badge coloured by transit
// mode (S-Bahn green, U-Bahn blue, bus magenta, tram red, ferry teal —
// BVG/VBB's own network colours, a data-identity exception to the
// token-only palette rule), the direction, and a right column with the
// clock time plus a relative "in N min" / delay indicator.

const PRODUCT_COLORS = {
  suburban: "#00933a",
  subway: "#0060a9",
  bus: "#a5017d",
  tram: "#be1414",
  ferry: "#0080a9",
  regional: "#f01536",
  express: "#f01536",
};

const PRODUCT_ICONS = {
  suburban: "ph-train",
  subway: "ph-train",
  bus: "ph-bus",
  tram: "ph-tram",
  ferry: "ph-boat",
  regional: "ph-train-simple",
  express: "ph-train-simple",
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtIn(minutes) {
  if (!Number.isFinite(minutes)) return "";
  if (minutes <= 0) return "now";
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

// Rows are sized so ~6 fill the height of any one canonical size. Two
// stacked groups double that content, so at anything below "lg" each
// group shows fewer rows than are actually fetched/cached — the rest
// stay in the data (and reappear at a bigger cell size) but would
// otherwise be clipped off the bottom of a fixed-size, non-scrolling
// e-ink render.
const VISIBLE_ROWS_PER_GROUP = { xs: 1, sm: 1, md: 1, lg: 3 };

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const stopName = data.stop_name || "BVG Departures";
  const size = ctx?.cell?.size || "md";
  const visibleRows = VISIBLE_ROWS_PER_GROUP[size] ?? VISIBLE_ROWS_PER_GROUP.md;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="bvg_departures">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(stopName)}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const groups = Array.isArray(data.groups) ? data.groups : [];
  const hasAnyDeparture = groups.some((g) => Array.isArray(g.departures) && g.departures.length > 0);

  if (!hasAnyDeparture) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="bvg_departures">
        <div class="w-title">
          <i class="ph-bold ph-train" style="color:var(--accent-4)"></i>
          <h3>${escapeHtml(stopName)}</h3>
        </div>
        <div class="w-body"><p class="u-muted">No departures in the next hour.</p></div>
      </div>`;
    return;
  }

  const renderRow = (d, i) => {
    const color = PRODUCT_COLORS[d.product] || "var(--accent-4)";
    const icon = PRODUCT_ICONS[d.product] || "ph-train";
    const delayed = Number(d.delay_min) > 0;
    const timeColor = i === 0 ? "var(--accent-1)" : "var(--text-primary)";
    return `
      <div class="bvg-row ${i % 2 ? "is-zebra" : ""}">
        <div class="bvg-line" style="background:color-mix(in oklab, ${color} 16%, var(--surface));color:${color}">
          <i class="ph-bold ${icon}"></i>${escapeHtml(d.line)}
        </div>
        <div class="list-lead bvg-direction">
          <span class="list-title">${escapeHtml(d.direction)}</span>
        </div>
        <div class="bvg-time-cell">
          <span class="bvg-time" style="color:${timeColor}">${escapeHtml(fmtIn(d.in_min))}</span>
          <small class="bvg-clock">${escapeHtml(d.time)}${delayed ? ` <span class="bvg-delay">+${d.delay_min}</span>` : ""}</small>
        </div>
      </div>`;
  };

  const sections = groups.map((g) => {
    const icon = PRODUCT_ICONS[g.key] || "ph-train";
    const departures = Array.isArray(g.departures) ? g.departures.slice(0, visibleRows) : [];
    const rows = departures.length > 0
      ? departures.map(renderRow).join("")
      : `<div class="bvg-row"><p class="u-muted">No ${escapeHtml(g.label)} departures in the next hour.</p></div>`;
    return `
      <div class="bvg-group">
        <div class="bvg-group-title"><i class="ph-bold ${icon}"></i>${escapeHtml(g.label)}</div>
        <div class="list-body">${rows}</div>
      </div>`;
  }).join("");

  const layout = `
    .bvg-group + .bvg-group {
      margin-top: var(--space-3);
    }
    .bvg-group-title {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 0 var(--space-3) var(--space-1);
      color: var(--text-muted);
      font-weight: var(--fw-black);
      font-size: var(--fs-caption);
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .bvg-row {
      display: flex;
      align-items: center;
      gap: var(--space-3);
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-1);
    }
    .bvg-row.is-zebra {
      background: color-mix(in oklab, var(--text-primary) 3%, transparent);
    }
    .bvg-line {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      flex: 0 0 auto;
      padding: 2px var(--space-2);
      border-radius: var(--radius-1);
      font-weight: var(--fw-black);
      font-size: var(--fs-caption);
      white-space: nowrap;
    }
    .bvg-line i {
      font-size: .9em;
    }
    .bvg-direction {
      flex: 1 1 auto;
      min-width: 0;
    }
    .bvg-direction .list-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .bvg-time-cell {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 0;
      flex: 0 0 auto;
    }
    .bvg-time {
      font-weight: var(--fw-black);
      font-variant-numeric: tabular-nums;
    }
    .bvg-clock {
      color: var(--text-muted);
      font-weight: var(--fw-semi);
      font-size: .75em;
      font-variant-numeric: tabular-nums;
    }
    .bvg-delay {
      color: var(--accent-2);
    }
    @container (max-width: 320px) {
      .bvg-clock { display: none; }
    }
  `;

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w" data-widget="bvg_departures">
      <div class="w-title">
        <i class="ph-bold ph-train" style="color:var(--accent-4)"></i>
        <h3>${escapeHtml(stopName)}</h3>
      </div>
      <div class="w-body">${sections}</div>
    </div>`;
}
