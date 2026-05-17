/* coach-calendar.js — Coach App /calendar (Phase 15.3)
 *
 * Month grid + day-detail panel + modal scaffolding for the Coach
 * Calendar. Modal INNER logic (forms, attendance, notes) comes in
 * Phase 15.4 — this file only wires open/close + data hookup.
 *
 * Data:   GET /api/coach/practice/range?start=&end=&team_id=
 * Render: 7×6 grid of cells (with leading/trailing days from
 *         adjacent months), expandable day-detail panel BELOW.
 * Theme:  Coach App dark/orange. RTL. Hebrew labels.
 *
 * Times come back as ISO UTC. We display them in Asia/Jerusalem
 * regardless of the user's browser TZ.
 *
 * Security note: every user-supplied string lands via textContent
 * / setAttribute / dataset, never innerHTML. The only innerHTML in
 * this file is for inline SVG markup we author ourselves.
 */
(function () {
    "use strict";

    // ── Constants ──────────────────────────────────────────────
    // Phase 15.9 — Coach Calendar is English-first. Hebrew strings move
    // here in a later phase; for now everything user-facing in this file
    // is English to match the per-user feedback.
    const TZ = "Asia/Jerusalem";
    const MONTH_HE = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ];
    const WEEKDAY_HE_LONG = [
        "Sunday", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday",
    ];
    const EVENT_TYPE_HE = {
        practice: "Practice",
        game:     "Game",
        event:    "Event",
        meeting:  "Meeting",
        other:    "Other",
    };
    const MAX_CHIPS_PER_CELL = 3;
    // Fallback color when an event row arrives without team_color_hex
    // (shouldn't happen — backend lazy-assigns colors — but defensive).
    const DEFAULT_CHIP_COLOR = "#ff6b35";

    // ── DOM handles ────────────────────────────────────────────
    const $grid = document.getElementById("cal-grid");
    if (!$grid) return;   // safety: page didn't render properly

    const $monthName = document.querySelector("[data-month-name]");
    const $yearLabel = document.querySelector("[data-year]");
    const $emptyMonth = document.getElementById("cal-empty-month");
    const $dayPanel = document.getElementById("cal-day-panel");
    const $dayPanelTitle = document.getElementById("cal-day-panel-title");
    const $dayPanelBody = document.getElementById("cal-day-panel-body");
    const $teamSelect = document.getElementById("cal-team-select");

    const $modals = {
        evt: document.getElementById("evt-modal"),
        attendance: document.getElementById("attendance-modal"),
        plan: document.getElementById("plan-modal"),
        note: document.getElementById("note-modal"),
    };

    // ── State ──────────────────────────────────────────────────
    const today = new Date();
    const state = {
        year: today.getFullYear(),
        month: today.getMonth(),       // 0..11
        teamId: "",                    // "" = all teams
        events: [],                    // current month's events
        teams: [],                     // distinct teams seen in the range (Phase 15.4)
        expandedDay: null,             // YYYY-MM-DD of selected day
        expandedEventId: null,         // event id whose card is expanded in panel
        loading: false,
        modalRestoreFocus: null,       // element to restore focus to on close
    };

    // Seed state.teams from the server-rendered <select> options so the
    // event modal can populate its team picker before the first range
    // fetch returns. The agent-built page already serializes the coach's
    // teams as <option> tags inside #cal-team-select.
    (function seedTeamsFromSelect() {
        const select = document.getElementById("cal-team-select");
        if (!select) return;
        const opts = Array.from(select.options || []);
        state.teams = opts
            .filter(o => o.value)  // skip the "All teams" placeholder
            .map(o => ({ id: parseInt(o.value, 10), team_name: o.textContent.trim() }));
    })();

    // ── Helpers ────────────────────────────────────────────────
    function pad2(n) { return n < 10 ? "0" + n : String(n); }
    function ymd(d) {
        return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
    }

    function el(tag, opts, children) {
        const n = document.createElement(tag);
        if (opts) {
            if (opts.className) n.className = opts.className;
            if (opts.text != null) n.textContent = opts.text;
            if (opts.attrs) {
                Object.keys(opts.attrs).forEach(k => n.setAttribute(k, opts.attrs[k]));
            }
            if (opts.dataset) {
                Object.keys(opts.dataset).forEach(k => { n.dataset[k] = opts.dataset[k]; });
            }
            if (opts.style) {
                Object.keys(opts.style).forEach(k => { n.style.setProperty(k, opts.style[k]); });
            }
        }
        if (children) {
            children.forEach(c => { if (c) n.appendChild(c); });
        }
        return n;
    }

    /** Build an inline SVG node. paths is an array of `<path>`-style snippets
     *  (we only ever set d/cx/cy/r/x1/y1/x2/y2/points — no user content). */
    function svgIcon(viewBox, paths) {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", viewBox || "0 0 24 24");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.setAttribute("aria-hidden", "true");
        paths.forEach(p => {
            const child = document.createElementNS(ns, p.tag || "path");
            Object.keys(p).forEach(k => {
                if (k === "tag") return;
                child.setAttribute(k, p[k]);
            });
            svg.appendChild(child);
        });
        return svg;
    }

    /** Convert an ISO datetime (UTC) to its Asia/Jerusalem clock parts.
     *  Returns {year, month (1..12), day, hour (0..23), minute, weekday (0..6, 0=Sun)}.
     *  Using Intl.DateTimeFormat avoids fragile UTC offset math.
     *
     *  Defensive parsing: the backend stores naive UTC and emits ISO
     *  strings WITHOUT a timezone suffix ("2026-05-15T16:00:00").
     *  `new Date("2026-05-15T16:00:00")` interprets that as LOCAL time,
     *  which would be wrong for a coach not on UTC. So if the string
     *  has neither a "Z" nor a "+/-HH:MM" suffix, we append "Z" before
     *  parsing — making it explicit UTC, which is what the backend
     *  actually stored. Strings already carrying a TZ suffix are
     *  passed through unchanged. */
    function toJerusalem(iso) {
        if (!iso) return null;
        let str = String(iso);
        const hasTzSuffix = /([Zz]|[+\-]\d{2}:?\d{2})$/.test(str);
        if (!hasTzSuffix) str += "Z";
        const d = new Date(str);
        if (isNaN(d.getTime())) return null;
        // formatToParts with the TZ option returns the wall-clock view at TZ.
        const fmt = new Intl.DateTimeFormat("en-GB", {
            timeZone: TZ,
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", hour12: false,
            weekday: "short",
        });
        const parts = {};
        fmt.formatToParts(d).forEach(p => { parts[p.type] = p.value; });
        // weekday short is "Sun".."Sat" — map to 0..6 (Sun-first per IL conv).
        const wkmap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
        return {
            year: parseInt(parts.year, 10),
            month: parseInt(parts.month, 10),
            day: parseInt(parts.day, 10),
            hour: parts.hour === "24" ? 0 : parseInt(parts.hour, 10),
            minute: parseInt(parts.minute, 10),
            weekday: wkmap[parts.weekday] != null ? wkmap[parts.weekday] : 0,
        };
    }

    /** "YYYY-MM-DD" string of this UTC ISO in Asia/Jerusalem TZ. */
    function jerusalemYmd(iso) {
        const j = toJerusalem(iso);
        if (!j) return null;
        return j.year + "-" + pad2(j.month) + "-" + pad2(j.day);
    }

    /** "HH:MM" string of this UTC ISO in Asia/Jerusalem TZ. */
    function jerusalemHm(iso) {
        const j = toJerusalem(iso);
        if (!j) return "";
        return pad2(j.hour) + ":" + pad2(j.minute);
    }

    /** "HH:MM - HH:MM" given a start ISO + duration in minutes (in Jerusalem TZ). */
    function jerusalemTimeRange(iso, durationMin) {
        const start = jerusalemHm(iso);
        if (!start) return "";
        if (!durationMin) return start;
        const j = toJerusalem(iso);
        const endMinutes = (j.hour * 60 + j.minute + durationMin) % (24 * 60);
        const eh = Math.floor(endMinutes / 60);
        const em = endMinutes % 60;
        return start + " - " + pad2(eh) + ":" + pad2(em);
    }

    /** Sunday-anchored grid bounds — 42 days (6 weeks) covering the month
     *  plus leading/trailing days from neighbors. Sunday is firstDow=0,
     *  matching JS `getDay()` and Israeli convention. */
    function gridBounds(year, month) {
        const first = new Date(year, month, 1);
        const firstDow = first.getDay();           // 0 = Sunday
        const start = new Date(year, month, 1 - firstDow);
        const end = new Date(start);
        end.setDate(start.getDate() + 42);         // exclusive
        return { start, end };
    }

    /** Group the events array into `{ "YYYY-MM-DD": [event, …] }`. */
    function bucketByDay(events) {
        const buckets = {};
        for (let i = 0; i < events.length; i++) {
            const ev = events[i];
            const key = jerusalemYmd(ev.scheduled_at);
            if (!key) continue;
            (buckets[key] || (buckets[key] = [])).push(ev);
        }
        // Sort each day's events ascending by time.
        Object.keys(buckets).forEach(k => {
            buckets[k].sort((a, b) => (a.scheduled_at || "").localeCompare(b.scheduled_at || ""));
        });
        return buckets;
    }

    // ── API ────────────────────────────────────────────────────
    async function fetchRange() {
        state.loading = true;
        renderSkeleton();
        const bounds = gridBounds(state.year, state.month);
        const params = new URLSearchParams({
            start: ymd(bounds.start),
            end: ymd(bounds.end),
        });
        if (state.teamId) params.set("team_id", state.teamId);
        let data = { events: [] };
        try {
            const res = await fetch("/api/coach/practice/range?" + params.toString(), {
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            });
            if (res.ok) {
                data = await res.json();
            } else {
                // 401 → middleware redirects to /login. For any other failure,
                // we'll just render the empty state below.
                console.warn("[calendar] /range returned", res.status);
            }
        } catch (e) {
            console.warn("[calendar] /range fetch failed", e);
        }
        state.loading = false;
        state.events = Array.isArray(data.events) ? data.events : [];
        // Merge any newly-seen teams into the filter dropdown so multi-team
        // coaches see all their teams even if the server-rendered list was
        // stale.
        mergeTeamsIntoFilter(state.events);
        render();
    }

    function mergeTeamsIntoFilter(events) {
        if (!$teamSelect) return;
        const known = {};
        Array.from($teamSelect.options).forEach(o => {
            if (o.value) known[o.value] = true;
        });
        const additions = [];
        events.forEach(ev => {
            if (ev.team_id == null) return;
            const v = String(ev.team_id);
            if (!known[v]) {
                known[v] = true;
                additions.push({ id: v, name: ev.team_name || "—" });
            }
        });
        additions.sort((a, b) => a.name.localeCompare(b.name, "he"));
        additions.forEach(t => {
            const o = document.createElement("option");
            o.value = t.id;
            o.textContent = t.name;
            o.setAttribute("dir", "auto");
            $teamSelect.appendChild(o);
        });
    }

    // ── Render: month label + grid ─────────────────────────────
    function renderMonthLabel() {
        if ($monthName) $monthName.textContent = MONTH_HE[state.month];
        if ($yearLabel) $yearLabel.textContent = String(state.year);
    }

    function renderSkeleton() {
        renderMonthLabel();
        $grid.replaceChildren();
        for (let i = 0; i < 42; i++) {
            $grid.appendChild(el("div", { className: "cal-cell is-skeleton" }));
        }
        if ($emptyMonth) $emptyMonth.hidden = true;
        // Skeleton wipes the day-panel too so it doesn't linger across
        // month-changes.
        if ($dayPanel) $dayPanel.hidden = true;
        state.expandedDay = null;
    }

    function render() {
        renderMonthLabel();
        $grid.replaceChildren();

        const bounds = gridBounds(state.year, state.month);
        const buckets = bucketByDay(state.events);
        const todayKey = ymd(new Date());
        const cur = new Date(bounds.start);

        let totalCurrentMonthEvents = 0;

        for (let i = 0; i < 42; i++) {
            const key = ymd(cur);
            const isOther = cur.getMonth() !== state.month;
            const isToday = key === todayKey;
            const isSelected = state.expandedDay === key;
            const dayEvents = buckets[key] || [];
            if (!isOther) totalCurrentMonthEvents += dayEvents.length;

            const cls = ["cal-cell"];
            if (isOther) cls.push("is-other-month");
            if (isToday) cls.push("is-today");
            if (isSelected) cls.push("is-selected");
            if (dayEvents.length === 0) cls.push("is-empty-hover");

            const cell = el("div", {
                className: cls.join(" "),
                attrs: {
                    role: "gridcell",
                    tabindex: "0",
                    "aria-label": cur.toLocaleDateString("en-US", { day: "numeric", month: "long" })
                        + (dayEvents.length ? " · " + dayEvents.length + " events" : ""),
                },
                dataset: { date: key },
            });

            cell.appendChild(el("div", {
                className: "cal-day-num",
                attrs: { dir: "ltr" },
                text: String(cur.getDate()),
            }));

            // Chips. Max MAX_CHIPS_PER_CELL; overflow renders a "+N more" pill.
            const visible = dayEvents.slice(0, MAX_CHIPS_PER_CELL);
            const overflow = dayEvents.length - visible.length;
            visible.forEach(ev => cell.appendChild(buildChip(ev, key)));
            if (overflow > 0) {
                const moreBtn = el("button", {
                    className: "cal-chip-more",
                    text: "+ " + overflow + " more",
                    attrs: { type: "button", "aria-label": "Show " + overflow + " more events" },
                    dataset: { dayExpand: key },
                });
                cell.appendChild(moreBtn);
            }

            $grid.appendChild(cell);
            cur.setDate(cur.getDate() + 1);
        }

        // Empty-month state: only show if the CURRENT month (not the
        // padding days) has no events, AND we're not loading.
        if ($emptyMonth) {
            $emptyMonth.hidden = state.loading || totalCurrentMonthEvents > 0;
        }

        renderDayPanel();
    }

    function buildChip(ev, dayKey) {
        const color = (ev.team_color_hex || DEFAULT_CHIP_COLOR);
        const time = jerusalemHm(ev.scheduled_at);
        const title = chipTitle(ev);

        const chip = el("button", {
            className: "cal-chip",
            attrs: {
                type: "button",
                title: chipTooltip(ev),
                "aria-label": time + " " + title,
            },
            dataset: {
                eventId: String(ev.id),
                dayExpand: dayKey,
            },
            style: { "--cal-chip-color": color },
        });
        chip.appendChild(el("span", {
            className: "cal-chip-dot",
            attrs: { "aria-hidden": "true" },
        }));
        chip.appendChild(el("span", {
            className: "cal-chip-time",
            attrs: { dir: "ltr" },
            text: time,
        }));
        chip.appendChild(el("span", {
            className: "cal-chip-label",
            attrs: { dir: "auto" },
            text: title,
        }));
        return chip;
    }

    function chipTitle(ev) {
        // Free-typed title wins. Falls back to event-type-aware label.
        if (ev.title) return ev.title;
        if (ev.event_type === "game") {
            const home = ev.opponent_home || ev.team_name || "";
            const away = ev.opponent_away || "";
            if (home && away) return home + " vs " + away;
            return EVENT_TYPE_HE.game + " · " + (ev.team_name || "—");
        }
        if (ev.event_type === "other" && ev.event_type_custom) {
            return ev.event_type_custom + " · " + (ev.team_name || "—");
        }
        const label = EVENT_TYPE_HE[ev.event_type || "practice"] || EVENT_TYPE_HE.practice;
        return label + " · " + (ev.team_name || "—");
    }

    function chipTooltip(ev) {
        const bits = [];
        bits.push(jerusalemHm(ev.scheduled_at));
        if (ev.title) bits.push(ev.title);
        if (ev.team_name) bits.push(ev.team_name);
        if (ev.location) bits.push(ev.location);
        return bits.join(" · ");
    }

    // ── Render: day-detail panel ───────────────────────────────
    function renderDayPanel() {
        if (!$dayPanel) return;
        if (!state.expandedDay) {
            $dayPanel.hidden = true;
            return;
        }
        const buckets = bucketByDay(state.events);
        const dayEvents = buckets[state.expandedDay] || [];

        // Header — "יום שישי, 15/5/2026"
        const d = new Date(state.expandedDay + "T00:00:00");
        const weekday = WEEKDAY_HE_LONG[d.getDay()];
        const dateLtr = d.getDate() + "/" + (d.getMonth() + 1) + "/" + d.getFullYear();
        $dayPanelTitle.replaceChildren();
        $dayPanelTitle.appendChild(document.createTextNode("יום " + weekday + ", "));
        const dateSpan = el("span", { attrs: { dir: "ltr" }, text: dateLtr });
        $dayPanelTitle.appendChild(dateSpan);

        $dayPanelBody.replaceChildren();

        if (dayEvents.length === 0) {
            // Empty day — friendly CTA so the click doesn't feel like a dead end.
            const empty = el("div", { className: "cal-empty-day" });
            const emptyIcon = svgIcon("0 0 24 24", [
                { d: "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" },
            ]);
            emptyIcon.setAttribute("class", "cal-empty-day-icon");
            empty.appendChild(emptyIcon);

            empty.appendChild(el("p", { text: "אין אירועים ביום זה" }));
            const cta = el("button", {
                className: "cal-action-btn",
                attrs: { type: "button" },
                dataset: { action: "new-event-for-day", date: state.expandedDay },
            });
            cta.appendChild(svgIcon("0 0 24 24", [
                { tag: "line", x1: "12", y1: "5", x2: "12", y2: "19" },
                { tag: "line", x1: "5", y1: "12", x2: "19", y2: "12" },
            ]));
            cta.appendChild(document.createTextNode("הוסף אירוע ליום זה"));
            empty.appendChild(cta);
            $dayPanelBody.appendChild(empty);
        } else {
            dayEvents.forEach(ev => $dayPanelBody.appendChild(buildEventCard(ev)));
        }

        $dayPanel.hidden = false;
    }

    function buildEventCard(ev) {
        const color = ev.team_color_hex || DEFAULT_CHIP_COLOR;
        const isExpanded = state.expandedEventId === ev.id;

        const card = el("article", {
            className: "cal-event-card" + (isExpanded ? " is-expanded" : ""),
            dataset: { eventId: String(ev.id) },
            style: { "--cal-chip-color": color },
        });

        // ── Summary row ──
        // We render the summary as a div with role="button" rather than a
        // real <button> so we can legally nest the inline "ערוך" pill
        // inside it. (button-inside-button is invalid HTML and browsers
        // hoist the inner button out of its parent, which breaks layout.)
        const summary = el("div", {
            className: "cal-event-summary",
            attrs: {
                role: "button",
                tabindex: "0",
                "aria-expanded": isExpanded ? "true" : "false",
                "aria-controls": "evt-body-" + ev.id,
            },
            dataset: { eventToggle: String(ev.id) },
        });

        // Chevron — pointing right (▶), CSS rotates on expand + flips for RTL.
        const chev = svgIcon("0 0 24 24", [
            { tag: "polyline", points: "9 18 15 12 9 6" },
        ]);
        chev.setAttribute("class", "cal-event-toggle");
        summary.appendChild(chev);

        // Team color dot
        summary.appendChild(el("span", {
            className: "cal-event-color",
            attrs: { "aria-hidden": "true" },
        }));

        // Title block — team name + meta (time + location)
        const titleWrap = el("div", { className: "cal-event-title" });
        titleWrap.appendChild(el("span", {
            className: "cal-event-team",
            attrs: { dir: "auto" },
            text: chipTitle(ev),
        }));

        const metaBits = [];
        const timeRange = jerusalemTimeRange(ev.scheduled_at, ev.duration_minutes);
        if (timeRange) metaBits.push(timeRange);
        if (ev.location) metaBits.push(ev.location);
        if (metaBits.length) {
            titleWrap.appendChild(el("span", { className: "cal-event-meta-sep", text: "·" }));
            const meta = el("span", { className: "cal-event-meta", attrs: { dir: "auto" } });
            metaBits.forEach((bit, idx) => {
                if (idx > 0) {
                    meta.appendChild(el("span", { className: "cal-event-meta-sep", text: " · " }));
                }
                // Time goes LTR; location is auto.
                const span = el("span", {
                    text: bit,
                    attrs: idx === 0 && timeRange ? { dir: "ltr" } : { dir: "auto" },
                });
                meta.appendChild(span);
            });
            titleWrap.appendChild(meta);
        }

        // Event-type pill (game / event / other — practice gets no pill since
        // it's the default).
        if (ev.event_type && ev.event_type !== "practice") {
            const pillLabel = ev.event_type === "other" && ev.event_type_custom
                ? ev.event_type_custom
                : (EVENT_TYPE_HE[ev.event_type] || ev.event_type);
            titleWrap.appendChild(el("span", { className: "cal-event-kind", text: pillLabel }));
        }

        // Phase 15.9 — status badges. Renders only when the event has the
        // corresponding data attached (attendance recorded, free note saved,
        // practice plan linked).
        if (ev.has_attendance) {
            titleWrap.appendChild(el("span", {
                className: "cal-event-badge cal-event-badge--att",
                text: "Attendance",
                attrs: { title: "Attendance recorded" },
            }));
        }
        if (ev.has_plan) {
            titleWrap.appendChild(el("span", {
                className: "cal-event-badge cal-event-badge--plan",
                text: "Plan",
                attrs: { title: "Practice plan attached" },
            }));
        }
        if (ev.has_note) {
            titleWrap.appendChild(el("span", {
                className: "cal-event-badge cal-event-badge--note",
                text: "Summary",
                attrs: { title: "Summary saved" },
            }));
        }

        summary.appendChild(titleWrap);

        // Edit pill — opens the new/edit event modal pre-filled
        const editBtn = el("button", {
            className: "cal-edit-btn",
            attrs: { type: "button", "aria-label": "Edit event" },
            text: "Edit",
            dataset: { editEventId: String(ev.id) },
        });
        summary.appendChild(editBtn);

        card.appendChild(summary);

        // ── Expanded body (action buttons + inline data) ──
        if (isExpanded) {
            const body = el("div", {
                className: "cal-event-body",
                attrs: { id: "evt-body-" + ev.id },
            });
            const inner = el("div", { className: "cal-event-body-inner" });
            const actions = el("div", { className: "cal-event-actions" });

            actions.appendChild(buildActionButton(
                "list_alt",
                ev.has_attendance ? "Edit attendance" : "Attendance",
                { action: "open-attendance", eventId: String(ev.id) },
            ));
            actions.appendChild(buildActionButton(
                "menu_book",
                ev.has_plan ? "Change plan" : "Practice plan",
                { action: "open-plan", eventId: String(ev.id) },
            ));
            actions.appendChild(buildActionButton(
                "edit_note",
                ev.has_note ? "Edit summary" : "Summary",
                { action: "open-note", eventId: String(ev.id) },
            ));

            inner.appendChild(actions);

            // Inline attendance display
            if (ev.has_attendance && Array.isArray(ev.attendance) && ev.attendance.length) {
                inner.appendChild(buildAttendanceSection(ev));
            }
            // Inline practice plan display
            if (ev.has_plan && ev.plan) {
                inner.appendChild(buildPlanSection(ev));
            }
            // Inline free-note (summary) display
            if (ev.has_note && ev.note) {
                inner.appendChild(buildNoteSection(ev));
            }

            body.appendChild(inner);
            card.appendChild(body);
        }

        return card;
    }

    // Phase 15.9 — render saved attendance/plan/summary inline in the
    // expanded card. Pure DOM (no innerHTML on user data).
    function buildAttendanceSection(ev) {
        const section = el("div", { className: "cal-event-section" });
        const head = el("div", { className: "cal-event-section-head" });
        head.appendChild(el("h4", { text: "Attendance" }));
        const summary = ev.attendance_summary || { present: 0, absent: 0, late: 0 };
        const counts = el("div", { className: "cal-event-section-counts" });
        if (summary.present) counts.appendChild(
            el("span", { className: "att-present", text: `${summary.present} present` })
        );
        if (summary.absent) counts.appendChild(
            el("span", { className: "att-absent", text: `${summary.absent} absent` })
        );
        if (summary.late) counts.appendChild(
            el("span", { className: "att-late", text: `${summary.late} late` })
        );
        head.appendChild(counts);
        section.appendChild(head);

        const list = el("div", { className: "cal-attendance-inline" });
        const statusLabels = { present: "Present", absent: "Absent", late: "Late" };
        ev.attendance.forEach(a => {
            const row = el("div", { className: "cal-attendance-inline-row" });
            row.appendChild(el("span", {
                className: "name",
                attrs: { dir: "auto" },
                text: a.name || "—",
            }));
            const status = el("span", {
                className: "status",
                attrs: { "data-status": a.status || "present" },
                text: statusLabels[a.status] || statusLabels.present,
            });
            row.appendChild(status);
            list.appendChild(row);
        });
        section.appendChild(list);
        return section;
    }

    function buildPlanSection(ev) {
        const section = el("div", { className: "cal-event-section" });
        const head = el("div", { className: "cal-event-section-head" });
        head.appendChild(el("h4", { text: "Practice plan" }));
        section.appendChild(head);
        section.appendChild(el("div", {
            className: "cal-event-inline-plan-title",
            attrs: { dir: "auto" },
            text: ev.plan.title || "Practice plan",
        }));
        if (ev.plan.preview) {
            section.appendChild(el("div", {
                className: "cal-event-inline-text",
                attrs: { dir: "auto" },
                text: ev.plan.preview,
            }));
        }
        // Link out to the full notebook entry
        const link = el("a", {
            className: "cal-event-inline-link",
            text: "Open in Notebook →",
            attrs: { href: `/notebook?entry=${ev.plan.entry_id}` },
        });
        section.appendChild(link);
        return section;
    }

    function buildNoteSection(ev) {
        const section = el("div", { className: "cal-event-section" });
        const head = el("div", { className: "cal-event-section-head" });
        head.appendChild(el("h4", { text: "Summary" }));
        section.appendChild(head);
        if (ev.note.title) {
            section.appendChild(el("div", {
                className: "cal-event-inline-plan-title",
                attrs: { dir: "auto" },
                text: ev.note.title,
            }));
        }
        section.appendChild(el("div", {
            className: "cal-event-inline-text",
            attrs: { dir: "auto" },
            text: ev.note.text || "",
        }));
        return section;
    }

    function buildActionButton(icon, label, dataset) {
        const btn = el("button", {
            className: "cal-action-btn",
            attrs: { type: "button" },
            dataset: dataset,
        });
        // Material symbol icon (font is already loaded by base.html).
        const span = document.createElement("span");
        span.className = "material-symbols-outlined";
        span.setAttribute("aria-hidden", "true");
        span.textContent = icon;
        btn.appendChild(span);
        btn.appendChild(document.createTextNode(label));
        return btn;
    }

    // ── Day expand / collapse ──────────────────────────────────
    function expandDay(dateKey, opts) {
        opts = opts || {};
        state.expandedDay = dateKey;
        // When the user clicks a SPECIFIC chip in the grid, opts.eventId is
        // set — that card auto-expands in the day panel. When clicking the
        // cell itself, no auto-expand (the user sees the list collapsed).
        state.expandedEventId = opts.eventId || null;
        render();
        if ($dayPanel && !$dayPanel.hidden && opts.scroll !== false) {
            // Scroll the panel into view; on mobile this is essential since
            // the grid is tall.
            requestAnimationFrame(() => {
                $dayPanel.scrollIntoView({ behavior: "smooth", block: "start" });
            });
        }
    }

    function collapseDay() {
        state.expandedDay = null;
        state.expandedEventId = null;
        render();
    }

    function toggleEventCard(eventId) {
        state.expandedEventId = (state.expandedEventId === eventId) ? null : eventId;
        renderDayPanel();
    }

    // ── Modal open / close ─────────────────────────────────────
    function openModal(id) {
        const modal = $modals[id];
        if (!modal) return;
        state.modalRestoreFocus = document.activeElement;
        modal.classList.add("is-open");
        modal.setAttribute("aria-hidden", "false");
        document.body.style.overflow = "hidden";
        // Focus the close button as a sensible default — Phase 15.4 will
        // override this with the first form field.
        requestAnimationFrame(() => {
            const close = modal.querySelector(".cal-modal-close");
            if (close) close.focus();
        });
    }

    function closeAllModals() {
        Object.keys($modals).forEach(id => {
            const m = $modals[id];
            if (m && m.classList.contains("is-open")) {
                m.classList.remove("is-open");
                m.setAttribute("aria-hidden", "true");
            }
        });
        document.body.style.overflow = "";
        if (state.modalRestoreFocus && typeof state.modalRestoreFocus.focus === "function") {
            state.modalRestoreFocus.focus();
        }
        state.modalRestoreFocus = null;
    }

    // Backdrop clicks close.
    Object.keys($modals).forEach(id => {
        const m = $modals[id];
        if (!m) return;
        m.addEventListener("click", e => {
            if (e.target === m) closeAllModals();
        });
    });

    // ── Event delegation ───────────────────────────────────────
    document.addEventListener("click", e => {
        // Modal close buttons (close-modal lives inside the modals).
        const closeBtn = e.target.closest('[data-action="close-modal"]');
        if (closeBtn) {
            e.preventDefault();
            closeAllModals();
            return;
        }

        // Header / monthbar actions.
        const actionEl = e.target.closest("[data-action]");
        if (actionEl) {
            const action = actionEl.dataset.action;
            if (action === "prev-month") {
                state.month -= 1;
                if (state.month < 0) { state.month = 11; state.year -= 1; }
                return fetchRange();
            }
            if (action === "next-month") {
                state.month += 1;
                if (state.month > 11) { state.month = 0; state.year += 1; }
                return fetchRange();
            }
            if (action === "today") {
                const now = new Date();
                state.year = now.getFullYear();
                state.month = now.getMonth();
                state.expandedDay = ymd(now);
                state.expandedEventId = null;
                return fetchRange();
            }
            if (action === "new-event" || action === "new-event-for-day") {
                openEventModal({ mode: "new", day: state.expandedDay });
                return;
            }
            if (action === "close-day") {
                return collapseDay();
            }
            if (action === "open-attendance") {
                const eid = actionEl.dataset.eventId
                    ? parseInt(actionEl.dataset.eventId, 10)
                    : state.expandedEventId;
                if (eid) openAttendanceModal(eid);
                return;
            }
            if (action === "open-plan") {
                const eid = actionEl.dataset.eventId
                    ? parseInt(actionEl.dataset.eventId, 10)
                    : state.expandedEventId;
                if (eid) openPlanModal(eid);
                return;
            }
            if (action === "open-note") {
                const eid = actionEl.dataset.eventId
                    ? parseInt(actionEl.dataset.eventId, 10)
                    : state.expandedEventId;
                if (eid) openNoteModal(eid);
                return;
            }
            if (action === "delete-event") {
                handleDelete();
                return;
            }
        }

        // Edit pill on an event card.
        const editEl = e.target.closest("[data-edit-event-id]");
        if (editEl) {
            e.preventDefault();
            e.stopPropagation();
            const id = parseInt(editEl.dataset.editEventId, 10);
            const ev = (state.events || []).find(x => x.id === id);
            if (ev) openEventModal({ mode: "edit", event: ev });
            return;
        }

        // Event-card toggle (the summary row of a card in the day panel).
        const toggleEl = e.target.closest("[data-event-toggle]");
        if (toggleEl) {
            const id = parseInt(toggleEl.dataset.eventToggle, 10);
            if (!isNaN(id)) toggleEventCard(id);
            return;
        }

        // Day expand from a grid chip OR the "+N נוספים" pill.
        const expandEl = e.target.closest("[data-day-expand]");
        if (expandEl) {
            e.stopPropagation();
            const key = expandEl.dataset.dayExpand;
            const evIdAttr = expandEl.dataset.eventId;
            const opts = {};
            if (evIdAttr) opts.eventId = parseInt(evIdAttr, 10);
            expandDay(key, opts);
            return;
        }

        // Grid cell click (no chip / pill intercepted).
        const cell = e.target.closest("[data-date]");
        if (cell) {
            const key = cell.dataset.date;
            const buckets = bucketByDay(state.events);
            const hasEvents = (buckets[key] || []).length > 0;
            if (hasEvents) {
                expandDay(key);
            } else {
                // Empty cell → expand day panel showing the "+ הוסף אירוע" CTA.
                expandDay(key);
            }
            return;
        }
    });

    // Keyboard activation on cells (Enter / Space) — a11y.
    $grid.addEventListener("keydown", e => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const cell = e.target.closest("[data-date]");
        if (!cell) return;
        e.preventDefault();
        cell.click();
    });

    // Keyboard activation on event-card summaries (div role="button").
    // Scoped to the day-panel body so we don't double-fire on chip clicks.
    if ($dayPanelBody) {
        $dayPanelBody.addEventListener("keydown", e => {
            if (e.key !== "Enter" && e.key !== " ") return;
            const summary = e.target.closest("[data-event-toggle]");
            if (!summary) return;
            e.preventDefault();
            summary.click();
        });
    }

    // Team filter change.
    if ($teamSelect) {
        $teamSelect.addEventListener("change", () => {
            state.teamId = $teamSelect.value || "";
            state.expandedDay = null;
            state.expandedEventId = null;
            fetchRange();
        });
    }

    // ESC closes any open modal, then the day panel.
    document.addEventListener("keydown", e => {
        if (e.key !== "Escape") return;
        const anyOpen = Object.keys($modals).some(id =>
            $modals[id] && $modals[id].classList.contains("is-open"));
        if (anyOpen) {
            closeAllModals();
        } else if (state.expandedDay) {
            collapseDay();
        }
    });

    // ── Phase 15.4 — Forms ─────────────────────────────────────
    const $evtForm = document.getElementById("evt-form");
    const $attForm = document.getElementById("attendance-form");
    const $noteForm = document.getElementById("note-form");
    const $seriesScopeModal = document.getElementById("series-scope-modal");
    let evtEditingEvent = null;
    let attendanceRoster = [];

    function setFormError(form, msg) {
        const el = form.querySelector("[data-form-error]");
        if (!el) return;
        if (msg) { el.textContent = msg; el.hidden = false; }
        else { el.textContent = ""; el.hidden = true; }
    }

    function populateTeamSelect(select, selectedId) {
        if (!select) return;
        select.replaceChildren();
        (state.teams || []).forEach(t => {
            const o = document.createElement("option");
            o.value = String(t.id);
            o.textContent = t.team_name || `Team ${t.id}`;
            if (selectedId && String(selectedId) === String(t.id)) o.selected = true;
            select.appendChild(o);
        });
    }

    function applyTypeVisibility(form) {
        const type = (form.querySelector('input[name="event_type"]:checked') || {}).value || "practice";
        form.querySelectorAll("[data-when-type]").forEach(el => {
            el.hidden = el.dataset.whenType !== type;
        });
    }

    function applyRecurrenceVisibility(form) {
        const cb = form.querySelector('input[name="recurring"]');
        const body = form.querySelector("#evt-recurrence-body");
        if (cb && body) body.hidden = !cb.checked;
    }

    function buildIsoDateTime(dateStr, timeStr) {
        if (!dateStr || !timeStr) return null;
        const d = new Date(`${dateStr}T${timeStr}:00`);
        if (isNaN(d.getTime())) return null;
        return d.toISOString();
    }

    function parseEventToFormFields(ev) {
        const iso = ev.scheduled_at.endsWith("Z") ? ev.scheduled_at : ev.scheduled_at + "Z";
        const localStr = new Date(iso).toLocaleString("en-US", { timeZone: "Asia/Jerusalem" });
        const local = new Date(localStr);
        const pad = n => String(n).padStart(2, "0");
        return {
            event_date: `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}`,
            event_time: `${pad(local.getHours())}:${pad(local.getMinutes())}`,
        };
    }

    function openEventModal({ mode, event, day }) {
        if (!$evtForm) return;
        evtEditingEvent = mode === "edit" ? event : null;
        $evtForm.reset();
        setFormError($evtForm, null);
        $evtForm.querySelector('[name="event_id"]').value = "";
        $evtForm.querySelector('[name="series_id"]').value = "";
        $evtForm.querySelector('[name="parent_event_id"]').value = "";
        document.getElementById("evt-modal-title").textContent =
            mode === "edit" ? "Edit event" : "New event";
        const delBtn = $evtForm.querySelector('[data-action="delete-event"]');
        if (delBtn) delBtn.hidden = mode !== "edit";

        const selectedTeamId = mode === "edit" ? event.team_id :
            (state.teamId ? Number(state.teamId) : (state.teams[0] && state.teams[0].id));
        populateTeamSelect($evtForm.querySelector('[name="team_id"]'), selectedTeamId);

        if (mode === "edit") {
            $evtForm.querySelector('[name="event_id"]').value = event.id;
            $evtForm.querySelector('[name="series_id"]').value = event.series_id || "";
            $evtForm.querySelector('[name="parent_event_id"]').value = event.parent_event_id || "";
            const et = event.event_type || "practice";
            const radio = $evtForm.querySelector(`input[name="event_type"][value="${et}"]`);
            if (radio) radio.checked = true;
            $evtForm.querySelector('[name="title"]').value = event.title || "";
            $evtForm.querySelector('[name="event_type_custom"]').value = event.event_type_custom || "";
            $evtForm.querySelector('[name="opponent_home"]').value = event.opponent_home || "";
            $evtForm.querySelector('[name="opponent_away"]').value = event.opponent_away || "";
            $evtForm.querySelector('[name="duration_minutes"]').value = event.duration_minutes || 90;
            $evtForm.querySelector('[name="location"]').value = event.location || "";
            const parts = parseEventToFormFields(event);
            $evtForm.querySelector('[name="event_date"]').value = parts.event_date;
            $evtForm.querySelector('[name="event_time"]').value = parts.event_time;
            const recRow = $evtForm.querySelector('[data-recurrence-section]');
            if (recRow) recRow.hidden = true;
        } else {
            const d = day ? new Date(day + "T16:00:00") : new Date();
            const pad = n => String(n).padStart(2, "0");
            $evtForm.querySelector('[name="event_date"]').value =
                `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
            $evtForm.querySelector('[name="event_time"]').value = "16:00";
            const recRow = $evtForm.querySelector('[data-recurrence-section]');
            if (recRow) recRow.hidden = false;
        }
        applyTypeVisibility($evtForm);
        applyRecurrenceVisibility($evtForm);
        openModal("evt");
    }

    function gatherEventPayload() {
        const fd = new FormData($evtForm);
        const eventType = fd.get("event_type") || "practice";
        const isoDate = buildIsoDateTime(fd.get("event_date"), fd.get("event_time"));
        if (!isoDate) return { error: "Invalid date or time." };
        const recurring = fd.get("recurring");
        const dows = Array.from($evtForm.querySelectorAll('input[name="dow"]:checked'))
            .map(c => parseInt(c.value, 10));
        const until = fd.get("recurrence_until");
        const payload = {
            team_id: parseInt(fd.get("team_id"), 10),
            event_type: eventType,
            title: fd.get("title") || null,
            scheduled_at: isoDate,
            duration_minutes: parseInt(fd.get("duration_minutes") || "90", 10),
            location: fd.get("location") || null,
        };
        if (eventType === "other") {
            const custom = (fd.get("event_type_custom") || "").trim();
            if (!custom) return { error: "'Other' requires a short custom label." };
            payload.event_type_custom = custom;
        }
        if (eventType === "game") {
            const h = (fd.get("opponent_home") || "").trim();
            const a = (fd.get("opponent_away") || "").trim();
            if (!h || !a) return { error: "Game requires both home and away team names." };
            payload.opponent_home = h;
            payload.opponent_away = a;
        }
        if (recurring) {
            if (!dows.length) return { error: "Pick at least one day for the repeat." };
            if (!until) return { error: "Pick an end date for the repeat." };
            payload.recurrence = { until_date: until, days_of_week: dows };
        }
        return { payload };
    }

    async function postJSON(method, url, body) {
        const r = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
            credentials: "same-origin",
            body: body ? JSON.stringify(body) : null,
        });
        const data = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, data };
    }

    async function handleCreateEvent(payload) {
        const r = await postJSON("POST", "/api/coach/practice", payload);
        if (!r.ok) {
            setFormError($evtForm, (r.data && r.data.detail) || "Could not create event.");
            return;
        }
        closeAllModals();
        await fetchRange();
    }

    async function handleEditEvent(payload) {
        if (!evtEditingEvent) return;
        const id = evtEditingEvent.id;
        const hasSeries = !!evtEditingEvent.series_id;
        const doPatch = async scope => {
            const r = await postJSON("PATCH", `/api/coach/practice/${id}?scope=${scope}`, payload);
            if (!r.ok) {
                setFormError($evtForm, (r.data && r.data.detail) || "Could not update event.");
                return false;
            }
            closeAllModals();
            await fetchRange();
            return true;
        };
        if (!hasSeries) { await doPatch("this"); return; }
        $seriesScopeModal.classList.add("is-open");
        $seriesScopeModal.setAttribute("aria-hidden", "false");
        $seriesScopeModal.querySelectorAll("[data-scope-choice]").forEach(btn => {
            const newBtn = btn.cloneNode(true);
            btn.parentNode.replaceChild(newBtn, btn);
            newBtn.addEventListener("click", async () => {
                const scope = newBtn.dataset.scopeChoice;
                $seriesScopeModal.classList.remove("is-open");
                $seriesScopeModal.setAttribute("aria-hidden", "true");
                await doPatch(scope);
            });
        });
    }

    async function handleDelete() {
        if (!evtEditingEvent) return;
        const id = evtEditingEvent.id;
        const hasSeries = !!evtEditingEvent.series_id;
        if (!hasSeries) {
            if (!confirm("Delete this event?")) return;
            const r = await postJSON("DELETE", `/api/coach/practice/${id}?scope=this`);
            if (r.ok) { closeAllModals(); await fetchRange(); }
            return;
        }
        const choice = prompt(
            "Delete — type:\n  this = only this event\n  series = the whole series\n  day_of_week = all events on the same weekday for this team",
            "this"
        );
        const scope = ["this", "series", "day_of_week"].includes(choice) ? choice : null;
        if (!scope) return;
        const r = await postJSON("DELETE", `/api/coach/practice/${id}?scope=${scope}`);
        if (r.ok) { closeAllModals(); await fetchRange(); }
    }

    if ($evtForm) {
        $evtForm.querySelectorAll('input[name="event_type"]').forEach(r => {
            r.addEventListener("change", () => applyTypeVisibility($evtForm));
        });
        const recCb = $evtForm.querySelector('input[name="recurring"]');
        if (recCb) recCb.addEventListener("change", () => applyRecurrenceVisibility($evtForm));
        $evtForm.addEventListener("submit", async e => {
            e.preventDefault();
            setFormError($evtForm, null);
            const { error, payload } = gatherEventPayload();
            if (error) { setFormError($evtForm, error); return; }
            if (evtEditingEvent) await handleEditEvent(payload);
            else await handleCreateEvent(payload);
        });
    }

    // ── Attendance ─────────────────────────────────────────────
    function buildAttendanceRow(player, status) {
        const row = document.createElement("div");
        row.className = "cal-att-row";
        row.dataset.playerId = String(player.id);

        const name = document.createElement("div");
        name.className = "cal-att-name";
        name.textContent = player.name || "—";
        if (player.number) {
            const num = document.createElement("span");
            num.className = "cal-att-number";
            num.textContent = `#${player.number}`;
            name.appendChild(num);
        }
        row.appendChild(name);

        const statuses = document.createElement("div");
        statuses.className = "cal-att-statuses";
        [["present", "Present"], ["absent", "Absent"], ["late", "Late"]].forEach(([s, label]) => {
            const wrap = document.createElement("label");
            wrap.className = "cal-att-status";
            const input = document.createElement("input");
            input.type = "radio";
            input.name = `att-${player.id}`;
            input.value = s;
            if (status === s) input.checked = true;
            const span = document.createElement("span");
            span.dataset.status = s;
            span.textContent = label;
            wrap.append(input, span);
            statuses.appendChild(wrap);
        });
        row.appendChild(statuses);
        return row;
    }

    async function openAttendanceModal(eventId) {
        if (!$attForm) return;
        $attForm.reset();
        setFormError($attForm, null);
        $attForm.querySelector('[name="event_id"]').value = String(eventId);

        const ev = (state.events || []).find(x => x.id === eventId);
        const summaryEl = $attForm.querySelector("[data-att-summary]");
        const listEl = $attForm.querySelector("[data-att-list]");
        listEl.replaceChildren();

        if (ev) {
            const iso = ev.scheduled_at.endsWith("Z") ? ev.scheduled_at : ev.scheduled_at + "Z";
            const dateStr = new Date(iso).toLocaleString("en-US", {
                timeZone: "Asia/Jerusalem", day: "2-digit", month: "2-digit", year: "numeric",
            });
            summaryEl.textContent = `${ev.team_name || "Team"} · ${dateStr}`;
        } else {
            summaryEl.textContent = "Loading…";
        }
        openModal("attendance");

        try {
            const teamId = ev ? ev.team_id : null;
            const r = await fetch(`/api/players?team_id=${teamId}`, {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!r.ok) throw new Error("HTTP " + r.status);
            const data = await r.json();
            attendanceRoster = Array.isArray(data) ? data : (data.players || []);
        } catch (err) {
            attendanceRoster = [];
            setFormError($attForm, "Could not load the player roster.");
            return;
        }

        if (!attendanceRoster.length) {
            const empty = document.createElement("div");
            empty.className = "cal-form-help";
            empty.textContent = "No players on this team yet. Add players first.";
            listEl.appendChild(empty);
            return;
        }

        let existing = {};
        try {
            const r = await fetch(
                `/api/notebook?team_id=${ev.team_id}&entry_type=attendance`,
                { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } }
            );
            if (r.ok) {
                const data = await r.json();
                const entry = (data.entries || data || []).find(e =>
                    e.practice_session_id === eventId);
                if (entry && entry.attendance) {
                    entry.attendance.forEach(a => { existing[a.player_id] = a.status; });
                }
            }
        } catch (_) {/* non-critical */}

        attendanceRoster.forEach(p => {
            const status = existing[p.id] || "present";
            listEl.appendChild(buildAttendanceRow(p, status));
        });
    }

    if ($attForm) {
        $attForm.addEventListener("submit", async e => {
            e.preventDefault();
            const eventId = parseInt($attForm.querySelector('[name="event_id"]').value, 10);
            if (!eventId) return;
            const players = attendanceRoster.map(p => {
                const checked = $attForm.querySelector(`input[name="att-${p.id}"]:checked`);
                return { player_id: p.id, status: checked ? checked.value : "present" };
            });
            const r = await postJSON("POST", `/api/coach/practice/${eventId}/attendance`, { players });
            if (!r.ok) {
                setFormError($attForm, (r.data && r.data.detail) || "Could not save attendance.");
                return;
            }
            closeAllModals();
            await fetchRange();
        });
    }

    // ── Free note ──────────────────────────────────────────────
    function openNoteModal(eventId) {
        if (!$noteForm) return;
        $noteForm.reset();
        setFormError($noteForm, null);
        $noteForm.querySelector('[name="event_id"]').value = String(eventId);
        const ev = (state.events || []).find(x => x.id === eventId);
        if (ev && ev.title) {
            $noteForm.querySelector('[name="title"]').value = `Summary: ${ev.title}`;
        }
        openModal("note");
    }

    if ($noteForm) {
        $noteForm.addEventListener("submit", async e => {
            e.preventDefault();
            setFormError($noteForm, null);
            const eventId = parseInt($noteForm.querySelector('[name="event_id"]').value, 10);
            const fd = new FormData($noteForm);
            const content = (fd.get("content") || "").trim();
            if (!content) { setFormError($noteForm, "Write something before saving."); return; }
            const r = await postJSON("POST", `/api/coach/practice/${eventId}/note`, {
                title: fd.get("title") || null, content,
            });
            if (!r.ok) {
                setFormError($noteForm, (r.data && r.data.detail) || "Could not save the summary.");
                return;
            }
            closeAllModals();
            await fetchRange();
        });
    }

    // ── Practice-plan picker ───────────────────────────────────
    // Lists existing NotebookEntry rows with entry_type='practice_plan'
    // for this coach. The coach picks one → POST /attach-plan saves the
    // FK on the practice_session. Detach button removes the link.
    const $planForm = document.getElementById("plan-form");
    let planSelectedId = null;
    let planEventId = null;
    let planCurrentEntryId = null;

    async function openPlanModal(eventId) {
        if (!$planForm) return;
        planEventId = eventId;
        planSelectedId = null;
        const ev = (state.events || []).find(x => x.id === eventId);
        planCurrentEntryId = ev && ev.plan ? ev.plan.entry_id : null;
        $planForm.querySelector('[name="event_id"]').value = String(eventId);
        setFormError($planForm, null);

        const listEl = $planForm.querySelector("[data-plan-list]");
        const attachBtn = $planForm.querySelector("[data-plan-attach-btn]");
        const detachBtn = $planForm.querySelector("[data-plan-detach]");
        listEl.replaceChildren();
        const loading = document.createElement("div");
        loading.className = "cal-form-help";
        loading.textContent = "Loading…";
        listEl.appendChild(loading);
        attachBtn.disabled = true;
        detachBtn.hidden = !planCurrentEntryId;

        openModal("plan");

        // Fetch existing practice_plan entries.
        let entries = [];
        try {
            const r = await fetch("/api/notebook?type=practice_plan&limit=100", {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (r.ok) {
                const data = await r.json();
                entries = data.data || data.entries || [];
            }
        } catch (_) { /* ignored — empty list rendered */ }

        listEl.replaceChildren();
        if (!entries.length) {
            const empty = document.createElement("div");
            empty.className = "cal-form-help";
            empty.textContent =
                "No practice plans yet. Open the Notebook or chat with the " +
                "Training agent to create one, then come back here to attach it.";
            listEl.appendChild(empty);
            return;
        }
        entries.forEach(entry => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "cal-plan-item";
            item.dataset.entryId = String(entry.id);
            if (entry.id === planCurrentEntryId) {
                item.classList.add("is-selected");
                planSelectedId = entry.id;
                attachBtn.disabled = false;
            }
            const t = document.createElement("div");
            t.className = "cal-plan-item-title";
            t.textContent = entry.title || "Untitled plan";
            if (entry.id === planCurrentEntryId) {
                const cur = document.createElement("span");
                cur.className = "cal-plan-item-current";
                cur.textContent = "Current";
                t.appendChild(cur);
            }
            item.appendChild(t);
            const meta = document.createElement("div");
            meta.className = "cal-plan-item-meta";
            meta.textContent = entry.entry_date || "";
            item.appendChild(meta);
            item.addEventListener("click", () => {
                listEl.querySelectorAll(".cal-plan-item").forEach(el => el.classList.remove("is-selected"));
                item.classList.add("is-selected");
                planSelectedId = entry.id;
                attachBtn.disabled = false;
            });
            listEl.appendChild(item);
        });
    }

    if ($planForm) {
        $planForm.addEventListener("submit", async e => {
            e.preventDefault();
            if (!planEventId || !planSelectedId) return;
            const r = await postJSON(
                "POST",
                `/api/coach/practice/${planEventId}/attach-plan`,
                { entry_id: planSelectedId },
            );
            if (!r.ok) {
                setFormError($planForm, (r.data && r.data.detail) || "Could not attach the plan.");
                return;
            }
            closeAllModals();
            await fetchRange();
        });
        $planForm.querySelector("[data-plan-detach]").addEventListener("click", async () => {
            if (!planEventId) return;
            const r = await postJSON(
                "POST",
                `/api/coach/practice/${planEventId}/attach-plan`,
                { entry_id: null },
            );
            if (!r.ok) {
                setFormError($planForm, (r.data && r.data.detail) || "Could not detach the plan.");
                return;
            }
            closeAllModals();
            await fetchRange();
        });
    }

    // ── Boot ───────────────────────────────────────────────────
    fetchRange();
    window.NPCoachCalendar = {
        state: state,
        refresh: fetchRange,
        openModal: openModal,
        openEventModal: openEventModal,
        openAttendanceModal: openAttendanceModal,
        openPlanModal: openPlanModal,
        openNoteModal: openNoteModal,
        closeAllModals: closeAllModals,
    };
})();
