"""HTTP routes: one HTML page plus a JSON API."""
import functools
import os
import re
import sqlite3
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .db import NOW

bp = Blueprint("main", __name__)


def _rec():
    return current_app.recommender


def _conn():
    return db.connect(current_app.config["DB_PATH"])


def _current_user(conn):
    uid = session.get("user_id")
    if uid is None:
        return None
    return conn.execute("SELECT id, name FROM users WHERE id = ?", (uid,)).fetchone()


def _uid():
    return session.get("user_id")


def _valid_genres(value):
    allowed = set(_rec().all_genres)
    return [g for g in dict.fromkeys(value or []) if g in allowed][:8] if isinstance(value, list) else []


def _error(message, status=400):
    return jsonify({"error": message}), status


@bp.get("/")
def index():
    return render_template("index.html")


# ---- catalogue ----------------------------------------------------------
# ---- accounts ----------------------------------------------------------------
# Passwords are never stored: only a salted scrypt hash (werkzeug) goes in the DB.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_FAILURES, FAILURE_WINDOW = 5, 600  # 5 bad logins per 10 minutes
_DUMMY_HASH = generate_password_hash("timing-equaliser")


TRACKED = {"recommended", "because", "genre", "trending", "top"}   # sections whose impressions we log


def _is_admin(conn, user_id):
    row = conn.execute("SELECT name, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return False
    env = {x.strip().lower() for x in os.environ.get("ADMIN_USERS", "").split(",") if x.strip()}
    return bool(row["is_admin"]) or row["name"].lower() in env


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        conn = _conn()
        user = _current_user(conn)
        ok = bool(user) and _is_admin(conn, user["id"])
        conn.close()
        if not user:
            return _error("Sign in first.", 401)
        if not ok:
            return _error("Admins only.", 403)
        return fn(*args, **kwargs)
    return wrapper


SOURCE_PRIORITY = ["recommended", "because", "genre", "trending", "top"]


def _mark_accepted(conn, user_id, movie_id):
    """A positive action (rating >= 4, favorite, watchlist) on a movie we recommended counts as ONE
    acceptance, credited to the section the person clicked from, else the highest-priority section
    that showed it. (Crediting every section would count one action several times.)"""
    rows = conn.execute("SELECT source, clicked_at, accepted_at FROM rec_log WHERE user_id=? AND movie_id=?",
                        (user_id, movie_id)).fetchall()
    if not rows or any(r["accepted_at"] for r in rows):
        return
    clicked = [r for r in rows if r["clicked_at"]]
    pick = (max(clicked, key=lambda r: r["clicked_at"]) if clicked
            else min(rows, key=lambda r: SOURCE_PRIORITY.index(r["source"])))
    conn.execute(f"UPDATE rec_log SET accepted_at={NOW} WHERE user_id=? AND movie_id=? AND source=?",
                 (user_id, movie_id, pick["source"]))


def _public_user(conn, user_id):
    row = conn.execute(
        "SELECT u.id, u.name, u.email, u.created_at, "
        "(SELECT COUNT(*) FROM ratings r WHERE r.user_id = u.id) AS num_ratings "
        "FROM users u WHERE u.id = ?", (user_id,)).fetchone()
    return {"id": row["id"], "name": row["name"], "email": row["email"],
            "member_since": (row["created_at"] or "")[:10], "num_ratings": row["num_ratings"],
            "is_admin": _is_admin(conn, user_id)}


def _start_session(user_id, remember):
    session.clear()
    session["user_id"] = user_id
    # Permanent = cookie survives closing the browser (30 days).
    session.permanent = bool(remember)


def _throttle_key(identifier):
    return f"{request.remote_addr}|{identifier.lower()}"


def _recent_failures(key):
    fails = current_app.config["LOGIN_FAILURES"]
    now = time.time()
    fails[key] = [t for t in fails.get(key, []) if now - t < FAILURE_WINDOW]
    return fails[key]


@bp.post("/api/register")
def register():
    body = request.get_json(silent=True) or {}
    name = str(body.get("username", "")).strip()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    if not USERNAME_RE.match(name):
        return _error("Username must be 3-24 characters: letters, numbers or underscores.")
    if name.lower().startswith("demo_user_"):
        return _error("That username is reserved. Pick another.")
    if not EMAIL_RE.match(email) or len(email) > 120:
        return _error("Enter a valid email address.")
    if not 8 <= len(password) <= 128:
        return _error("Password must be 8 to 128 characters.")
    if password.lower() in (name.lower(), email.lower()):
        return _error("Password can't be the same as your username or email.")

    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, created_at, fav_genres) "
            "VALUES (?, ?, ?, datetime('now'), ?)",
            (name, email, generate_password_hash(password), "|".join(_valid_genres(body.get("genres")))))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return _error("That username or email is already registered.", 409)
    if not os.environ.get("ADMIN_USERS") and not conn.execute("SELECT 1 FROM users WHERE is_admin=1 AND is_demo=0").fetchone():
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (cur.lastrowid,))
        conn.commit()
    _start_session(cur.lastrowid, body.get("remember", True))
    user = _public_user(conn, cur.lastrowid)
    conn.close()
    return jsonify(user), 201


@bp.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    identifier = str(body.get("identifier", "")).strip()
    password = str(body.get("password", ""))
    if not identifier or not password:
        return _error("Enter your username (or email) and password.")

    key = _throttle_key(identifier)
    if len(_recent_failures(key)) >= MAX_FAILURES:
        return _error("Too many failed attempts. Try again in a few minutes.", 429)

    conn = _conn()
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE is_demo = 0 AND "
        "(name = ? COLLATE NOCASE OR email = ? COLLATE NOCASE)", (identifier, identifier)).fetchone()
    # Always run one hash check so a missing account takes as long as a wrong password.
    ok = check_password_hash(row["password_hash"] if row and row["password_hash"] else _DUMMY_HASH, password)
    if not (row and row["password_hash"] and ok):
        conn.close()
        _recent_failures(key).append(time.time())
        return _error("Incorrect username or password.", 401)

    current_app.config["LOGIN_FAILURES"].pop(key, None)
    _start_session(row["id"], body.get("remember", False))
    user = _public_user(conn, row["id"])
    conn.close()
    return jsonify(user)


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/api/me")
def me():
    conn = _conn()
    user = _current_user(conn)
    data = _public_user(conn, user["id"]) if user else None
    if data:
        data["fav_genres"] = [g for g in (conn.execute("SELECT fav_genres FROM users WHERE id=?", (user["id"],)).fetchone()[0] or "").split("|") if g]
    conn.close()
    return jsonify({"user": data, "genres": _rec().all_genres})


# ---- helpers for signed-in actions ------------------------------------------------
def _need_user(conn):
    user = _current_user(conn)
    if not user:
        conn.close()
        return None, _error("Sign in first.", 401)
    return user, None


def _cards(cards):
    return jsonify(_rec().annotate(cards, _uid()))


# ---- catalogue -------------------------------------------------------------------
@bp.get("/api/genres")
def genres():
    return jsonify(_rec().all_genres)


@bp.get("/api/titles")
def titles():
    return jsonify(_rec().titles())


@bp.get("/api/movies")
def movies():
    data = _rec().browse(genre=request.args.get("genre", "").strip(),
                         sort=request.args.get("sort", "popular"),
                         page=max(1, request.args.get("page", 1, type=int)))
    _rec().annotate(data["results"], _uid())
    return jsonify(data)


@bp.get("/api/search")
def search():
    q = request.args.get("q", "").strip()[:120]
    if not q:
        return _error("Type something to search.")
    data = _rec().smart_search(q)
    _rec().annotate(data["results"], _uid())
    if request.args.get("save") and _uid() and len(q) >= 3:
        conn = _conn()
        conn.execute("DELETE FROM searches WHERE user_id=? AND query=? COLLATE NOCASE", (_uid(), q))
        conn.execute(f"INSERT INTO searches (user_id, query, searched_at) VALUES (?,?,{NOW})", (_uid(), q))
        conn.execute("DELETE FROM searches WHERE user_id=? AND id NOT IN "
                     "(SELECT id FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 30)", (_uid(), _uid()))
        conn.commit()
        conn.close()
    return jsonify(data)


@bp.get("/api/movies/<int:movie_id>")
def movie_detail(movie_id):
    rec = _rec()
    movie = rec.get_movie(movie_id)
    if movie is None:
        return _error("Movie not found", 404)
    uid = _uid()
    rec.annotate([movie], uid)
    seen = set()
    if uid:
        s = rec.user_signals(uid)
        movie["predicted"] = rec.predict(s, movie_id)
        seen = set(s["ratings"])
    movie["similar"] = rec.annotate(rec.similar(movie_id, n=8), uid)
    return jsonify(movie)


@bp.get("/api/similar/<int:movie_id>")
def similar(movie_id):
    if _rec().get_movie(movie_id) is None:
        return _error("Movie not found", 404)
    return _cards(_rec().similar(movie_id, n=12))


@bp.post("/api/movies/<int:movie_id>/view")
def record_view(movie_id):
    if _rec().get_movie(movie_id) is None:
        return _error("Movie not found", 404)
    conn = _conn()
    user = _current_user(conn)
    source = (request.get_json(silent=True) or {}).get("source")
    if user and source in TRACKED:            # opened from a recommendation card = a click
        conn.execute(f"UPDATE rec_log SET clicked_at={NOW} WHERE user_id=? AND movie_id=? AND source=? AND clicked_at IS NULL",
                     (user["id"], movie_id, source))
    if user:
        conn.execute(
            f"INSERT INTO views (user_id, movie_id, viewed_at, count) VALUES (?,?,{NOW},1) "
            f"ON CONFLICT(user_id, movie_id) DO UPDATE SET viewed_at={NOW}, count=count+1",
            (user["id"], movie_id))
        conn.commit()
        _rec().refresh(collab=False)
    conn.close()
    return jsonify({"ok": True})


# ---- ratings -----------------------------------------------------------------------
@bp.post("/api/ratings")
def rate():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    movie_id, rating = body.get("movie_id"), body.get("rating")
    if not isinstance(movie_id, int) or _rec().get_movie(movie_id) is None:
        conn.close()
        return _error("Unknown movie.", 404)
    if not isinstance(rating, (int, float)) or isinstance(rating, bool) or not 1 <= rating <= 5:
        conn.close()
        return _error("Rating must be a number from 1 to 5.")
    conn.execute(
        f"INSERT INTO ratings (user_id, movie_id, rating, rated_at) VALUES (?,?,?,{NOW}) "
        f"ON CONFLICT(user_id, movie_id) DO UPDATE SET rating=excluded.rating, rated_at={NOW}",
        (user["id"], movie_id, float(rating)))
    if rating >= 4:
        _mark_accepted(conn, user["id"], movie_id)
    conn.commit()
    conn.close()
    _rec().refresh()
    return jsonify({"movie_id": movie_id, "rating": float(rating)})


@bp.delete("/api/ratings/<int:movie_id>")
def unrate(movie_id):
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    conn.execute("DELETE FROM ratings WHERE user_id=? AND movie_id=?", (user["id"], movie_id))
    conn.commit()
    conn.close()
    _rec().refresh()
    return jsonify({"ok": True})


# ---- watchlist and favourites (same shape, different table) -------------------------
def _toggle_routes(table, noun):
    def add(movie_id):
        if _rec().get_movie(movie_id) is None:
            return _error("Movie not found", 404)
        conn = _conn()
        user, err = _need_user(conn)
        if err:
            return err
        conn.execute(f"INSERT OR IGNORE INTO {table} (user_id, movie_id, added_at) VALUES (?,?,{NOW})",
                     (user["id"], movie_id))
        _mark_accepted(conn, user["id"], movie_id)
        conn.commit()
        conn.close()
        _rec().refresh(collab=False)
        return jsonify({"ok": True, noun: True})

    def remove(movie_id):
        conn = _conn()
        user, err = _need_user(conn)
        if err:
            return err
        conn.execute(f"DELETE FROM {table} WHERE user_id=? AND movie_id=?", (user["id"], movie_id))
        conn.commit()
        conn.close()
        _rec().refresh(collab=False)
        return jsonify({"ok": True, noun: False})

    def listing():
        conn = _conn()
        user, err = _need_user(conn)
        if err:
            return err
        ids = [r[0] for r in conn.execute(
            f"SELECT movie_id FROM {table} WHERE user_id=? ORDER BY added_at DESC, rowid DESC", (user["id"],))]
        conn.close()
        return _cards(_rec().cards(ids))

    bp.add_url_rule(f"/api/{table}/<int:movie_id>", f"add_{table}", add, methods=["POST"])
    bp.add_url_rule(f"/api/{table}/<int:movie_id>", f"remove_{table}", remove, methods=["DELETE"])
    bp.add_url_rule(f"/api/me/{table}", f"list_{table}", listing)


_toggle_routes("watchlist", "in_watchlist")
_toggle_routes("favorites", "is_favorite")


@bp.get("/api/me/ratings")
def my_ratings():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    rows = conn.execute("SELECT movie_id FROM ratings WHERE user_id=? ORDER BY rated_at DESC, rowid DESC",
                        (user["id"],)).fetchall()
    conn.close()
    return _cards(_rec().cards([r[0] for r in rows]))


@bp.get("/api/me/recent")
def my_recent():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    ids = [r[0] for r in conn.execute(
        "SELECT movie_id FROM views WHERE user_id=? ORDER BY viewed_at DESC, rowid DESC LIMIT 24", (user["id"],))]
    conn.close()
    return _cards(_rec().cards(ids))


@bp.get("/api/me/profile")
def my_profile():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    conn.close()
    return jsonify(_rec().profile(_rec().user_signals(user["id"])))


@bp.put("/api/me/genres")
def set_genres():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    genres = _valid_genres((request.get_json(silent=True) or {}).get("genres"))
    conn.execute("UPDATE users SET fav_genres=? WHERE id=?", ("|".join(genres), user["id"]))
    conn.commit()
    conn.close()
    return jsonify({"genres": genres})


@bp.delete("/api/me/searches")
def clear_searches():
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    conn.execute("DELETE FROM searches WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---- recommendations ------------------------------------------------------------------
@bp.get("/api/home")
def home():
    uid = _uid()
    data = _rec().home(uid)
    if uid:                                   # log what this person was shown (once per movie and section)
        rows = [(uid, m["id"], sec["id"]) for sec in data["sections"] if sec["id"] in TRACKED for m in sec["results"]]
        conn = _conn()
        conn.executemany(f"INSERT OR IGNORE INTO rec_log (user_id, movie_id, source, shown_at) VALUES (?,?,?,{NOW})", rows)
        conn.commit()
        conn.close()
    return jsonify(data)


@bp.get("/api/recommendations")
def recommendations():
    uid = _uid()
    s = _rec().user_signals(uid) if uid else _rec()._empty_signals()
    strategy, results = _rec().recommend(s, 12)
    return jsonify({"strategy": strategy, "results": _rec().annotate(results, uid)})


@bp.get("/api/trending")
def trending():
    return _cards(_rec().trending(12))


@bp.get("/api/top-rated")
def top_rated():
    return _cards(_rec().top_rated(12))


@bp.get("/api/moods")
def moods():
    return jsonify(_rec().moods())


@bp.get("/api/mood/<key>")
def mood(key):
    uid = _uid()
    data = _rec().mood(key, _rec().user_signals(uid) if uid else None)
    if data is None:
        return _error("Unknown mood.", 404)
    _rec().annotate(data["results"], uid)
    return jsonify(data)


def _evaluation(repeats, refresh=False):
    """Run (or reuse) the offline evaluation. Cached per number of ratings, so new ratings invalidate it."""
    cache = current_app.config.setdefault("EVAL_CACHE", {})
    key = (repeats, len(_rec().ratings))
    fresh = refresh or key not in cache
    if fresh:
        cache.clear()
        cache[key] = _rec().evaluate(repeats=repeats)
    return cache[key], not fresh


@bp.get("/api/model-info")
def model_info():
    """Offline evaluation + blend weights (public: it describes the method, not any user)."""
    from . import recommender as r
    evaluation, _ = _evaluation(3)
    return jsonify({"evaluation": evaluation, "field_weights": r.FIELD_WEIGHTS,
                    "prediction_weights": r.PRED_WEIGHTS, "dev_gain": r.DEV_GAIN,
                    "catalogue": len(_rec().ids), "ratings": int(len(_rec().ratings))})


# ---- reviews and replies --------------------------------------------------------------
@bp.get("/api/movies/<int:movie_id>/reviews")
def list_reviews(movie_id):
    if _rec().get_movie(movie_id) is None:
        return _error("Movie not found", 404)
    conn = _conn()
    user = _current_user(conn)
    uid = user["id"] if user else None
    admin = bool(user) and _is_admin(conn, uid)
    rows = conn.execute(
        "SELECT r.id, r.parent_id, r.body, r.created_at, r.user_id, u.name, rt.rating FROM reviews r "
        "JOIN users u ON u.id = r.user_id LEFT JOIN ratings rt ON rt.user_id = r.user_id AND rt.movie_id = r.movie_id "
        "WHERE r.movie_id = ? ORDER BY r.id DESC", (movie_id,)).fetchall()
    conn.close()
    tops, replies = [], {}
    for r in rows:
        item = {"id": r["id"], "author": r["name"], "body": r["body"], "created_at": r["created_at"][:10],
                "rating": r["rating"], "mine": r["user_id"] == uid, "can_delete": r["user_id"] == uid or admin}
        if r["parent_id"] is None:
            tops.append(item)
        else:
            replies.setdefault(r["parent_id"], []).append(item)
    for t in tops:
        t["replies"] = list(reversed(replies.get(t["id"], [])))      # oldest reply first
    return jsonify({"count": len(tops), "reviews": tops})


@bp.post("/api/movies/<int:movie_id>/reviews")
def post_review(movie_id):
    if _rec().get_movie(movie_id) is None:
        return _error("Movie not found", 404)
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    text = str(body.get("body", "")).strip()
    parent_id = body.get("parent_id")
    if not 2 <= len(text) <= 1000:
        conn.close()
        return _error("Write between 2 and 1000 characters.")
    recent = conn.execute(f"SELECT COUNT(*) FROM reviews WHERE user_id=? AND created_at > strftime('%Y-%m-%d %H:%M:%f','now','-1 hour')",
                          (user["id"],)).fetchone()[0]
    if recent >= 30:
        conn.close()
        return _error("You're posting very quickly. Try again in a while.", 429)
    if parent_id is not None:
        ok = isinstance(parent_id, int) and conn.execute(
            "SELECT 1 FROM reviews WHERE id=? AND movie_id=? AND parent_id IS NULL", (parent_id, movie_id)).fetchone()
        if not ok:
            conn.close()
            return _error("That review no longer exists.", 404)
        conn.execute(f"INSERT INTO reviews (user_id, movie_id, parent_id, body, created_at) VALUES (?,?,?,?,{NOW})",
                     (user["id"], movie_id, parent_id, text))
        status = 201
    elif conn.execute("SELECT 1 FROM reviews WHERE user_id=? AND movie_id=? AND parent_id IS NULL",
                      (user["id"], movie_id)).fetchone():
        conn.execute("UPDATE reviews SET body=? WHERE user_id=? AND movie_id=? AND parent_id IS NULL",
                     (text, user["id"], movie_id))
        status = 200                                              # one review per person per movie: edit it
    else:
        conn.execute(f"INSERT INTO reviews (user_id, movie_id, body, created_at) VALUES (?,?,?,{NOW})",
                     (user["id"], movie_id, text))
        status = 201
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), status


@bp.delete("/api/reviews/<int:review_id>")
def delete_review(review_id):
    conn = _conn()
    user, err = _need_user(conn)
    if err:
        return err
    row = conn.execute("SELECT user_id FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        return _error("Review not found.", 404)
    if row["user_id"] != user["id"] and not _is_admin(conn, user["id"]):
        conn.close()
        return _error("You can only delete your own reviews.", 403)
    conn.execute("DELETE FROM reviews WHERE id=? OR parent_id=?", (review_id, review_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---- admin dashboard -----------------------------------------------------------------------
@bp.get("/api/admin/stats")
@admin_required
def admin_stats():
    from .analytics import dashboard_stats
    conn = _conn()
    data = dashboard_stats(conn, _rec(), include_demo=request.args.get("demo", "1") != "0")
    conn.close()
    return jsonify(data)


@bp.get("/api/admin/evaluation")
@admin_required
def admin_evaluation():
    repeats = min(10, max(1, request.args.get("repeats", 5, type=int)))
    evaluation, cached = _evaluation(repeats, refresh=bool(request.args.get("refresh")))
    return jsonify({"evaluation": evaluation, "cached": cached})
