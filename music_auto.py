#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
music_auto.py — Continuous music discovery for CT103's italiarr/Navidrome stack.

Every run (cron: every 30 min):
  1. Seeds artists from what the user REALLY listens to:
       - Navidrome DB: top artists by play_count
       - Last.fm:      user.getTopArtists + user.getLovedTracks (synced via the
                       API key/session already configured in Navidrome)
  2. Expands with Last.fm artist.getSimilar + artist.getTopTracks.
  3. Builds candidate tracks, dedupes against:
       - library (Navidrome DB media_file)
       - italiarr queue (GET /api/downloads) + local state file
  4. Searches each candidate on YouTube Music and enqueues into italiarr's
     SERIAL download queue (one download at a time -> no disk saturation).
  5. Refreshes the "Discover Weekly" playlist (m3u) from tracks that landed.

Safety: flock single-instance lock, load-average gate, per-run cap, queue
backpressure. Reversible: --dry-run prints without enqueuing.
"""

import os
import sys
import json
import re
import time
import fcntl
import socket
import sqlite3
import hashlib
import argparse
import datetime
import urllib.parse

# ytmusicapi makes network calls without timeouts and can hang forever on a
# stalled connection; this global socket timeout bounds every call so a hung
# run can never block the cron (and the flock) for hours.
socket.setdefaulttimeout(30)

import requests

# ----------------------------------------------------------------- config ---
DB_PATH = "/data/config/navidrome/navidrome.db"
MUSIC_DIR = "/data/music"
PLAYLISTS_DIR = os.path.join(MUSIC_DIR, "Playlists")
DISCOVER_WEEKLY_M3U = os.path.join(PLAYLISTS_DIR, "Discover Weekly Auto.m3u")  # ours; generate_playlists.py owns 'Discover Weekly.m3u'
STATE_FILE = "/opt/music-stack/.music_auto_state.json"
LOCK_FILE = "/opt/music-stack/.music_auto.lock"
LOG_FILE = "/var/log/music_auto.log"

ITALIARR = "http://127.0.0.1:8686"

SECRETS_FILE = "/opt/music-stack/.secrets.env"


def _load_secrets():
    """Load secrets from a root-only file (chmod 600) into the environment."""
    try:
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_secrets()

ITALIARR_PASSWORD = os.environ.get("ITALIARR_PASSWORD", "")

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_SECRET = os.environ.get("LASTFM_SECRET", "")
LASTFM_SESSION = os.environ.get("LASTFM_SESSION", "")
LASTFM_USER = "notsim"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

MAX_NEW_PER_RUN = 5          # how many new tracks to enqueue per run
MAX_SEARCHES = 15            # ytm searches per run (bounds runtime)
QUEUE_PENDING_LIMIT = 15     # skip run if italiarr has this many pending
NOMATCH_TTL_DAYS = 7         # how long a failed YT-Music search is remembered

# stage-name aliases (thasup = tha Supreme): a failed artist containment match
# is retried against these before declaring a track "not on YouTube Music"
ARTIST_ALIASES = {
    "thasup": {"tha supreme"},
    "tha supreme": {"thasup"},
}
# canonical spelling used at enqueue time, so albums don't split across
# artist folders ("tha Supreme" -> "thasup")
ARTIST_CANONICAL = {"tha supreme": "thasup"}
LOAD_LIMIT = 4.0             # skip run if 1-min load average is above this
STATE_TTL_DAYS = 30          # don't re-add a track within this window
TOP_ARTIST_SEEDS = 12        # seed artists from each source
SIMILAR_PER_SEED = 3         # similar artists to expand each top seed
SIMILAR_SEED_TOP = 5         # how many top seeds get similarity expansion
TOPTRACKS_PER_ARTIST = 4     # top tracks fetched per seed artist
LOVED_LIMIT = 60
USER_TOPTRACKS_LIMIT = 30


def log(msg):
    # stdout is captured by cron into LOG_FILE; print keeps manual runs visible
    print("%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ last.fm --
def lastfm(params):
    """Public Last.fm API call. params: dict with method/... (no api_sig)."""
    params = dict(params)
    params["api_key"] = LASTFM_API_KEY
    params["format"] = "json"
    r = requests.get(LASTFM_API, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def lastfm_signed(method, extra):
    """Signed Last.fm call (needs session)."""
    params = {"method": method, "api_key": LASTFM_API_KEY, "sk": LASTFM_SESSION}
    params.update(extra)
    # signature: sorted params (name+value concatenated) + secret, md5
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    params["api_sig"] = hashlib.md5((base + LASTFM_SECRET).encode()).hexdigest()
    params["format"] = "json"
    r = requests.get(LASTFM_API, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _lf_text(obj):
    """Extract a plain string from Last.fm's various field shapes."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("#text") or ""
    return str(obj)


def lastfm_loved():
    try:
        data = lastfm({"method": "user.getLovedTracks", "user": LASTFM_USER, "limit": LOVED_LIMIT})
        out = []
        for t in data.get("lovedtracks", {}).get("track", []):
            art = _lf_text(t.get("artist"))
            alb = _lf_text(t.get("album")) if isinstance(t.get("album"), (dict, str)) else None
            out.append((art, t.get("name"), alb))
        return out, None
    except Exception as e:
        return [], str(e)


def lastfm_top_artists():
    try:
        data = lastfm({"method": "user.getTopArtists", "user": LASTFM_USER,
                       "period": "overall", "limit": TOP_ARTIST_SEEDS})
        return [a["name"] for a in data.get("topartists", {}).get("artist", [])], None
    except Exception as e:
        return [], str(e)


def lastfm_user_toptracks():
    try:
        data = lastfm({"method": "user.getTopTracks", "user": LASTFM_USER,
                       "period": "overall", "limit": USER_TOPTRACKS_LIMIT})
        out = []
        for t in data.get("toptracks", {}).get("track", []):
            art = _lf_text(t.get("artist"))
            alb = _lf_text(t.get("album")) if isinstance(t.get("album"), (dict, str)) else None
            out.append((art, t.get("name"), alb))
        return out, None
    except Exception as e:
        return [], str(e)


def lastfm_similar(artist):
    try:
        data = lastfm({"method": "artist.getSimilar", "artist": artist, "limit": SIMILAR_PER_SEED})
        return [a["name"] for a in data.get("similarartists", {}).get("artist", [])], None
    except Exception as e:
        return [], str(e)


def lastfm_artist_toptracks(artist):
    try:
        data = lastfm({"method": "artist.getTopTracks", "artist": artist, "limit": TOPTRACKS_PER_ARTIST})
        out = []
        for t in data.get("toptracks", {}).get("track", []):
            art = _lf_text(t.get("artist"))
            alb = _lf_text(t.get("album")) if isinstance(t.get("album"), (dict, str)) else None
            out.append((art, t.get("name"), alb))
        return out, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------ navidrome db --
def db_connect():
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
    # tolerate corrupt/non-UTF8 text in the DB (Navidrome has a few bad rows)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


def navidrome_top_artists():
    try:
        conn = db_connect()
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT m.artist, SUM(a.play_count) as plays
            FROM media_file m JOIN annotation a ON m.id = a.item_id
            WHERE a.item_type='media_file' AND a.play_count > 0
              AND m.artist NOT IN ('[Unknown Artist]','Discover Weekly','Saved Discovery','')
            GROUP BY m.artist ORDER BY plays DESC LIMIT ?""",
            (TOP_ARTIST_SEEDS,)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        log("navidrome_top_artists error: %s" % e)
        return []


def library_tracks():
    """Return set of normalized (artist, title) already in the library."""
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT artist, title FROM media_file WHERE title IS NOT NULL").fetchall()
        conn.close()
        out = set()
        for artist, title in rows:
            out.add((norm(artist), norm(title)))
        return out
    except Exception as e:
        log("library_tracks error: %s" % e)
        return set()


def library_paths_for(titles_artists):
    """Return m3u-relative paths of library tracks matching (artist,title) set."""
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT path, title FROM media_file WHERE title IS NOT NULL").fetchall()
        conn.close()
        # titles_artists carries NORMALIZED titles (see library_tracks); match
        # them against raw DB titles via norm() instead of SQL equality, which
        # would never match (norm() strips spaces/punctuation in the key).
        want = {norm(t) for _, t in titles_artists}
        return [p for p, t in rows if norm(t) in want]
    except Exception as e:
        log("library_paths error: %s" % e)
        return []


def norm(s):
    """Lowercase, keep alnum, turn every other char into a separator."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (s or "").lower()).split())


def _tkey(s):
    """Normalized title, with feat/ft/featuring removed so 'Talk To You
    (ft. 54 Ultra)' and 'Talk To You (feat. 54 Ultra)' match each other."""
    s = norm(s)
    s = re.sub(r"\b(feat|featuring|ft)\.?\b", " ", s)
    return " ".join(s.split())


def title_match(t1, t2):
    """True if titles plausibly match (normalized equality or containment)."""
    a, b = _tkey(t1), _tkey(t2)
    if not a or not b:
        return False
    return a == b or (len(a) > 5 and (a in b or b in a))


def artist_match(a1, a2):
    a, b = norm(a1), norm(a2)
    if not a or not b:
        return False
    return a == b or a in b or b in a


# -------------------------------------------------------------- italiarr api --
def italiarr_session():
    s = requests.Session()
    r = s.post(ITALIARR + "/api/login", json={"password": ITALIARR_PASSWORD}, timeout=15)
    return s if r.ok and r.json().get("status") == "ok" else None


def italiarr_queue(sess):
    try:
        r = sess.get(ITALIARR + "/api/downloads", timeout=15)
        data = r.json().get("downloads", [])
        pending = [t for t in data if t.get("status") in ("queued", "downloading", "retrying")]
        queued_titles = {(norm(t.get("artist")), norm(t.get("title"))) for t in data}
        return pending, queued_titles
    except Exception as e:
        log("italiarr_queue error: %s" % e)
        return [], set()


def italiarr_failed(sess, days=7):
    """Return {(norm_artist, norm_title)} of tracks that failed to download in
    the last `days` days (from Italiarr's persistent history), so the automation
    does not re-enqueue them every run (e.g. age-restricted videos)."""
    try:
        r = sess.get(ITALIARR + "/api/history", timeout=15)
        rows = r.json().get("history", []) if r.status_code == 200 else []
    except Exception:
        return set()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    failed = set()
    for h in rows:
        if h.get("status") != "failed":
            continue
        ts = h.get("ts") or ""
        if ts[:10] < cutoff:
            continue
        a = norm(h.get("artist") or "")
        t = norm(h.get("title") or "")
        if a and t:
            failed.add((a, t))
    return failed


def italiarr_enqueue(sess, artist, title, album, video_id):
    r = sess.post(ITALIARR + "/api/download", json={
        "videoId": video_id, "title": title, "artist": artist, "album": album or "Singoli",
        "source": "auto",
    }, timeout=15)
    return r.ok and r.json().get("status") == "ok"


# ------------------------------------------------------------------ youtube --
def ytm_search(yt, artist, title):
    """Search YouTube Music and return (videoId, album) ONLY when the result
    actually corresponds to the wanted track (title + artist match). This
    prevents enqueueing phantom entries (e.g. 'EGLI È IL RE' resolving to a
    different thasup song or a gospel cover)."""
    try:
        res = yt.search("%s %s" % (artist, title), filter="songs")
        if not res:
            return None, None
        want_t = _tkey(title)
        want_a = norm(artist)

        def title_ok(rt):
            t = _tkey(rt)
            if not t:
                return False
            return t == want_t or (len(want_t) > 4 and (want_t in t or t in want_t))

        def artist_ok(ra):
            a = norm(ra)
            if not a or not want_a:
                return False
            if want_a in a or a in want_a:
                return True
            # stage-name aliases (e.g. thasup vs tha Supreme)
            if a in ARTIST_ALIASES.get(want_a, set()) or \
               want_a in ARTIST_ALIASES.get(a, set()):
                return True
            wa = [w for w in want_a.split() if len(w) > 2]
            wb = [w for w in a.split() if len(w) > 2]
            if not wa or not wb:
                return False
            common = set(wa) & set(wb)
            if not common:
                return False
            short = wa if len(wa) <= len(wb) else wb
            other = wb if short is wa else wa
            return all(w in other for w in short)

        for r in res[:10]:
            rt = r.get("title")
            ra = ", ".join(a_["name"] for a_ in (r.get("artists") or []))
            if title_ok(rt) and artist_ok(ra):
                return r.get("videoId"), (r.get("album") or {}).get("name")
        # no verified match: log and skip (never enqueue a wrong track)
        log("ytm NO-MATCH for %s - %s (first result: %s)" %
            (artist, title, (res[0].get("title") if res else "none")))
        return None, None
    except Exception as e:
        log("ytm_search error for %s - %s: %s" % (artist, title, e))
        return None, None


# ---------------------------------------------------------------- playlist --
def write_discover_weekly(loved_artists_titles):
    """Write Discover Weekly.m3u from loved/similar tracks present in library."""
    try:
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        paths = library_paths_for(loved_artists_titles)
        if not paths:
            return
        with open(DISCOVER_WEEKLY_M3U, "w") as f:
            f.write("#EXTM3U\n")
            for p in sorted(set(paths)):
                f.write("/music/%s\n" % p)
        log("Discover Weekly Auto.m3u written (%d tracks)" % len(set(paths)))
    except Exception as e:
        log("write_discover_weekly error: %s" % e)


RECENTLY_ADDED_M3U = os.path.join(PLAYLISTS_DIR, "Aggiunti di recente.m3u")


def write_recently_added(limit=100, days=30):
    """Write 'Aggiunti di recente.m3u' with tracks added in the last `days`
    days that have NOT been played yet (play_count 0 in the annotation table).
    Once a track is listened to in Navidrome it drops out of the playlist but
    stays in the library, so the playlist never becomes an archive."""
    try:
        conn = db_connect()
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT mf.path FROM media_file mf "
            "WHERE substr(mf.created_at, 1, 10) >= ? "
            "AND NOT EXISTS (SELECT 1 FROM annotation a "
            "   WHERE a.item_id = mf.id AND a.item_type = 'media_file' AND a.play_count > 0) "
            "ORDER BY mf.created_at DESC LIMIT ?",
            (cutoff, limit)).fetchall()
        conn.close()
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        with open(RECENTLY_ADDED_M3U, "w") as f:
            f.write("#EXTM3U\n")
            for (p,) in rows:
                f.write("/music/%s\n" % p)
        log("Aggiunti di recente.m3u written (%d tracks, ultimi %d giorni, non ascoltati)"
            % (len(rows), days))
    except Exception as e:
        log("write_recently_added error: %s" % e)


# -------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print candidates without enqueuing")
    args = ap.parse_args()

    log("=== music_auto run%s ===" % (" (DRY RUN)" if args.dry_run else ""))

    # single instance lock
    try:
        lock = open(LOCK_FILE, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is active, skipping")
        return

    # load gate
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        if load1 > LOAD_LIMIT:
            log("load %.2f > %.1f, skipping this run" % (load1, LOAD_LIMIT))
            return
    except Exception:
        pass

    # state
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=STATE_TTL_DAYS)).isoformat()

    # 1) seeds
    seeds = []
    nav_seeds = navidrome_top_artists()
    lfm_artists, err1 = lastfm_top_artists()
    if err1:
        log("lastfm top artists error: %s" % err1)
    seen = set()
    for a in nav_seeds + lfm_artists:
        k = norm(a)
        if k and k not in seen:
            seen.add(k)
            seeds.append(a)
    log("seed artists (%d): %s" % (len(seeds), ", ".join(seeds[:10])))

    # 2) expand with similar artists
    expanded = list(seeds)
    for a in seeds[:SIMILAR_SEED_TOP]:
        sims, err = lastfm_similar(a)
        if err:
            log("similar for %s error: %s" % (a, err))
        for s in sims:
            k = norm(s)
            if k and k not in seen:
                seen.add(k)
                expanded.append(s)
    log("expanded artist pool: %d" % len(expanded))

    # 3) candidate tracks (priority order: loved > user top > artist top tracks)
    candidates = []          # (artist, title, album)
    cand_keys = set()

    def add_candidate(artist, title, album=None):
        a, t = norm(artist), norm(title)
        if not a or not t:
            return
        key = (a, t)
        if key not in cand_keys:
            cand_keys.add(key)
            candidates.append((artist, title, album))

    loved, err = lastfm_loved()
    if err:
        log("lastfm loved error: %s" % err)
    else:
        for art, t, alb in loved:
            add_candidate(art, t, alb)
    log("loved tracks: %d" % len(loved))

    utt, err = lastfm_user_toptracks()
    if err:
        log("lastfm user toptracks error: %s" % err)
    for art, t, alb in utt:
        add_candidate(art, t, alb)
    log("user top tracks: %d" % len(utt))

    for a in expanded[:SIMILAR_SEED_TOP + 3]:
        tops, err = lastfm_artist_toptracks(a)
        if err:
            log("toptracks for %s error: %s" % (a, err))
        for art, t, alb in tops:
            add_candidate(art, t, alb)
    log("total candidate titles: %d" % len(candidates))

    # 4) dedup vs library + queue + state
    lib = library_tracks()
    try:
        sess = italiarr_session()
    except Exception as e:
        log("italiarr API unreachable: %s" % e)
        sess = None
    pending, queued = (([], set()) if sess is None else italiarr_queue(sess))
    if sess is None:
        log("italiarr API unreachable - will skip enqueue")
    if len(pending) >= QUEUE_PENDING_LIMIT:
        log("italiarr queue busy (%d pending >= %d), skipping enqueue" % (len(pending), QUEUE_PENDING_LIMIT))

    actionable = []
    lib_by_title = {}
    for la, lt in lib:
        lib_by_title.setdefault(lt, set()).add(la)
    nomatch_cutoff = (datetime.datetime.utcnow() -
                      datetime.timedelta(days=NOMATCH_TTL_DAYS)).isoformat()
    failed_recent = italiarr_failed(sess) if sess is not None else set()
    if failed_recent:
        log("skipping %d recently-failed tracks" % len(failed_recent))
    for artist, title, album in candidates:
        a, t = norm(artist), norm(title)
        if t in lib_by_title:
            # artist containment: candidate "thasup" matches library "thasup,
            # Mara Sattei" for the same title (also catches nickname variants)
            artists = lib_by_title[t]
            if a in artists or any(a in la or la in a for la in artists):
                continue
        if (a, t) in queued:
            continue
        if (a, t) in failed_recent:
            continue
        key = "%s|%s" % (a, t)
        if key in state and state[key] >= cutoff:
            continue
        nk = "nomatch|%s|%s" % (a, t)
        if nk in state and state[nk] >= nomatch_cutoff:
            continue
        actionable.append((artist, title, album))

    # self-heal: drop state entries whose track is neither in the library nor in
    # the (in-memory) italiarr queue — they were lost on an italiarr restart and
    # must be allowed to be re-added. NO-MATCH entries are pruned by their own TTL.
    stale = []
    for k in list(state):
        if k.startswith("nomatch|"):
            if state[k] < nomatch_cutoff:
                stale.append(k)
            continue
        parts = k.split("|", 1)
        if len(parts) != 2:
            stale.append(k)
            continue
        a, t = parts
        if (a, t) not in lib and (a, t) not in queued:
            stale.append(k)
    for k in stale:
        del state[k]
    if stale:
        log("pruned %d lost-from-restart state entries" % len(stale))

    log("actionable after dedup: %d" % len(actionable))

    # 5) resolve via YouTube Music and enqueue
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
    except Exception as e:
        log("ytmusicapi import failed: %s" % e)
        return

    added = 0
    searched = 0
    for artist, title, album in actionable:
        if added >= MAX_NEW_PER_RUN:
            break
        if len(pending) >= QUEUE_PENDING_LIMIT:
            log("queue busy, stopping early")
            break
        if searched >= MAX_SEARCHES:
            log("search budget exhausted")
            break
        searched += 1
        try:
            vid, yt_album = ytm_search(yt, artist or "", title)
        except Exception as e:
            log("ytm_search error for %s - %s: %s" % (artist, title, e))
            vid, yt_album = None, None
        if not vid:
            # remember the failed search so we don't retry it every run
            state["nomatch|%s|%s" % (norm(artist), norm(title))] = \
                datetime.datetime.utcnow().isoformat()
            continue
        real_artist = artist
        album_final = album or yt_album
        key = "%s|%s" % (norm(real_artist), norm(title))
        if key in state and state[key] >= cutoff:
            continue
        if args.dry_run:
            log("DRY: would enqueue %s - %s (%s)" % (real_artist, title, vid))
        else:
            if sess is None:
                log("no italiarr session, skipping enqueue of %s - %s" % (real_artist, title))
                break
            try:
                ok = italiarr_enqueue(sess, ARTIST_CANONICAL.get(norm(real_artist), real_artist),
                                      title, album_final, vid)
            except Exception as e:
                log("enqueue exception for %s - %s: %s" % (real_artist, title, e))
                ok = False
            if ok:
                state[key] = datetime.datetime.utcnow().isoformat()
                added += 1
                log("enqueued %s - %s (album=%s)" % (real_artist, title, album_final))
            else:
                log("enqueue FAILED for %s - %s" % (real_artist, title))

    # 6) refresh Discover Weekly playlist from loved tracks present in library
    loved_pairs = []
    for art, t, alb in loved:
        for (a2, t2) in lib:
            if title_match(t, t2):
                loved_pairs.append((a2, t2))
                break
    if not args.dry_run:
        write_discover_weekly(loved_pairs[:60])
        write_recently_added()

    if state:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)

    log("run finished: enqueued=%d, searched=%d" % (added, searched))


if __name__ == "__main__":
    main()
