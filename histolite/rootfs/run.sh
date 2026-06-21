#!/bin/sh
# ==============================================================================
# HistoLite - Avvio add-on
# ==============================================================================

# Leggi da /config/options.json le opzioni impostate in Home Assistant
if [ -f /config/options.json ]; then
  DB_PATH=$(grep -o '"db_path":"[^"]*' /config/options.json | cut -d'"' -f4)
  LOG_LEVEL=$(grep -o '"log_level":"[^"]*' /config/options.json | cut -d'"' -f4)
  MAX_ROWS_PER_BATCH=$(grep -o '"max_rows_per_batch":[^,}]*' /config/options.json | cut -d':' -f2)
fi

# Valori di default
DB_PATH="${DB_PATH:-/config/home-assistant_v2.db}"
LOG_LEVEL="${LOG_LEVEL:-info}"
MAX_ROWS_PER_BATCH="${MAX_ROWS_PER_BATCH:-5000}"
DATA_PATH="/config"

# INGRESS_PATH: HA Supervisor imposta questa variabile con il path reale
# (es. /api/hassio_ingress/TOKEN). Se non e' presente la lasciamo vuota;
# app.py legge l'header X-Ingress-Path su ogni richiesta (piu' affidabile).
INGRESS_PATH="${INGRESS_PATH:-}"

echo "HistoLite: Avvio..."
echo "Database: $DB_PATH"
echo "Ingress path: $INGRESS_PATH"
echo "Log level: $LOG_LEVEL"

# Crea directory dati persistenti se non esiste
mkdir -p /config/histolite

# Esporta variabili d'ambiente
export DB_PATH
export LOG_LEVEL
export MAX_ROWS_PER_BATCH
export DATA_PATH
export INGRESS_PATH
export PORT=8099

# Avvia l'applicazione con Gunicorn
# - 1 worker gthread + 4 thread: ottimale per add-on locale (bassa RAM, concorrenza sufficiente)
# - max_requests: riavvia il worker ogni 200 richieste per liberare memoria frammentata
# - timeout 120s: sufficiente per query SQLite pesanti
cd /opt/histolite
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 4 \
  --worker-class gthread \
  --max-requests 200 \
  --max-requests-jitter 30 \
  --timeout 120 \
  --keep-alive 2 \
  --log-level "${LOG_LEVEL:-info}" \
  --access-logfile - \
  --error-logfile - \
  "app:app"
