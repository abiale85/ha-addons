#!/bin/sh
# ==============================================================================
# HistoLite - Avvio add-on
# ==============================================================================

# Leggi da /config/options.json le opzioni impostate in Home Assistant
if [ -f /config/options.json ]; then
  DB_PATH=$(grep -o '"db_path":"[^"]*' /config/options.json | cut -d'"' -f4)
  BACKUP_BEFORE_PURGE=$(grep -o '"backup_before_purge":[^,}]*' /config/options.json | cut -d':' -f2)
  BACKUP_PATH=$(grep -o '"backup_path":"[^"]*' /config/options.json | cut -d'"' -f4)
  LOG_LEVEL=$(grep -o '"log_level":"[^"]*' /config/options.json | cut -d'"' -f4)
  MAX_ROWS_PER_BATCH=$(grep -o '"max_rows_per_batch":[^,}]*' /config/options.json | cut -d':' -f2)
fi

# Valori di default
DB_PATH="${DB_PATH:-/config/home-assistant_v2.db}"
BACKUP_BEFORE_PURGE="${BACKUP_BEFORE_PURGE:-true}"
BACKUP_PATH="${BACKUP_PATH:-/backup}"
LOG_LEVEL="${LOG_LEVEL:-info}"
MAX_ROWS_PER_BATCH="${MAX_ROWS_PER_BATCH:-5000}"
DATA_PATH="/data"

# Leggi INGRESS_PATH dalla variabile d'ambiente (impostata da HA)
INGRESS_PATH="${INGRESS_PATH:-/api/addons/70b55b62_histolite}"

echo "HistoLite: Avvio..."
echo "Database: $DB_PATH"
echo "Ingress path: $INGRESS_PATH"
echo "Log level: $LOG_LEVEL"

# Crea directory dati se non esiste
mkdir -p /data/histolite

# Esporta variabili d'ambiente
export DB_PATH
export BACKUP_BEFORE_PURGE
export BACKUP_PATH
export LOG_LEVEL
export MAX_ROWS_PER_BATCH
export DATA_PATH
export INGRESS_PATH
export PORT=8099

# Avvia l'applicazione Flask
cd /opt/histolite
exec python3 app.py
