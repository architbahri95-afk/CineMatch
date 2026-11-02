"""Download real poster URLs from TMDB (free API key) and save them for CineMatch.

    1. Create a free account at https://www.themoviedb.org, then request an API key
       under Settings > API (use the "API Key (v3 auth)" value).
    2. Run:   python fetch_posters.py YOUR_KEY        (or set TMDB_API_KEY)
    3. Restart the app and hard-refresh the browser.

    Optional: add exact trailer links and "where to watch" (streaming providers for your country):
        python fetch_posters.py YOUR_KEY --extras --region IN
    Without it, the app shows YouTube/JustWatch search links, which always work but aren't exact.

It is safe to stop and re-run: progress is saved after every movie, movies that already
have a poster are skipped, and temporary network errors are retried automatically.
Writes app/data/posters.csv (survives a database reset) and updates cinematch.db.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app import db

ROOT = Path(__file__).parent
DB_PATH = ROOT / "cinematch.db"
OUT = db.DATA_DIR / "posters.csv"
EXTRAS = db.DATA_DIR / "extras.csv"
IMG = "https://image.tmdb.org/t/p/w342"
HEADERS = {"User-Agent": "CineMatch/1.0 (student project)", "Accept": "application/json"}
RETRIES, PAUSE, MAX_IN_A_ROW = 4, 0.4, 5

NETWORK_HELP = """
Several requests in a row were blocked or reset, so this looks like a network restriction
rather than a bug. Things to try:
  - switch network (a phone hotspot often works) and run the script again
  - turn a VPN on or off
  - set your DNS to 1.1.1.1 or 8.8.8.8
  - wait a few minutes in case TMDB is rate-limiting you
Nothing is lost: re-run the same command and it continues where it stopped."""


def request_json(url):
    """GET with retries and backoff. Raises OSError only after all retries fail."""
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("TMDB rejected your API key (401). Check you copied the v3 'API Key'.")
            if e.code == 429 or e.code >= 500:           # rate limited or server trouble: wait, retry
                wait = float(e.headers.get("Retry-After", 2 ** attempt))
            else:
                raise OSError(f"HTTP {e.code}")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            wait = 2 ** attempt                          # connection reset, timeout, etc.
        if attempt < RETRIES - 1:
            time.sleep(wait)
    raise OSError("network error after retries")


def search_movie(key, title, year):
    """Best TMDB match for a title and year (or None)."""
    q = urllib.parse.urlencode({"api_key": key, "query": title, "year": year})
    results = request_json(f"https://api.themoviedb.org/3/search/movie?{q}").get("results", [])
    with_art = [m for m in results if m.get("poster_path")]
    close = [m for m in with_art if (m.get("release_date") or "")[:4].isdigit()
             and abs(int(m["release_date"][:4]) - year) <= 1]
    return (close or with_art or [None])[0]


def find_poster(key, title, year):
    pick = search_movie(key, title, year)
    return IMG + pick["poster_path"] if pick else ""


def find_extras(key, title, year, region):
    """Trailer URL and streaming providers for one movie, from TMDB (data from JustWatch)."""
    pick = search_movie(key, title, year)
    if not pick:
        return "", {}, ""
    base = f"https://api.themoviedb.org/3/movie/{pick['id']}"
    videos = request_json(f"{base}/videos?api_key={key}").get("results", [])
    yt = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]
    ranked = sorted(yt, key=lambda v: (v.get("type") != "Trailer", not v.get("official"), v.get("type") != "Teaser"))
    trailer = f"https://www.youtube.com/watch?v={ranked[0]['key']}" if ranked else ""
    prov = request_json(f"{base}/watch/providers?api_key={key}").get("results", {}).get(region, {})
    names = {k: [p["provider_name"] for p in prov.get(k, [])] for k in ("flatrate", "rent", "buy")}
    return trailer, names, prov.get("link", "")


def load_saved():
    if not OUT.exists():
        return {}
    with open(OUT, newline="", encoding="utf-8") as f:
        return {int(r["id"]): r["poster_url"] for r in csv.DictReader(f)}


def save(posters):
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "poster_url"])
        w.writerows(sorted(posters.items()))


def fetch_extras(key, movies, region):
    """Second pass: trailers + where to watch. Resumable, same retry rules as posters."""
    done = {}
    if EXTRAS.exists():
        with open(EXTRAS, newline="", encoding="utf-8") as f:
            done = {int(r["id"]): r for r in csv.DictReader(f) if r["region"] == region}
    todo = [m for m in movies if m["id"] not in done]
    print(f"\nExtras ({region}): {len(done)} already saved, {len(todo)} to fetch.")
    fails = in_a_row = 0
    for m in todo:
        try:
            trailer, names, link = find_extras(key, m["title"], m["year"], region)
            in_a_row = 0
        except OSError:
            fails += 1; in_a_row += 1
            print(f"FAILED  {m['title']} (network)")
            if in_a_row >= MAX_IN_A_ROW:
                print(NETWORK_HELP); break
            continue
        done[m["id"]] = {"id": m["id"], "trailer_url": trailer, "streaming": json.dumps(names), "watch_link": link,
                         "region": region, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(EXTRAS, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "trailer_url", "streaming", "watch_link", "region", "fetched_at"])
            w.writeheader(); w.writerows(done[k] for k in sorted(done))
        where = ", ".join(names["flatrate"]) or "no subscription provider listed"
        print(f"ok      {m['title']}: {'trailer' if trailer else 'no trailer'}; {where}")
        time.sleep(PAUSE)
    print(f"{len(done)}/{len(movies)} movies have trailer and streaming info." + (f" {fails} failed: run again." if fails else ""))


def main():
    ap = argparse.ArgumentParser(description="Fetch poster, trailer and streaming data from TMDB.")
    ap.add_argument("key", nargs="?", default=os.environ.get("TMDB_API_KEY", ""))
    ap.add_argument("--extras", action="store_true", help="also fetch trailers and where-to-watch")
    ap.add_argument("--extras-only", action="store_true", help="only fetch trailers and where-to-watch")
    ap.add_argument("--region", default=os.environ.get("WATCH_REGION", "IN").upper(), help="2-letter country code")
    args = ap.parse_args()
    key = args.key
    if not key:
        sys.exit(__doc__)
    if args.extras_only:
        db.init_db(str(DB_PATH))
        conn = db.connect(str(DB_PATH)); movies = conn.execute("SELECT id, title, year FROM movies ORDER BY id").fetchall(); conn.close()
        fetch_extras(key, movies, args.region); db.init_db(str(DB_PATH))
        return
    db.init_db(str(DB_PATH))
    conn = db.connect(str(DB_PATH))
    movies = conn.execute("SELECT id, title, year FROM movies ORDER BY id").fetchall()
    conn.close()

    posters = load_saved()
    todo = [m for m in movies if not posters.get(m["id"])]
    print(f"{len(movies) - len(todo)} already saved, {len(todo)} to fetch.")
    failed, in_a_row = [], 0
    for m in todo:
        try:
            url = find_poster(key, m["title"], m["year"])
            in_a_row = 0
        except OSError:
            failed.append(m["title"]); in_a_row += 1
            print(f"FAILED  {m['title']} (network)")
            if in_a_row >= MAX_IN_A_ROW:
                save(posters); db.init_db(str(DB_PATH))
                print(NETWORK_HELP)
                sys.exit(1)
            continue
        posters[m["id"]] = url
        save(posters)                                   # progress survives Ctrl+C
        print(f"{'ok     ' if url else 'NONE   '} {m['title']}")
        time.sleep(PAUSE)

    db.init_db(str(DB_PATH))                            # applies posters.csv to the database
    have = sum(1 for m in movies if posters.get(m["id"]))
    print(f"\n{have}/{len(movies)} movies now have posters.")
    if args.extras:
        fetch_extras(key, movies, args.region)
        db.init_db(str(DB_PATH))
    if failed:
        print("Network errors for:", ", ".join(failed), "\nRun the same command again to retry just those.")


if __name__ == "__main__":
    main()
