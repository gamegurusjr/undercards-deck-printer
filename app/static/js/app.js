/* Undercards Deck Printer - vanilla JS front-end. */
"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  code: $("code"), btnParse: $("btn-parse"), btnClear: $("btn-clear"),
  parseError: $("parse-error"),
  panelProgress: $("panel-progress"), progressTitle: $("progress-title"),
  bar: $("bar"), progressDetail: $("progress-detail"), joblog: $("joblog"),
  panelDeck: $("panel-deck"), deckSummary: $("deck-summary"), deckGrid: $("deck-grid"),
  missbox: $("missbox"), missText: $("miss-text"), missTable: $("miss-table"),
  btnProceed: $("btn-proceed"), btnStop: $("btn-stop"),
  panelSettings: $("panel-settings"),
  setPaper: $("set-paper"), setOrientation: $("set-orientation"),
  setGap: $("set-gap"), gapval: $("gapval"), setCut: $("set-cut"), setArtifacts: $("set-artifacts"),
  pagecount: $("pagecount"),
  dlDeck: $("dl-deck"), dlArtifacts: $("dl-artifacts"),
  panelPages: $("panel-pages"), pages: $("pages"),
  tabDeck: document.querySelector('.tab[data-which="deck"]'),
  tabArtifacts: $("tab-artifacts"),
  panelHarvest: $("panel-harvest"),
  harvestStart: $("harvest-start"), harvestCancel: $("harvest-cancel"),
  harvestStatus: $("harvest-status"), harvestLog: $("harvest-log"),
  dbinfo: $("dbinfo"), btnDb: $("btn-db-refresh"), btnHarvest: $("btn-harvest"),
};

const state = { jobId: null, pollTimer: null, renderKeys: "", which: "deck" };

/* ---------------------------------------------------------------- helpers */

async function api(path, opts) {
  const resp = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, opts));
  let payload = null;
  try { payload = await resp.clone().json(); } catch { /* binary ok */ }
  if (!resp.ok) {
    const msg = (payload && (payload.message || payload.error)) || ("HTTP " + resp.status);
    const err = new Error(msg);
    err.payload = payload;
    throw err;
  }
  return payload;
}

function showError(where, e) {
  $(where).hidden = false;
  $(where).innerHTML = "<b>Something went wrong</b>" + String(e.message || e);
}

/* ------------------------------------------------------------ deck input */

els.btnParse.addEventListener("click", async () => {
  hideErrors();
  const code = els.code.value.trim();
  if (!code) { showInputError("Paste a deck code first - it is the base64 blob from the deck share link (or the whole link ending in ?code=...)."); return; }
  els.btnParse.disabled = true;
  try {
    const res = await api("/api/parse", {method: "POST", body: JSON.stringify({
      code, settings: readSettings(),
    })});
    startJobView(res.id);
  } catch (e) {
    els.btnParse.disabled = false;
    showInputError(e.message);
  }
});

els.btnClear.addEventListener("click", () => {
  els.code.value = ""; hideErrors();
  els.panelProgress.hidden = true; els.panelDeck.hidden = true;
  els.panelSettings.hidden = true; els.panelPages.hidden = true;
  state.jobId = null;
});

function showInputError(msg) {
  els.parseError.hidden = false;
  els.parseError.innerHTML = "<b>Deck code error</b>" + escapeHtml(String(msg));
}
function hideErrors() { els.parseError.hidden = true; els.parseError.innerHTML = ""; }

function readSettings() {
  return {
    paper: els.setPaper.value,
    orientation: els.setOrientation.value,
    gap_mm: parseFloat(els.setGap.value),
    cut_guides: els.setCut.checked,
    include_artifacts: els.setArtifacts.checked,
    proceed_missing: false,
  };
}

/* ---------------------------------------------------------------- polling */

function startJobView(id) {
  stopPoll();
  state.jobId = id;
  state.renderKeys = "";
  els.panelProgress.hidden = false;
  els.progressTitle.textContent = "Working…";
  els.bar.style.width = "0%";
  els.progressDetail.textContent = "starting…";
  els.joblog.textContent = "";
  els.panelSettings.hidden = false;
  els.panelDeck.hidden = false;
  els.deckGrid.innerHTML = "";
  els.deckSummary.textContent = "";
  els.missbox.hidden = true;
  els.panelPages.hidden = true;
  els.pages.innerHTML = "";
  els.dlDeck.hidden = true; els.dlArtifacts.hidden = true;
  els.btnParse.disabled = true;
  poll();
}

function stopPoll() {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
}

async function poll() {
  if (!state.jobId) return;
  let job = null;
  try {
    job = await api("/api/job/" + state.jobId);
  } catch (e) {
    els.progressDetail.textContent = "poll failed: " + e.message;
    stopPoll();
    els.btnParse.disabled = false;
    return;
  }
  renderProgress(job);

  if (job.status === "running") {
    stopPoll();
    state.pollTimer = setTimeout(poll, 600);
    return;
  }
  stopPoll();
  els.btnParse.disabled = false;
  elsgRefreshAfterDone(job);
}

/* progress bar text */
function renderProgress(job) {
  const pct = job.art_total ? Math.round(100 * job.art_done / job.art_total) : 0;
  els.bar.style.width = pct + "%";
  els.progressDetail.textContent =
    job.stage + (job.stage_detail ? " — " + job.stage_detail : "") +
    (job.art_total ? `  [${job.art_done}/${job.art_total}]` : "");
  els.joblog.textContent = (job.log || []).join("\n");
}

function elsgRefreshAfterDone(job) {
  renderProgress(job);
  if (job.kind === "dbrefresh") {
    if (job.status === "done") {
      els.progressTitle.textContent = "Card database refreshed";
      saveSettingsNote(job);
    } else {
      showInputError("Could not (re)build the card database: " + (job.error || "?"));
    }
    loadDbInfo();
    return;
  }
  renderDeckGrid(job);
  if (job.status === "error") {
    els.progressTitle.textContent = "Failed";
    showInputError(job.error || "unknown error");
    return;
  }
  els.progressTitle.textContent = job.status === "blocked" ? "Blocked — art is missing" : "Ready";
  if (job.status === "blocked") {
    showMissbox(job);
  } else {
    els.missbox.hidden = true;
  }
  renderCounts(job);
}

/* ---------------------------------------------------------------- grid */

function renderDeckGrid(job) {
  const summary = job.deck;
  els.deckSummary.textContent =
    `${summary.total_cards} cards · ${summary.unique_cards} unique · ` +
    `${summary.duplicates} duplicates · soul ${summary.soul || "—"}` +
    (summary.artifact_ids.length ? ` · ${summary.artifact_ids.length} artifact entr${summary.artifact_ids.length === 1 ? "y" : "ies"}` : "");

  let html = "";
  for (const e of job.entries) {
    const key = `${e.id}:${e.face_k}:${e.art_ok}`;
    if (!state.renderKeys.includes(key)) {
      state.renderKeys += "|" + key;
    }
    const img = e.face_k === null
      ? `<div class="cellmeta">queued…</div>`
      : `<img loading="lazy" src="/api/face/${job.id}?k=${e.face_k}&width=330&r=${job.rev}" alt="${escapeHtml(e.name)}">`;
    const badge = e.qty > 1 ? `<span class="qtybadge">×${e.qty}</span>` : "";
    html += `<div class="cardcell${e.art_ok ? "" : " missing"}">
      ${badge}${img}
      <div class="cellmeta">${escapeHtml(e.name)} ${e.qty > 1 ? "×" + e.qty : ""}</div>
    </div>`;
  }
  els.deckGrid.innerHTML = html;
}

function showMissbox(job) {
  els.missbox.hidden = false;
  els.missText.innerHTML =
    `<b>${job.missing.length}</b> card(s) have no art from either source. ` +
    `Nothing is substituted silently — decide below.`;
  let rows = "";
  for (const m of job.missing) {
    rows += `<tr><td>#${m.id}</td><td>${escapeHtml(m.name || "(unnamed)")}</td>
      <td>${escapeHtml((m.message || m.art_status || "unavailable") + (m.title ? " · wiki: " + m.title : ""))}</td></tr>`;
  }
  els.missTable.innerHTML = rows;
}

els.btnProceed.addEventListener("click", () => bumpSettings({proceed_missing: true}));
els.btnStop.addEventListener("click", () => {
  els.missbox.hidden = true;
  els.progressTitle.textContent = "Stopped — nothing printed";
});

/* ------------------------------------------------------------- settings */

els.setGap.addEventListener("input", () => { els.gapval.textContent = parseFloat(els.setGap.value).toFixed(1); });

const settingControls = [els.setPaper, els.setOrientation, els.setGap, els.setCut, els.setArtifacts];
for (const c of settingControls) {
  c.addEventListener("change", () => bumpSettings(readSettings()));
}

function saveSettingsNote(job) {
  els.pagecount.innerHTML =
    `${job.counts.cards} cards — <b>${job.counts.deck_pages}</b> page(s) ` +
    `at exactly ${job.counts.card_mm[0]}×${job.counts.card_mm[1]} mm (never scaled).`;
}

async function bumpSettings(patch) {
  if (!state.jobId) return;
  try {
    await api("/api/settings/" + state.jobId, {method: "POST", body: JSON.stringify(patch)});
    startJobView(state.jobId);          // re-enter polling; state preserved server-side
  } catch (e) {
    showInputError("Could not apply settings: " + e.message);
  }
}

/* --------------------------------------------------------------- preview */

function renderCounts(job) {
  saveSettingsNote(job);
  const per = job.counts.per_page;
  els.pages.innerHTML = "";
  const buildPages = (which, count) => {
    for (let p = 0; p < count; p++) {
      const src = `/api/preview/${job.id}?page=${p}&which=${which}&r=${job.rev}`;
      els.pages.insertAdjacentHTML("beforeend",
        `<figure><img loading="lazy" src="${src}" alt="page ${p + 1}">
         <figcaption>${which === "deck" ? "Sheet" : "Artifact sheet"} ${p + 1} / ${count}</figcaption></figure>`);
    }
  };
  buildPages("deck", job.counts.deck_pages || 0);
  const artCount = job.counts.artifact_pages || 0;
  if (job.settings.include_artifacts && artCount > 0) {
    els.tabArtifacts.hidden = false;
    buildPages("artifacts", artCount);
  } else {
    els.tabArtifacts.hidden = true;
  }
  els.panelPages.hidden = false;

  els.dlDeck.hidden = false;
  els.dlDeck.href = `/api/download/${job.id}?which=deck`;
  if (job.settings.include_artifacts && artCount > 0) {
    els.dlArtifacts.hidden = false;
    els.dlArtifacts.href = `/api/download/${job.id}?which=artifacts`;
  } else {
    els.dlArtifacts.hidden = true;
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
  });
});

/* --------------------------------------------------------------- harvest */

els.btnHarvest.addEventListener("click", () => {
  els.panelHarvest.hidden = !els.panelHarvest.hidden;
  refreshHarvestStatus();
});

els.harvestStart.addEventListener("click", async () => {
  els.harvestStart.disabled = true;
  try { await api("/api/harvest/start", {method: "POST", body: "{}"}); }
  catch (e) { els.harvestStatus.textContent = e.message; }
  els.harvestStart.disabled = false;
  pollHarvest();
});

els.harvestCancel.addEventListener("click", async () => {
  try { await api("/api/harvest/cancel", {method: "POST", body: "{}"}); } catch {}
  pollHarvest();
});

let harvestPoll = null;
async function refreshHarvestStatus() {
  try {
    const s = await api("/api/harvest/status");
    els.harvestStatus.textContent = `status: ${s.status} — ${s.message || ""} ${s.error ? "(error: " + s.error + ")" : ""}`;
    els.harvestLog.textContent = (s.log || []).join("\n");
    return s;
  } catch (e) {
    els.harvestStatus.textContent = "status unavailable: " + e.message;
    return null;
  }
}

async function pollHarvest() {
  const s = await refreshHarvestStatus();
  const active = s && (s.status === "starting" || s.status === "waiting-login" || s.status === "fetching");
  if (s && s.status === "done") loadDbInfo();
  if (harvestPoll) clearTimeout(harvestPoll);
  if (active) harvestPoll = setTimeout(pollHarvest, 1500);
}

/* --------------------------------------------------------------- db info */

async function loadDbInfo() {
  try {
    const d = await api("/api/dbinfo");
    if (!d.available) {
      els.dbinfo.innerHTML = `<b>Database missing</b> — ${escapeHtml(d.hint || "")}`;
      return;
    }
    const auth = d.authoritative
      ? `<b>authoritative</b> (/AllCards, ${new Date(d.authoritative_at).toLocaleString()})`
      : d.wiki_only ? `wiki fallback (<a href="${d.source}" target="_blank" rel="noopener">CC BY-SA</a>)` : "";
    els.dbinfo.innerHTML =
      `<b>${d.cards}</b> cards · <b>${d.artifacts}</b> artifacts · ` +
      `${d.en_source_keys} translation keys · ${auth}`;
  } catch (e) {
    els.dbinfo.innerHTML = "dbinfo error: " + escapeHtml(e.message);
  }
}

els.btnDb.addEventListener("click", async () => {
  els.btnDb.disabled = true;
  try {
    const res = await api("/api/db/refresh", {method: "POST", body: "{}"});
    startJobView(res.id);
    els.progressTitle.textContent = "Refreshing card database…";
  } catch (e) { showInputError(e.message); }
  els.btnDb.disabled = false;
});

/* ----------------------------------------------------------------- utils */

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

loadDbInfo();
