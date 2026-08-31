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
