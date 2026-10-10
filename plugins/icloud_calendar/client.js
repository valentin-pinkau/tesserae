// icloud_calendar, Spectra timetable (.tt-*), size-adaptive.
//
// One private iCloud calendar as a time-grid. Day count follows the
// cell size: xs/sm = today only, md = 3 rolling days, lg = rolling
// 7-day week. Single-day uses the calendar_day layout (big .cal-head +
// one lane + all-day strip); multi-day uses the calendar_week layout
// (range header + per-day column heads + one lane each).
//
// Events arrive from server.py already bucketed into data.days, tagged
// with an accent slot (1..6) per source calendar. We paint each event
// from var(--accent-N) / var(--accent-N-soft) so the widget re-themes
// with the page — no hex, accent-by-role (today = accent-1, the "now"
// slot; calendars are categorical series).

const MONTH_FULL = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const MONTH_SHORT = [
  "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
];
const DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Parse the UTC ISO through Date so the hour lands in the renderer's
// LOCAL timezone (server.py normalises timed events to UTC ISO). The
// naive string-slice path mislands events by the UTC offset — the same
// bug called out in calendar_day / calendar_week.
function parseTime(iso) {
  if (typeof iso !== "string") return null;
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return null;
  return d.getHours() + d.getMinutes() / 60;
}

function fmtHm(iso) {
  if (typeof iso !== "string") return "";
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Auto-fit the hour axis to the visible days' timed events, one hour of
// padding each side. Business-day 8→18 default when nothing is timed.
function computeRange(days) {
  let lo = 24, hi = 0, has = false;
  for (const d of days) {
    for (const ev of d.events || []) {
      if (ev.all_day) continue;
      const s = parseTime(ev.start);
      if (s != null) { lo = Math.min(lo, s); has = true; }
      const e = parseTime(ev.end) ?? (s != null ? s + 1 : null);
      if (e != null) { hi = Math.max(hi, e); has = true; }
    }
  }
  if (!has) return { start: 8, end: 18 };
  return {
    start: Math.max(0, Math.floor(lo) - 1),
    end: Math.min(24, Math.ceil(hi) + 1),
  };
}

// Sun-cycle glyph for the canonical solar transitions, else a 2-hourly
// numeric tick. Reuses the calendar_day treatment so the gutter reads
// morning / midday / evening before the numbers parse.
function todIcon(h) {
  if (h === 0 || h === 24) return { ph: "ph-moon", accent: "var(--text-muted)" };
  if (h === 6) return { ph: "ph-sun-horizon", accent: "var(--accent-2)" };
  if (h === 12) return { ph: "ph-sun", accent: "var(--accent-2)" };
  if (h === 18) return { ph: "ph-sun-horizon", accent: "var(--accent-1)" };
  return null;
}

// Numeric ticks every `step` hours (icons at the solar transitions
// always show). Short lanes (single-day xs/sm, 3-up md) with a wide
// span would collide at every-2h, so we thin to every-4h once the
// range is tall; lg keeps the detailed 2h axis since its lane is tall.
function hourLabels(range, size) {
  const span = range.end - range.start;
  const step = size === "lg" || span <= 9 ? 2 : 4;
  const out = [];
  for (let h = range.start; h <= range.end; h++) {
    const icon = todIcon(h);
    if (icon) {
      out.push(`<span class="tt-hours-icon"><i class="ph-bold ${icon.ph}" style="color:${icon.accent}"></i></span>`);
    } else if (h % step === 0) {
      out.push(`<span>${String(h).padStart(2, "0")}:00</span>`);
    } else {
      out.push(`<span style="opacity:0"></span>`);
    }
  }
  return out.join("");
}

function fmtRange(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const [sy, sm, sd] = startIso.split("-").map(Number);
  const [ey, em, ed] = endIso.split("-").map(Number);
  const startBit = `${MONTH_SHORT[sm - 1] || ""} ${sd}`;
  const endBit = sm === em ? `${ed}` : `${MONTH_SHORT[em - 1] || ""} ${ed}`;
  const year = sy === ey ? `${sy}` : `${sy}/${ey}`;
  return `${startBit} → ${endBit} · ${year}`;
}

function dayCountForSize(size) {
  if (size === "md") return 3;
  if (size === "lg") return 7;
  return 1; // xs / sm
}

// Lay out one day's timed events so none hides another, the way desktop
// calendars do it:
//  * Events starting within CASCADE_MIN_GAP_H of each other while
//    overlapping form a group and split the width side by side (neither
//    title would get a line otherwise).
//  * A group that starts later, while earlier ones are still running, is
//    cascaded: indented one CASCADE_STEP per still-running level and drawn
//    on top, so the earlier event's title stays readable above it and its
//    spine stays visible in the indent.
// Side-by-side splitting alone left 3-4 characters per line in the narrow
// week columns once three events overlapped.
//
// Each event also gets a label room: from its start down to the next
// event that starts below it and covers the same horizontal span (or the
// lane end), so short events can run their title on into free space.
const CASCADE_MIN_GAP_H = 0.5;
const CASCADE_STEP = 0.18;
const CASCADE_MAX_DEPTH = 3;

function layoutDay(timed, range) {
  const items = timed
    .map((ev) => {
      const s = parseTime(ev.start);
      if (s == null) return null;
      const e = Math.max(parseTime(ev.end) ?? s + 1, s + 0.25);
      return { ev, s, e };
    })
    .filter(Boolean)
    .sort((a, b) => a.s - b.s || b.e - a.e);

  const groups = [];
  const running = (g, t) => g.members.some((o) => o.e > t);
  for (const it of items) {
    const join = [...groups].reverse().find(
      (g) => it.s - g.s < CASCADE_MIN_GAP_H && running(g, it.s)
    );
    if (join) {
      join.members.push(it);
      continue;
    }
    const active = groups.filter((g) => running(g, it.s));
    const depth = active.length
      ? Math.min(CASCADE_MAX_DEPTH, Math.max(...active.map((g) => g.depth)) + 1)
      : 0;
    groups.push({ s: it.s, depth, members: [it] });
  }
  let z = 1;
  for (const g of groups) {
    const indent = g.depth * CASCADE_STEP;
    const w = (1 - indent) / g.members.length;
    g.members.forEach((it, i) => {
      it.x0 = indent + i * w;
      it.w = w;
      it.z = z++;
    });
  }

  const xOverlap = (a, b) => a.x0 < b.x0 + b.w - 1e-6 && b.x0 < a.x0 + a.w - 1e-6;
  for (const it of items) {
    let next = range.end;
    for (const o of items) {
      if (o !== it && o.s > it.s && o.s < next && xOverlap(it, o)) next = o.s;
    }
    it.labelEnd = next;
  }
  return items;
}

// One positioned event block. The outer box spans the label room; the
// inner .icd-dur paints the actual duration (accent spine + soft tint),
// so the spine length stays true even when the title runs on below it.
// `opts.showSub` adds the time line, `opts.showLoc` the location row.
function eventBlock(it, range, span, opts) {
  const { ev, s, e } = it;
  const pct = (h) => ((h - range.start) / span) * 100;
  const top = Math.max(0, pct(s));
  const durH = Math.max(0.8, pct(Math.min(e, range.end)) - top);
  // The title may run past the duration into free space, but must stop
  // where a later event starts on top of this one (labelEnd < end then).
  const labelH = Math.max(0.8, pct(Math.min(it.labelEnd, range.end)) - top);
  const boxH = Math.max(durH, labelH);
  const left = it.x0 * 100;
  const width = it.w * 100;
  const n = ev.accent || 4;
  const time = `${fmtHm(ev.start)}${ev.end ? `–${fmtHm(ev.end)}` : ""}`;
  const loc = ev.location || "";
  const sub = opts.showSub ? `<span class="tt-sub">${escapeHtml(time)}</span>` : "";
  const locRow = opts.showLoc && loc
    ? `<span class="tt-loc"><i class="ph-bold ph-map-pin"></i>${escapeHtml(loc)}</span>` : "";
  return `
    <div class="tt-event icd-ev"
         style="top:${top.toFixed(2)}%;height:${boxH.toFixed(2)}%;left:${left.toFixed(2)}%;width:${width.toFixed(2)}%;z-index:${it.z};--tt-bg:var(--accent-${n}-soft)"
         data-label-frac="${(labelH / boxH).toFixed(4)}"
         title="${escapeHtml(time)} ${escapeHtml(ev.summary || "")}${loc ? ` · ${escapeHtml(loc)}` : ""}">
      <span class="icd-dur" style="height:${((durH / boxH) * 100).toFixed(2)}%;border-left-color:var(--accent-${n})"></span>
      <span class="tt-name">${escapeHtml(ev.summary || "")}</span>
      ${sub}
      ${locRow}
    </div>`;
}

// After layout: fit each title to the room its box actually has, as a
// whole number of lines with an ellipsis on the last one, instead of a
// fixed 3-line clamp that either wastes free space or cuts mid-line.
function fitTitles(root) {
  for (const box of root.querySelectorAll(".icd-ev")) {
    const name = box.querySelector(".tt-name");
    if (!name) continue;
    const cs = getComputedStyle(box);
    const frac = Number(box.dataset.labelFrac) || 1;
    let room = box.clientHeight * frac - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const lh = parseFloat(getComputedStyle(name).lineHeight) || name.offsetHeight || 16;
    const minLine = lh * 0.75;
    // Title first; the time / location rows only if a title line still
    // fits beside them, otherwise they'd overlap the event below.
    for (const extra of box.querySelectorAll(".tt-sub, .tt-loc")) {
      if (room - extra.offsetHeight >= minLine) room -= extra.offsetHeight;
      else extra.style.display = "none";
    }
    const lines = Math.floor((room + lh * 0.25) / lh);
    if (lines < 1) {
      name.style.display = "none";
    } else {
      name.style.webkitLineClamp = String(lines);
      name.style.lineClamp = String(lines);
    }
  }
}

function laneHtml(day, range, span, nowH, opts) {
  const timed = (day.events || []).filter((e) => !e.all_day);
  const blocks = layoutDay(timed, range).map((it) => eventBlock(it, range, span, opts)).join("");
  const showNow = day.is_today && nowH >= range.start && nowH <= range.end;
  const nowLine = showNow
    ? `<div class="tt-now" style="top:${(((nowH - range.start) / span) * 100).toFixed(2)}%"></div>`
    : "";
  const weekend = day.weekday >= 5 ? " is-weekend" : "";
  return `<div class="tt-lane has-rule${day.is_today ? " is-today" : ""}${weekend}">${blocks}${nowLine}</div>`;
}

// One day's all-day events as a stacked column of pills, sitting in the
// all-day band above the lanes (multi-day views). All-day events have no
// position on the hour axis, so they'd otherwise be dropped from the lane.
// Capped so a busy day can't push the band tall (it steals lane height);
// overflow shows "+N". Tighter cap at md — the 3-up cell is short.
function alldayCell(day, maxPills) {
  const ad = (day.events || []).filter((e) => e.all_day);
  if (!ad.length) return `<div class="tt-allday-col"></div>`;
  const pills = ad.slice(0, maxPills).map((ev) => {
    const n = ev.accent || 4;
    return `<span class="tt-allday-pill" style="border-left-color:var(--accent-${n});--tt-bg:var(--accent-${n}-soft)" title="${escapeHtml(ev.summary || "")}">${escapeHtml(ev.summary || "")}</span>`;
  }).join("");
  const more = ad.length > maxPills
    ? `<span class="tt-allday-more">+${ad.length - maxPills}</span>`
    : "";
  return `<div class="tt-allday-col">${pills}${more}</div>`;
}

const HERO_EMPTY = `
  <div class="icd-empty">
    <i class="ph-bold ph-calendar-blank"></i>
    <span class="u-label">Nothing scheduled</span>
  </div>`;

export default async function render(shadow, ctx) {
  const __t0 = performance.now();
  const data = ctx?.data ?? {};
  const size = ctx?.cell?.size || "md";
  const opts = ctx?.cell?.options || {};
  const hideLabels = opts.hide_labels === true;
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="icloud_calendar">
        <div class="w-title"><i class="ph-bold ph-warning-circle" style="color:var(--accent-1)"></i><h3>iCloud Calendar</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    console.log(`[icloud_calendar] render() took ${(performance.now() - __t0).toFixed(2)}ms (error path)`);
    return;
  }

  const allDays = Array.isArray(data.days) ? data.days : [];
  const count = dayCountForSize(size);
  const days = allDays.slice(0, count);
  const range = computeRange(days);
  const span = Math.max(1, range.end - range.start);
  const now = new Date();
  const nowH = now.getHours() + now.getMinutes() / 60;
  const totalEvents = days.reduce((a, d) => a + (d.events?.length || 0), 0);

  const layout = `
    .icd-lead { color: var(--accent-1); font-size: var(--icon-sm); margin-right: 0.4em; }
    .tt-hours-icon { display: inline-flex; align-items: center; justify-content: flex-end; font-size: 1.05em; line-height: 1; }
    .tt-hours-icon .ph-bold { line-height: 1; }
    /* Hourly grid lines (the base archetype rules every 2h and faintly):
       one grey line per hour, aligned to the gutter labels, so an event's
       top/bottom edge reads against a known hour. --tt-hours is the visible
       span, set on .tt-body. */
    .tt-lane.has-rule {
      /* Darker than the default --surface-sunken rule: mix ~18% toward
         text so the hourly line reads clearly and survives the e-ink
         quantize, on any theme. */
      --tt-rule: color-mix(in oklab, var(--text-primary) 18%, transparent);
      background-image: repeating-linear-gradient(
        to bottom,
        transparent 0,
        transparent calc(100% / var(--tt-hours, 12) - 1px),
        var(--tt-rule) calc(100% / var(--tt-hours, 12) - 1px),
        var(--tt-rule) calc(100% / var(--tt-hours, 12))
      );
    }
    /* Event blocks paint from accent-N-soft (the documented soft
       companion) with a solid accent-N spine, so they re-theme without
       color-mix. Week columns are narrow, so tighten the title. */
    .tt-event .tt-loc {
      display: inline-flex; align-items: center; gap: 0.25em;
      font-size: var(--fs-caption); font-weight: var(--fw-bold);
      color: var(--text-muted); letter-spacing: var(--ls-label);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
    }
    .tt-event .tt-loc .ph-bold { font-size: 0.95em; color: var(--text-muted); flex: 0 0 auto; }
    /* Event box = label room (see layoutDay); the duration itself is the
       .icd-dur layer behind the text. Boxes sit side by side when events
       overlap, with a hairline gap so neighbouring spines stay apart. The
       title wraps to as many lines as the box has room for (fitTitles
       sets the clamp after layout), breaking long words if it must. */
    .tt-event.icd-ev {
      right: auto;
      background: none;
      border-left: none;
      box-sizing: border-box;
      padding: 2px 3px 2px calc(var(--stroke-3) + 4px);
      gap: 0;
      isolation: isolate;
      border-right: 2px solid transparent;
      background-clip: padding-box;
    }
    .icd-dur {
      position: absolute; left: 0; top: 0; right: 2px;
      z-index: -1;
      border-left: var(--stroke-3) solid var(--accent-4);
      /* Opaque (tint over surface) so a cascaded event cleanly covers
         the lower part of the one it overlaps. */
      background: linear-gradient(var(--tt-bg, var(--surface)), var(--tt-bg, var(--surface))), var(--surface);
      border-top: 1px solid var(--text-muted);
    }
    .tt-event .tt-name {
      font-size: calc(var(--fs-caption) * 0.9);
      line-height: 1.05;
      font-weight: var(--fw-bold);
      white-space: normal;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
      overflow-wrap: anywhere;
      hyphens: auto;
      flex: 0 0 auto;
    }
    .tt-event .tt-sub { font-size: calc(var(--fs-caption) * 0.85); }
    [data-hide-labels="true"] .tt-event .tt-name,
    [data-hide-labels="true"] .tt-event .tt-sub,
    [data-hide-labels="true"] .tt-event .tt-loc { display: none; }
    [data-hide-labels="true"] .tt-event { padding-top: 0; padding-bottom: 0; }

    /* Per-column head (multi-day): DOW + day number. Today is picked out
       with the inverse accent-1 chip; weekends get a faint sunken tint
       (heads + lanes) for orientation. Mirrors calendar_week. */
    .tt-col-head { display: flex; flex-direction: column; align-items: center; gap: 0.15em; padding-bottom: var(--space-1); }
    .tt-col-dow { font-size: var(--fs-caption); font-weight: var(--fw-black); letter-spacing: var(--ls-label); text-transform: var(--label-transform, uppercase); color: var(--text-muted); }
    .tt-col-day { font-size: var(--fs-body); font-weight: var(--fw-bold); color: var(--text-primary); line-height: 1.1; }
    .tt-col-head.is-today { color: var(--accent-1); }
    .tt-col-head.is-today .tt-col-dow { color: var(--accent-1); }
    .tt-col-head.is-today .tt-col-day { background: var(--accent-1); color: var(--on-accent); width: 1.7em; height: 1.7em; display: inline-grid; place-items: center; border-radius: 999px; font-weight: var(--fw-black); }
    /* Weekend tint: soft neutral wash, not an accent (Sat/Sun aren't an
       alert). No surface-*-soft token exists, so mix toward text at low % —
       the same subtle wash calendar_week uses. */
    .tt-col-head.is-weekend .tt-col-dow { color: var(--text-secondary); }
    .tt-lane.is-weekend { background-color: color-mix(in oklab, var(--text-primary) 4%, transparent); }

    /* All-day band (multi-day): one stacked column of pills per day,
       aligned under the column heads. Pills reuse .tt-allday-pill (accent
       spine + soft fill) but stack full-width and shrink to fit the
       column. hide_labels collapses them to plain colour bars. */
    .tt-allday-col { display: flex; flex-direction: column; gap: 2px; min-width: 0; align-self: start; padding: 0 var(--space-1) var(--space-2); }
    .tt-allday-col .tt-allday-pill {
      display: block;
      font-size: calc(var(--fs-caption) * 0.85);
      font-weight: var(--fw-bold);
      padding: 1px var(--space-1);
      color: var(--text-primary);
      /* Two wrapped lines before the ellipsis: one line cut most names
         ("Urlaub Bran…") in the narrow week columns. */
      white-space: normal; overflow: hidden; overflow-wrap: anywhere;
      display: -webkit-box; -webkit-box-orient: vertical;
      -webkit-line-clamp: 2; line-clamp: 2;
    }
    .tt-allday-more { font-size: var(--fs-caption); font-weight: var(--fw-bold); color: var(--text-muted); padding-left: var(--space-1); }
    [data-hide-labels="true"] .tt-allday-col .tt-allday-pill { font-size: 0; min-height: 0.55em; }

    /* Empty-state hero, the one big bold Phosphor glyph. */
    .icd-empty { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-3); }
    .icd-empty .ph-bold { font-size: clamp(48px, 22cqmin, 160px); color: var(--accent-1); line-height: 1; }

    /* Compact: drop event locations to keep tight cells uncluttered. */
    @container (max-width: 360px) {
      .tt-event .tt-loc { display: none; }
    }
  `;

  let headerHtml;
  let bodyHtml;

  if (count === 1) {
    // ---- single day (xs / sm) : calendar_day layout ----
    const day = days[0] || { events: [] };
    const [y, m, d] = (day.date || "").split("-").map(Number);
    const local = Number.isFinite(y) ? new Date(y, m - 1, d) : now;
    // Short weekday (THU) not full (THURSDAY): the single-day header
    // only renders at xs/sm, where the jumbo title has no room for the
    // long word — it would ellipsis to "•••".
    const weekday = DOW[(local.getDay() + 6) % 7];
    const monthName = (MONTH_FULL[(m || 1) - 1] || "").toUpperCase();
    // xs (180px) has no room for the lead glyph + month meta alongside
    // the jumbo title — drop both so "THU 9" doesn't ellipsis to "•••".
    const lead = size === "xs" ? "" : `<i class="ph-bold ph-calendar-dots icd-lead"></i>`;
    const meta = size === "xs"
      ? ""
      : `<span class="cal-head-meta">${escapeHtml(monthName)} ${escapeHtml(String(y || ""))}</span>`;
    headerHtml = `
      <div class="cal-head">
        <div class="cal-head-row">
          <span class="cal-head-title">${lead}${escapeHtml(weekday)} <span class="num">${escapeHtml(String(d || ""))}</span></span>
          ${meta}
        </div>
        <div class="cal-head-rule"></div>
      </div>`;

    if (totalEvents === 0) {
      bodyHtml = HERO_EMPTY;
    } else {
      const allDay = (day.events || []).filter((e) => e.all_day);
      const allDayStrip = allDay.length
        ? `<div class="tt-allday">${allDay.map((ev) => {
            const n = ev.accent || 4;
            return `<span class="tt-allday-pill" style="border-left-color:var(--accent-${n});--tt-bg:var(--accent-${n}-soft)">${escapeHtml(ev.summary || "")}</span>`;
          }).join("")}</div>`
        : "";
      const lane = laneHtml(day, range, span, nowH, { showSub: true, showLoc: true });
      bodyHtml = `
        ${allDayStrip}
        <div class="tt-body" style="--tt-hours:${span};flex:1 1 auto;min-height:0;display:flex;flex-direction:column">
          <div class="tt" style="flex:1 1 auto;min-height:0">
            <div class="tt-hours">${hourLabels(range, size)}</div>
            ${lane}
          </div>
        </div>`;
    }
  } else {
    // ---- multi day (md = 3, lg = 7) : calendar_week layout ----
    // Kept short: the jumbo title is ~2.5× the base font, so "NEXT 3
    // DAYS" overflows a 640px md cell and ellipsises to "NEX…".
    const title = count === 7 ? "THIS WEEK" : `${count} DAYS`;
    // The date-range meta only fits alongside the jumbo title at lg; at
    // md the per-column heads already carry the dates, so drop it rather
    // than let the title ellipsise.
    const meta = size === "lg"
      ? `<span class="cal-head-meta">${escapeHtml(fmtRange(data.start, days[days.length - 1]?.date))}</span>`
      : "";
    headerHtml = `
      <div class="cal-head">
        <div class="cal-head-row">
          <span class="cal-head-title"><i class="ph-bold ph-calendar-dots icd-lead"></i>${escapeHtml(title)}</span>
          ${meta}
        </div>
        <div class="cal-head-rule"></div>
      </div>`;

    if (totalEvents === 0) {
      bodyHtml = HERO_EMPTY;
    } else {
      const heads = days.map((day) => {
        const cls = `tt-col-head${day.is_today ? " is-today" : ""}${day.weekday >= 5 ? " is-weekend" : ""}`;
        return `
          <div class="${cls}">
            <span class="tt-col-dow">${escapeHtml(DOW[day.weekday] || "")}</span>
            <span class="tt-col-day">${escapeHtml(String(day.day || ""))}</span>
          </div>`;
      }).join("");
      const lanes = days
        .map((day) => laneHtml(day, range, span, nowH, { showSub: false, showLoc: false }))
        .join("");
      // All-day band: only rendered when some visible day has an all-day
      // event, so the grid stays 2-row (heads / lanes) otherwise. When
      // present it becomes a 3rd `auto` row above the lanes, aligned to the
      // day columns (col 1 is the empty hour-gutter spacer).
      const hasAllDay = days.some((d) => (d.events || []).some((e) => e.all_day));
      const maxPills = size === "lg" ? 2 : 1;
      const alldayRow = hasAllDay
        ? `<div class="tt-allday-gutter"></div>${days.map((d) => alldayCell(d, maxPills)).join("")}`
        : "";
      const cols = `auto repeat(${count}, minmax(0, 1fr))`;
      const gridRows = hasAllDay ? "auto auto 1fr" : "auto 1fr";
      bodyHtml = `
        <div class="tt-body" style="--tt-hours:${span};flex:1 1 auto;min-height:0;display:flex;flex-direction:column">
          <div class="tt is-week" style="flex:1 1 auto;min-height:0;grid-template-columns:${cols};grid-template-rows:${gridRows}">
            <div></div>
            ${heads}
            ${alldayRow}
            <div class="tt-hours">${hourLabels(range, size)}</div>
            ${lanes}
          </div>
        </div>`;
    }
  }

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w size-${escapeHtml(size)}" lang="de" data-widget="icloud_calendar" data-hide-labels="${hideLabels}">
      <div class="w-body" style="gap:var(--space-3)">
        ${headerHtml}
        ${bodyHtml}
      </div>
    </div>`;
  // fitTitles measures real box heights, so the stylesheet (lane
  // positioning, font sizes) and web fonts must be in before it runs. The
  // composer awaits render() before the screenshot.
  if (!hideLabels) {
    const link = shadow.querySelector('link[rel="stylesheet"]');
    if (link && !link.sheet) {
      await new Promise((resolve) => {
        link.addEventListener("load", resolve, { once: true });
        link.addEventListener("error", resolve, { once: true });
      });
    }
    await document.fonts?.ready;
    fitTitles(shadow);
  }
  console.log(`[icloud_calendar] render() took ${(performance.now() - __t0).toFixed(2)}ms`);
}
