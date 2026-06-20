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
  const timeoutId = setTimeout(() => timeoutController.abort(), timeoutMs);

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

async function apiGet(path) {
  return apiFetch(path, { method: 'GET' });
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
// Dimensione DB in topbar (aggiornamento passivo)
// ---------------------------------------------------------------------------

async function updateDbSizeBadge() {
  try {
    const data = await apiGet('/api/overview');
    const el = document.getElementById('db-size-val');
    if (el && data.db_size_human) el.textContent = data.db_size_human;
  } catch {}
}

// Aggiorna ogni 60 secondi
document.addEventListener('DOMContentLoaded', () => {
  updateDbSizeBadge();
  setInterval(updateDbSizeBadge, 60_000);
});
