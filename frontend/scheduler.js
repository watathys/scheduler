// ── BYU Schedule Builder — vanilla JS frontend ───────────────────────────────
// Talks to the existing /api/v1/scheduler endpoints. Never renders API strings
// with innerHTML: all dynamic content goes through textContent (or is built via
// createElement), so course titles, instructor names, and explanations are
// treated as untrusted data.

"use strict";

// ── Constants ────────────────────────────────────────────────────────────────
const PREF_OPTIONS = [
  { id: "easy", label: "Easy classes", desc: "Favor professors with higher ease ratings." },
  { id: "no_gaps", label: "No gaps", desc: "Avoid idle time between classes on the same day." },
  { id: "fewer_days", label: "Fewer days on campus", desc: "Pack classes into fewer days, e.g. Mon–Wed instead of Mon–Fri." },
];
const DEFAULT_PREFS = ["easy", "no_gaps", "fewer_days"];
const DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const DAY_SHORT = { Monday: "Mon", Tuesday: "Tue", Wednesday: "Wed", Thursday: "Thu", Friday: "Fri", Saturday: "Sat", Sunday: "Sun" };
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const MAX_COURSES = 8;
const LOW_CONFIDENCE_EASE = 0.5;
const EARLIEST_HOUR = 8;   // 8:00 AM
const LATEST_HOUR = 22;    // 10:00 PM
const TIME_STEP = 30;

// A palette of distinct, light background colors (dark text keeps contrast in
// both themes). Color is never the only way to tell courses apart — every
// block also shows the course code as text.
const PALETTE = [
  "#cfe6f7", "#d3efdc", "#f3e7bd", "#f6d5d5", "#e3d8f3",
  "#c9eaee", "#f6ddc6", "#d6dbf4", "#f4d9e8", "#e1efc4",
];

// Plain-English explanation of the ease score. Weights mirror backend/rmp.py
// ``ease_score``: difficulty 0.5, rating 0.3, would-take-again 0.2.
const EASE_FORMULA = "Ease is a 0–100 score. 50% comes from difficulty (easier = higher), " +
  "30% from the overall rating, and 20% from the share of students who would take the " +
  "professor again. Missing data is dropped and the rest re-weighted; professors with " +
  "fewer than 3 ratings are held at a neutral 50 with low confidence.";

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  step: 0,
  maxReached: 0,
  courses: [],            // [{code, title, credits, section_count, instructor, professors, hasOnline}]
  envelopes: [],          // [{slug, title, courses, ok_bundles}]
  preferences: DEFAULT_PREFS.slice(),  // rank 1..3
  daysOff: ["Friday"],
  possibleDaysOff: WEEKDAYS.slice(),  // weekdays that can actually be taken off
  earliestStart: "08:00",
  latestEnd: "22:00",
  allowOnline: false,
  allowSaltLake: false,
  extraText: "",
  result: null,           // last GenerateResponse (status "ok")
  noSchedule: null,       // last no_schedule response
  selectedOption: 0,
  submitting: false,
  shownScheduleIds: [],   // section-id sets already shown (for "generate more")
  hasMore: false,         // whether more distinct schedules remain
};

// ── DOM helpers ───────────────────────────────────────────────────────────────
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function normCode(raw) {
  return String(raw || "").replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
}

// Title-case an instructor's display name. BYU returns names already lowercased
// ("seth mccombie"), so this mirrors backend/matching.py ``title_case_name``:
// each whitespace-, hyphen-, or apostrophe-separated part is capitalized, with
// a small Mc/Mac surname fix-up ("mccombie" -> "McCombie").
function titleCaseName(raw) {
  if (!raw) return "";
  const titleWord = (word) => {
    if (!word) return word;
    const w = word.toLowerCase();
    if (w.startsWith("mc") && w.length > 2) return "Mc" + w.charAt(2).toUpperCase() + w.slice(3);
    if (w.startsWith("mac") && w.length > 4) return "Mac" + w.charAt(3).toUpperCase() + w.slice(4);
    return w.charAt(0).toUpperCase() + w.slice(1);
  };
  return String(raw).trim().split(/\s+/).map((token) =>
    token.split(/([-'])/).map((part) =>
      (part === "-" || part === "'") ? part : titleWord(part)
    ).join("")
  ).join(" ");
}

function announce(msg) {
  const region = document.getElementById("live-region");
  if (region) region.textContent = msg;
}

function $id(id) { return document.getElementById(id); }

// ── Theme ─────────────────────────────────────────────────────────────────────
function initTheme() {
  let theme = "light";
  try { theme = localStorage.getItem("theme") || "light"; } catch (e) {}
  applyTheme(theme);
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = $id("btn-theme");
  if (btn) btn.textContent = theme === "dark" ? "Light" : "Dark";
  try { localStorage.setItem("theme", theme); } catch (e) {}
}
function toggleTheme() {
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
}

// ── Time formatting ───────────────────────────────────────────────────────────
function fmtMin(min) {
  if (min === null || min === undefined) return "—";
  const h = Math.floor(min / 60);
  const m = min % 60;
  const ampm = h < 12 ? "AM" : "PM";
  const hh = h % 12 === 0 ? 12 : h % 12;
  return `${hh}:${String(m).padStart(2, "0")} ${ampm}`;
}
function fmtRange(start, end) { return `${fmtMin(start)}–${fmtMin(end)}`; }
function fmtDuration(min) {
  const m = Math.round(min || 0);
  if (m <= 0) return "0 min";
  const h = Math.floor(m / 60);
  const mm = m % 60;
  const parts = [];
  if (h) parts.push(`${h}h`);
  if (mm) parts.push(`${mm}m`);
  return parts.join(" ");
}
function timeValueToLabel(hhmm) { return fmtMin(timeToMin(hhmm)); }
function timeToMin(hhmm) {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
}

function easeLabel(score) {
  if (score >= 85) return "Very easy";
  if (score >= 70) return "Easy";
  if (score >= 55) return "Moderate";
  if (score >= 40) return "Challenging";
  return "Difficult";
}

// ── Stepper navigation ────────────────────────────────────────────────────────
function showStep(index) {
  state.step = index;
  if (index > state.maxReached) state.maxReached = index;
  document.querySelectorAll(".step-panel").forEach((panel) => {
    panel.hidden = String(panel.dataset.step) !== String(index);
  });
  document.querySelectorAll(".step-dot").forEach((dot) => {
    const g = parseInt(dot.dataset.goto, 10);
    dot.classList.toggle("is-active", g === index);
    dot.classList.toggle("is-done", g < state.maxReached);
    if (g === index) dot.setAttribute("aria-current", "step");
    else dot.removeAttribute("aria-current");
  });
  const back = $id("btn-back");
  back.hidden = index === 0;
  back.disabled = index === 0;
  const next = $id("btn-next");
  next.textContent = index === 2 ? "Build my schedules" : "Next";
  hideFormError();
  renderDynamicStep(index);
}

function renderDynamicStep(index) {
  if (index === 0) renderCourseList();
  if (index === 1) renderPreferences();
  if (index === 2) renderQuestions();
}

function goNext() {
  hideFormError();
  if (state.step === 0 && !validateCourses()) return;
  if (state.step === 2) {
    if (!validateQuestions()) return;
    submit();
    return;
  }
  if (state.step < 2) showStep(state.step + 1);
}

function goBack() {
  hideFormError();
  if (state.step > 0) showStep(state.step - 1);
}

function showFormError(msg) {
  const box = $id("form-error");
  box.textContent = msg;
  box.hidden = false;
  box.focus();
}
function hideFormError() {
  const box = $id("form-error");
  box.hidden = true;
  box.textContent = "";
}

// ── Step 1: courses ───────────────────────────────────────────────────────────
const courseInput = $id("course-input");
const suggestionsBox = $id("course-suggestions");
let debounceTimer = null;
let currentSuggestions = [];
let activeSuggestion = -1;
let searchSeq = 0;
let daysOffSeq = 0;

courseInput.addEventListener("input", onCourseInput);
courseInput.addEventListener("keydown", onCourseKeydown);
courseInput.addEventListener("blur", () => {
  // Give click handlers a beat to fire before clearing.
  setTimeout(() => { if (document.activeElement !== suggestionsBox) clearSuggestions(); }, 150);
});

function onCourseInput() {
  const q = courseInput.value.trim();
  if (q.length < 2) { clearSuggestions(); return; }
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => fetchSuggestions(q), 250);
}

async function fetchSuggestions(q) {
  const seq = ++searchSeq;
  try {
    const res = await fetch(`/api/v1/scheduler/courses/search?q=${encodeURIComponent(q)}&limit=8`);
    if (seq !== searchSeq) return;
    if (!res.ok) { clearSuggestions(); return; }
    const data = await res.json();
    currentSuggestions = data || [];
    activeSuggestion = currentSuggestions.length ? 0 : -1;
    renderSuggestions();
  } catch (e) {
    currentSuggestions = [];
    renderSuggestions();
  }
}

function renderSuggestions() {
  suggestionsBox.innerHTML = "";
  if (currentSuggestions.length === 0) {
    if (courseInput.value.trim().length >= 2) {
      suggestionsBox.appendChild(el("li", "suggestion-empty", "No matching courses."));
      suggestionsBox.hidden = false;
    } else {
      suggestionsBox.hidden = true;
    }
    courseInput.setAttribute("aria-expanded", "false");
    return;
  }
  suggestionsBox.hidden = false;
  courseInput.setAttribute("aria-expanded", "true");
  currentSuggestions.forEach((item, i) => {
    const li = el("li", "suggestion" + (i === activeSuggestion ? " is-active" : ""));
    li.setAttribute("role", "option");
    li.setAttribute("id", `course-sug-${i}`);
    li.setAttribute("aria-selected", i === activeSuggestion ? "true" : "false");
    li.appendChild(el("span", "sug-code", item.code));
    if (item.title) li.appendChild(el("span", "sug-title", item.title));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); addCourse(item); });
    suggestionsBox.appendChild(li);
  });
  courseInput.setAttribute("aria-activedescendant", `course-sug-${activeSuggestion}`);
}

function clearSuggestions() {
  suggestionsBox.innerHTML = "";
  suggestionsBox.hidden = true;
  currentSuggestions = [];
  activeSuggestion = -1;
  courseInput.setAttribute("aria-expanded", "false");
  courseInput.removeAttribute("aria-activedescendant");
}

function onCourseKeydown(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    if (currentSuggestions.length > 0 && activeSuggestion >= 0) {
      addCourse(currentSuggestions[activeSuggestion]);
    }
    return;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (currentSuggestions.length) {
      activeSuggestion = (activeSuggestion + 1) % currentSuggestions.length;
      renderSuggestions();
    }
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    if (currentSuggestions.length) {
      activeSuggestion = (activeSuggestion - 1 + currentSuggestions.length) % currentSuggestions.length;
      renderSuggestions();
    }
  } else if (e.key === "Escape") {
    clearSuggestions();
  }
}

function addCourse(item) {
  if (!item || !item.code) return;
  if (state.courses.length >= MAX_COURSES) {
    announce(`You can add up to ${MAX_COURSES} courses.`);
    return;
  }
  const key = normCode(item.code);
  if (state.courses.some((c) => normCode(c.code) === key)) {
    announce("That course is already added.");
    return;
  }
  const course = {
    code: item.code,
    title: item.title || null,
    credits: item.credits ?? null,
    section_count: item.section_count ?? 0,
    instructor: null,       // selected key: professor id ("123"), "online", or null
    professors: [],         // [{instructor_id, name}] fetched from the API
    hasOnline: false,
  };
  state.courses.push(course);
  renderCourseList();
  clearSuggestions();
  courseInput.value = "";
  courseInput.focus();
  refreshDaysOff();
  fetchCourseInstructors(course);
}

function removeCourse(index) {
  state.courses.splice(index, 1);
  renderCourseList();
  refreshDaysOff();
}

async function fetchCourseInstructors(course) {
  try {
    const res = await fetch(`/api/v1/scheduler/courses/instructors?course=${encodeURIComponent(course.code)}`);
    if (!res.ok) return;
    const data = await res.json();
    course.professors = data.professors || [];
    course.hasOnline = !!data.has_online;
  } catch (e) {
    course.professors = [];
    course.hasOnline = false;
  }
  renderCourseList();
}

function renderCourseList() {
  const wrap = $id("course-list");
  wrap.innerHTML = "";
  state.courses.forEach((c, i) => {
    const row = el("li", "course-row");
    row.style.setProperty("--course-color", PALETTE[i % PALETTE.length]);

    // Left: color swatch + code + title + remove button.
    const main = el("div", "course-row-main");
    main.appendChild(el("span", "course-swatch"));
    main.appendChild(el("span", "course-code", c.code));
    if (c.title) main.appendChild(el("span", "course-title", c.title));
    const btn = el("button", "course-remove", "×");
    btn.type = "button";
    btn.setAttribute("aria-label", `Remove ${c.code}`);
    btn.addEventListener("click", () => removeCourse(i));
    main.appendChild(btn);
    row.appendChild(main);

    // Right: professor dropdown (Any instructor / each professor / Online).
    const prof = el("div", "course-prof");
    const label = el("label", "sr-only", `Instructor for ${c.code}`);
    label.htmlFor = `course-prof-${i}`;
    const select = document.createElement("select");
    select.id = `course-prof-${i}`;
    select.className = "course-prof-select";
    select.add(new Option("Any instructor", ""));
    (c.professors || []).forEach((p) => {
      select.add(new Option(titleCaseName(p.name) || "Staff", String(p.instructor_id)));
    });
    if (c.hasOnline) select.add(new Option("Online / no set time", "online"));
    select.value = c.instructor || "";
    select.addEventListener("change", () => {
      c.instructor = select.value || null;
      const chosen = c.instructor === "online"
        ? "Online / no set time" : (select.selectedOptions[0] ? select.selectedOptions[0].text : "Any instructor");
      announce(c.instructor ? `Only ${chosen} for ${c.code}.` : `Any instructor for ${c.code}.`);
    });
    prof.appendChild(label);
    prof.appendChild(select);
    row.appendChild(prof);

    wrap.appendChild(row);
  });
  $id("course-count").textContent = `${state.courses.length} of ${MAX_COURSES} courses added`;
}

function validateCourses() {
  if (state.courses.length === 0 && state.envelopes.length === 0) {
    showFormError("Please add at least one course or envelope.");
    return false;
  }
  return true;
}

// ── Step 1: envelopes ────────────────────────────────────────────────────────
const envelopeInput = $id("envelope-input");
const envelopeSuggestionsBox = $id("envelope-suggestions");
const envelopeToggle = $id("envelope-toggle");
const envelopePanel = $id("envelope-panel");
let allEnvelopes = [];         // fetched once from /envelopes
let activeEnvelopeSuggestion = -1;
let envelopeLoaded = false;

envelopeInput.addEventListener("input", onEnvelopeInput);
envelopeInput.addEventListener("keydown", onEnvelopeKeydown);
envelopeInput.addEventListener("blur", () => {
  setTimeout(() => { if (document.activeElement !== envelopeSuggestionsBox) clearEnvelopeSuggestions(); }, 150);
});

function setEnvelopePickerOpen(open) {
  envelopeToggle.setAttribute("aria-expanded", String(open));
  envelopePanel.hidden = !open;
  if (open) {
    loadEnvelopes();
    envelopeInput.focus();
  } else {
    clearEnvelopeSuggestions();
  }
}

envelopeToggle.addEventListener("click", () => setEnvelopePickerOpen(envelopePanel.hidden));

async function loadEnvelopes() {
  if (envelopeLoaded) return;
  envelopeLoaded = true;
  try {
    const res = await fetch("/api/v1/scheduler/envelopes");
    if (!res.ok) return;
    const data = await res.json();
    // Only offer envelopes that actually have verified-ok bundles.
    allEnvelopes = (data || []).filter((e) => e.ok_bundles > 0);
  } catch (e) {
    allEnvelopes = [];
  }
}

function onEnvelopeInput() {
  const q = envelopeInput.value.trim();
  if (q.length < 1) { clearEnvelopeSuggestions(); return; }
  const ql = q.toLowerCase();
  const matches = allEnvelopes
    .filter((e) => e.title.toLowerCase().includes(ql)
      || (e.courses || []).some((c) => c.toLowerCase().includes(ql)))
    .slice(0, 8);
  activeEnvelopeSuggestion = matches.length ? 0 : -1;
  renderEnvelopeSuggestions(matches);
}

function renderEnvelopeSuggestions(items) {
  envelopeSuggestionsBox.innerHTML = "";
  if (!items || items.length === 0) {
    envelopeSuggestionsBox.hidden = true;
    envelopeInput.setAttribute("aria-expanded", "false");
    envelopeInput.removeAttribute("aria-activedescendant");
    return;
  }
  envelopeSuggestionsBox.hidden = false;
  envelopeInput.setAttribute("aria-expanded", "true");
  items.forEach((item, i) => {
    const li = el("li", "suggestion" + (i === activeEnvelopeSuggestion ? " is-active" : ""));
    li.setAttribute("role", "option");
    li.setAttribute("id", `envelope-sug-${i}`);
    li.setAttribute("aria-selected", i === activeEnvelopeSuggestion ? "true" : "false");
    li.appendChild(el("span", "sug-code", item.title));
    if (item.courses && item.courses.length) {
      li.appendChild(el("span", "sug-title", item.courses.join(", ")));
    }
    li.addEventListener("mousedown", (e) => { e.preventDefault(); addEnvelope(item); });
    envelopeSuggestionsBox.appendChild(li);
  });
  envelopeInput.setAttribute("aria-activedescendant", `envelope-sug-${activeEnvelopeSuggestion}`);
}

function clearEnvelopeSuggestions() {
  envelopeSuggestionsBox.innerHTML = "";
  envelopeSuggestionsBox.hidden = true;
  activeEnvelopeSuggestion = -1;
  envelopeInput.setAttribute("aria-expanded", "false");
  envelopeInput.removeAttribute("aria-activedescendant");
}

function onEnvelopeKeydown(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    const visible = allEnvelopes.filter((env) => {
      const q = envelopeInput.value.trim().toLowerCase();
      return !q || env.title.toLowerCase().includes(q)
        || (env.courses || []).some((c) => c.toLowerCase().includes(q));
    }).slice(0, 8);
    if (visible.length && activeEnvelopeSuggestion >= 0) addEnvelope(visible[activeEnvelopeSuggestion]);
    return;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    const items = envelopeSuggestionsBox.querySelectorAll(".suggestion");
    if (items.length) {
      activeEnvelopeSuggestion = (activeEnvelopeSuggestion + 1) % items.length;
      renderEnvelopeSuggestions(visibleEnvelopesForInput());
    }
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    const items = envelopeSuggestionsBox.querySelectorAll(".suggestion");
    if (items.length) {
      activeEnvelopeSuggestion = (activeEnvelopeSuggestion - 1 + items.length) % items.length;
      renderEnvelopeSuggestions(visibleEnvelopesForInput());
    }
  } else if (e.key === "Escape") {
    clearEnvelopeSuggestions();
  }
}

function visibleEnvelopesForInput() {
  const q = envelopeInput.value.trim().toLowerCase();
  return allEnvelopes
    .filter((e) => !q || e.title.toLowerCase().includes(q)
      || (e.courses || []).some((c) => c.toLowerCase().includes(q)))
    .slice(0, 8);
}

function addEnvelope(item) {
  if (!item || !item.slug) return;
  if (state.envelopes.some((e) => e.slug === item.slug)) {
    announce("That envelope is already added.");
    return;
  }
  state.envelopes.push({
    slug: item.slug,
    title: item.title,
    courses: (item.courses || []).slice(),
    ok_bundles: item.ok_bundles || 0,
  });
  renderEnvelopeChips();
  clearEnvelopeSuggestions();
  envelopeInput.value = "";
  envelopeInput.focus();
  refreshDaysOff();
}

function removeEnvelope(index) {
  state.envelopes.splice(index, 1);
  renderEnvelopeChips();
  refreshDaysOff();
}

function renderEnvelopeChips() {
  const wrap = $id("envelope-chips");
  wrap.innerHTML = "";
  state.envelopes.forEach((env, i) => {
    const chip = el("div", "envelope-chip");
    const body = el("div", "envelope-chip-body");
    body.hidden = true;
    if (env.courses && env.courses.length) {
      body.appendChild(el("div", "envelope-chip-courses", env.courses.join(" · ")));
    }

    const head = el("button", "envelope-chip-toggle");
    head.type = "button";
    head.setAttribute("aria-expanded", "false");
    head.appendChild(el("span", "chip-code", env.title));
    head.appendChild(el("span", "envelope-chip-caret", "▾"));
    head.addEventListener("click", () => {
      const open = body.hidden;
      body.hidden = !open;
      head.setAttribute("aria-expanded", String(open));
      chip.classList.toggle("is-open", open);
    });

    const remove = el("button", "chip-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${env.title}`);
    remove.addEventListener("click", () => removeEnvelope(i));

    chip.appendChild(head);
    chip.appendChild(remove);
    chip.appendChild(body);
    wrap.appendChild(chip);
  });
}

// ── Step 2: preferences ───────────────────────────────────────────────────────
function renderPreferences() {
  const list = $id("pref-list");
  list.innerHTML = "";
  state.preferences.forEach((pid, i) => {
    const opt = PREF_OPTIONS.find((o) => o.id === pid);
    const item = el("li", "pref-item");
    item.setAttribute("draggable", "true");
    item.dataset.index = String(i);

    item.appendChild(el("span", "pref-rank", String(i + 1)));
    item.appendChild(el("span", "pref-grip", "⠿"));
    const text = el("div", "pref-text");
    text.appendChild(el("span", "pref-label", opt ? opt.label : pid));
    if (opt && opt.desc) text.appendChild(el("span", "pref-desc", opt.desc));
    item.appendChild(text);

    const btns = el("div", "pref-btns");
    const up = el("button", "pref-btn", "▲");
    up.type = "button";
    up.setAttribute("aria-label", `Move ${opt.label} up`);
    up.disabled = i === 0;
    up.addEventListener("click", () => movePref(i, -1));
    const down = el("button", "pref-btn", "▼");
    down.type = "button";
    down.setAttribute("aria-label", `Move ${opt.label} down`);
    down.disabled = i === state.preferences.length - 1;
    down.addEventListener("click", () => movePref(i, 1));
    btns.appendChild(up);
    btns.appendChild(down);
    item.appendChild(btns);

    // Drag & drop
    item.addEventListener("dragstart", (e) => {
      item.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(i));
    });
    item.addEventListener("dragend", () => item.classList.remove("dragging"));
    item.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      item.classList.add("drag-over");
    });
    item.addEventListener("dragleave", () => item.classList.remove("drag-over"));
    item.addEventListener("drop", (e) => {
      e.preventDefault();
      item.classList.remove("drag-over");
      const from = parseInt(e.dataTransfer.getData("text/plain"), 10);
      const to = parseInt(item.dataset.index, 10);
      if (!Number.isNaN(from) && from !== to) reorderPref(from, to);
    });

    list.appendChild(item);
  });
  renderDaysOff();
}

function movePref(from, delta) {
  const to = from + delta;
  if (to < 0 || to >= state.preferences.length) return;
  reorderPref(from, to);
}

function reorderPref(from, to) {
  const arr = state.preferences.slice();
  const [moved] = arr.splice(from, 1);
  arr.splice(to, 0, moved);
  state.preferences = arr;
  renderPreferences();
}

function renderDaysOff() {
  const wrap = $id("days-off-wrap");
  wrap.hidden = false;
  const opts = $id("weekday-options");
  opts.innerHTML = "";
  const possible = new Set(state.possibleDaysOff);
  // Drop any previously-selected day that is no longer possible to take off.
  state.daysOff = state.daysOff.filter((d) => possible.has(d));
  WEEKDAYS.forEach((day) => {
    const available = possible.has(day);
    const label = el("label");
    if (!available) {
      label.classList.add("is-disabled");
      label.title = `${day} can't be taken off with these courses.`;
    }
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = day;
    cb.disabled = !available;
    cb.checked = available && state.daysOff.includes(day);
    cb.addEventListener("change", () => {
      if (cb.checked) {
        if (!state.daysOff.includes(day)) state.daysOff.push(day);
      } else {
        state.daysOff = state.daysOff.filter((d) => d !== day);
      }
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(day));
    opts.appendChild(label);
  });
}

async function refreshDaysOff() {
  const codeSet = new Set();
  state.courses.forEach((c) => codeSet.add(c.code));
  state.envelopes.forEach((e) => (e.courses || []).forEach((c) => codeSet.add(c)));
  const codes = Array.from(codeSet);
  const seq = ++daysOffSeq;
  if (codes.length === 0) {
    state.possibleDaysOff = WEEKDAYS.slice();
    renderDaysOff();
    return;
  }
  const params = new URLSearchParams();
  codes.forEach((c) => params.append("courses", c));
  try {
    const res = await fetch(`/api/v1/scheduler/courses/days-off?${params.toString()}`);
    if (seq !== daysOffSeq) return;
    if (!res.ok) {
      state.possibleDaysOff = WEEKDAYS.slice();
      renderDaysOff();
      return;
    }
    const data = await res.json();
    const possible = Array.isArray(data.possible_days_off)
      ? data.possible_days_off.filter((d) => WEEKDAYS.includes(d))
      : WEEKDAYS.slice();
    state.possibleDaysOff = possible.length ? possible : WEEKDAYS.slice();
    renderDaysOff();
  } catch (e) {
    if (seq !== daysOffSeq) return;
    state.possibleDaysOff = WEEKDAYS.slice();
    renderDaysOff();
  }
}

// ── Step 3: questions ────────────────────────────────────────────────────────
function populateTimeDropdowns() {
  const startSel = $id("earliest-start");
  const endSel = $id("latest-end");
  startSel.innerHTML = "";
  endSel.innerHTML = "";
  for (let min = EARLIEST_HOUR * 60; min <= LATEST_HOUR * 60; min += TIME_STEP) {
    const hh = String(Math.floor(min / 60)).padStart(2, "0");
    const mm = String(min % 60).padStart(2, "0");
    const value = `${hh}:${mm}`;
    const label = fmtMin(min);
    startSel.add(new Option(label, value));
    endSel.add(new Option(label, value));
  }
  startSel.value = state.earliestStart;
  endSel.value = state.latestEnd;
}

function renderQuestions() {
  $id("earliest-start").value = state.earliestStart;
  $id("latest-end").value = state.latestEnd;
  $id("allow-online").checked = state.allowOnline;
  $id("allow-salt-lake").checked = state.allowSaltLake;
  $id("extra-text").value = state.extraText;
}

function validateQuestions() {
  const earliest = timeToMin($id("earliest-start").value);
  const latest = timeToMin($id("latest-end").value);
  if (earliest > latest) {
    showFormError("Your latest end time must be on or after your earliest start time.");
    return false;
  }
  return true;
}

function captureQuestions() {
  state.earliestStart = $id("earliest-start").value;
  state.latestEnd = $id("latest-end").value;
  state.allowOnline = $id("allow-online").checked;
  state.allowSaltLake = $id("allow-salt-lake").checked;
  state.extraText = $id("extra-text").value;
}

function totalCredits() {
  let total = 0;
  let complete = true;
  for (const c of state.courses) {
    if (c.credits == null) { complete = false; continue; }
    total += c.credits;
  }
  return { total: Math.round(total * 100) / 100, complete };
}

// ── Submit ────────────────────────────────────────────────────────────────────
function buildPayload() {
  const payload = {
    courses: state.courses.map((c) => c.code),
    preferences: state.preferences,
    days_off: state.daysOff,
    earliest_start: state.earliestStart,
    latest_end: state.latestEnd,
    allow_online: state.allowOnline,
    allow_salt_lake: state.allowSaltLake,
  };
  const instructors = {};
  state.courses.forEach((c) => {
    if (c.instructor) instructors[c.code] = [c.instructor];
  });
  if (Object.keys(instructors).length) payload.instructors = instructors;
  if (state.envelopes.length) payload.envelopes = state.envelopes.map((e) => e.slug);
  if (state.extraText.trim()) payload.extra_constraints_text = state.extraText.trim();
  return payload;
}

async function submit() {
  if (state.submitting) return;
  captureQuestions();
  state.submitting = true;
  setSubmitLoading(true);

  const payload = buildPayload();

  try {
    const res = await fetch("/api/v1/scheduler/schedules/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (res.status === 429) {
      showFormError("You're going a little fast — give it a moment, then try again.");
      return;
    }
    if (res.status === 503) {
      showFormError("Course data is still loading. Please try again in a few minutes.");
      return;
    }
    if (res.status === 422) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      handle422(body);
      return;
    }
    if (!res.ok) {
      showFormError("Something went wrong while building your schedules. Please try again.");
      return;
    }

    const data = await res.json();
    if (data.status === "no_schedule") {
      state.noSchedule = data;
      renderNoSchedule(data);
    } else {
      state.result = data;
      state.selectedOption = 0;
      state.shownScheduleIds = (data.schedules || []).map(scheduleSectionIds);
      state.hasMore = !!data.has_more;
      renderResults(data);
    }
  } catch (e) {
    showNetworkError();
  } finally {
    state.submitting = false;
    setSubmitLoading(false);
  }
}

function setSubmitLoading(loading) {
  const btn = $id("btn-next");
  btn.disabled = loading;
  btn.textContent = loading ? "Building your schedules…" : "Build my schedules";
  if (loading) announce("Building your schedules…");
}

function showNetworkError() {
  const box = $id("form-error");
  box.innerHTML = "";
  box.appendChild(document.createTextNode("Couldn't reach the server. "));
  const retry = el("button", "suggestion-pill", "Retry");
  retry.type = "button";
  retry.addEventListener("click", () => { hideFormError(); submit(); });
  box.appendChild(retry);
  box.hidden = false;
  box.focus();
}

function handle422(body) {
  const detail = body && body.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && Array.isArray(detail.suggestions) && detail.suggestions.length) {
    renderUnknownCourse(detail);
    return;
  }
  showFormError("Some answers need adjusting — check your courses, preferences, and time window.");
}

function renderUnknownCourse(detail) {
  const box = $id("form-error");
  box.innerHTML = "";
  const courseLabel = detail.course || "that course";
  box.appendChild(el("strong", null, `We couldn't find “${courseLabel}”. `));
  box.appendChild(document.createTextNode("Did you mean: "));
  detail.suggestions.forEach((sug) => {
    const pill = el("button", "suggestion-pill", sug);
    pill.type = "button";
    pill.addEventListener("click", () => swapCourse(detail.course, sug));
    box.appendChild(pill);
  });
  box.hidden = false;
  box.focus();
}

async function swapCourse(oldCode, newCode) {
  const idx = state.courses.findIndex((c) => normCode(c.code) === normCode(oldCode));
  let title = null;
  let credits = null;
  try {
    const res = await fetch(`/api/v1/scheduler/courses/search?q=${encodeURIComponent(newCode)}&limit=5`);
    const list = await res.json();
    const match = (list || []).find((x) => normCode(x.code) === normCode(newCode));
    if (match) { title = match.title; credits = match.credits ?? null; }
  } catch (e) {}
  const replacement = {
    code: newCode, title, credits, section_count: 0,
    instructor: null, professors: [], hasOnline: false,
  };
  if (idx >= 0) state.courses[idx] = replacement;
  else if (state.courses.length < MAX_COURSES) state.courses.push(replacement);
  hideFormError();
  showStep(0);
  renderCourseList();
  refreshDaysOff();
  fetchCourseInstructors(replacement);
  announce(`Swapped in ${newCode}. Review your courses and resubmit.`);
}

// ── Results ───────────────────────────────────────────────────────────────────
function showView(view) {
  $id("stepper-wrap").hidden = view !== "stepper";
  $id("results-wrap").hidden = view !== "results";
  $id("no-schedule-wrap").hidden = view !== "no-schedule";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderResults(data) {
  $id("results-notices").innerHTML = "";
  renderNotices(data);
  const colorMap = buildColorMap(data.schedules);

  const tabs = $id("option-tabs");
  tabs.innerHTML = "";
  data.schedules.forEach((sched, i) => {
    const tab = el("button", "option-tab");
    tab.appendChild(el("span", "option-tab-name", `Schedule ${i + 1}`));
    tab.appendChild(el("span", "option-tab-score", `Score ${fmtScore(sched.total_score)}`));
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", i === 0 ? "true" : "false");
    tab.addEventListener("click", () => selectOption(i));
    tabs.appendChild(tab);
  });

  const panels = $id("option-panels");
  panels.innerHTML = "";
  data.schedules.forEach((sched, i) => {
    const panel = el("section", "option-panel" + (i === 0 ? " is-active" : ""));
    panel.setAttribute("role", "tabpanel");
    panel.id = `option-panel-${i}`;
    panel.appendChild(renderOptionCard(sched, i, colorMap, data.term));
    panels.appendChild(panel);
  });

  updateMoreButton();
  showView("results");
}

// ── Generate more schedules ───────────────────────────────────────────────────
function scheduleSectionIds(sched) {
  const ids = [];
  [...(sched.sections || []), ...(sched.async_online || [])].forEach((s) => {
    if (s.id !== null && s.id !== undefined) ids.push(s.id);
  });
  return ids.sort((a, b) => a - b);
}

function updateMoreButton() {
  const btn = $id("btn-more");
  if (!btn) return;
  btn.hidden = !state.hasMore;
}

function setMoreLoading(loading) {
  const btn = $id("btn-more");
  if (!btn) return;
  btn.disabled = loading;
  btn.textContent = loading ? "Generating…" : "Generate 3 more schedules";
}

function showMoreMessage(kind, text) {
  const wrap = $id("results-notices");
  if (!wrap) return;
  const n = el("div", "notice notice-" + kind);
  n.textContent = text;
  wrap.appendChild(n);
}

async function generateMore() {
  if (state.submitting) return;
  state.submitting = true;
  setMoreLoading(true);

  const payload = buildPayload();
  payload.exclude = state.shownScheduleIds;

  try {
    const res = await fetch("/api/v1/scheduler/schedules/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (res.status === 429) {
      showMoreMessage("warning", "You're going a little fast — give it a moment, then try again.");
      return;
    }
    if (res.status === 503) {
      showMoreMessage("warning", "Course data is still loading. Please try again in a few minutes.");
      return;
    }
    if (!res.ok) {
      showMoreMessage("warning", "Something went wrong while building more schedules. Please try again.");
      return;
    }

    const data = await res.json();
    const schedules = data.schedules || [];
    if (data.status === "ok" && schedules.length) {
      // Append the new batch to the already-shown schedules so they display
      // side-by-side (Schedule 4, 5, 6, …).
      state.result.schedules = state.result.schedules.concat(schedules);
      state.result.has_more = !!data.has_more;
      state.selectedOption = 0;
      state.shownScheduleIds = state.shownScheduleIds.concat(schedules.map(scheduleSectionIds));
      state.hasMore = !!data.has_more;
      renderResults(state.result);
    } else {
      state.hasMore = false;
      updateMoreButton();
      showMoreMessage("info", "No more schedule combinations are available.");
    }
  } catch (e) {
    showMoreMessage("warning", "Couldn't reach the server. Please try again.");
  } finally {
    state.submitting = false;
    setMoreLoading(false);
  }
}

function renderNotices(data) {
  const wrap = $id("results-notices");
  const addNotice = (kind, text) => {
    const n = el("div", "notice notice-" + kind);
    n.textContent = text;
    wrap.appendChild(n);
  };
  (data.warnings || []).forEach((w) => addNotice("warning", w));
  (data.assumptions || []).forEach((a) => addNotice("assumption", `I read that as: ${a}`));
  if (data.cap_hit && data.has_more) {
    addNotice("info", "Many schedules matched your criteria — use “Generate 3 more schedules” to browse more.");
  }
}

function selectOption(i) {
  state.selectedOption = i;
  document.querySelectorAll(".option-tab").forEach((t, idx) => {
    t.setAttribute("aria-selected", idx === i ? "true" : "false");
  });
  document.querySelectorAll(".option-panel").forEach((p, idx) => {
    p.classList.toggle("is-active", idx === i);
  });
}

function buildColorMap(schedules) {
  const codes = new Set();
  schedules.forEach((s) => {
    [...(s.sections || []), ...(s.async_online || [])].forEach((sec) => codes.add(sec.course_code));
  });
  const sorted = [...codes].sort();
  const map = {};
  sorted.forEach((code, i) => { map[code] = PALETTE[i % PALETTE.length]; });
  return map;
}

function fmtScore(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(1) : "—";
}

function renderOptionCard(sched, index, colorMap, term) {
  const layout = el("div", "option-layout");

  // Main column: envelope notice (if any) + calendar + online classes.
  const main = el("div", "option-main");
  const envNotice = buildEnvelopeNotice(sched, term);
  if (envNotice) main.appendChild(envNotice);
  const calCard = el("div", "card card-flush");
  calCard.appendChild(renderCalendar(sched, colorMap));
  main.appendChild(calCard);

  if ((sched.async_online || []).length) {
    const online = el("div", "card");
    online.appendChild(el("h3", "card-title", "Online / no set time"));
    online.appendChild(renderAsyncList(sched.async_online));
    main.appendChild(online);
  }

  // Ease gets its own full-width section below the calendar.
  main.appendChild(buildEaseSection(sched.metrics));
  layout.appendChild(main);

  // Side column: overall score (expandable).
  layout.appendChild(renderScoreCard(sched, index));
  return layout;
}

function renderScoreCard(sched, index) {
  const m = sched.metrics;
  const side = el("aside", "option-side");
  const card = el("div", "card score-card");

  // The overall score itself is the disclosure toggle for the details.
  const toggle = el("button", "score-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  const score = el("span", "score-row");
  score.appendChild(el("span", "score-value", fmtScore(sched.total_score)));
  score.appendChild(el("span", "score-label", "overall score"));
  toggle.appendChild(score);
  toggle.appendChild(el("span", "score-chevron", "▾"));
  card.appendChild(toggle);

  const details = el("div", "score-details");
  details.hidden = true;

  if (sched.explanation) details.appendChild(el("p", "explanation", sched.explanation));

  const stats = el("dl", "stat-list");
  const addStat = (k, v) => {
    const row = el("div", "stat-row");
    row.appendChild(el("dt", null, k));
    row.appendChild(el("dd", null, v));
    stats.appendChild(row);
  };
  addStat("Days on campus", String(m.days_on_campus));
  addStat("Total gap time", fmtDuration(m.total_gap_minutes));
  addStat("Earliest start", fmtMin(m.earliest_start));
  addStat("Latest end", fmtMin(m.latest_end));
  const credits = totalCredits();
  if (credits.total > 0) addStat(credits.complete ? "Credit hours" : "Credit hours (approx.)", String(credits.total));
  details.appendChild(stats);
  card.appendChild(details);

  toggle.addEventListener("click", () => {
    const open = details.hidden;
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    card.classList.toggle("is-open", open);
  });

  side.appendChild(card);
  side.appendChild(renderFeedbackBox());
  return side;
}

// A small collapsible "Feedback?" box shown under the overall score.
function renderFeedbackBox() {
  const box = el("div", "card feedback-card");
  const toggle = el("button", "feedback-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.appendChild(el("span", "feedback-label", "Feedback?"));
  toggle.appendChild(el("span", "feedback-chevron", "▾"));
  box.appendChild(toggle);

  const details = el("div", "feedback-details");
  details.hidden = true;
  const msg = el("p", "feedback-text");
  msg.appendChild(document.createTextNode("Please email "));
  const link = el("a", "feedback-email", "watathys@byu.edu");
  link.href = "mailto:watathys@byu.edu";
  msg.appendChild(link);
  msg.appendChild(document.createTextNode(" if you have any feature requests or problems you want to report."));
  details.appendChild(msg);
  box.appendChild(details);

  toggle.addEventListener("click", () => {
    const open = details.hidden;
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    box.classList.toggle("is-open", open);
  });

  return box;
}

// ── Envelope notice ──────────────────────────────────────────────────────────
function buildEnvelopeNotice(sched, term) {
  const envSections = (sched.sections || []).filter((s) => s.via_envelope);
  if (!envSections.length) return null;

  const card = el("section", "card envelope-notice");
  card.setAttribute("aria-label", "Envelope registration required");
  card.appendChild(el("h3", "card-title", "Envelope registration"));

  // Group sections by their envelope id (fall back to the title).
  const groups = [];
  const indexByKey = new Map();
  envSections.forEach((s) => {
    const key = s.env_id || s.via_envelope;
    if (!indexByKey.has(key)) {
      indexByKey.set(key, { envId: s.env_id, title: s.via_envelope, capturedAt: s.captured_at, sections: [] });
      groups.push(indexByKey.get(key));
    }
    indexByKey.get(key).sections.push(s);
  });

  groups.forEach((g) => {
    const block = el("div", "envelope-notice-block");
    block.appendChild(el("p", "envelope-notice-title", g.title));
    block.appendChild(el("p", "envelope-notice-rule",
      "Register in the Registration Cart by choosing the envelope that lists exactly these sections"));
    const list = el("ul", "envelope-notice-list");
    g.sections.forEach((s) => {
      list.appendChild(el("li", null, `${s.course_code} ${s.section_number}`));
    });
    block.appendChild(list);
    const meta = g.envId
      ? `Env ${g.envId} (Env numbers are specific to ${term} and the date this list was captured ${g.capturedAt})`
      : `This envelope was captured ${g.capturedAt}`;
    block.appendChild(el("p", "envelope-notice-meta", meta));
    card.appendChild(block);
  });

  card.appendChild(el("p", "envelope-notice-disclaimer",
    "Some envelopes are restricted to certain programs or students. Confirm eligibility in myBYU."));

  return card;
}

// ── Calendar grid ─────────────────────────────────────────────────────────────
function renderCalendar(sched, colorMap) {
  const sections = (sched.sections || []).filter((s) => (s.meetings || []).length > 0);
  if (sections.length === 0) {
    return el("div", "cal-empty", "All of these sections are online with no set meeting time.");
  }

  // Days: Mon–Fri always, plus any weekend day that actually has a meeting.
  const meetingDays = new Set();
  sections.forEach((s) => s.meetings.forEach((m) => meetingDays.add(m.day)));
  const days = DAY_ORDER.filter((d) => WEEKDAYS.includes(d) || meetingDays.has(d));

  // Time range (minutes).
  let earliest = null, latest = null;
  sections.forEach((s) => s.meetings.forEach((m) => {
    if (earliest === null || m.start_min < earliest) earliest = m.start_min;
    if (latest === null || m.end_min > latest) latest = m.end_min;
  }));
  const gridStart = Math.floor(earliest / 60) * 60;
  const gridEnd = Math.ceil(latest / 60) * 60;
  const totalMin = gridEnd - gridStart;
  const pxPerMin = 1;

  const cal = el("div", "cal");
  const scroller = el("div", "cal-scroller");
  const grid = el("div", "cal-grid");
  grid.style.gridTemplateColumns = `56px repeat(${days.length}, minmax(110px, 1fr))`;

  // Header row: corner + day names.
  grid.appendChild(el("div", "cal-corner"));
  days.forEach((d) => grid.appendChild(el("div", "cal-day-head", DAY_SHORT[d])));

  // Body: time gutter + day columns.
  const times = el("div", "cal-times");
  for (let t = gridStart; t < gridEnd; t += 60) {
    const hour = t / 60;
    const hh = hour % 12 === 0 ? 12 : hour % 12;
    const ampm = hour < 12 ? "AM" : "PM";
    times.appendChild(el("div", "cal-hour", `${hh} ${ampm}`));
  }
  grid.appendChild(times);

  days.forEach((day) => {
    const col = el("div", "cal-day-col");
    col.style.height = `${totalMin}px`;
    sections.forEach((sec) => {
      const color = colorMap[sec.course_code] || PALETTE[0];
      (sec.meetings || []).forEach((m) => {
        if (m.day !== day) return;
        const top = (m.start_min - gridStart) * pxPerMin;
        const height = Math.max((m.end_min - m.start_min) * pxPerMin, 24);
        const block = el("div", "cal-block");
        block.style.top = `${top}px`;
        block.style.height = `${height}px`;
        block.style.backgroundColor = color;
        const codeLine = el("div", "cal-block-code");
        codeLine.appendChild(document.createTextNode(sec.course_code));
        if (sec.section_number) {
          codeLine.appendChild(el("span", "cal-block-section", `-${sec.section_number}`));
        }
        block.appendChild(codeLine);
        if (sec.via_envelope) block.appendChild(el("div", "cal-block-badge", "Envelope"));
        block.appendChild(el("div", "cal-block-time", fmtRange(m.start_min, m.end_min)));
        const roomParts = [];
        if (m.building && m.room) roomParts.push(`${m.building} ${m.room}`);
        else if (m.room) roomParts.push(m.room);
        if (roomParts.length) block.appendChild(el("div", "cal-block-loc", roomParts.join(" · ")));
        // Professor name sits on its own line below the room/building.
        const instructor = titleCaseName(sec.instructor_name);
        if (instructor) block.appendChild(el("div", "cal-block-prof", instructor));
        col.appendChild(block);
      });
    });
    grid.appendChild(col);
  });

  scroller.appendChild(grid);
  cal.appendChild(scroller);
  return cal;
}

// ── Ease section ──────────────────────────────────────────────────────────────
function buildEaseSection(metrics) {
  const pwr = metrics.professors_without_ratings || 0;
  const hasEase = metrics.average_ease !== null && metrics.average_ease !== undefined;
  const section = el("section", "card ease-section");
  section.setAttribute("aria-label", "Ease calculation");

  const head = el("div", "ease-head");

  const titleBox = el("div", "ease-title-box");
  titleBox.appendChild(el("h3", "card-title", "Ease of instructors"));

  if (hasEase) {
    const score = el("div", "ease-score");
    score.appendChild(el("span", "ease-score-num", String(Math.round(metrics.average_ease))));
    score.appendChild(el("span", "ease-score-max", "/ 100"));
    head.appendChild(score);

    titleBox.appendChild(el("p", "ease-verdict",
      pwr > 0
        ? `${easeLabel(metrics.average_ease)} · ${pwr} professor${pwr === 1 ? "" : "s"} without ratings excluded`
        : easeLabel(metrics.average_ease)));
  } else {
    titleBox.appendChild(el("p", "ease-verdict",
      "Can't be calculated — none of these professors have RateMyProfessors ratings."));
  }
  head.appendChild(titleBox);
  section.appendChild(head);

  if (hasEase) {
    const bar = el("div", "ease-bar");
    const fill = el("div", "ease-bar-fill");
    fill.style.width = `${Math.max(0, Math.min(100, metrics.average_ease))}%`;
    bar.appendChild(fill);
    section.appendChild(bar);
  }

  const list = el("ul", "prof-list");
  const items = metrics.ease_breakdown || [];
  if (!items.length) list.appendChild(el("li", "prof-item prof-empty", "No professor data available."));
  else items.forEach((p) => list.appendChild(buildProfessorRow(p)));
  section.appendChild(list);

  section.appendChild(el("p", "ease-breakdown-footer",
    "Ratings from RateMyProfessors. Matches are automatic; use the link to confirm it's the right professor."));

  // Formula explanation lives behind a "How is this score calculated?" link at the bottom.
  const formulaToggle = el("button", "ease-formula-toggle");
  formulaToggle.type = "button";
  formulaToggle.setAttribute("aria-expanded", "false");
  formulaToggle.appendChild(el("span", null, "How is this score calculated?"));
  formulaToggle.appendChild(el("span", "ease-formula-chevron", "▾"));
  section.appendChild(formulaToggle);

  const formula = el("p", "ease-formula-text", EASE_FORMULA);
  formula.hidden = true;
  section.appendChild(formula);

  formulaToggle.addEventListener("click", () => {
    const open = formula.hidden;
    formula.hidden = !open;
    formulaToggle.setAttribute("aria-expanded", String(open));
    formulaToggle.classList.toggle("is-open", open);
  });

  return section;
}

function fmtOneDecimal(n) {
  if (n === null || n === undefined) return null;
  const v = Number(n);
  if (!Number.isFinite(v)) return null;
  return (Math.round(v * 10) / 10).toFixed(1);
}

function profStat(label, value) {
  const s = el("span", "prof-stat");
  s.appendChild(el("span", "prof-stat-label", label));
  s.appendChild(el("span", "prof-stat-value", value));
  return s;
}

function buildProfessorRow(p) {
  const li = el("li", "prof-item");

  // Summary row (always visible): class, professor, overall ease score.
  const toggle = el("button", "prof-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");

  const summary = el("div", "prof-summary");
  const courses = (p.courses || []).filter(Boolean).join(", ");
  summary.appendChild(el("span", "prof-course", courses || "—"));
  summary.appendChild(el("span", "prof-name", p.display_name || "Unknown instructor"));
  const rating = (p.data_status === "none" || p.ease_score == null)
    ? "—"
    : `${Math.round(p.ease_score)} / 100`;
  const ratingEl = el("span", "prof-rating", rating);
  ratingEl.title = "Ease score out of 100";
  summary.appendChild(ratingEl);
  toggle.appendChild(summary);
  toggle.appendChild(el("span", "prof-chevron", "▾"));
  li.appendChild(toggle);

  // Expanded details.
  const details = el("div", "prof-details");
  details.hidden = true;

  if (p.data_status === "none") {
    details.appendChild(el("div", "prof-none", "No RateMyProfessors page found"));
  } else {
    const stats = el("div", "prof-stats");
    if (p.avg_rating != null) {
      stats.appendChild(profStat("Rating", `${fmtOneDecimal(p.avg_rating)} / 5`));
    }
    if (p.avg_difficulty != null) {
      stats.appendChild(profStat("Difficulty", `${fmtOneDecimal(p.avg_difficulty)} / 5`));
    }
    if (p.would_take_again_percent != null) {
      stats.appendChild(profStat("Would take again", `${Math.round(p.would_take_again_percent)}%`));
    }
    if (p.num_ratings != null) {
      stats.appendChild(profStat("Based on", `${p.num_ratings} rating${p.num_ratings === 1 ? "" : "s"}`));
    }
    details.appendChild(stats);

    if (p.data_status === "limited") {
      details.appendChild(el("div", "prof-limited",
        `Limited data (${p.num_ratings} rating${p.num_ratings === 1 ? "" : "s"})`));
    }

    if (p.rmp_url) {
      const a = el("a", "prof-link", "View on RateMyProfessors");
      a.href = p.rmp_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      details.appendChild(a);
    }
  }

  li.appendChild(details);

  toggle.addEventListener("click", () => {
    const open = details.hidden;
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    li.classList.toggle("is-open", open);
  });

  return li;
}

// ── Async online list ─────────────────────────────────────────────────────────
function renderAsyncList(items) {
  const list = el("div", "async-list");
  items.forEach((sec) => {
    const item = el("div", "async-item");
    const main = el("div", "async-main");
    main.appendChild(document.createTextNode(sec.course_code));
    if (sec.section_number) {
      main.appendChild(el("span", "async-section", `-${sec.section_number}`));
    }
    const subParts = [];
    if (sec.title) subParts.push(sec.title);
    const asyncInstructor = titleCaseName(sec.instructor_name);
    if (asyncInstructor) subParts.push(asyncInstructor);
    const sub = el("div", "async-sub", subParts.join(" · "));
    item.appendChild(main);
    item.appendChild(sub);
    list.appendChild(item);
  });
  return list;
}

// ── No schedule ───────────────────────────────────────────────────────────────
function renderNoSchedule(data) {
  const body = $id("bottleneck-body");
  body.innerHTML = "";

  const b = data.bottleneck || {};

  (b.no_sections || []).forEach((r) => {
    const removed = r.removed_by || {};
    let why;
    if (removed.days_off) {
      const days = (state.daysOff && state.daysOff.length) ? state.daysOff.join(", ") : "a day you want off";
      why = `every section meets on ${days}, which you wanted off`;
    } else if (Object.keys(removed).length === 1 && removed.online && !state.allowOnline) {
      why = "it only has online sections and you said no online classes";
    } else if (Object.keys(removed).length === 1 && removed.salt_lake && !state.allowSaltLake) {
      why = "its sections are at the Salt Lake Center and you said no Salt Lake classes";
    } else {
      const reasons = [];
      if (removed.instructor) reasons.push("no section has the instructor you selected");
      if (removed.online) reasons.push("online-only");
      if (removed.salt_lake) reasons.push("only at the Salt Lake Center");
      if (removed.full) reasons.push("all full");
      if (removed.earliest_latest) reasons.push("outside your time window");
      if (removed.blocked_times) reasons.push("conflicts with your blocked times");
      why = reasons.length ? `its sections are ${reasons.join(", ")}` : "it has no matching sections";
    }
    body.appendChild(el("p", "bottleneck-item", `${r.course}: impossible because ${why}.`));
  });

  (b.envelope_problems || []).forEach((ep) => {
    const removed = ep.removed_by || {};
    const reasons = [];
    if (removed.days_off) reasons.push("conflicts with a day you want off");
    if (removed.online) reasons.push("has online sections");
    if (removed.salt_lake) reasons.push("has Salt Lake Center sections");
    if (removed.full) reasons.push("all bundles full");
    if (removed.earliest_latest) reasons.push("outside your time window");
    if (removed.blocked_times) reasons.push("conflicts with your blocked times");
    const why = reasons.length ? reasons.join(", ") : "no matching bundles";
    body.appendChild(el("p", "bottleneck-item", `${ep.title}: envelope could not be scheduled (${why}).`));
  });

  (b.always_conflicting_pairs || []).forEach(([a, c]) => {
    body.appendChild(el("p", "bottleneck-item", `${a} and ${c} conflict — every section of one overlaps the other.`));
  });

  if ((b.feasible_relaxations || []).length) {
    const box = el("div", "bottleneck-suggest");
    box.appendChild(el("strong", null, "Try loosening one of these:"));
    const ul = el("ul");
    b.feasible_relaxations.forEach((label) => {
      ul.appendChild(el("li", null, relaxationText(label)));
    });
    box.appendChild(ul);
    body.appendChild(box);
  } else if (!(b.no_sections || []).length && !(b.envelope_problems || []).length && !(b.always_conflicting_pairs || []).length) {
    body.appendChild(el("p", "bottleneck-item", "Your constraints are too tight to fit these courses together."));
  }

  if (data.warnings && data.warnings.length) {
    data.warnings.forEach((w) => body.appendChild(el("p", "notice notice-warning", w)));
  }

  showView("no-schedule");
}

function relaxationText(label) {
  const map = {
    "earliest/latest window": "Widen your earliest start or latest end time",
    "blocked times": "Remove or relax a blocked time from your free-text notes",
    "online allowed": "Allow online classes",
    "skip full sections": "Allow sections that are currently full",
    "salt lake allowed": "Allow Salt Lake classes",
  };
  return map[label] || label;
}

// ── Init ──────────────────────────────────────────────────────────────────────
function init() {
  initTheme();
  populateTimeDropdowns();
  loadEnvelopes();

  $id("btn-theme").addEventListener("click", toggleTheme);
  $id("btn-next").addEventListener("click", goNext);
  $id("btn-back").addEventListener("click", goBack);

  $id("earliest-start").addEventListener("change", (e) => { state.earliestStart = e.target.value; });
  $id("latest-end").addEventListener("change", (e) => { state.latestEnd = e.target.value; });
  $id("allow-online").addEventListener("change", (e) => { state.allowOnline = e.target.checked; });
  $id("allow-salt-lake").addEventListener("change", (e) => { state.allowSaltLake = e.target.checked; });
  $id("extra-text").addEventListener("input", (e) => { state.extraText = e.target.value; });

  document.querySelectorAll(".step-dot").forEach((dot) => {
    dot.addEventListener("click", () => {
      const target = parseInt(dot.dataset.goto, 10);
      // Allow free navigation back through already-reached steps only.
      if (target <= state.maxReached) showStep(target);
    });
  });

  $id("btn-edit").addEventListener("click", () => { showView("stepper"); showStep(state.maxReached); });
  $id("btn-more").addEventListener("click", generateMore);
  $id("btn-edit-constraints").addEventListener("click", () => { showView("stepper"); showStep(2); });

  showStep(0);
}

document.addEventListener("DOMContentLoaded", init);
