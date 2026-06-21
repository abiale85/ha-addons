# HistoLite

**Add-on Home Assistant per la gestione intelligente della history dei sensori**

HistoLite analizza il database SQLite di Home Assistant (`home-assistant_v2.db`) e permette di alleggerire la history registrata da sensori che cambiano continuamente valore: sensori elettrici, velocità di rete, consumi, temperatura, ecc.

---

## Funzionalità

### Dashboard
- Panoramica del database: dimensione totale, numero di stati, distribuzione nelle tabelle
- Top 10 sensori per numero di record (dati dalla cache, aggiornati ogni 5 minuti)
- Grafico distribuzione tabelle e barra top sensori
- Strumenti di manutenzione rapida (cleanup attributi orfani, VACUUM, purge statistics)
- Pulsante **Aggiorna** per forzare il ricalcolo immediato della cache

### Sensori
- **Caricamento istantaneo** dalla cache overview (top sensori già calcolati)
- Pannello dettaglio a lato: statistiche e ultimi valori caricati al click sulla riga
- Ricerca live con caricamento lista veloce (senza GROUP BY)
- **Carica lista completa** su richiesta esplicita (con conteggi aggiornati)
- Selezione multipla per operazioni batch
- **Pulsante Annulla** per interrompere un caricamento pesante
- Pulsante **Modifica storia** su ogni riga → accesso diretto all'editor

### Dettaglio sensore
- Grafico densità record per giorno (fino a 1 anno)
- Ultimi valori registrati
- Stima del risparmio potenziale con le diverse strategie
- Applicazione immediata di qualsiasi strategia

### Modifica storia sensore *(nuovo)*
- Filtro record per **intervallo di date** e/o **range di valori** (min/max)
- Tabella paginata (100 record per pagina) con tutti i valori raw
- Mini-grafico dei valori nel range selezionato
- **Eliminazione singola** record (icona cestino per riga)
- **Eliminazione bulk** con checkbox multipli
- **Pannello Rimozione Anomalie**: definisci criteri → anteprima conteggio → esegui

### Strategie
- Creazione e salvataggio di strategie con nome personalizzato
- 5 tipologie disponibili (vedi sezione strategie)
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
log_level: info
max_rows_per_batch: 5000
```

| Opzione | Descrizione | Default |
|---------|-------------|---------|
| `db_path` | Percorso del database SQLite di HA | `/homeassistant/home-assistant_v2.db` |
| `log_level` | Livello di log (`debug`, `info`, `warning`, `error`) | `info` |
| `max_rows_per_batch` | Record da elaborare per batch (ridurre in caso di problemi) | `5000` |

### 4. Avvia l'add-on

Clicca **Avvia** e poi **Apri interfaccia web** (oppure accedi da Sidebar → HistoLite).

---

## Le 5 strategie

### 1. Purge Semplice
Elimina **tutti** i record più vecchi di N giorni per le entità selezionate.  
Veloce e aggressivo. Consigliato per sensori con storia non necessaria.

**Esempio:** elimina tutto ciò che è più vecchio di 30 giorni per `sensor.energy_power`.

---

### 2. Decimazione Temporale
Mantiene **1 record per ora** per dati oltre N giorni e **1 record per giorno** per dati oltre 2N giorni.  
Conserva la storicità con un impatto contenuto.

**Esempio:** con soglia 14 giorni:
- dati 0–14 giorni: tutti i record
- dati 14–28 giorni: 1 record/ora
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

### 5. Rimozione Anomalie *(nuovo)*
Elimina valori **impossibili o fuori range** senza toccare il resto della storia.  
Utile quando un sensore ha prodotto rilevazioni errate che falsano le statistiche.

**Criteri configurabili (combinati con OR):**

| Criterio | Descrizione | Esempio |
|----------|-------------|---------|
| Valori negativi | Rimuovi tutti i valori < 0 | Sensori di consumo che non possono essere negativi |
| Range assoluto min/max | Rimuovi valori fuori da [min, max] | Temperatura: elimina < -30 o > 100 |
| Deviazione standard (N sigma) | Rimuovi outlier statistici > N σ dalla media | `3.0` elimina i picchi anomali |
| Blacklist stati | Rimuovi stati specifici | `unavailable`, `unknown`, `-1` |

**Flusso consigliato:**
1. Vai su **Sensori → icona matita** accanto al sensore
2. Imposta filtri data/valore per visualizzare il periodo con anomalie
3. Nel pannello **Rimozione Anomalie** definisci i criteri
4. Clicca **Anteprima** per vedere quanti record verranno eliminati (con esempi)
5. Clicca **Esegui rimozione** se il numero è corretto

La strategia è disponibile anche nella pagina Strategie e nel modal "Strategia rapida" della pagina Sensori.

---

## Modifica diretta della storia *(nuovo)*

Accessibile da **Sensori → icona matita** o da **Dettaglio sensore → Modifica storia**.

### Filtri disponibili
- **Da / A**: intervallo di date e ora (datetime-local)
- **Valore min / max**: filtra per range numerico

### Operazioni
- **Anteprima** in tabella paginata con 100 record per pagina
- **Eliminazione singola**: clic sull'icona 🗑 nella riga
- **Eliminazione bulk**: seleziona con checkbox → "Elimina N selezionati"
- **Mini-grafico** a destra mostra l'andamento dei valori nel range filtrato

> ⚠️ Le eliminazioni sono irreversibili.

---

## Anteprima (Dry Run)

Qualsiasi strategia può essere eseguita in **modalità anteprima** prima dell'applicazione reale. L'anteprima mostra il numero stimato di record che verrebbero modificati/eliminati senza toccare il database.

---

## Performance e memoria

HistoLite è progettato per funzionare con database di grandi dimensioni (>10M record, >5GB):

- **Server HTTP**: Gunicorn con 1 worker + 4 thread (`gthread`) — bassa RAM, concorrenza sufficiente
- **Riavvio automatico worker** ogni 200 richieste per liberare memoria frammentata di Python
- **SQLite ottimizzato**: cache limitata a 2MB per connessione, nessun memory-mapping, temp store su file
- **Cache overview** con TTL 5 minuti — evita query GROUP BY ripetute su milioni di righe
- **Indici automatici** su `entity_id` e `last_updated_ts` creati al primo avvio (può richiedere qualche secondo)
- **Timeout query**: 5 secondi per query di lettura, impedisce il blocco del sistema
- **Pulsante Annulla** sulla pagina Sensori per interrompere caricamenti in corso

---

## Note tecniche

- **Database**: solo SQLite (`home-assistant_v2.db`).
- **Compatibilità schema**: gestisce sia lo schema con colonne `last_updated` (datetime, HA < 2023) che quello con `last_updated_ts` (Unix timestamp float, HA ≥ 2023.3).
- **Concorrenza**: usa WAL mode con checkpoint automatico ogni 500 pagine. Le operazioni vengono eseguite in batch per minimizzare il lock sul DB principale di HA.
- **Sicurezza referenziale**: l'operazione di flatten e delete aggiorna i riferimenti `old_state_id` prima di eliminare le righe per evitare dangling references.

---

## Changelog

### 1.1.1
- Fix: argomento Gunicorn `--keep-alive` (era `--keepalive`)
- Fix RAM alta: SQLite `cache_size=-512`, `mmap_size=0`, `temp_store=FILE`
- Fix RAM: Gunicorn con `max_requests=200` riavvia worker periodicamente

### 1.1.0
- **Nuova funzionalità**: pagina **Modifica storia sensore** (`/sensors/<entity>/edit`)
  - Filtro per timeframe e range valori
  - Eliminazione singola e bulk di record
  - Mini-grafico valori nel range filtrato
- **Nuova strategia**: **Rimozione Anomalie** (`outlier_purge`)
  - Criteri: valori negativi, range assoluto, deviazione standard, blacklist stati
  - Disponibile in Strategie, Strategia rapida e pannello edit sensore
- **Pagina Sensori migliorata**:
  - Caricamento istantaneo da cache overview
  - Pannello dettaglio on-demand al click (senza ricaricare tutta la lista)
  - Pulsante Annulla per query in corso
- **Fix blocco Hassio**: indici SQLite automatici, timeout 5s su ogni query
- **Fix RAM alta**: Gunicorn al posto di Flask dev server, PRAGMA SQLite per ridurre footprint

### 1.0.1
- Aggiunto sistema di cache con TTL 5 minuti per `/api/overview`
- Endpoint `/api/overview/refresh` per forzare ricalcolo
- Dashboard: badge "Cached", timestamp ultimo aggiornamento, pulsante Aggiorna
- Script `release.ps1` per automazione versioning

### 1.0.0
- Rilascio iniziale
- 4 strategie: Purge Semplice, Decimazione Temporale, Media Mobile, Purge Adattivo
- Dashboard con statistiche e grafici
- Interfaccia sensori con ricerca e selezione multipla
- Anteprima dry-run
- Backup automatico
- Cronologia operazioni
- Supporto schema HA pre/post 2023


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
log_level: info
max_rows_per_batch: 5000
```

| Opzione | Descrizione | Default |
|---------|-------------|---------|
| `db_path` | Percorso del database SQLite di HA | `/homeassistant/home-assistant_v2.db` |
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
