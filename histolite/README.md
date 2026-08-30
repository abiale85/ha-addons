# HistoLite — Add-on Home Assistant

Gestione intelligente della history dei sensori Home Assistant.  
Supporta SQLite, PostgreSQL e MariaDB; TimescaleDB viene usato tramite il backend PostgreSQL senza opzioni separate. Controlla automaticamente lo schema moderno di Home Assistant e blocca l'avvio se trova un backend o una struttura legacy non supportata.

## Caratteristiche principali

- **Dashboard** con Top 10 sensori, dimensione DB e distribuzione tabelle
- **7 strategie** di alleggerimento: Purge Semplice, Decimazione Temporale, Media Mobile, Purge Adattivo, Rimozione Anomalie, Picco per Bucket, Deduplica Valori
- **Anteprima dry-run** prima di ogni operazione
- **Backup automatico** del database prima di modifiche
- **Interfaccia completamente italiana** accessibile via HA Ingress
- **Cronologia** completa di tutte le operazioni eseguite
- Richiede lo schema moderno di Home Assistant (`states_meta` + `metadata_id`)

## Configurazione rapida

### PostgreSQL / TimescaleDB

```yaml
db_type: postgresql
db_url: postgresql://homeassistant:your_password@postgres:5432/homeassistant
log_level: info
max_rows_per_batch: 5000
```

> TimescaleDB usa lo stesso backend PostgreSQL. Non esiste una voce separata `timescaledb`.

### MariaDB

```yaml
db_type: mariadb
db_host: mariadb
db_port: 3306
db_user: homeassistant
db_password: your_password
db_name: homeassistant
log_level: info
max_rows_per_batch: 5000
```

### SQLite

```yaml
db_type: sqlite
db_path: /config/home-assistant_v2.db
log_level: info
max_rows_per_batch: 5000
```

## Documentazione

Vedi [DOCS.md](DOCS.md) per la documentazione completa.
