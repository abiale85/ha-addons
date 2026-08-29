# HistoLite — Add-on Home Assistant

Gestione intelligente della history dei sensori Home Assistant.  
Supporta SQLite, PostgreSQL, MariaDB e TimescaleDB (via PostgreSQL), controlla automaticamente lo schema moderno di Home Assistant e blocca l'avvio se trova un backend o una struttura legacy non supportata.

## Caratteristiche principali

- **Dashboard** con Top 10 sensori, dimensione DB e distribuzione tabelle
- **7 strategie** di alleggerimento: Purge Semplice, Decimazione Temporale, Media Mobile, Purge Adattivo, Rimozione Anomalie, Picco per Bucket, Deduplica Valori
- **Anteprima dry-run** prima di ogni operazione
- **Backup automatico** del database prima di modifiche
- **Interfaccia completamente italiana** accessibile via HA Ingress
- **Cronologia** completa di tutte le operazioni eseguite
- Richiede lo schema moderno di Home Assistant (`states_meta` + `metadata_id`)

## Documentazione

Vedi [DOCS.md](DOCS.md) per la documentazione completa.
