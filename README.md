<div align="center">

# 🎵 Italiarr

### Scarica e ascolta la tua musica — semplicemente

[![Stars](https://img.shields.io/github/stars/notsim/italiarr?style=social)](https://github.com/notsim/italiarr)
[![License](https://img.shields.io/github/license/notsim/italiarr?style=flat-square)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/notsim/italiarr/main?style=flat-square)](https://github.com/notsim/italiarr/commits/main)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![Top language](https://img.shields.io/github/languages/top/notsim/italiarr?style=flat-square)]()
[![Repo size](https://img.shields.io/github/repo-size/notsim/italiarr?style=flat-square)]()

[![Works on my machine](https://img.shields.io/badge/Works%20on%20my%20machine-Yes-success?style=flat-square)]()
[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-Yes-brightgreen?style=flat-square)]()
[![100% self-hosted](https://img.shields.io/badge/100%25%20self--hosted-Yes-ff69b4?style=flat-square)]()
[![Made in Italy](https://img.shields.io/badge/Made%20in%20Italy-S%C3%AC-27ae60?style=flat-square)]()
[![No cloud](https://img.shields.io/badge/No%20cloud-Only%20your%20server-6f42c1?style=flat-square)]()
[![Powered by yt-dlp](https://img.shields.io/badge/Powered%20by-yt--dlp-FF0000?style=flat-square)](https://github.com/yt-dlp/yt-dlp)

Italiarr è un **server di download musicale** self-hosted: cerchi una canzone o un album, lo scarichi da
YouTube/YouTube Music come **MP3 320kbps con testi sincronizzati (`.lrc`) e copertina**, e lo ritrovi
subito nella tua libreria musicale (Navidrome) pronta per l'ascolto.

</div>

---

## ✨ Perché Italiarr?

| | |
|---|---|
| 🔍 **Cerca ovunque** | Trova canzoni e album su YouTube Music **e** su YouTube (anche i brani che esistono solo su YouTube, come certe uscite speciali) |
| ⏱ **Coda intelligente** | I download partono in **parallelo (3)** ma senza saturare disco e RAM |
| 🎼 **Qualità** | MP3 a **320kbps** con ID3 tag corretti, **copertina integrata** e **testi sincronizzati** |
| 📚 **Album completi** | Scarica un intero album con un click — funziona anche con le playlist/serie solo-YouTube (trailer esclusi) |
| 🧠 **Niente doppioni** | I brani già in libreria vengono riconosciuti e saltati |
| 📊 **Storico** | Ogni download resta registrato con data, stato e origine (manuale o automatico) |
| 🎁 **Automazione** | Con `music_auto.py` trova da sola nuova musica ogni 10 minuti, la scarica e aggiorna le playlist |
| 🔒 **Sicuro** | Password di accesso salvate solo come hash SHA-256 |

---

## 🏗 Come funziona

![Architettura](assets/architecture.svg)

1. **Tu** apri la web app di Italiarr (o l'automazione `music_auto`)
2. **Italiarr** cerca e scarica da **YouTube / YouTube Music** usando `yt-dlp`
3. I file (**MP3 + .lrc + copertine**) finiscono nella cartella musica condivisa
4. **Navidrome** la scansiona automaticamente e la musica è subito ascoltabile

---

## 🚀 Installazione

### Opzione A — Docker (consigliata)

**Dall'immagine pubblicata su GitHub Container Registry** (la GitHub Action del repo la genera
automaticamente a ogni release `v*`):

```bash
docker run -d \
  -p 8686:8686 \
  -e ITALIARR_PASSWORDS="scegli-una-password-forte" \
  -v /percorso/della/mia/musica:/data/music \
  --name italiarr \
  --restart unless-stopped \
  ghcr.io/notsim/italiarr:latest
```

**Oppure dal codice:**

```bash
git clone https://github.com/notsim/italiarr.git
cd italiarr

# imposta la tua password
export ITALIARR_PASSWORDS="scegli-una-password-forte"

docker build -t italiarr .
docker run -d \
  -p 8686:8686 \
  -e ITALIARR_PASSWORDS="$ITALIARR_PASSWORDS" \
  -v /percorso/della/mia/musica:/data/music \
  --name italiarr \
  --restart unless-stopped \
  italiarr
```

Oppure con **docker compose** (vedi [`docker-compose.example.yml`](docker-compose.example.yml)):

```bash
cp docker-compose.example.yml docker-compose.yml
# modifica ITALIARR_PASSWORDS nel file
docker compose up -d
```

### Opzione B — Python diretto

```bash
pip install -r requirements.txt
ITALIARR_PASSWORDS="la-tua-password" uvicorn main:app --host 0.0.0.0 --port 8686
```

Apri poi il browser su **http://localhost:8686** e accedi con la password scelta.

> 💡 **Prima di tutto**: nella cartella musica che monti in `/data/music`, crea una sottocartella
> `Playlists/` (ci finiscono le playlist generate, vedi sotto).

---

## ⚙️ Configurazione

| Variabile | Default | Descrizione |
|---|---|---|
| `ITALIARR_PASSWORDS` | `italiarr` | Password di accesso, separate da virgola se ne vuoi più di una. **Cambiala!** |
| `ITALIARR_MUSIC_DIR` | `/data/music` | Cartella dove vengono salvati i brani |

---

## 🎮 Come si usa

![Pipeline](assets/pipeline.svg)

### 1️⃣ Cerchi
Nella barra di ricerca scrivi un artista, una canzone o un album, poi scegli la scheda **Singoli Brani**
o **Interi Album**. I risultati della tua libreria compaiono per primi.

### 2️⃣ Scarichi
- **Singolo brano** → click su *Scarica MP3*
- **Intero album** → click su *Scarica Album Intero*

I download vanno in coda (3 alla volta) e in fondo vedi lo stato: *in coda → scaricando → completato*.
Se un brano è già in libreria viene marcato come *già presente* senza riscaricarlo.

### 3️⃣ Ascolti
Apri Navidrome (o la tua app musicale preferita che parla Subsonic/Jellyfin):
- la nuova musica è in **Recenti / Aggiunti di recente**
- i **testi sincronizzati** (.lrc) compaiono nella schermata del player
- le copertine sono già integrate nei file

### 📊 Storico
Il tab **Storico** mostra tutti i download: data, stato e origine — **Auto added** (dall'automazione)
o **Downloaded** (fatto a mano). Lo storico è persistente (sopravvive ai riavvii).

---

## 🤖 Automazione (`music_auto.py`)

Vuoi che la libreria si riempia **da sola**? `music_auto.py` ogni 10 minuti:

1. raccoglie i tuoi **preferiti** (Navidrome + Last.fm: loved, top, artisti simili)
2. controlla cosa **manca** in libreria (niente doppioni)
3. cerca su YouTube Music e accoda i brani mancanti (max 5 per run)
4. aggiorna la playlist **`Discover Weekly Auto`** e **`Aggiunti di recente`** in Navidrome

```bash
# configuralo con le tue chiavi (vedi music_auto.py in testa al file)
export ITALIARR_URL="http://localhost:8686"
export ITALIARR_PASSWORD="la-tua-password"
export LASTFM_API_KEY="..."
export LASTFM_SECRET="..."

# nel crontab:
*/10 * * * * python3 /percorso/italiarr/music_auto.py >> /var/log/music_auto.log 2>&1
```

---

## 🗂 Struttura del progetto

```
italiarr/
├── main.py                  # l'app (FastAPI): ricerca, download, coda, storico
├── static/                  # interfaccia web (HTML/CSS/JS)
├── music_auto.py            # automazione opzionale (scoperta musica)
├── Dockerfile               # build Docker
├── requirements.txt         # dipendenze Python
└── docker-compose.example.yml
```

---

## ❓ Domande frequenti

- **Perché la password va in `ITALIARR_PASSWORDS`?** Nel codice sono salvati solo gli hash SHA-256,
  mai la password in chiaro; configurandola via variabile d'ambiente nessun segreto finisce nel repo.
- **Si possono scaricare canzoni che non sono su YouTube Music?** Sì: la ricerca ha un fallback su
  YouTube vero e proprio, quindi trovi anche uscite esclusive.
- **Posso usare un altro server musicale al posto di Navidrome?** Italiarr salva semplicemente i file:
  qualsiasi server che scansiona una cartella (Jellyfin, Plex...) va bene.

## 🤖 Installazione con un agente AI

Stai usando un agente AI (ChatGPT, Claude, Copilot, ecc.) per installare Italiarr?
Apri **[LLM_INSTALL.md](LLM_INSTALL.md)**: è una guida passo-passo scritta apposta per essere
eseguita da un agente, con comandi esatti, verifiche intermedie e risoluzione dei problemi.

---

## 📄 Licenza

[MIT](LICENSE)
