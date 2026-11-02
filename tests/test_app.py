import sqlite3

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    return app.test_client()


def signup(client, name="tester", email=None, password="correct horse 9", **extra):
    return client.post("/api/register", json={
        "username": name, "email": email or f"{name}@example.com", "password": password, **extra})


def rate(client, ids, value=5):
    for mid in ids:
        assert client.post("/api/ratings", json={"movie_id": mid, "rating": value}).status_code == 200


def titles(items):
    return [m["title"] for m in items]


# ---- accounts ------------------------------------------------------------------------
def test_register_validation(client):
    assert signup(client, "ab").status_code == 400
    assert signup(client, "has space").status_code == 400
    assert signup(client, "demo_user_1").status_code == 400
    assert signup(client, "okname", email="not-an-email").status_code == 400
    assert signup(client, "okname", password="short").status_code == 400
    assert signup(client, "okname", password="okname").status_code == 400


def test_register_login_logout_roundtrip(client):
    r = signup(client, "alice")
    assert r.status_code == 201 and r.json["name"] == "alice" and "password" not in str(r.json).lower()
    client.post("/api/logout")
    assert client.get("/api/me").json["user"] is None
    assert client.post("/api/login", json={"identifier": "alice", "password": "nope nope"}).status_code == 401
    assert client.post("/api/login", json={"identifier": "ALICE", "password": "correct horse 9"}).status_code == 200
    client.post("/api/logout")
    assert client.post("/api/login", json={"identifier": "Alice@Example.com", "password": "correct horse 9"}).status_code == 200


def test_duplicates_and_hashing(client):
    assert signup(client, "bob").status_code == 201
    assert signup(client, "BOB", email="other@example.com").status_code == 409
    assert signup(client, "bob2", email="BOB@example.com").status_code == 409
    from app import db
    conn = db.connect(client.application.config["DB_PATH"])
    stored = conn.execute("SELECT password_hash FROM users WHERE name='bob'").fetchone()[0]
    conn.close()
    assert stored and "correct horse 9" not in stored


def test_everything_survives_restart(tmp_path):
    path = str(tmp_path / "persist.db")
    c1 = create_app(path).test_client()
    signup(c1, "dave", genres=["Sci-Fi", "Not A Genre"])
    rate(c1, [3])
    c1.post("/api/watchlist/8"); c1.post("/api/favorites/6"); c1.post("/api/movies/7/view")
    c2 = create_app(path).test_client()           # new process, new browser
    assert c2.post("/api/login", json={"identifier": "dave", "password": "correct horse 9"}).status_code == 200
    assert [m["id"] for m in c2.get("/api/me/ratings").json] == [3]
    assert [m["id"] for m in c2.get("/api/me/watchlist").json] == [8]
    assert [m["id"] for m in c2.get("/api/me/favorites").json] == [6]
    assert [m["id"] for m in c2.get("/api/me/recent").json] == [7]
    assert c2.get("/api/me").json["user"]["fav_genres"] == ["Sci-Fi"]     # invalid genre dropped


def test_remember_me_and_lockout(client):
    signup(client, "erin", remember=True)
    assert client.get_cookie("session").expires is not None
    client.post("/api/logout")
    signup(client, "frank", remember=False)
    assert client.get_cookie("session").expires is None
    client.post("/api/logout")
    for _ in range(5):
        assert client.post("/api/login", json={"identifier": "frank", "password": "wrong wrong"}).status_code == 401
    assert client.post("/api/login", json={"identifier": "frank", "password": "correct horse 9"}).status_code == 429


# ---- movie details ---------------------------------------------------------------------
def test_movie_detail_fields(client):
    m = client.get("/api/movies/8").json            # Interstellar
    assert m["title"] == "Interstellar" and m["year"] == 2014
    assert m["director"] == "Christopher Nolan" and "Matthew McConaughey" in m["cast"]
    assert m["runtime"] == 169 and m["runtime_text"] == "2h 49m" and m["language"] == "English"
    assert "Sci-Fi" in m["genres"] and m["overview"] and m["num_ratings"] > 0 and m["avg_rating"]
    assert "poster_url" in m and len(m["similar"]) == 8
    assert client.get("/api/movies/999").status_code == 404


def test_browse_genres_and_sorting(client):
    assert {"Action", "Comedy", "Horror", "Sci-Fi"} <= set(client.get("/api/genres").json)
    horror = client.get("/api/movies?genre=Horror").json
    assert horror["total"] >= 5 and all("Horror" in m["genres"] for m in horror["results"])
    years = [m["year"] for m in client.get("/api/movies?sort=year").json["results"]]
    assert years == sorted(years, reverse=True)


# ---- search --------------------------------------------------------------------------------
def test_title_search_prefers_titles(client):
    assert titles(client.get("/api/search?q=dark knight").json["results"])[0] == "The Dark Knight"
    r = client.get("/api/search?q=inter").json
    assert r["mode"] == "title" and titles(r["results"]) == ["Interstellar"]


def test_natural_language_search(client):
    r = client.get("/api/search?q=funny movies with a happy ending").json
    assert r["mode"] == "natural" and any("Comedy" in n for n in r["understood"])
    assert r["results"] and all("Comedy" in m["genres"] and "happy ending" in m["tags"] for m in r["results"][:5])

    r = client.get("/api/search?q=sci-fi from the 90s").json
    assert r["results"] and all(1990 <= m["year"] <= 1999 and "Sci-Fi" in m["genres"] for m in r["results"])

    r = client.get("/api/search?q=christopher nolan movies under 2 hours").json
    assert r["results"] and all(m["director"] == "Christopher Nolan" and m["runtime"] <= 120 for m in r["results"])

    r = client.get("/api/search?q=korean").json
    assert titles(r["results"]) == ["Parasite"]
    assert client.get("/api/search?q=").status_code == 400


def test_search_history_saved_only_when_asked(client):
    signup(client)
    client.get("/api/search?q=space survival")
    assert client.get("/api/me/profile").json["recent_searches"] == []
    client.get("/api/search?q=space survival&save=1")
    client.get("/api/search?q=space survival&save=1")           # de-duplicated
    assert client.get("/api/me/profile").json["recent_searches"] == ["space survival"]
    client.delete("/api/me/searches")
    assert client.get("/api/me/profile").json["recent_searches"] == []


# ---- watchlist / favourites / recently viewed --------------------------------------------
def test_watchlist_favorites_and_flags(client):
    assert client.post("/api/watchlist/1").status_code == 401
    signup(client)
    client.post("/api/watchlist/1"); client.post("/api/watchlist/1")           # idempotent
    client.post("/api/favorites/2")
    assert [m["id"] for m in client.get("/api/me/watchlist").json] == [1]
    assert client.get("/api/movies/1").json["in_watchlist"] is True
    assert client.get("/api/movies/2").json["is_favorite"] is True
    assert client.get("/api/movies/3").json["in_watchlist"] is False
    client.delete("/api/watchlist/1")
    assert client.get("/api/me/watchlist").json == []


def test_recently_viewed_orders_by_latest(client):
    signup(client)
    for mid in (1, 2, 3, 1):
        client.post(f"/api/movies/{mid}/view")
    assert [m["id"] for m in client.get("/api/me/recent").json] == [1, 3, 2]


# ---- recommendations -------------------------------------------------------------------------
def test_new_user_cold_start(client):
    home = client.get("/api/home").json                      # signed out
    assert home["strategy"] == "trending"
    assert [s["id"] for s in home["sections"]] == ["trending", "top"]
    signup(client, "newbie")
    assert client.get("/api/recommendations").json["strategy"] == "trending"


def test_cold_start_with_picked_genres(client):
    signup(client, "picky", genres=["Horror"])
    rec = client.get("/api/recommendations").json
    assert rec["strategy"] == "genres"
    assert sum("Horror" in m["genres"] for m in rec["results"][:5]) >= 4
    assert rec["results"][0]["reasons"][0].startswith("Matches genres you picked")


def test_hybrid_recommendations_predictions_and_explanations(client):
    signup(client, "cinephile")
    scifi = [6, 7, 8, 26]
    rate(client, scifi, 5)
    home = client.get("/api/home").json
    assert home["strategy"] == "hybrid"
    ids = [s["id"] for s in home["sections"]]
    assert ids[0] == "recommended" and "because" in ids and "genre" in ids and "trending" in ids and "top" in ids
    recs = home["sections"][0]["results"]
    assert not {m["id"] for m in recs} & set(scifi)
    assert sum("Sci-Fi" in m["genres"] for m in recs[:6]) >= 3
    top = recs[0]
    assert 1 <= top["predicted_rating"] <= 5 and 0 <= top["confidence"] <= 100 and 0 <= top["match"] <= 100
    assert any(r.startswith("Because you rated") for r in top["reasons"])
    assert any("Matches your favorite genre" in r for r in top["reasons"])


def test_rating_prediction_on_detail_page(client):
    signup(client, "predictor")
    assert client.get("/api/movies/9").json["predicted"] is None          # too few ratings
    rate(client, [6, 7, 8, 26], 5); rate(client, [43, 44], 1)
    pred = client.get("/api/movies/16").json["predicted"]                  # Star Wars: sci-fi
    comedy = client.get("/api/movies/42").json["predicted"]
    assert pred["rating"] > comedy["rating"]
    assert 0 <= pred["confidence"] <= 100 and pred["reasons"]


def test_collaborative_filtering_uses_similar_users(tmp_path):
    """Two users agree on many movies; the one who loved an extra movie should pull it up for the other.
    (Ratings must vary: users who give everything 5 stars have no taste signal after mean-centring.)"""
    app = create_app(str(tmp_path / "cf.db"))
    a, b = app.test_client(), app.test_client()
    signup(a, "usera"); signup(b, "userb")
    pattern = {1: 5, 2: 5, 10: 4, 34: 5, 35: 2, 4: 5, 9: 1, 12: 2, 43: 1}
    for client in (a, b):
        for mid, val in pattern.items():
            client.post("/api/ratings", json={"movie_id": mid, "rating": val})
    b.post("/api/ratings", json={"movie_id": 36, "rating": 5})   # only B has seen There Will Be Blood
    s = app.recommender.user_signals(a.get("/api/me").json["user"]["id"])
    _, sc, _, _ = app.recommender._rank(s)
    j = app.recommender.idx[36]
    assert sc["comp"]["support"][j] >= 1 and sc["comp"]["user"][j] > 0   # neighbour evidence pushes it up
    assert 36 in [m["id"] for m in a.get("/api/recommendations").json["results"]]


def test_moods(client):
    assert {m["key"] for m in client.get("/api/moods").json} >= {"happy", "excited", "relaxed"}
    happy = client.get("/api/mood/happy").json["results"]
    assert sum("Comedy" in m["genres"] or "Family" in m["genres"] for m in happy[:6]) >= 5
    excited = client.get("/api/mood/excited").json["results"]
    assert sum("Action" in m["genres"] or "Adventure" in m["genres"] for m in excited[:6]) >= 5
    assert client.get("/api/mood/nonsense").status_code == 404


def test_trending_reacts_to_activity(client):
    rec = client.application.recommender
    signup(client)
    target = 5
    before_score, before_rank = rec.trend[rec.idx[target]], int((rec.trend > rec.trend[rec.idx[target]]).sum())
    for _ in range(3):
        client.post(f"/api/movies/{target}/view")                    # repeat views count once per user
    client.post(f"/api/favorites/{target}")                          # favourite weight 3
    client.post(f"/api/watchlist/{target}")                          # watchlist weight 2
    rate(client, [target], 5)                                        # rating weight 3
    gained = rec.trend[rec.idx[target]] - before_score
    assert 8.5 < gained <= 9.0                                       # view 1 + favourite 3 + watchlist 2 + rating 3, almost no decay
    assert int((rec.trend > rec.trend[rec.idx[target]]).sum()) < before_rank


def test_old_activity_counts_less_than_new(client):
    rec = client.application.recommender
    import numpy as np
    assert np.exp(-np.log(2) * 14 / 7) == pytest.approx(0.25)       # two half-lives = quarter weight
    assert rec.trend.max() > 0 and len(rec.trending(5)) == 5


def test_similar_movie_finder_explains_itself(client):
    sim = client.get("/api/similar/8").json                     # Interstellar
    assert len(sim) == 12 and "Interstellar" not in titles(sim)
    top3 = sim[:3]
    assert any("Sci-Fi" in m["genres"] for m in top3)
    assert any("director Christopher Nolan" in " ".join(m["shared"]) for m in sim[:6])


def test_profile_reflects_taste(client):
    signup(client)
    rate(client, [6, 7, 8, 26, 16], 5)
    client.post("/api/favorites/6")
    p = client.get("/api/me/profile").json
    assert p["counts"]["rated"] == 5 and p["avg_rating_given"] == 5
    assert p["top_genres"][0]["genre"] in ("Sci-Fi", "Action", "Adventure") and p["top_genres"][0]["score"] == 100
    assert p["summary"].startswith("You lean toward")


# ---- model quality ---------------------------------------------------------------------------------
def test_offline_evaluation_compares_algorithms(client):
    ev = client.application.recommender.evaluate(repeats=2)
    err, rank, cfg = ev["rating_prediction"], ev["ranking"]["10"], ev["config"]
    assert cfg["repeats"] == 2 and cfg["ks"] == [5, 10, 20] and cfg["users"] > 20
    assert err["hybrid"]["rmse"] < err["global_mean"]["rmse"] and err["hybrid"]["rmse"] < err["user_mean"]["rmse"]
    assert err["hybrid"]["rmse"] <= min(err["user_cf"]["rmse"], err["item_cf"]["rmse"])
    for algo in ("popularity", "random", "content", "user_cf", "item_cf", "collab", "hybrid"):
        row = rank[algo]
        assert all(0 <= row[m] <= 1 for m in ("precision", "recall", "f1", "ndcg", "hit", "coverage"))
        p, r = row["precision"], row["recall"]
        assert row["f1"] <= max(p, r) + 1e-9                          # mean of per-user F1 can't exceed the best of P and R
    # the hybrid should beat the non-personalised baselines and the collaborative-only models
    for weaker in ("random", "popularity", "user_cf", "item_cf", "collab"):
        assert rank["hybrid"]["hit"] > rank[weaker]["hit"], weaker
        assert rank["hybrid"]["precision"] > rank[weaker]["precision"], weaker
    assert set(ev["comparisons"]) == {"popularity", "random", "content", "user_cf", "item_cf", "collab"}
    assert ev["comparisons"]["random"]["significant"] and ev["comparisons"]["random"]["ci_low"] > 0
    # precision@K is bounded by how many liked movies were held out, so higher K must not lower recall
    assert ev["ranking"]["20"]["hybrid"]["recall"] >= ev["ranking"]["5"]["hybrid"]["recall"]


def test_popularity_baseline_does_not_peek_at_test_ratings(client):
    """Popularity must come from training ratings only, otherwise evaluation leaks the answers."""
    import numpy as np, pandas as pd
    rec = client.application.recommender
    train = rec.ratings.iloc[: len(rec.ratings) // 2]
    pop = rec._bayes_pop(train, rec.ids)
    assert not np.allclose(pop, rec.popularity)
    first = train[train["movie_id"] == rec.ids[0]]["rating"]
    expected = (len(first) * first.mean() + 5 * train["rating"].mean()) / (len(first) + 5)
    assert pop[0] == pytest.approx(expected)


def test_model_info_is_public_and_cached(client):
    info = client.get("/api/model-info").json
    assert info["prediction_weights"] and info["evaluation"]["config"]["users"] > 20


# ---- reviews and replies --------------------------------------------------------------------
def test_reviews_post_edit_reply_delete(client):
    assert client.get("/api/movies/8/reviews").json == {"count": 0, "reviews": []}      # anyone can read
    assert client.post("/api/movies/8/reviews", json={"body": "Great"}).status_code == 401
    signup(client, "ann")
    rate(client, [8], 5)
    assert client.post("/api/movies/8/reviews", json={"body": "x"}).status_code == 400   # too short
    assert client.post("/api/movies/8/reviews", json={"body": "y" * 1001}).status_code == 400
    assert client.post("/api/movies/8/reviews", json={"body": "Stunning visuals, big ideas."}).status_code == 201
    assert client.post("/api/movies/8/reviews", json={"body": "Edited: even better on rewatch."}).status_code == 200  # edit, not duplicate
    data = client.get("/api/movies/8/reviews").json
    assert data["count"] == 1 and data["reviews"][0]["body"].startswith("Edited") and data["reviews"][0]["rating"] == 5
    assert data["reviews"][0]["mine"] and data["reviews"][0]["author"] == "ann"
    rid = data["reviews"][0]["id"]

    client.post("/api/logout"); signup(client, "bobby")
    assert client.post("/api/movies/8/reviews", json={"body": "Agreed!", "parent_id": rid}).status_code == 201
    assert client.post("/api/movies/9/reviews", json={"body": "Wrong movie", "parent_id": rid}).status_code == 404
    reply = client.get("/api/movies/8/reviews").json["reviews"][0]["replies"]
    assert [r["body"] for r in reply] == ["Agreed!"] and reply[0]["mine"] is True
    assert client.delete(f"/api/reviews/{rid}").status_code == 403           # not bobby's review (bobby is not an admin)
    client.post("/api/logout"); client.post("/api/login", json={"identifier": "ann", "password": "correct horse 9"})
    assert client.delete(f"/api/reviews/{rid}").status_code == 200           # ann deletes her review and its replies
    assert client.get("/api/movies/8/reviews").json["count"] == 0


def test_review_text_is_returned_as_data_not_html(client):
    signup(client)
    client.post("/api/movies/1/reviews", json={"body": "<script>alert(1)</script> nice"})
    assert client.get("/api/movies/1/reviews").json["reviews"][0]["body"] == "<script>alert(1)</script> nice"


# ---- trailers and where to watch ------------------------------------------------------------
def test_trailer_and_watch_links_fall_back_to_search(client):
    m = client.get("/api/movies/8").json
    assert m["trailer"]["exact"] is False and m["trailer"]["url"].startswith("https://www.youtube.com/results?search_query=Interstellar")
    assert m["watch"]["exact"] is False and "justwatch.com/" in m["watch"]["url"] and not m["watch"]["has_providers"]


def test_exact_trailer_and_providers_from_extras_csv(tmp_path, monkeypatch):
    import shutil, json
    from app import db
    data = tmp_path / "data"; shutil.copytree(db.DATA_DIR, data)
    (data / "extras.csv").write_text(
        "id,trailer_url,streaming,watch_link,region,fetched_at\n"
        '8,https://www.youtube.com/watch?v=abc123,"' + json.dumps({"flatrate": ["Netflix"], "rent": ["Apple TV"], "buy": []}).replace('"', '""') + '",'
        "https://www.themoviedb.org/movie/157336/watch?locale=IN,IN,2026-11-01T10:00:00Z\n")
    monkeypatch.setattr(db, "DATA_DIR", data)
    m = create_app(str(tmp_path / "x.db")).test_client().get("/api/movies/8").json
    assert m["trailer"] == {"url": "https://www.youtube.com/watch?v=abc123", "exact": True}
    assert m["watch"]["providers"]["flatrate"] == ["Netflix"] and m["watch"]["region"] == "IN"
    assert m["watch"]["has_providers"] and m["watch"]["updated"] == "2026-11-01"


def test_non_https_links_in_data_are_ignored(tmp_path):
    from app.recommender import Recommender
    r = {"title": "T", "year": 2000, "trailer_url": "javascript:alert(1)", "streaming": "", "watch_link": "http://evil",
         "streaming_region": "", "extras_at": ""}
    trailer, watch = Recommender._links(r)
    assert trailer["exact"] is False and watch["exact"] is False and trailer["url"].startswith("https://www.youtube.com")


# ---- admin dashboard ---------------------------------------------------------------------------
def test_admin_access_rules(client):
    assert client.get("/api/admin/stats").status_code == 401
    assert signup(client, "boss").json["is_admin"] is True                    # first real account is the admin
    assert client.get("/api/admin/stats").status_code == 200
    client.post("/api/logout")
    assert signup(client, "regular").json["is_admin"] is False
    assert client.get("/api/admin/stats").status_code == 403
    assert client.get("/api/admin/evaluation").status_code == 403


def test_admin_users_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "Chief")
    c = create_app(str(tmp_path / "env.db")).test_client()
    assert signup(c, "first").json["is_admin"] is False                      # no auto-admin when ADMIN_USERS is set
    c.post("/api/logout")
    assert signup(c, "chief").json["is_admin"] is True


def test_dashboard_stats_content(client):
    signup(client, "boss")
    s = client.get("/api/admin/stats").json
    t = s["totals"]
    assert t["users_real"] == 1 and t["users_demo"] == 60 and t["movies"] == 70 and t["ratings"] > 1000
    assert 1 < s["avg_rating"] < 5 and s["genres"][0]["ratings"] >= s["genres"][-1]["ratings"]
    assert len(s["activity"]["days"]) == 30 and sum(s["activity"]["ratings"]) > 0
    assert sum(d["count"] for d in s["rating_distribution"]) == t["ratings"]
    only_real = client.get("/api/admin/stats?demo=0").json
    assert only_real["totals"]["ratings"] == 0 and only_real["totals"]["users_demo"] == 0
    assert s["funnel"]["all"]["impressions"] == 0 and s["funnel"]["all"]["ctr"] is None     # nothing shown yet


def test_recommendation_funnel_impression_click_acceptance(client):
    signup(client, "boss"); rate(client, [6, 7, 8, 26], 5)
    sections = client.get("/api/home").json["sections"]
    rec = sections[0]["results"]
    target, other = rec[0]["id"], rec[1]["id"]
    f = client.get("/api/admin/stats").json["funnel"]
    assert f["personalised"]["impressions"] >= 12 and f["personalised"]["clicks"] == 0 and f["personalised"]["accepted"] == 0
    client.get("/api/home")                                                     # reloading must not inflate impressions
    assert client.get("/api/admin/stats").json["funnel"]["all"]["impressions"] == f["all"]["impressions"]

    client.post(f"/api/movies/{target}/view", json={"source": "recommended"})   # click
    client.post(f"/api/movies/{target}/view", json={"source": "recommended"})   # repeat click counts once
    client.post(f"/api/movies/{other}/view", json={"source": "search"})         # not from a recommendation: no click
    client.post("/api/ratings", json={"movie_id": target, "rating": 5})         # accept
    client.post("/api/ratings", json={"movie_id": other, "rating": 2})          # a low rating is not acceptance
    f2 = client.get("/api/admin/stats").json["funnel"]["personalised"]
    assert f2["clicks"] == 1 and f2["accepted"] == 1
    assert f2["ctr"] == pytest.approx(1 / f2["impressions"], abs=1e-3) and f2["acceptance"] == pytest.approx(1 / f2["impressions"], abs=1e-3)
    by_src = {r["source"]: r for r in client.get("/api/admin/stats").json["funnel"]["by_source"]}
    assert by_src["recommended"]["clicks"] == 1 and "trending" in by_src
    most = client.get("/api/admin/stats").json["most_recommended"]
    assert most and most[0]["shown_to"] == 1
    client.post(f"/api/watchlist/{rec[2]['id']}")                               # saving also counts as acceptance
    assert client.get("/api/admin/stats").json["funnel"]["personalised"]["accepted"] == 2


def test_dashboard_activity_counts_new_events(client):
    signup(client, "boss")
    before = client.get("/api/admin/stats?demo=0").json
    client.post("/api/ratings", json={"movie_id": 3, "rating": 4}); client.post("/api/movies/3/view")
    client.post("/api/movies/3/reviews", json={"body": "Solid."}); client.get("/api/search?q=space&save=1")
    after = client.get("/api/admin/stats?demo=0").json
    assert after["activity"]["ratings"][-1] == 1 and after["activity"]["views"][-1] == 1
    assert after["activity"]["reviews"][-1] == 1 and after["activity"]["searches"][-1] == 1
    assert after["active_users_7d"] == 1 and before["active_users_7d"] == 0


# ---- upgrading an old database ----------------------------------------------------------------
def test_migrates_database_from_previous_version(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE movies (id INTEGER PRIMARY KEY, title TEXT NOT NULL, year INTEGER, genres TEXT NOT NULL,
                             keywords TEXT NOT NULL, overview TEXT NOT NULL);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                            is_demo INTEGER NOT NULL DEFAULT 0, email TEXT, password_hash TEXT, created_at TEXT);
        CREATE TABLE ratings (user_id INTEGER, movie_id INTEGER, rating REAL, PRIMARY KEY (user_id, movie_id));
        INSERT INTO movies VALUES (8, 'Interstellar', 2014, 'Sci-Fi', 'space', 'Explorers travel through a wormhole.');
        INSERT INTO users (name, is_demo) VALUES ('legacy', 1);
        INSERT INTO ratings VALUES (1, 8, 4.5);
    """)
    old.commit(); old.close()
    from app import db
    db.init_db(path)
    conn = db.connect(path)
    m = conn.execute("SELECT director, runtime, tags FROM movies WHERE id=8").fetchone()
    r = conn.execute("SELECT rating, rated_at FROM ratings").fetchone()
    conn.close()
    assert m["director"] == "Christopher Nolan" and m["runtime"] == 169 and m["tags"]
    assert r["rating"] == 4.5 and r["rated_at"]
    conn = db.connect(path)
    tables = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {x[1] for x in conn.execute("PRAGMA table_info(movies)")} | {x[1] for x in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert {"reviews", "rec_log", "watchlist"} <= tables and {"trailer_url", "streaming", "is_admin"} <= cols
