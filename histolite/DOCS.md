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
- 7 tipologie disponibili (vedi sezione strategie)
- Selezione entità con autocompletamento
- Supporto wildcard nelle entità, ad esempio `device_tracker.*` o `sensor.casa_?`
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

> Nota: puoi specificare anche wildcard nelle entità. Al momento dell'esecuzione HistoLite espande pattern come `device_tracker.*` in tutti gli entity_id corrispondenti e rimuove i duplicati.
>
> Nella pagina **Strategie** ogni voce mostra un'illustrazione schematica *Prima / Dopo* che rende immediato l'effetto sui dati, più una nota sulle relazioni con le altre strategie.

### 1. Purge Semplice
Elimina **tutti** i record più vecchi di N giorni per le entità selezionate.  
Veloce e aggressivo. Consigliato per sensori con storia non necessaria.

**Esempio:** elimina tutto ciò che è più vecchio di 30 giorni per `sensor.energy_power`.

È il caso particolare del *Purge Adattivo* con la sola fascia di eliminazione.

---

### 2. Purge Adattivo
Gestione **multi-fascia** completamente personalizzabile, in un unico passaggio:
- < Soglia 1: mantieni tutto
- Soglia 1 → Soglia 2: appiattimento orario (il record tenuto assume la media pesata del bucket)
- Soglia 2 → Soglia 3: appiattimento giornaliero
- > Soglia 3: eliminazione completa

Offre il massimo controllo sulla storicità in funzione dell'età del dato. Impostando una sola fascia equivale a una media mobile oraria/giornaliera; con due fasce a una decimazione temporale.

**Esempio:** con soglie 7 / 30 / 365 giorni:
- dati 0–7 giorni: tutti i record
- dati 7–30 giorni: 1 record/ora, con valore medio del bucket
- dati 30–365 giorni: 1 record/giorno
- dati > 365 giorni: eliminati

---

### 3. Rimozione Anomalie *(nuovo)*
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

- **Database supportati**: SQLite, PostgreSQL e MariaDB. TimescaleDB usa lo stesso backend PostgreSQL senza una configurazione separata.
- **Compatibilità schema**: richiede lo schema moderno di Home Assistant, con `states_meta` e `metadata_id`, e rifiuta il supporto per i vecchi schemi legacy o in migrazione.
- **Concorrenza**: per SQLite usa WAL mode con checkpoint automatico ogni 500 pagine; per PostgreSQL/MariaDB il backend delega la gestione delle transazioni al motore SQL.
- **Sicurezza referenziale**: l'operazione di flatten e delete aggiorna i riferimenti `old_state_id` prima di eliminare le righe per evitare dangling references.
- **Persistenza**: strategie salvate, cronologia job e cache stanno nel volume privato dell'add-on (`/data/histolite`). Disinstallando **con** "rimuovi dati" vengono cancellate; disinstallando **senza**, restano e si ritrovano al reinstall. La **configurazione del database** (backend, URL/host…) è invece un'*opzione* dell'add-on gestita dal Supervisor: per conservarla tra disinstallazioni serve un backup di Home Assistant.

---

## Changelog

### 2.6.0
- **Persistenza spostata in `/data`**: strategie, cronologia e cache erano salvate in `/config/histolite`, cioè nella cartella condivisa di Home Assistant, che sopravvive a *qualsiasi* disinstallazione (anche con "rimuovi dati"). Ora stanno in `/data/histolite`, il volume privato dell'add-on, così la disinstallazione con "rimuovi dati" le elimina davvero e quella con "mantieni dati" le conserva. I file eventualmente presenti nel vecchio percorso vengono spostati automaticamente al primo avvio.

### 2.5.0
- **Strategie ridotte da 7 a 5**: rimosse *Media Mobile* e *Decimazione Temporale*, che erano casi particolari del *Purge Adattivo* (stesso motore `flatten_entity`): una fascia = media mobile, due fasce = decimazione temporale. Le strategie salvate di questi due tipi non sono più eseguibili — ricreale come *Purge Adattivo* con le soglie desiderate. Restano *Purge Semplice*, *Purge Adattivo*, *Rimozione Anomalie*, *Picco per Bucket*, *Deduplica Valori*.
- **DOCS**: rimossa una vecchia copia duplicata del documento in coda al file.

### 2.4.0
- **Strategie – schema visivo**: ogni strategia nella pagina *Strategie* ora mostra un'illustrazione schematica "Prima / Dopo" (pallini su una linea temporale) che rende immediato capire come i record vengono ridotti o modificati.
- **Strategie – sovrapposizioni**: aggiunta per ogni strategia una nota "Relazione con le altre". In sintesi: *Media Mobile*, *Decimazione Temporale* e *Purge Semplice* sono casi particolari di *Purge Adattivo* (stesso motore `flatten`/`purge`); *Picco per Bucket*, *Rimozione Anomalie* e *Deduplica Valori* sono invece indipendenti e complementari.

### 2.3.2
- **Fix PostgreSQL/MariaDB**: le query eseguivano `PRAGMA busy_timeout` (istruzione solo-SQLite) su qualsiasi backend, causando `syntax error at or near "PRAGMA"` su PostgreSQL. Ora la PRAGMA viene applicata solo su SQLite. *(Nota: il supporto PostgreSQL/MariaDB resta incompleto — vedi README.)*

### 2.3.1
- **Feedback UI**: quando il caricamento dei dati di una pagina fallisce (es. Dashboard o Dettaglio sensore) ora compare un banner d'errore persistente con pulsante "Riprova", invece di un toast che sparisce dopo pochi secondi lasciando spinner o trattini a schermo.
- **Fix**: `analyze_sensor` cattura le eccezioni e restituiva `{"error": ...}` con HTTP 200; l'endpoint `/api/sensors/<entity_id>` ora propaga 404/500 così il client tratta il caso come errore.

### 2.3.0
- **Feedback UI**: le strategie lanciate in background non davano alcun riscontro in caso di fallimento (l'utente doveva controllare i log o la pagina Job). Ora `/api/strategy-status` espone l'esito dell'ultima esecuzione (`last_result`) e l'interfaccia mostra un toast di successo/errore al termine, sulle pagine Strategie, Sensori e Dettaglio sensore.
- **Fix**: la pagina Sensori mostrava uno spinner infinito senza messaggi se anche la query di fallback (`/api/sensors/list`) falliva; ora viene mostrato l'errore in tabella e come toast.

### 2.2.2
- **Pulizia**: rimosse le righe `DEBUG:` temporanee in `run.sh` (dump di `db_type`/`db_path`/`db_url`/`db_host`/`db_name` ad ogni avvio) usate per diagnosticare il problema di lettura delle opzioni.

### 2.2.1
- **Fix critico**: il rilevamento dello schema classificava come "transitional" (e quindi rifiutava l'avvio) ogni database HA moderno in cui la colonna `states.entity_id` è ancora presente. HA mantiene quella colonna vestigiale nullable anche dopo la migrazione a `states_meta`, quindi la sua presenza è normale. Ora lo schema è "modern" quando esistono `states_meta` e `states.metadata_id`, indipendentemente da `states.entity_id`.

### 2.2.0
- **Fix critico**: `run.sh` leggeva le opzioni da `/config/options.json`, percorso inesistente: il Supervisor le scrive in `/data/options.json`. Di conseguenza `db_type` ricadeva sempre sul default `sqlite` e ogni backend PostgreSQL/MariaDB configurato veniva ignorato.
- **Fix**: parsing di `options.json` con JSON reale (Python) invece di `grep`, che falliva sugli spazi dopo i due punti generati dal Supervisor.
- **Sicurezza**: rimossa la rimozione automatica dei file `home-assistant_v2.db` nella config dir quando il backend non è SQLite: quei file appartengono a Home Assistant, non all'add-on.

### 2.1.1
- **Fix**: rilevata e ignorata la configurazione SQLite legacy quando il backend selezionato è PostgreSQL/MariaDB
- **Fix**: evitato l’avvio con `db_path` obsoleto che puntava a `/config/home-assistant_v2.db`
- **Logging**: aggiunto warning esplicito quando viene rilevata una configurazione non compatibile con il backend attivo

### 2.1.0
- **Documentazione**: aggiunti esempi concreti di configurazione PostgreSQL, MariaDB e SQLite
- **Deployment**: aggiunto un esempio di setup e di `db_url` per PostgreSQL/TimescaleDB in ambiente containerizzato
- **Chiarezza**: rimosso il punto ambiguo su TimescaleDB, che usa il backend PostgreSQL senza opzione separata
- **Compatibilità**: confermato il fail-fast per schema legacy/non supportato

### 2.0.0
- **Nuovo**: supporto a PostgreSQL e MariaDB; TimescaleDB usa lo stesso backend PostgreSQL senza opzione separata
- **Nuovo**: configurazione backend via `db_type`, `db_url`, `db_host`, `db_port`, `db_user`, `db_password`, `db_name`
- **Breaking change**: rimosso il supporto per gli schemi legacy Home Assistant e per lo schema in transizione
- **Sicurezza**: l’add-on fallisce all’avvio se rileva uno schema non supportato invece di operare su dati incompatibili
- **Documentazione**: aggiornate configurazione e istruzioni per i database supportati

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
