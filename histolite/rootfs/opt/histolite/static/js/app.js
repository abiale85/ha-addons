/**
 * HistoLite - JavaScript principale
 * Utility condivise tra tutte le pagine
 */

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function apiFetch(path, options = {}) {
  const url = BASE_PATH + path;

  // Timeout di default 30s; il chiamante può sovrascriverlo con options.timeout
  const timeoutMs = options.timeout ?? 30000;
  const timeoutController = new AbortController();
  const timeoutId = setTimeout(
    () => timeoutController.abort(new DOMException(`Timeout dopo ${timeoutMs}ms`, 'TimeoutError')),
    timeoutMs
  );

  // Combina il signal del chiamante con il timeout interno
  let signal = timeoutController.signal;
  if (options.signal) {
    // Se il chiamante ha già un signal, usarlo insieme al timeout
    const callerSignal = options.signal;
    if (callerSignal.aborted) { clearTimeout(timeoutId); throw new DOMException('Aborted', 'AbortError'); }
    callerSignal.addEventListener('abort', () => timeoutController.abort(), { once: true });
  }

  try {
    const resp = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...options.headers },
      ...options,
      signal,
    });
    if (!resp.ok) {
      let errMsg = `HTTP ${resp.status}`;
      try { const j = await resp.json(); errMsg = j.error || errMsg; } catch {}
      throw new Error(errMsg);
    }
    return resp.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

async function apiGet(path, options = {}) {
  return apiFetch(path, { method: 'GET', ...options });
}

async function apiPost(path, body) {
  return apiFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Global loading overlay
// ---------------------------------------------------------------------------

/**
 * Mostra un overlay full-screen bloccante durante operazioni pesanti sul DB.
 */
function showGlobalLoading(msg) {
  const overlay = document.getElementById('global-loading-overlay');
  if (!overlay) return;
  const msgEl = document.getElementById('global-loading-msg');
  if (msgEl && msg) msgEl.textContent = msg;
  overlay.classList.remove('d-none');
}

function hideGlobalLoading() {
  const overlay = document.getElementById('global-loading-overlay');
  if (overlay) overlay.classList.add('d-none');
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

/**
 * Formatta un numero con separatori di migliaia.
 */
function fmt(n) {
  if (n == null || n === '') return '—';
  return Number(n).toLocaleString('it-IT');
}

/**
 * Formatta un timestamp Unix (float) o stringa ISO in data leggibile.
 */
function fmtTs(ts) {
  if (ts == null) return '—';
  let d;
  if (typeof ts === 'number') {
    d = new Date(ts * 1000);
  } else {
    d = new Date(ts);
  }
  if (isNaN(d)) return String(ts);
  return d.toLocaleDateString('it-IT', {
    day: '2-digit', month: '2-digit', year: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

/**
 * Formatta bytes in formato leggibile.
 */
function fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024;
    i++;
  }
  return `${bytes.toFixed(1)} ${units[i]}`;
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------

const TOAST_COLORS = {
  success: 'bg-success',
  danger:  'bg-danger',
  warning: 'bg-warning text-dark',
  info:    'bg-info text-dark',
};

/**
 * Mostra una notifica toast in basso a destra.
 */
function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const id = 'toast-' + Date.now();
  const colorClass = TOAST_COLORS[type] || 'bg-secondary';
  const html = `
    <div id="${id}" class="toast align-items-center text-white border-0 ${colorClass}"
         role="alert" aria-live="assertive">
      <div class="d-flex">
        <div class="toast-body">${message}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto"
                data-bs-dismiss="toast"></button>
      </div>
    </div>`;
  container.insertAdjacentHTML('beforeend', html);
  const el = document.getElementById(id);
  const toast = new bootstrap.Toast(el, { delay: duration });
  toast.show();
  el.addEventListener('hidden.bs.toast', () => el.remove());
}

// ---------------------------------------------------------------------------
// Banner d'errore persistente (per fallimenti di caricamento pagina)
// ---------------------------------------------------------------------------

/**
 * Mostra un banner d'errore in cima al contenuto della pagina. A differenza
 * del toast (che scompare dopo pochi secondi) resta visibile finché la pagina
 * non viene ricaricata o l'errore non viene rimosso con clearPageError().
 * Da usare quando il caricamento dei dati di una pagina fallisce.
 */
function showPageError(message, { title = 'Errore nel caricamento dei dati' } = {}) {
  const host = document.querySelector('.content-area') || document.body;
  let banner = document.getElementById('page-error-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'page-error-banner';
    banner.className = 'alert alert-danger d-flex align-items-start gap-2 mb-3';
    banner.setAttribute('role', 'alert');
    host.prepend(banner);
  }
  banner.innerHTML = `
    <i class="bi bi-exclamation-octagon-fill flex-shrink-0 mt-1"></i>
    <div class="flex-grow-1">
      <strong>${title}</strong><br>
      <span class="small">${message || 'Errore sconosciuto'}</span>
    </div>
    <button type="button" class="btn btn-sm btn-outline-light flex-shrink-0"
            onclick="location.reload()">
      <i class="bi bi-arrow-clockwise me-1"></i>Riprova
    </button>`;
  banner.scrollIntoView({ block: 'nearest' });
}

function clearPageError() {
  document.getElementById('page-error-banner')?.remove();
}

// ---------------------------------------------------------------------------
// Feedback operazioni in background
// ---------------------------------------------------------------------------

/**
 * Memorizza un messaggio da mostrare come toast dopo un reload di pagina.
 * I toast non sopravvivono al reload, quindi passiamo per sessionStorage.
 */
function flashAfterReload(message, type = 'info') {
  try {
    sessionStorage.setItem('histolite_flash', JSON.stringify({ message, type }));
  } catch {}
}

/**
 * Interroga /api/strategy-status finché l'operazione in background non termina.
 * Ritorna l'oggetto last_result ({ ok, summary, ... }) oppure null.
 * Gli errori di rete transitori non interrompono il polling.
 */
async function pollBackgroundStrategy({ intervalMs = 2000, onProgress = null } = {}) {
  // Piccola attesa iniziale: dà tempo al worker di registrarsi come "running".
  await new Promise(r => setTimeout(r, 800));
  while (true) {
    let status;
    try {
      status = await apiGet('/api/strategy-status', { timeout: 5000 });
    } catch {
      await new Promise(r => setTimeout(r, intervalMs));
      continue;
    }
    if (status.running) {
      if (onProgress) onProgress(status);
      await new Promise(r => setTimeout(r, intervalMs));
      continue;
    }
    return status.last_result || null;
  }
}

/**
 * Notifica l'esito di una strategia lanciata in background e ricarica la pagina.
 */
async function reportBackgroundStrategyAndReload() {
  const last = await pollBackgroundStrategy();
  if (last && !last.ok) {
    flashAfterReload(`Strategia fallita: ${last.summary}`, 'danger');
  } else {
    flashAfterReload(last ? `Strategia completata: ${last.summary}` : 'Strategia completata', 'success');
  }
  window.location.reload();
}

// ---------------------------------------------------------------------------
// Strategie: builder parametri condiviso
// (Purge Adattivo a fasce, Picco per Bucket, Deduplica Valori)
// Usato sia dalla pagina Strategie (prefix "fp") sia dal modale del
// dettaglio sensore (prefix "dp"). Le altre strategie restano gestite
// localmente dalle rispettive pagine.
// ---------------------------------------------------------------------------

const HL_TIME_UNITS = [
  { value: 'minute', label: 'minuti',    sec: 60 },
  { value: 'hour',   label: 'ore',       sec: 3600 },
  { value: 'day',    label: 'giorni',    sec: 86400 },
  { value: 'week',   label: 'settimane', sec: 604800 },
];

const HL_AGG_OPTIONS = [
  ['time_weighted_mean', 'Media pesata sul tempo'],
  ['mean',       'Media'],
  ['median',     'Mediana'],
  ['mode',       'Moda (più frequente)'],
  ['min',        'Minimo'],
  ['max',        'Massimo'],
  ['first',      'Primo valore'],
  ['last',       'Ultimo valore'],
  ['percentile', 'Percentile'],
];

function hlUnitOptionsHtml(selected) {
  return HL_TIME_UNITS.map(u =>
    `<option value="${u.value}"${u.value === selected ? ' selected' : ''}>${u.label}</option>`
  ).join('');
}

function hlAggOptionsHtml(selected) {
  return HL_AGG_OPTIONS.map(([v, l]) =>
    `<option value="${v}"${v === selected ? ' selected' : ''}>${l}</option>`
  ).join('');
}

/** Converte una durata in secondi nella specifica {every, unit} più naturale. */
function hlIntervalToSpec(seconds) {
  seconds = Number(seconds) || 0;
  if (seconds <= 0) return { every: 0, unit: 'hour' };
  for (let i = HL_TIME_UNITS.length - 1; i >= 0; i--) {
    const u = HL_TIME_UNITS[i];
    if (seconds % u.sec === 0) return { every: seconds / u.sec, unit: u.value };
  }
  return { every: Math.max(1, Math.round(seconds / 60)), unit: 'minute' };
}

/** {every, unit} come lo salviamo nei params (nessuna conversione a secondi lato client). */
function hlReadInterval(everyEl, unitEl) {
  const every = parseFloat(everyEl?.value);
  return {
    every: Number.isFinite(every) && every >= 0 ? every : 1,
    unit: unitEl?.value || 'hour',
  };
}

// --- Fasce del Purge Adattivo -------------------------------------------------

function hlTierRowHtml() {
  return `
  <div class="hl-tier border border-secondary rounded p-2 mb-2 d-flex flex-wrap align-items-end gap-2">
    <div style="width:5.5rem">
      <label class="form-label small mb-0">Dopo (gg)</label>
      <input type="number" class="form-control form-control-sm hl-tier-after" min="1" value="7">
    </div>
    <div style="width:7.5rem">
      <label class="form-label small mb-0">Azione</label>
      <select class="form-select form-select-sm hl-tier-action">
        <option value="flatten">Appiattisci</option>
        <option value="delete">Elimina</option>
      </select>
    </div>
    <div class="hl-tier-flatten" style="width:9.5rem">
      <label class="form-label small mb-0">Bucket ogni</label>
      <div class="input-group input-group-sm">
        <input type="number" class="form-control hl-tier-every" min="1" value="1">
        <select class="form-select hl-tier-unit" style="max-width:6rem">${hlUnitOptionsHtml('hour')}</select>
      </div>
    </div>
    <div class="hl-tier-flatten" style="width:12rem">
      <label class="form-label small mb-0">Aggregazione</label>
      <select class="form-select form-select-sm hl-tier-agg">${hlAggOptionsHtml('time_weighted_mean')}</select>
    </div>
    <div class="hl-tier-flatten hl-tier-pct-wrap d-none" style="width:5rem">
      <label class="form-label small mb-0">%ile</label>
      <input type="number" class="form-control form-control-sm hl-tier-pct" min="0" max="100" value="95">
    </div>
    <button type="button" class="btn btn-sm btn-outline-danger hl-tier-del" title="Rimuovi fascia">
      <i class="bi bi-trash"></i>
    </button>
  </div>`;
}

function hlSyncTierRow(row) {
  const isFlatten = row.querySelector('.hl-tier-action').value === 'flatten';
  row.querySelectorAll('.hl-tier-flatten').forEach(el => el.classList.toggle('d-none', !isFlatten));
  const isPct = row.querySelector('.hl-tier-agg').value === 'percentile';
  const pct = row.querySelector('.hl-tier-pct-wrap');
  if (pct) pct.classList.toggle('d-none', !isFlatten || !isPct);
}

function hlAddTierRow(container, tier) {
  container.insertAdjacentHTML('beforeend', hlTierRowHtml());
  const row = container.lastElementChild;
  if (tier) {
    if (tier.after_days != null) row.querySelector('.hl-tier-after').value = tier.after_days;
    row.querySelector('.hl-tier-action').value = tier.action === 'delete' ? 'delete' : 'flatten';
    const spec = hlIntervalToSpec(
      tier.bucket_seconds != null
        ? tier.bucket_seconds
        : ((tier.bucket?.every ?? 1) * (HL_TIME_UNITS.find(u => u.value === (tier.bucket?.unit || 'hour'))?.sec || 3600))
    );
    row.querySelector('.hl-tier-every').value = spec.every || 1;
    row.querySelector('.hl-tier-unit').value = spec.unit;
    if (tier.agg) row.querySelector('.hl-tier-agg').value = tier.agg;
    if (tier.agg_pct != null) row.querySelector('.hl-tier-pct').value = tier.agg_pct;
  }
  row.querySelector('.hl-tier-action').addEventListener('change', () => hlSyncTierRow(row));
  row.querySelector('.hl-tier-agg').addEventListener('change', () => hlSyncTierRow(row));
  row.querySelector('.hl-tier-del').addEventListener('click', () => row.remove());
  hlSyncTierRow(row);
  return row;
}

function hlSetTiers(container, tiers) {
  container.innerHTML = '';
  (tiers && tiers.length ? tiers : [
    { after_days: 7,   action: 'flatten', bucket: { every: 1, unit: 'hour' }, agg: 'time_weighted_mean' },
    { after_days: 365, action: 'delete' },
  ]).forEach(t => hlAddTierRow(container, t));
}

function hlCollectTiers(container) {
  return [...container.querySelectorAll('.hl-tier')].map(row => {
    const after = parseInt(row.querySelector('.hl-tier-after').value, 10) || 1;
    const action = row.querySelector('.hl-tier-action').value === 'delete' ? 'delete' : 'flatten';
    if (action === 'delete') return { after_days: after, action };
    const t = {
      after_days: after,
      action: 'flatten',
      bucket: hlReadInterval(row.querySelector('.hl-tier-every'), row.querySelector('.hl-tier-unit')),
      agg: row.querySelector('.hl-tier-agg').value,
    };
    if (t.agg === 'percentile') t.agg_pct = parseFloat(row.querySelector('.hl-tier-pct').value) || 95;
    return t;
  }).sort((a, b) => a.after_days - b.after_days);
}

/** Ricrea le fasce da parametri salvati, con fallback ai vecchi threshold_*. */
function hlLegacyTiers(p) {
  const n = v => (v == null || v === '' ? null : parseInt(v, 10));
  const t1 = n(p.threshold_1_days), t2 = n(p.threshold_2_days);
  const t3 = n(p.threshold_3_days), t4 = n(p.threshold_4_days);
  const out = [];
  if (t1 && t2 && t2 > t1) out.push({ after_days: t1, action: 'flatten', bucket: { every: 1, unit: 'hour' }, agg: 'time_weighted_mean' });
  if (t2 && t3 && t3 > t2) out.push({ after_days: t2, action: 'flatten', bucket: { every: 1, unit: 'day' }, agg: 'time_weighted_mean' });
  if (t4 && t3 && t3 < t4 && t4 < 36500) out.push({ after_days: t4, action: 'delete' });
  else if (p.threshold_4_days == null && t3) out.push({ after_days: t3, action: 'delete' });
  return out;
}

// --- API di alto livello ----------------------------------------------------

/**
 * Costruisce i params per adaptive_purge / peak_decimation / deduplicate_values.
 * Ritorna null per gli altri tipi (il chiamante usa la propria logica).
 */
function hlBuildStrategyParams(prefix, strategyType) {
  const $ = id => document.getElementById(`${prefix}-${id}`);
  if (strategyType === 'adaptive_purge') {
    return { tiers: hlCollectTiers($('adp-tiers')) };
  }
  if (strategyType === 'peak_decimation') {
    const p = {
      older_than_days: parseInt($('peak-days').value, 10),
      bucket: hlReadInterval($('peak-every'), $('peak-unit')),
      agg: $('peak-agg').value,
      keep_resets: $('peak-resets').checked,
    };
    if (p.agg === 'percentile') p.agg_pct = parseFloat($('peak-pct').value) || 95;
    if (p.keep_resets) p.reset_threshold_pct = parseFloat($('peak-threshold').value) || 50;
    return p;
  }
  if (strategyType === 'deduplicate_values') {
    return {
      older_than_days: parseInt($('dedup-days').value, 10),
      keep_interval: hlReadInterval($('dedup-every'), $('dedup-unit')),
    };
  }
  return null;
}

/**
 * Popola i campi del form per adaptive/peak/dedup a partire dai params salvati.
 * Ritorna true se ha gestito il tipo, false altrimenti.
 */
function hlHydrateStrategyForm(prefix, strategyType, params) {
  const $ = id => document.getElementById(`${prefix}-${id}`);
  const p = params || {};
  if (strategyType === 'adaptive_purge') {
    const tiers = Array.isArray(p.tiers) && p.tiers.length ? p.tiers : hlLegacyTiers(p);
    hlSetTiers($('adp-tiers'), tiers);
    return true;
  }
  if (strategyType === 'peak_decimation') {
    $('peak-days').value = p.older_than_days || 7;
    const spec = p.bucket
      ? { every: p.bucket.every ?? 1, unit: p.bucket.unit || 'hour' }
      : hlIntervalToSpec(p.granularity === 'day' ? 86400 : 3600);
    $('peak-every').value = spec.every || 1;
    $('peak-unit').value = spec.unit;
    $('peak-agg').value = p.agg || 'max';
    $('peak-pct').value = p.agg_pct ?? 95;
    $('peak-resets').checked = p.keep_resets !== false;
    $('peak-threshold').value = p.reset_threshold_pct || 50;
    hlSyncPeakAgg(prefix);
    return true;
  }
  if (strategyType === 'deduplicate_values') {
    $('dedup-days').value = p.older_than_days || 7;
    const spec = p.keep_interval
      ? { every: p.keep_interval.every ?? 1, unit: p.keep_interval.unit || 'day' }
      : { every: 1, unit: 'day' };
    $('dedup-every').value = spec.every;
    $('dedup-unit').value = spec.unit;
    return true;
  }
  return false;
}

/** Mostra/nasconde il campo percentile del blocco Picco per Bucket. */
function hlSyncPeakAgg(prefix) {
  const agg = document.getElementById(`${prefix}-peak-agg`);
  const wrap = document.getElementById(`${prefix}-peak-pct-wrap`);
  if (agg && wrap) wrap.classList.toggle('d-none', agg.value !== 'percentile');
}

/** Collega i pulsanti "aggiungi fascia" e i toggle del blocco peak. */
function hlInitStrategyParamControls(prefix) {
  const addBtn = document.getElementById(`${prefix}-adp-add-tier`);
  const cont = document.getElementById(`${prefix}-adp-tiers`);
  if (addBtn && cont && !addBtn.dataset.hlWired) {
    addBtn.dataset.hlWired = '1';
    addBtn.addEventListener('click', () => hlAddTierRow(cont));
  }
  const peakAgg = document.getElementById(`${prefix}-peak-agg`);
  if (peakAgg && !peakAgg.dataset.hlWired) {
    peakAgg.dataset.hlWired = '1';
    peakAgg.addEventListener('change', () => hlSyncPeakAgg(prefix));
  }
}

// ---------------------------------------------------------------------------
// Dimensione DB in topbar (aggiornamento passivo)
// ---------------------------------------------------------------------------

async function updateDbSizeBadge() {
  try {
    const data = await apiGet('/api/db-size');
    const el = document.getElementById('db-size-val');
    if (el && data.db_size_human) el.textContent = data.db_size_human;
  } catch {}
}

// Aggiorna ogni 60 secondi
document.addEventListener('DOMContentLoaded', () => {
  // Mostra un eventuale messaggio "flash" salvato prima di un reload.
  try {
    const raw = sessionStorage.getItem('histolite_flash');
    if (raw) {
      sessionStorage.removeItem('histolite_flash');
      const f = JSON.parse(raw);
      if (f && f.message) showToast(f.message, f.type || 'info', 6000);
    }
  } catch {}

  updateDbSizeBadge();
  setInterval(updateDbSizeBadge, 60_000);
});
