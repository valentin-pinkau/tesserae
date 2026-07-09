// __NAME__ (__ID__), Spectra list archetype.
//
// Template widget: title bar + a zebra list of "icon → label → value" rows.
// Swap the archetype (.list-body here) for the one that matches your data
// shape; copy the closest shipped widget for the exact markup. See
// .claude/skills/create-tesserae-widget/SKILL.md.
//
// Rules baked in below (keep them): paint from semantic tokens only, one
// idempotent shadow.innerHTML, an error state, an empty state, and size
// branching via ctx.cell.size. No fetch / no animation — this is a still
// frame that gets screenshotted.

// Accent slot 1..6 by role (1 alert/now · 2 warning/winner · 3 positive ·
// 4 primary/today · 5 secondary · 6 third). Reach by role, not by hue.
function accentVar(n) {
  const slot = Number(n) >= 1 && Number(n) <= 6 ? Number(n) : 4;
  return `var(--accent-${slot})`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Cap rows on smaller cells so the list never overflows the body. Reserve
// size classes for structural changes like this; use clamp()/cqmin for
// fluid type/icon scaling.
function maxRowsForSize(size, cap) {
  const bySize = { xs: 3, sm: 4, md: 8, lg: 16 };
  return Math.min(cap || 99, bySize[size] ?? 8);
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const size = ctx?.cell?.size || "md";
  const opts = ctx?.cell?.options || {};
  const pluginId = ctx?.cell?.plugin_id || "__ID__";
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  // --- error state (server.py returned {error}) ---
  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="${escapeHtml(pluginId)}">
        <div class="w-title">
          <i class="ph-bold ph-warning-circle" style="color:var(--accent-1)"></i>
          <h3>__NAME__</h3>
        </div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const title = opts.title || data.title || "__NAME__";
  const all = Array.isArray(data.items) ? data.items : [];
  const rows = all.slice(0, maxRowsForSize(size, Number(opts.max_rows)));

  // --- empty state: one big bold Phosphor hero glyph ---
  const body = rows.length === 0
    ? `<div class="tpl-empty">
         <i class="ph-bold ph-tray"></i>
         <span class="u-label">Nothing to show</span>
       </div>`
    : rows.map((it, i) => {
        const ph = it.icon ? `ph-${escapeHtml(it.icon)}` : "ph-circle";
        return `
          <div class="list-row ${i % 2 ? "is-zebra" : ""}">
            <div class="list-lead">
              <i class="ph-bold ${ph}" style="color:${accentVar(it.accent)}"></i>
              <span class="list-title">${escapeHtml(it.label || "")}</span>
            </div>
            <span class="list-meta is-accent">${escapeHtml(it.value ?? "")}</span>
          </div>`;
      }).join("");

  const layout = `
    .list-meta { font-variant-numeric: tabular-nums; }
    .tpl-empty {
      flex: 1 1 auto; min-height: 0;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: var(--space-3);
    }
    .tpl-empty .ph-bold {
      font-size: clamp(48px, 22cqmin, 160px);
      color: var(--accent-1); line-height: 1;
    }
    /* Compact cells: trim the lead icon to save width. */
    @container (max-width: 360px) { .list-lead .ph-bold { display: none; } }
  `;

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w size-${escapeHtml(size)}" data-widget="${escapeHtml(pluginId)}">
      <div class="w-title">
        <i class="ph-bold ph-squares-four" style="color:var(--accent-4)"></i>
        <h3>${escapeHtml(title)}</h3>
        ${all.length ? `<span class="w-title-meta">${all.length}</span>` : ""}
      </div>
      <div class="w-body list-body">${body}</div>
    </div>`;
}
