#!/usr/bin/with-contenv bashio
# ==============================================================================
# HistoLite - Avvio add-on
# ==============================================================================

bashio::log.info "Avvio HistoLite..."

# Leggi opzioni dalla configurazione HA
export DB_PATH=$(bashio::config 'db_path' '/config/home-assistant_v2.db')
export BACKUP_BEFORE_PURGE=$(bashio::config 'backup_before_purge')
export BACKUP_PATH=$(bashio::config 'backup_path')
export LOG_LEVEL=$(bashio::config 'log_level')
export MAX_ROWS_PER_BATCH=$(bashio::config 'max_rows_per_batch')
export DATA_PATH="/data"
export INGRESS_PATH=$(bashio::addon.ingress_path)
export PORT=8099

bashio::log.info "Database: ${DB_PATH}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"
bashio::log.info "Log level: ${LOG_LEVEL}"

# Crea directory dati se non esiste
mkdir -p /data/histolite

# Avvia l'applicazione Flask
cd /opt/histolite
exec python3 app.py
