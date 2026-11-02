"""Numbers for the admin dashboard: totals, genres, ratings, recommendation funnel, activity."""
from datetime import datetime, timedelta, timezone

PERSONALISED = ("recommended", "because", "genre")          # sources that are personalised to the user
SOURCE_LABELS = {"recommended": "Recommended for you", "because": "Because you watched",
                 "genre": "Based on your genres", "trending": "Trending", "top": "Top rated"}


def _rate(num, den):
    return round(num / den, 4) if den else None


def dashboard_stats(conn, rec, include_demo=True):
    uf = "" if include_demo else " AND u.is_demo = 0"
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    joined = lambda t: f"FROM {t} x JOIN users u ON u.id = x.user_id WHERE 1=1{uf}"

    users_real, users_demo = one("SELECT COUNT(*) FROM users WHERE is_demo=0"), one("SELECT COUNT(*) FROM users WHERE is_demo=1")
    totals = {
        "users_real": users_real, "users_demo": users_demo if include_demo else 0,
        "movies": len(rec.ids),
        "ratings": one(f"SELECT COUNT(*) {joined('ratings')}"),
        "reviews": one(f"SELECT COUNT(*) {joined('reviews')} AND x.parent_id IS NULL"),
        "comments": one(f"SELECT COUNT(*) {joined('reviews')} AND x.parent_id IS NOT NULL"),
        "watchlist": one(f"SELECT COUNT(*) {joined('watchlist')}"),
        "favorites": one(f"SELECT COUNT(*) {joined('favorites')}"),
        "views": one(f"SELECT COUNT(*) {joined('views')}"),
        "searches": one(f"SELECT COUNT(*) {joined('searches')}"),
    }
    rated_users = one(f"SELECT COUNT(DISTINCT x.user_id) {joined('ratings')}")
    avg = conn.execute(f"SELECT AVG(x.rating) {joined('ratings')}").fetchone()[0]

    distribution = [{"rating": r[0], "count": r[1]} for r in conn.execute(
        f"SELECT x.rating, COUNT(*) {joined('ratings')} GROUP BY x.rating ORDER BY x.rating")]

    per_movie = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT x.movie_id, COUNT(*), SUM(x.rating) {joined('ratings')} GROUP BY x.movie_id")}
    genres = {}
    for i, row in enumerate(rec.rows):
        cnt, total = per_movie.get(row["id"], (0, 0.0))
        for g in row["genre_list"]:
            d = genres.setdefault(g, {"genre": g, "movies": 0, "ratings": 0, "sum": 0.0})
            d["movies"] += 1; d["ratings"] += cnt; d["sum"] += total
    genre_rows = sorted(({"genre": d["genre"], "movies": d["movies"], "ratings": d["ratings"],
                          "avg_rating": round(d["sum"] / d["ratings"], 2) if d["ratings"] else None}
                         for d in genres.values()), key=lambda d: -d["ratings"])
    title = {r["id"]: r["title"] for r in rec.rows}
    best = sorted(((mid, c, t / c) for mid, (c, t) in per_movie.items() if c >= 5), key=lambda x: -x[2])[:5]
    top_rated = [{"title": title[m], "avg_rating": round(a, 2), "ratings": c} for m, c, a in best]

    # --- recommendation funnel (real users only: demo users never see the app)
    by_source = []
    for src, imps, clicks, acc in conn.execute(
            "SELECT source, COUNT(*), COALESCE(SUM(clicked_at IS NOT NULL),0), COALESCE(SUM(accepted_at IS NOT NULL),0) "
            "FROM rec_log GROUP BY source ORDER BY COUNT(*) DESC"):
        by_source.append({"source": src, "label": SOURCE_LABELS.get(src, src), "personalised": src in PERSONALISED,
                          "impressions": imps, "clicks": clicks, "accepted": acc,
                          "ctr": _rate(clicks, imps), "acceptance": _rate(acc, imps)})
    def funnel(rows):
        i, c, a = (sum(r[k] for r in rows) for k in ("impressions", "clicks", "accepted"))
        return {"impressions": i, "clicks": c, "accepted": a, "ctr": _rate(c, i), "acceptance": _rate(a, i)}
    marks = ",".join("?" * len(PERSONALISED))
    most = [{"title": title[m], "shown_to": n, "clicks": c, "accepted": a} for m, n, c, a in conn.execute(
        f"SELECT movie_id, COUNT(DISTINCT user_id), COUNT(DISTINCT CASE WHEN clicked_at IS NOT NULL THEN user_id END), "
        f"COUNT(DISTINCT CASE WHEN accepted_at IS NOT NULL THEN user_id END) FROM rec_log "
        f"WHERE source IN ({marks}) GROUP BY movie_id ORDER BY 2 DESC, 4 DESC, 3 DESC LIMIT 10",
        PERSONALISED) if m in title]

    # --- activity over the last 30 days
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]
    def daily(table, col, extra=""):
        got = dict(conn.execute(
            f"SELECT substr(x.{col},1,10), COUNT(*) {joined(table)} AND x.{col} >= date('now','-29 days'){extra} GROUP BY 1").fetchall())
        return [got.get(d, 0) for d in days]
    saves = [a + b for a, b in zip(daily("watchlist", "added_at"), daily("favorites", "added_at"))]
    new_users = dict(conn.execute("SELECT substr(created_at,1,10), COUNT(*) FROM users WHERE is_demo=0 "
                                  "AND created_at >= date('now','-29 days') GROUP BY 1").fetchall())
    activity = {"days": days, "ratings": daily("ratings", "rated_at"), "views": daily("views", "viewed_at"),
                "reviews": daily("reviews", "created_at"), "saves": saves, "searches": daily("searches", "searched_at"),
                "new_users": [new_users.get(d, 0) for d in days]}
    active_7d = one(
        "SELECT COUNT(DISTINCT a.uid) FROM (SELECT user_id uid, rated_at ts FROM ratings UNION ALL "
        "SELECT user_id, viewed_at FROM views UNION ALL SELECT user_id, added_at FROM watchlist UNION ALL "
        "SELECT user_id, added_at FROM favorites UNION ALL SELECT user_id, created_at FROM reviews UNION ALL "
        f"SELECT user_id, searched_at FROM searches) a JOIN users u ON u.id = a.uid WHERE a.ts >= date('now','-6 days'){uf}")

    return {
        "include_demo": include_demo, "totals": totals, "active_users_7d": active_7d,
        "avg_rating": round(avg, 2) if avg else None,
        "ratings_per_user": round(totals["ratings"] / rated_users, 1) if rated_users else None,
        "rating_distribution": distribution, "genres": genre_rows, "top_rated": top_rated,
        "funnel": {"personalised": funnel([r for r in by_source if r["personalised"]]),
                   "all": funnel(by_source), "by_source": by_source},
        "most_recommended": most, "activity": activity,
        "personalised_users": int(sum(1 for n in rec.model.n_ratings if n >= 3)),
    }
