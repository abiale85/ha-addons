#!/bin/sh
# ==============================================================================
# HistoLite - Avvio add-on
# ==============================================================================

# Leggi le opzioni impostate in Home Assistant.
# Il Supervisor scrive le opzioni dell'add-on in /data/options.json
# (NON in /config/options.json, che e' la config dir di Home Assistant).
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
  # Parsing JSON reale con Python (gia' presente nell'immagine): il file e'
  # indentato con spazi dopo i due punti, quindi il match via grep fallirebbe.
  eval "$(python3 - "$OPTIONS_FILE" <<'PY'
import json, shlex, sys

keys = [
    "db_type", "db_path", "db_url", "db_host", "db_port",
    "db_user", "db_password", "db_name", "log_level", "max_rows_per_batch",
]
try:
    with open(sys.argv[1]) as fh:
        opts = json.load(fh)
except (OSError, ValueError):
    opts = {}
for key in keys:
    value = opts.get(key)
    if value is not None:
        print(f"{key.upper()}={shlex.quote(str(value))}")
PY
)"
else
  echo "ATTENZIONE: $OPTIONS_FILE non trovato, uso i valori di default"
fi

# Valori di default
DB_TYPE="${DB_TYPE:-sqlite}"
if [ "$DB_TYPE" != "sqlite" ]; then
  # Backend remoto: nessun percorso di file locale. NON toccare i file .db
  # nella config dir: appartengono a Home Assistant, non all'add-on.
  DB_PATH=""
elif [ -z "$DB_PATH" ] || [ "$DB_PATH" = "/config/home-assistant_v2.db" ] || [ "$DB_PATH" = "/homeassistant/home-assistant_v2.db" ]; then
  DB_PATH="/config/home-assistant_v2.db"
fi
DB_URL="${DB_URL:-}"
DB_HOST="${DB_HOST:-}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-}"
LOG_LEVEL="${LOG_LEVEL:-info}"
MAX_ROWS_PER_BATCH="${MAX_ROWS_PER_BATCH:-5000}"
# Persistenza dell'add-on (strategie salvate, cronologia job, cache) nel volume
# privato /data: viene rimosso disinstallando con "rimuovi dati" e conservato
# altrimenti. NON in /config, che è la cartella condivisa di Home Assistant e
# sopravvive comunque a ogni disinstallazione.
DATA_PATH="/data"

# INGRESS_PATH: HA Supervisor imposta questa variabile con il path reale
# (es. /api/hassio_ingress/TOKEN). Se non e' presente la lasciamo vuota;
# app.py legge l'header X-Ingress-Path su ogni richiesta (piu' affidabile).
INGRESS_PATH="${INGRESS_PATH:-}"

echo "HistoLite: Avvio..."
echo "Database backend: $DB_TYPE"
echo "Database: $DB_PATH"
echo "Ingress path: $INGRESS_PATH"
echo "Log level: $LOG_LEVEL"

# Crea directory dati persistenti se non esiste
mkdir -p /data/histolite

# Esporta variabili d'ambiente
export DB_TYPE
export DB_PATH
export DB_URL
export DB_HOST
export DB_PORT
export DB_USER
export DB_PASSWORD
export DB_NAME
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
