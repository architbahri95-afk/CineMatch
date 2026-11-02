"""SQLite persistence: schema, lightweight migrations and first-run seeding."""
import csv
import random
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"   # millisecond precision keeps "most recent" ordering exact

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    genres TEXT NOT NULL,
    keywords TEXT NOT NULL,
    overview TEXT NOT NULL,
    director TEXT DEFAULT '',
    actors TEXT DEFAULT '',
    language TEXT DEFAULT 'English',
    runtime INTEGER DEFAULT 0,
    tags TEXT DEFAULT '',
    poster_url TEXT DEFAULT '',
    trailer_url TEXT DEFAULT '',
    streaming TEXT DEFAULT '',
    watch_link TEXT DEFAULT '',
    streaming_region TEXT DEFAULT '',
    extras_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    password_hash TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    fav_genres TEXT DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ratings (
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    rating REAL NOT NULL CHECK (rating BETWEEN 1 AND 5),
    rated_at TEXT,
    PRIMARY KEY (user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS views (
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    viewed_at TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    parent_id INTEGER REFERENCES reviews(id),      -- NULL = a review, otherwise a reply to that review
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- One row per recommendation a person was shown: powers click-through and acceptance rates.
CREATE TABLE IF NOT EXISTS rec_log (
    user_id INTEGER NOT NULL REFERENCES users(id),
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    source TEXT NOT NULL,
    shown_at TEXT NOT NULL,
    clicked_at TEXT,
    accepted_at TEXT,
    PRIMARY KEY (user_id, movie_id, source)
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    query TEXT NOT NULL,
    searched_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("users", "email", "TEXT"), ("users", "password_hash", "TEXT"),
    ("users", "created_at", "TEXT"), ("users", "fav_genres", "TEXT DEFAULT ''"),
    ("ratings", "rated_at", "TEXT"),
    ("movies", "director", "TEXT DEFAULT ''"), ("movies", "actors", "TEXT DEFAULT ''"),
    ("movies", "language", "TEXT DEFAULT 'English'"), ("movies", "runtime", "INTEGER DEFAULT 0"),
    ("movies", "tags", "TEXT DEFAULT ''"), ("movies", "poster_url", "TEXT DEFAULT ''"),
    ("movies", "trailer_url", "TEXT DEFAULT ''"), ("movies", "streaming", "TEXT DEFAULT ''"),
    ("movies", "watch_link", "TEXT DEFAULT ''"), ("movies", "streaming_region", "TEXT DEFAULT ''"),
    ("movies", "extras_at", "TEXT DEFAULT ''"), ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
]


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    """Bring databases from earlier versions up to date without losing data."""
    for table, col, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.execute("UPDATE ratings SET rated_at = datetime('now') WHERE rated_at IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name ON users(name COLLATE NOCASE)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id, id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_review_one ON reviews(user_id, movie_id) WHERE parent_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id, id)")


def init_db(path):
    """Create tables, migrate old databases, and seed on first run."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 0:
        _seed_movies(conn)
        _seed_demo_users(conn)
    _sync_movie_meta(conn)
    _sync_posters(conn)
    _sync_extras(conn)
    _ensure_admin(conn)
    conn.commit()
    conn.close()


def _read_csv(name):
    with open(DATA_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _seed_movies(conn):
    conn.executemany(
        "INSERT INTO movies (id, title, year, genres, keywords, overview) VALUES (?,?,?,?,?,?)",
        [(int(r["id"]), r["title"], int(r["year"]), r["genres"], r["keywords"], r["overview"])
         for r in _read_csv("movies.csv")])


def _sync_movie_meta(conn):
    """Fill director/cast/language/runtime/tags from movies_meta.csv for any
    movie that doesn't have them yet (covers fresh installs and upgrades)."""
    for r in _read_csv("movies_meta.csv"):
        conn.execute(
            "UPDATE movies SET director=?, actors=?, language=?, runtime=?, tags=? "
            "WHERE id=? AND (director IS NULL OR director = '')",
            (r["director"], r["actors"], r["language"], int(r["runtime"]), r["tags"], int(r["id"])))


def _sync_posters(conn):
    """Apply app/data/posters.csv (written by fetch_posters.py) if it exists."""
    if not (DATA_DIR / "posters.csv").exists():
        return
    for r in _read_csv("posters.csv"):
        conn.execute("UPDATE movies SET poster_url=? WHERE id=?", (r["poster_url"], int(r["id"])))


def _sync_extras(conn):
    """Apply app/data/extras.csv (trailers and where-to-watch, from fetch_posters.py --extras)."""
    if not (DATA_DIR / "extras.csv").exists():
        return
    for r in _read_csv("extras.csv"):
        conn.execute(
            "UPDATE movies SET trailer_url=?, streaming=?, watch_link=?, streaming_region=?, extras_at=? WHERE id=?",
            (r["trailer_url"], r["streaming"], r["watch_link"], r["region"], r["fetched_at"], int(r["id"])))


def _ensure_admin(conn):
    """Someone must be able to open the dashboard: if there is no admin yet,
    the earliest real (non-demo) account becomes one."""
    if conn.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():
        return
    row = conn.execute("SELECT id FROM users WHERE is_demo = 0 ORDER BY id LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))


def _seed_demo_users(conn, n_users=60, seed=7):
    """Synthetic users with genre tastes (and timestamps over the last 45
    days) so collaborative filtering and trending have signal on day one.
    They have no password, so nobody can sign in as them."""
    rng = random.Random(seed)
    movies = conn.execute("SELECT id, genres FROM movies").fetchall()
    all_genres = sorted({g for m in movies for g in m["genres"].split("|")})
    for i in range(n_users):
        cur = conn.execute(
            "INSERT INTO users (name, is_demo, created_at) VALUES (?, 1, datetime('now'))",
            (f"demo_user_{i + 1}",))
        uid = cur.lastrowid
        likes = set(rng.sample(all_genres, 2))
        dislikes = set(rng.sample([g for g in all_genres if g not in likes], 2))
        for m in rng.sample(movies, rng.randint(14, 28)):
            genres = set(m["genres"].split("|"))
            score = 3.0 + 1.1 * len(genres & likes) - 1.1 * len(genres & dislikes)
            score += rng.gauss(0, 0.6)
            rating = min(5.0, max(1.0, round(score * 2) / 2))
            conn.execute(
                "INSERT INTO ratings VALUES (?,?,?, datetime('now', ?))",
                (uid, m["id"], rating, f"-{rng.randint(0, 45)} days"))
