# LLM Install Instruction — Italiarr

> Guida di installazione scritta **per un agente AI** (o per un umano molto metodico).
> Segui i passi in ordine, esegui ogni comando e verifica ogni passo prima di passare al successivo.
> Ogni comando tra ` ``` ` va eseguito in un terminale. Sostituisci i segnaposto `<...>` con i valori reali.

---

## 1. Obiettivo

Installare **Italiarr**: un server web self-hosted che cerca e scarica musica da YouTube/YouTube Music
come MP3 (320kbps) con testi sincronizzati `.lrc`, salvandola in una cartella locale.

- App web su porta **8686**
- Password di accesso configurabile via variabile d'ambiente
- I brani finiscono nella cartella montata su `/data/music`

---

## 2. Prerequisiti

- **Docker** (percorso consigliato) **oppure** Python 3.11+ con `pip`
- Accesso a Internet (serve per scaricare da YouTube e per `pip install` / `docker build`)
- ~2 GB di spazio libero

Verifica con:

```bash
docker --version          # atteso: Docker version 2x.x.x
git --version             # atteso: git version 2.x
```

> Se Docker non è installato, vai alla sezione 4 (Python diretto).

---

## 3. Installazione con Docker (consigliata)

### 3.1 Scegli una password (obbligatorio)

```bash
export ITALIARR_PASSWORDS="una-password-lunga-e-casuale"
```

> ⚠️ NON usare il default `italiarr`. Se non imposti la variabile, l'app accetta la password `italiarr`.

### 3.2 Scarica il codice

```bash
git clone https://github.com/notsim/italiarr.git
cd italiarr
```

### 3.3 Crea la cartella della musica

```bash
mkdir -p music/Playlists
```

### 3.4 Build dell'immagine

```bash
docker build -t italiarr .
```

Verifica (atteso: `Successfully tagged italiarr:latest`):

```bash
docker images | grep italiarr
```

### 3.5 Avvia il container

```bash
docker run -d \
  --name italiarr \
  -p 8686:8686 \
  -e ITALIARR_PASSWORDS="$ITALIARR_PASSWORDS" \
  -v "$PWD/music:/data/music" \
  --restart unless-stopped \
  italiarr
```

Verifica (atteso: `Up ...`):

```bash
docker ps --filter name=italiarr
```

### 3.6 Vai alla verifica (sezione 5)

---

## 4. Installazione senza Docker (Python diretto)

### 4.1 Prerequisiti di sistema

```bash
python3 --version        # atteso: Python 3.11 o superiore
pip3 --version
ffmpeg -version          # atteso: ffmpeg version ...  (obbligatorio per yt-dlp)
```

Se `ffmpeg` manca: `sudo apt-get install -y ffmpeg` (Debian/Ubuntu) o equivalente.

### 4.2 Scarica e installa

```bash
git clone https://github.com/notsim/italiarr.git
cd italiarr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p music/Playlists
```

### 4.3 Avvia

```bash
export ITALIARR_PASSWORDS="una-password-lunga-e-casuale"
ITALIARR_MUSIC_DIR="$PWD/music" uvicorn main:app --host 0.0.0.0 --port 8686
```

> Tieni il terminale aperto. Per fermare: `Ctrl+C`.

---

## 5. Verifica dell'installazione

Esegui questi controlli **in quest'ordine**. Se uno fallisce, fermati e risolvi prima di continuare.

### 5.1 La web app risponde

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8686/italiarr/
```

Atteso: `200`.

### 5.2 Il login funziona

```bash
curl -s -c /tmp/nd.txt -X POST http://localhost:8686/api/login \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$ITALIARR_PASSWORDS\"}"
```

Atteso: `{"status":"ok",...}` (un JSON che contiene `"status":"ok"`).

> Se risponde `401` o `{"status":"error"}`: la password non è quella della variabile.
> Controlla che la variabile sia esportata nello stesso terminale del server.

### 5.3 Le API autenticate rispondono

```bash
curl -s -b /tmp/nd.txt http://localhost:8686/api/downloads
```

Atteso: un JSON con `"downloads":[]` (o una lista).

### 5.4 Prova un download reale (opzionale ma raccomandato)

Scegli un video breve qualunque e usa il suo ID (`VIDEO_ID`) con:

```bash
curl -s -b /tmp/nd.txt -X POST http://localhost:8686/api/download \
  -H "Content-Type: application/json" \
  -d "{\"videoId\":\"$VIDEO_ID\",\"title\":\"Brano di prova\",\"artist\":\"Artista\",\"album\":\"Singoli\"}"
```

Atteso: `{"status":"ok","taskId":"..."}`. Poi controlla che compaia il file:

```bash
sleep 20
find music -name "*.mp3" | head
```

> Se non appare entro un paio di minuti, guarda i log: `docker logs italiarr --tail 50`.

---

## 6. Configurazione

| Variabile | Default | Effetto |
|---|---|---|
| `ITALIARR_PASSWORDS` | `italiarr` | Password di accesso (più password separate da virgola). **Da cambiare.** |
| `ITALIARR_MUSIC_DIR` | `/data/music` | Cartella di destinazione dei brani (solo in modalità Python; in Docker è il volume). |

---

## 7. Risoluzione dei problemi

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| `401` al login | Password errata / variabile non esportata | Riesporta `ITALIARR_PASSWORDS` e riavvia |
| Porta 8686 occupata | Altro processo | Cambia porta: `-p 8687:8686` e usa `localhost:8687` |
| I download falliscono con `ffmpeg` non trovato | `ffmpeg` assente | In Python: installa ffmpeg. In Docker: è già incluso |
| `docker build` lento | Download delle dipendenze | Normale la prima volta |
| I brani non compaiono nella libreria esterna | Navidrome non è configurato | Italiarr salva solo file: punta Navidrome/Jellyfin alla stessa cartella |

---

## 8. Automazione opzionale (`music_auto.py`)

Se vuoi che la libreria si riempia da sola ogni 10 minuti:

```bash
export ITALIARR_URL="http://localhost:8686"
export ITALIARR_PASSWORD="$ITALIARR_PASSWORDS"
export ITALIARR_MUSIC_DIR="$PWD/music"          # stessa cartella della sezione 4
export NAVIDROME_DB="/path/to/navidrome.db"     # DB di Navidrome (opzionale)
export LASTFM_API_KEY="..."                     # opzionale (Last.fm)
export LASTFM_SECRET="..."                      # opzionale (Last.fm)
export LASTFM_USER="..."                        # opzionale (Last.fm)
```

E nel crontab:

```cron
*/10 * * * * cd /percorso/italiarr && python3 music_auto.py >> /var/log/music_auto.log 2>&1
```

> `music_auto.py` usa un lock file per non sovrapporsi: se un run è ancora attivo, salta.

---

## 9. Note di sicurezza per l'agente

- **CHIEDI SEMPRE** all'utente cosa vuole fare, se vuole eseguire l'installazione tramite Docker o Python
- **NON PROSEGUIRE** senza sapere tutti i dati necessari dall'utente
- **MAI lasciare le password hardcoded, usa SEMPRE le variabili d'ambiente!**
-  **MAI** MAI lasciare le password default.
- Il default `ITALIARR_PASSWORDS=italiarr` va sempre sostituito.
- Le password in `main.py` sono conservate solo come hash SHA-256 generati a runtime.
- Non esporre la porta 8686 su Internet senza un reverse proxy con TLS (es. Cloudflare Tunnel, Caddy, nginx).
