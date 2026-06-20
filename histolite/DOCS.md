# HistoLite

**Add-on Home Assistant per la gestione intelligente della history dei sensori**

HistoLite analizza il database SQLite di Home Assistant (`home-assistant_v2.db`) e permette di alleggerire la history registrata da sensori che cambiano continuamente valore: sensori elettrici, velocità di rete, consumi, temperatura, ecc.

---

## Funzionalità

### Dashboard
- Panoramica del database: dimensione totale, numero di stati, distribuzione nelle tabelle
- Top 10 sensori per numero di record registrati
- Grafico distribuzione e tabella dettagliata
- Strumenti di manutenzione rapida (cleanup attributi orfani, VACUUM, purge statistics)

### Sensori
- Elenco completo di tutti i sensori tracciati con conteggio record
- Ricerca live e ordinamento
- Selezione multipla per operazioni batch
- Barra visiva della proporzione record

### Dettaglio sensore
- Grafico densità record per giorno (fino a 1 anno)
- Ultimi valori registrati
- Stima del risparmio potenziale con le diverse strategie
- Applicazione immediata di qualsiasi strategia

### Strategie
- Creazione e salvataggio di strategie con nome personalizzato
- Selezione entità con autocompletamento
- Esecuzione one-click delle strategie salvate

### Cronologia
- Log completo di tutte le operazioni eseguite
- Dettaglio parametri, entità coinvolte, record eliminati, backup creati

---

## Installazione

### 1. Aggiungi il repository

Nel Supervisor di Home Assistant, vai su **Add-on Store → Repository** e aggiungi l'URL del tuo repository.

### 2. Installa HistoLite

Cerca "HistoLite" nell'Add-on Store e clicca **Installa**.

### 3. Configura le opzioni

```yaml
db_path: /homeassistant/home-assistant_v2.db
backup_before_purge: true
backup_path: /backup
log_level: info
max_rows_per_batch: 5000
```

| Opzione | Descrizione | Default |
|---------|-------------|---------|
| `db_path` | Percorso del database SQLite di HA | `/homeassistant/home-assistant_v2.db` |
| `backup_before_purge` | Crea backup prima di ogni operazione distruttiva | `true` |
| `backup_path` | Dove salvare i backup | `/backup` |
| `log_level` | Livello di log (`debug`, `info`, `warning`, `error`) | `info` |
| `max_rows_per_batch` | Record da elaborare per batch (ridurre in caso di problemi) | `5000` |

### 4. Avvia l'add-on

Clicca **Avvia** e poi **Apri interfaccia web** (oppure accedi da Sidebar → HistoLite).

---

## Le 4 strategie

### 1. Purge Semplice
Elimina **tutti** i record più vecchi di N giorni per le entità selezionate.  
Veloce e aggressivo. Consigliato per sensori con storia non necessaria.

**Esempio:** elimina tutto ciò che è più vecchio di 30 giorni per `sensor.energy_power`.

---

### 2. Decimazione Temporale
Mantiene **1 record per ora** per dati oltre N giorni e **1 record per giorno** per dati oltre 2N giorni.  
Conserva la storicità con un impatto contenuto.

**Esempio:** con soglia 14 giorni:
- dati 0-14 giorni: tutti i record
- dati 14-28 giorni: 1 record/ora
- dati > 28 giorni: 1 record/giorno

---

### 3. Media Mobile
Sostituisce i record originali con la **media del bucket** temporale (orario o giornaliero).  
**Solo per sensori numerici.** Preserva la tendenza media eliminando le variazioni istantanee.

**Esempio:** 86 record di `sensor.power` in un'ora (da 1500W a 2300W) diventano 1 record con valore medio 1900W.

---

### 4. Purge Adattivo
Gestione **multi-fascia** completamente personalizzabile:
- < Soglia 1: mantieni tutto
- Soglia 1 → Soglia 2: appiattimento orario
- Soglia 2 → Soglia 3: appiattimento giornaliero  
- > Soglia 3: eliminazione completa

Offre il massimo controllo sulla storicità in funzione dell'età del dato.

---

## Anteprima (Dry Run)

Qualsiasi strategia può essere eseguita in **modalità anteprima** prima dell'applicazione reale. L'anteprima mostra il numero stimato di record che verrebbero modificati/eliminati senza toccare il database.

---

## Backup

Se `backup_before_purge: true`, prima di ogni operazione distruttiva viene creato automaticamente un backup del database nella cartella `/backup/histolite_backup_YYYYMMDD_HHMMSS.db`.

---

## Note tecniche

- **Database**: solo SQLite (`home-assistant_v2.db`). Supporto per altri DB previsto in versioni future.
- **Compatibilità schema**: gestisce sia lo schema con colonne `last_updated` (datetime) che quello con `last_updated_ts` (Unix timestamp float) introdotto in HA 2023.x.
- **Concorrenza**: usa WAL mode di SQLite. Le operazioni vengono eseguite in batch per minimizzare il lock.
- **Sicurezza**: l'operazione di flatten aggiorna i riferimenti `old_state_id` prima di eliminare le righe per evitare dangling references. Gli attributi orfani vengono gestiti dalla funzione di manutenzione dedicata.

---

## Changelog

### 1.0.0
- Rilascio iniziale
- 4 strategie: Purge Semplice, Decimazione Temporale, Media Mobile, Purge Adattivo
- Dashboard con statistiche e grafici
- Interfaccia sensori con ricerca e selezione multipla
- Anteprima dry-run
- Backup automatico
- Cronologia operazioni
- Supporto schema HA pre/post 2023
