import os, subprocess, uuid, glob, time, threading, queue, hashlib, json, re, sqlite3, urllib.request, urllib.parse
from fastapi import FastAPI, BackgroundTasks, Query, Request, Response, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from ytmusicapi import YTMusic
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, APIC, ID3NoHeaderError

app = FastAPI(title="Italiarr")
ytmusic = YTMusic()
# Folder where downloaded music is saved (mounted volume in Docker)
DOWNLOAD_DIR = os.environ.get("ITALIARR_MUSIC_DIR", "/data/music")
active_sessions = set()
downloads = {}
_state_lock = threading.RLock()

# ------------------------------------------------------------------ history --
# Persistent download history (survives container restarts). Lives on the
# /data/music volume as a hidden dotfile; Navidrome ignores non-media files.
HISTORY_DB = os.path.join(DOWNLOAD_DIR, ".italiarr_history.db")


def _history_db():
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, artist TEXT, title TEXT, album TEXT, source TEXT, status TEXT)""")
    conn.commit()
    return conn


def record_history(artist, title, album, source, status):
    try:
        conn = _history_db()
        conn.execute("INSERT INTO history (ts, artist, title, album, source, status) VALUES (?,?,?,?,?,?)",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), artist, title, album, source, status))
        conn.commit()
        conn.close()
    except Exception as e:
        _log("history write error: %s" % e)


def _history_list(limit=100):
    try:
        conn = _history_db()
        rows = conn.execute("SELECT ts, artist, title, album, source, status FROM history ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        conn.close()
        return [{"ts": r[0], "artist": r[1], "title": r[2], "album": r[3],
                 "source": r[4], "status": r[5]} for r in rows]
    except Exception:
        return []

# Accepted login passwords, stored ONLY as SHA-256 digests (no plaintext on disk).
# Set them with the ITALIARR_PASSWORDS env var (comma separated).
# Default (change it!): "italiarr"
AUTH_PASSWORD_HASHES = {
    hashlib.sha256(p.strip().encode()).hexdigest()
    for p in os.environ.get("ITALIARR_PASSWORDS", "italiarr").split(",") if p.strip()
}
# Download queue with a small pool of workers: 3 parallel yt-dlp jobs are far
# below the ~12 that saturated the host's disk/RAM, but drain the queue ~3x
# faster than the original single worker.
PARALLEL_DOWNLOADS = 3
_download_queue = queue.Queue()
_queue_workers_started = 0


def _queue_worker():
    while True:
        item = _download_queue.get()
        if item is None:
            break
        task_id, artist, album, video_id, title, source = item
        try:
            download_task(task_id, artist, album, video_id, title, source)
        except Exception:
            pass
        finally:
            _download_queue.task_done()


def _enqueue(task_id, artist, album, video_id, title, source="manual"):
    global _queue_workers_started
    with _state_lock:
        while _queue_workers_started < PARALLEL_DOWNLOADS:
            _queue_workers_started += 1
            threading.Thread(target=_queue_worker,
                             name="download-worker-%d" % _queue_workers_started,
                             daemon=True).start()
    _download_queue.put((task_id, artist, album, video_id, title, source))

def embed_id3_tags(mp3_path, title, artist, album):
    try:
        try:
            audio = ID3(mp3_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.add(TIT2(encoding=3, text=title))
        audio.add(TPE1(encoding=3, text=artist))
        audio.add(TPE2(encoding=3, text=artist))
        audio.add(TALB(encoding=3, text=album or "Singoli"))
        audio.save(mp3_path)
    except Exception as e:
        print(f"Error embedding ID3 tags for {mp3_path}: {e}")


_LRC_TIME = re.compile(r"^\[\d{1,2}:\d{2}([.:]\d+)?\]")


def _lrc_is_plain(path):
    """True if the .lrc has no timestamps (plain text, not synced)."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read().splitlines()[:25]
        return not any(_LRC_TIME.match(line.strip()) for line in head)
    except Exception:
        return True


def fetch_synced_lrc(artist, title, album=None):
    """Fetch properly synchronized lyrics from LRCLIB. Returns LRC text or None."""
    try:
        q = urllib.parse.urlencode({"artist_name": artist, "track_name": title,
                                    "album_name": album or ""})
        req = urllib.request.Request("https://lrclib.net/api/search?" + q,
                                     headers={"User-Agent": "italiarr/1.0 (music server)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            results = json.loads(r.read().decode())
        for res in results:
            synced = res.get("syncedLyrics")
            if synced:
                return synced
        return None
    except Exception:
        return None


def ensure_cover(mp3_path, video_id):
    """Embed album art if missing, from the YouTube video thumbnail (JPEG)."""
    try:
        t = ID3(mp3_path)
        if any(k.startswith("APIC") for k in t.keys()):
            return
        data = None
        for tmpl in ("https://i.ytimg.com/vi/%s/maxresdefault.jpg",
                     "https://i.ytimg.com/vi/%s/hqdefault.jpg"):
            try:
                req = urllib.request.Request(tmpl % video_id, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                if data and len(data) > 1000:
                    break
                data = None
            except Exception:
                data = None
        if not data:
            return
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
        t.save(mp3_path)
    except Exception:
        pass

def check_auth(request: Request):
    token = request.cookies.get("italiarr_session")
    if token not in active_sessions:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    return True

@app.get("/Italiarr")
@app.get("/italiarr")
@app.get("/lidarr")
def redirect_trailing():
    return RedirectResponse(url="/Italiarr/")

@app.get("/Italiarr/")
@app.get("/italiarr/")
@app.get("/lidarr/")
@app.get("/")
def serve_index():
    return FileResponse("/app/static/index.html")

@app.get("/Italiarr/{filename}")
@app.get("/italiarr/{filename}")
@app.get("/lidarr/{filename}")
def serve_subpath_static(filename: str):
    file_path = os.path.join("/app/static", filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return FileResponse("/app/static/index.html")

@app.post("/api/login")
@app.post("/Italiarr/api/login")
@app.post("/italiarr/api/login")
@app.post("/lidarr/api/login")
def login(data: dict, response: Response):
    password = data.get("password", "").strip()
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    pw_hash_lower = hashlib.sha256(password.lower().encode()).hexdigest()
    if pw_hash in AUTH_PASSWORD_HASHES or pw_hash_lower in AUTH_PASSWORD_HASHES:
        session_token = str(uuid.uuid4())
        active_sessions.add(session_token)
        response.set_cookie(key="italiarr_session", value=session_token, httponly=True, max_age=31536000, samesite="lax", path="/")
        return {"status": "ok"}
    return JSONResponse(status_code=401, content={"status": "error", "message": "Password errata"})

@app.post("/api/logout")
@app.post("/Italiarr/api/logout")
@app.post("/italiarr/api/logout")
@app.post("/lidarr/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("italiarr_session")
    if token in active_sessions:
        active_sessions.remove(token)
    response.delete_cookie(key="italiarr_session", path="/")
    return {"status": "ok"}

@app.get("/api/auth_status")
@app.get("/Italiarr/api/auth_status")
@app.get("/italiarr/api/auth_status")
@app.get("/lidarr/api/auth_status")
def auth_status(request: Request):
    token = request.cookies.get("italiarr_session")
    return {"authenticated": token in active_sessions}

_SAFE_DIRNAME_KEEP = " _-@+?!"  # chars allowed in folder names besides alnum

# stage-name aliases (thasup = tha Supreme) shared by find_existing, the
# get_song sanity guard and folder matching
ARTIST_ALIASES = {
    "thasup": {"tha supreme"},
    "tha supreme": {"thasup"},
}
# canonical spelling used when tagging/moving tracks
ARTIST_CANONICAL = {"tha supreme": "thasup"}


def _tk(s):
    """Lightweight title key: lowercase, collapse non-alnum to spaces."""
    return " ".join(re.sub(r"\W+", " ", (s or "").lower()).split())


def artists_match(a, b):
    """True if two artist strings plausibly refer to the same artist:
    containment either way, or a known stage-name alias."""
    x, y = _tk(a), _tk(b)
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    return y in ARTIST_ALIASES.get(x, set()) or x in ARTIST_ALIASES.get(y, set())


def safe_dirname(name, fallback):
    """Sanitize a name for use as a folder name. Keeps letters/digits and a
    small set of punctuation (including @ + ? !, so albums like
    'c@ra++ere s?ec!@le' keep their real spelling); strips commas, '&', '/',
    parentheses etc. — consistent with existing library folders."""
    s = "".join(ch for ch in (name or "") if ch.isalnum() or ch in _SAFE_DIRNAME_KEEP).strip()
    return s or fallback


def find_existing(artist, title):
    safe_title = _tk(title)
    if not safe_title:
        return None
    # title without the "feat. X" part, so "s!r! (feat. Lazza & Sfera Ebbasta)"
    # matches a file named just "s!r!"
    title_core = re.sub(r"\b(feat|featuring|ft)\b.*$", "", safe_title).strip()
    # same sanitization as download_task's safe_artist/safe_album so folder
    # names ("thasup mara sattei") match artist strings ("thasup, Mara Sattei")
    safe_artist = safe_dirname(artist, "Vari").lower()
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for f in files:
            if f.endswith(".mp3"):
                fname = _tk(f[:-4])
                if safe_title in fname or (title_core and title_core in fname):
                    parent = os.path.basename(os.path.dirname(root)).lower()
                    grandparent = os.path.basename(root).lower()
                    if safe_artist in parent or safe_artist in grandparent or safe_artist in root.lower():
                        return os.path.join(root, f)
                    # stage-name variant (thasup vs tha supreme)
                    for alias in ARTIST_ALIASES.get(safe_artist, set()):
                        if alias in parent or alias in grandparent or alias in root.lower():
                            return os.path.join(root, f)
                    if len(safe_title) > 10 and (safe_title in fname or (title_core and title_core in fname)):
                        return os.path.join(root, f)
    return None

_AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".flac", ".opus", ".ogg")

def search_local_library(q, limit=10):
    """Search tracks already in the local library (filename / artist / album)."""
    results = []
    ql = q.lower().strip()
    if not ql:
        return results
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        if root == DOWNLOAD_DIR:
            continue
        album = os.path.basename(root)
        artist = os.path.basename(os.path.dirname(root))
        for f in files:
            if not f.lower().endswith(_AUDIO_EXTS):
                continue
            stem = os.path.splitext(f)[0]
            if ql in (stem + " " + artist + " " + album).lower():
                results.append({
                    "videoId": None,
                    "title": stem,
                    "artist": artist or "Sconosciuto",
                    "album": album or "Singolo",
                    "duration": None,
                    "thumbnail": "",
                    "inLibrary": True,
                    "source": "local",
                    "path": os.path.relpath(os.path.join(root, f), DOWNLOAD_DIR),
                })
                if len(results) >= limit:
                    return results
    return results

def download_task(task_id, artist, album, video_id, title, source="manual"):
    max_retries = 10
    retry_count = 0

    while retry_count < max_retries:
        if task_id not in downloads or downloads[task_id].get("cancelled"):
            return

        try:
            existing = find_existing(artist, title)
            if existing:
                downloads[task_id]["status"] = "exists"
                downloads[task_id]["progress"] = 100
                downloads[task_id]["message"] = "Gia presente: " + os.path.basename(existing)
                record_history(artist, title, album, source, "exists")
                return

            downloads[task_id]["status"] = "downloading"
            downloads[task_id]["progress"] = 30
            safe_artist = safe_dirname(artist, "Vari")
            safe_album = safe_dirname(album, "Singoli")
            out_dir = os.path.join(DOWNLOAD_DIR, safe_artist, safe_album)
            os.makedirs(out_dir, exist_ok=True)
            url = "https://www.youtube.com/watch?v=" + video_id
            out_template = os.path.join(out_dir, "%(title)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "0",
                "--add-metadata",
                "--embed-thumbnail",
                "--write-subs",
                "--sub-langs", "all",
                "--convert-subs", "lrc",
                "-o", out_template,
                url
            ]
            downloads[task_id]["progress"] = 60
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            # Find the downloaded MP3 and force-embed correct ID3 tags
            mp3_files = glob.glob(os.path.join(out_dir, "*.mp3"))
            newest = max(mp3_files, key=os.path.getctime) if mp3_files else None
            if newest:
                # authoritative title/artist from YouTube Music (fixes wrong tags),
                # but only when it plausibly matches the downloaded file/video —
                # get_song sometimes returns unrelated metadata under load
                real_title, real_artist = title, artist
                try:
                    song = ytmusic.get_song(video_id)
                    vd = (song or {}).get("videoDetails") or {}
                    fname_key = _tk(os.path.basename(newest))
                    if vd.get("title") and (_tk(vd["title"]) in fname_key or fname_key in _tk(vd["title"])):
                        real_title = vd["title"]
                    if vd.get("author") and (artists_match(vd["author"], artist) or
                                             _tk(vd["author"]) in fname_key):
                        real_artist = vd["author"]
                except Exception:
                    pass
                embed_id3_tags(newest, real_title, real_artist, album)
                ensure_cover(newest, video_id)

            # Synchronized lyrics: LRCLIB first, fallback to existing/plain
            if newest:
                lrc_path = newest.rsplit(".", 1)[0] + ".lrc"
                synced = fetch_synced_lrc(artist, title, safe_album)
                if synced:
                    try:
                        with open(lrc_path, "w", encoding="utf-8") as f:
                            f.write(synced)
                    except Exception:
                        pass
                elif not os.path.exists(lrc_path) or _lrc_is_plain(lrc_path):
                    # fallback: plain lyrics from YouTube Music
                    try:
                        watch = ytmusic.get_watch_playlist(video_id)
                        if watch and watch.get("lyrics"):
                            lyrics_id = watch["lyrics"]
                            lyrics_data = ytmusic.get_lyrics(lyrics_id)
                            if lyrics_data and lyrics_data.get("lyrics"):
                                with open(lrc_path, "w", encoding="utf-8") as f:
                                    f.write(lyrics_data["lyrics"])
                    except Exception:
                        pass

            if res.returncode == 0:
                downloads[task_id]["status"] = "completed"
                downloads[task_id]["progress"] = 100
                record_history(artist, title, album, source, "completed")
                return
            else:
                err = (res.stderr or "") + (res.stdout or "")
                # permanent failures: retrying would never succeed
                if re.search(r"Sign in to confirm your age|age[ -]restricted|Video unavailable|Private video",
                             err, re.I):
                    downloads[task_id]["status"] = "failed"
                    downloads[task_id]["message"] = "Video non scaricabile (age-restricted/unavailable)"
                    downloads[task_id]["progress"] = 0
                    record_history(artist, title, album, source, "failed")
                    return
                retry_count += 1
                downloads[task_id]["status"] = "retrying"
                downloads[task_id]["message"] = f"Tentativo {retry_count}/{max_retries} fallito, riprovo..."
                downloads[task_id]["progress"] = 10
                time.sleep(3)
        except Exception as e:
            retry_count += 1
            downloads[task_id]["status"] = "retrying"
            downloads[task_id]["message"] = f"Errore, riprovo tra 3s ({retry_count}/{max_retries})"
            downloads[task_id]["progress"] = 10
            time.sleep(3)

    if task_id in downloads and not downloads[task_id].get("cancelled"):
        downloads[task_id]["status"] = "failed"
        downloads[task_id]["message"] = "Fallito dopo 10 tentativi"
        record_history(artist, title, album, source, "failed")

def _log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# tiny in-memory cache for the slow yt-dlp searches (per query, 10 min TTL)
_ytdlp_cache = {}
_ytdlp_cache_lock = threading.Lock()


def _ytdlp_json(args, timeout):
    """Run yt-dlp and return parsed JSON, or None on any failure (logged).
    Results are cached per-query so repeated searches are instant."""
    key = json.dumps(args)
    with _ytdlp_cache_lock:
        hit = _ytdlp_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    t0 = time.time()
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if not out.stdout.strip():
            _log("yt-dlp empty output: %s" % " ".join(args[:2]))
            return None
        data = json.loads(out.stdout)
        with _ytdlp_cache_lock:
            _ytdlp_cache[key] = (time.time(), data)
            if len(_ytdlp_cache) > 200:
                now = time.time()
                for k in [k for k, v in _ytdlp_cache.items() if now - v[0] > 600]:
                    _ytdlp_cache.pop(k, None)
        _log("yt-dlp fetched in %.1fs: %s" % (time.time() - t0, args[0]))
        return data
    except Exception as e:
        _log("yt-dlp error (%s): %s" % (" ".join(args[:2]), e))
        return None


@app.get("/api/search", dependencies=[Depends(check_auth)])
@app.get("/Italiarr/api/search", dependencies=[Depends(check_auth)])
@app.get("/italiarr/api/search", dependencies=[Depends(check_auth)])
@app.get("/lidarr/api/search", dependencies=[Depends(check_auth)])
def search_music(q: str = Query(..., min_length=1), type: str = Query("songs")):
    try:
        if type == "albums":
            ql = q.lower().strip()
            q_tokens = [t for t in re.split(r"\W+", ql) if t]
            albums = []
            # local albums first
            seen_albums = set()
            for root, dirs, files in os.walk(DOWNLOAD_DIR):
                if root == DOWNLOAD_DIR:
                    continue
                album = os.path.basename(root)
                artist = os.path.basename(os.path.dirname(root))
                if ql in (album + " " + artist).lower():
                    key = (artist.lower(), album.lower())
                    if key not in seen_albums:
                        seen_albums.add(key)
                        albums.append({
                            "browseId": None,
                            "title": album,
                            "artist": artist,
                            "year": "",
                            "thumbnail": "",
                            "type": "album",
                            "source": "local",
                            "inLibrary": True,
                        })
            # YouTube Music albums, filtered by relevance: for multi-word queries
            # only albums matching ALL words (title/artist) are relevant; fall
            # back to fewer/no results instead of burying the real album in noise
            results = ytm_albums = ytmusic.search(q, filter="albums")
            if q_tokens:
                def _matches(r):
                    artists = ", ".join([a["name"] for a in r.get("artists",[])]) if r.get("artists") else ""
                    hay = ((r.get("title") or "") + " " + artists).lower()
                    return all(t in hay for t in q_tokens)
                rel = [r for r in results[:15] if _matches(r)]
                if not rel and len(q_tokens) == 1:
                    rel = results[:5]
                # rank: titles starting with the query first (exact matches)
                rel.sort(key=lambda r: 0 if (r.get("title") or "").lower().lstrip().startswith(ql) else 1)
                results = rel
            for r in results:
                artists = ", ".join([a["name"] for a in r.get("artists",[])]) if r.get("artists") else "Sconosciuto"
                thumbnails = r.get("thumbnails",[])
                thumb = thumbnails[-1]["url"] if thumbnails else ""
                albums.append({
                    "browseId": r.get("browseId"),
                    "title": r.get("title",""),
                    "artist": artists,
                    "year": r.get("year",""),
                    "thumbnail": thumb,
                    "type": "album",
                    "source": "youtube",
                })
            # YouTube-only albums/series (not in the YT Music catalog): fetch the
            # playlist search and a plain search in parallel, then append
            playlist_data = {}
            search_data = {}

            def _fetch_playlists():
                playlist_data["d"] = _ytdlp_json(
                    ["yt-dlp", "-J", "--flat-playlist", "--no-warnings", "--quiet",
                     "https://www.youtube.com/results?search_query=%s&sp=EgIQAw%%3D%%3D"
                     % urllib.parse.quote(q)], 45)

            def _fetch_ytsearch():
                search_data["d"] = _ytdlp_json(
                    ["yt-dlp", "ytsearch8:%s" % q, "--flat-playlist", "-J",
                     "--no-warnings", "--quiet"], 25)

            t1 = threading.Thread(target=_fetch_playlists)
            t2 = threading.Thread(target=_fetch_ytsearch)
            t1.start(); t2.start(); t1.join(50); t2.join(30)

            pld = playlist_data.get("d") or {}
            seen_pl = set()
            n_pl = 0
            skip_pat = re.compile(r"(trailer|teaser|reaction|behind the scenes)", re.I)
            for pl in (pld.get("entries") or []):
                pid = pl.get("id") or ""
                ptitle = pl.get("title") or ""
                if not pid.startswith("PL") or ql not in ptitle.lower():
                    continue
                if pid in seen_pl:
                    continue
                seen_pl.add(pid)
                n_pl += 1
                albums.append({
                    "browseId": pid,
                    "title": ptitle,
                    "artist": pl.get("channel") or pl.get("uploader") or "YouTube",
                    "year": "",
                    "thumbnail": "",
                    "type": "album",
                    "source": "youtube-playlist",
                })
                if n_pl >= 4:
                    break
            # fallback: group ytsearch results by channel when the track titles
            # contain the query (no official playlist exists)
            ytdata = search_data.get("d") or {}
            groups = {}
            for e in (ytdata.get("entries") or []):
                t = e.get("title") or ""
                ch = e.get("channel") or "YouTube"
                if ql in t.lower():
                    groups.setdefault(ch, []).append(e)
            for ch, g in groups.items():
                songs_only = [e for e in g if not skip_pat.search(e.get("title") or "")]
                if not songs_only:
                    continue
                albums.append({
                    "browseId": "ytsearchall:%s|%s" % (urllib.parse.quote(q), urllib.parse.quote(ch)),
                    "title": "%s (YouTube)" % q,
                    "artist": ch,
                    "year": "",
                    "thumbnail": "",
                    "type": "album",
                    "source": "youtube-search",
                    "trackCount": len(songs_only),
                })
            return {"status":"ok","results":albums,"searchType":"albums"}
        else:
            # local library matches first, so your own songs always show up
            songs = search_local_library(q)
            results = ytmusic.search(q, filter="songs")
            seen_vid = set()
            for r in results[:15]:
                vid = r.get("videoId")
                if not vid or vid in seen_vid:
                    continue
                seen_vid.add(vid)
                artists = ", ".join([a["name"] for a in r.get("artists",[])]) if r.get("artists") else "Sconosciuto"
                album_name = r.get("album",{}).get("name") if r.get("album") else "Singolo"
                thumbnails = r.get("thumbnails",[])
                thumb = thumbnails[-1]["url"] if thumbnails else ""
                title = r.get("title","")
                already = find_existing(artists.split(",")[0].strip(), title) is not None
                songs.append({
                    "videoId": vid,
                    "title": title,
                    "artist": artists,
                    "album": album_name,
                    "duration": r.get("duration"),
                    "thumbnail": thumb,
                    "inLibrary": already,
                    "type": "song",
                    "source": "youtube",
                })
            # regular-YouTube fallback (tracks that exist on YouTube but not in
            # the YouTube Music catalog, e.g. thasup & Mara Sattei "EGLI È IL RE")
            ytdata = _ytdlp_json(
                ["yt-dlp", "ytsearch5:%s" % q, "--flat-playlist", "-J",
                 "--no-warnings", "--quiet"], 25) or {}
            for e in (ytdata.get("entries") or []):
                evid = e.get("id")
                if not evid or evid in seen_vid:
                    continue
                seen_vid.add(evid)
                songs.append({
                    "videoId": evid,
                    "title": e.get("title") or "",
                    "artist": e.get("channel") or "YouTube",
                    "album": "YouTube",
                    "duration": e.get("duration"),
                    "thumbnail": "",
                    "inLibrary": False,
                    "type": "song",
                    "source": "youtube-search",
                })
            return {"status":"ok","results":songs,"searchType":"songs"}
    except Exception as e:
        return JSONResponse(status_code=500,content={"status":"error","message":str(e)})

@app.post("/api/download", dependencies=[Depends(check_auth)])
@app.post("/Italiarr/api/download", dependencies=[Depends(check_auth)])
@app.post("/italiarr/api/download", dependencies=[Depends(check_auth)])
@app.post("/lidarr/api/download", dependencies=[Depends(check_auth)])
def start_download(background_tasks: BackgroundTasks, data: dict):
    video_id = data.get("videoId")
    title = data.get("title","Brano")
    artist = data.get("artist","Artista")
    album = data.get("album","Singolo")
    source = data.get("source", "manual")
    if not video_id:
        return JSONResponse(status_code=400,content={"message":"videoId required"})
    task_id = str(uuid.uuid4())
    with _state_lock:
        downloads[task_id] = {"id":task_id,"title":title,"artist":artist,"album":album,"source":source,"status":"queued","progress":0,"cancelled":False}
        _enqueue(task_id, artist, album, video_id, title, source)
    return {"status":"ok","taskId":task_id}

@app.post("/api/download_album", dependencies=[Depends(check_auth)])
@app.post("/Italiarr/api/download_album", dependencies=[Depends(check_auth)])
@app.post("/italiarr/api/download_album", dependencies=[Depends(check_auth)])
@app.post("/lidarr/api/download_album", dependencies=[Depends(check_auth)])
def download_album(background_tasks: BackgroundTasks, data: dict):
    browse_id = data.get("browseId")
    album_title = data.get("title","Album")
    artist_name = data.get("artist","Artista")
    if not browse_id:
        return JSONResponse(status_code=400,content={"message":"browseId required"})
    # clean playlist-style titles ("X (Album) | Vevo Playlist" -> "X") so the
    # album tag stays consistent with the rest of the library
    if browse_id.startswith(("PL", "OLAK5uy_", "UU", "ytsearchall:")):
        album_title = re.sub(r"\s*[|(].*$", "", album_title).strip() or album_title
    skip_pat = re.compile(r"(trailer|teaser|reaction|behind the scenes|documentary|interview)", re.I)
    queued_count = 0
    try:
        entries = []
        if browse_id.startswith("ytsearchall:"):
            # YouTube-only "album" (not in the YT Music catalog): query|channel
            try:
                q, ch = browse_id[len("ytsearchall:"):].split("|", 1)
                q = urllib.parse.unquote(q)
                ch = urllib.parse.unquote(ch)
            except Exception:
                return JSONResponse(status_code=400, content={"message":"bad browseId"})
            out = subprocess.run(["yt-dlp", "ytsearch30:%s" % q, "--flat-playlist", "-J",
                                  "--no-warnings", "--quiet"],
                                 capture_output=True, text=True, timeout=60)
            ytdata = json.loads(out.stdout) if out.stdout.strip() else {}
            for e in (ytdata.get("entries") or []):
                if (e.get("channel") or "") != ch:
                    continue
                if skip_pat.search(e.get("title") or ""):
                    continue
                if e.get("id"):
                    entries.append(e)
        elif browse_id.startswith(("PL", "OLAK5uy_", "UU")):
            # explicit YouTube playlist / album (incl. Vevo "Album" playlists)
            out = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", "--quiet",
                                  "https://www.youtube.com/playlist?list=" + browse_id],
                                 capture_output=True, text=True, timeout=90)
            ytdata = json.loads(out.stdout) if out.stdout.strip() else {}
            for e in (ytdata.get("entries") or []):
                if not e.get("id"):
                    continue
                if skip_pat.search(e.get("title") or ""):
                    continue
                entries.append(e)
        else:
            # normal YouTube Music album
            album_data = ytmusic.get_album(browse_id)
            for track in album_data.get("tracks", []):
                if track.get("videoId") and track.get("title"):
                    entries.append({"id": track["videoId"], "title": track["title"]})

        for e in entries:
            vid = e.get("id") or e.get("videoId")
            tr_title = e.get("title")
            if vid and tr_title:
                task_id = str(uuid.uuid4())
                source = data.get("source", "manual")
                with _state_lock:
                    downloads[task_id] = {"id":task_id,"title":tr_title,"artist":artist_name,"album":album_title,"source":source,"status":"queued","progress":0,"cancelled":False}
                    _enqueue(task_id, artist_name, album_title, vid, tr_title, source)
                queued_count += 1
        return {"status":"ok","queued":queued_count,"album":album_title}
    except Exception as e:
        return JSONResponse(status_code=500,content={"status":"error","message":str(e)})

@app.post("/api/download/cancel", dependencies=[Depends(check_auth)])
@app.post("/Italiarr/api/download/cancel", dependencies=[Depends(check_auth)])
@app.post("/italiarr/api/download/cancel", dependencies=[Depends(check_auth)])
@app.post("/lidarr/api/download/cancel", dependencies=[Depends(check_auth)])
def cancel_download(data: dict):
    task_id = data.get("taskId")
    with _state_lock:
        if task_id in downloads:
            downloads[task_id]["cancelled"] = True
            downloads[task_id]["status"] = "cancelled"
            del downloads[task_id]
            return {"status":"ok"}
    return JSONResponse(status_code=404,content={"message":"Task not found"})

@app.get("/api/downloads", dependencies=[Depends(check_auth)])
@app.get("/Italiarr/api/downloads", dependencies=[Depends(check_auth)])
@app.get("/italiarr/api/downloads", dependencies=[Depends(check_auth)])
@app.get("/lidarr/api/downloads", dependencies=[Depends(check_auth)])
def get_downloads():
    with _state_lock:
        items = list(downloads.values())
    return {"downloads": items}

@app.get("/api/history", dependencies=[Depends(check_auth)])
@app.get("/Italiarr/api/history", dependencies=[Depends(check_auth)])
@app.get("/italiarr/api/history", dependencies=[Depends(check_auth)])
@app.get("/lidarr/api/history", dependencies=[Depends(check_auth)])
def get_history():
    return {"history": _history_list(100)}

@app.get("/api/library", dependencies=[Depends(check_auth)])
@app.get("/Italiarr/api/library", dependencies=[Depends(check_auth)])
@app.get("/italiarr/api/library", dependencies=[Depends(check_auth)])
@app.get("/lidarr/api/library", dependencies=[Depends(check_auth)])
def get_library():
    library = []
    if os.path.exists(DOWNLOAD_DIR):
        for artist in sorted(os.listdir(DOWNLOAD_DIR)):
            ap = os.path.join(DOWNLOAD_DIR,artist)
            if os.path.isdir(ap):
                albums = []
                for alb in os.listdir(ap):
                    albp = os.path.join(ap,alb)
                    if os.path.isdir(albp):
                        tracks = [f for f in os.listdir(albp) if f.endswith(".mp3")]
                        albums.append({"name":alb,"tracksCount":len(tracks)})
                library.append({"artist":artist,"albums":albums,"totalTracks":sum(a["tracksCount"] for a in albums)})
    return {"library":library}

app.mount("/",StaticFiles(directory="/app/static",html=True),name="static")
