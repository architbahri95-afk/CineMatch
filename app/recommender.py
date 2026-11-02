"""CineMatch recommendation engine.

ContentModel   TF-IDF per field (genres, keywords, plot, director, cast,
               language), weighted and concatenated; cosine similarity.
CollabModel    user-based and item-based collaborative filtering on
               mean-centred ratings, with significance weighting.
Recommender    hybrid ranking, rating prediction + confidence, explanations,
               trending, moods, natural-language search, evaluation.
"""
import json
import math
import os
import re
import threading
from urllib.parse import quote_plus
from collections import Counter

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

from . import nlp

FIELD_WEIGHTS = {"genres": 0.28, "keywords": 0.22, "plot": 0.20,
                 "director": 0.14, "cast": 0.12, "language": 0.04}
# Blend weights and gain were tuned on one train/test split and checked on four others (see README).
# DEV_GAIN undoes the shrinkage applied to each signal so predictions are not pulled to the user mean.
PRED_WEIGHTS = {"user": 0.10, "item": 0.15, "content": 0.65, "bias": 0.10}
DEV_GAIN = 3.0
EVENT_WEIGHTS = {"rating": 3.0, "favorite": 3.0, "watchlist": 2.0, "view": 1.0}
TREND_HALF_LIFE_DAYS = 7.0
WATCH_REGION = os.environ.get("WATCH_REGION", "in").lower()      # used for JustWatch links
ALGOS = [("popularity", "Most popular"), ("random", "Random"), ("content", "Content-based"),
         ("user_cf", "User-based CF"), ("item_cf", "Item-based CF"),
         ("collab", "Collaborative (user + item)"), ("hybrid", "Hybrid (CineMatch)")]
RANK_METRICS = ("precision", "recall", "f1", "ndcg", "hit")


def _pipe_tokens(s):
    return [t for t in s.split("|") if t]


def _mm(a, mask=None):
    """Min-max scale to [0, 1], optionally using only masked entries."""
    a = np.asarray(a, dtype=float)
    vals = a[mask] if mask is not None else a
    if vals.size == 0:
        return np.zeros_like(a)
    lo, hi = vals.min(), vals.max()
    return (a - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros_like(a)


def fmt_runtime(minutes):
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes else ""


# --------------------------------------------------------------------------
class ContentModel:
    def __init__(self, movies):
        pipe = dict(tokenizer=_pipe_tokens, token_pattern=None, lowercase=False)
        plot_text = movies["overview"] + " " + movies["keywords"].str.replace("|", " ", regex=False)
        sources = {
            "genres": (movies["genres"], pipe), "keywords": (movies["keywords"], pipe),
            "director": (movies["director"], pipe), "cast": (movies["actors"], pipe),
            "language": (movies["language"], pipe),
            "plot": (plot_text, dict(stop_words="english", sublinear_tf=True)),
        }
        blocks = []
        for name, (texts, kw) in sources.items():
            block = TfidfVectorizer(**kw).fit_transform(texts)
            blocks.append(normalize(block) * math.sqrt(FIELD_WEIGHTS[name]))
        self.X = normalize(hstack(blocks).tocsr())
        self.sim = cosine_similarity(self.X)
        # Separate plot+keyword space for free-text queries.
        self.text_vec = TfidfVectorizer(stop_words="english", sublinear_tf=True)
        self.text_X = self.text_vec.fit_transform(plot_text)
        self.attrs = [{
            "genres": set(_pipe_tokens(r.genres)), "keywords": set(_pipe_tokens(r.keywords)),
            "director": set(_pipe_tokens(r.director)), "cast": set(_pipe_tokens(r.actors)),
            "language": r.language,
        } for r in movies.itertuples()]

    def shared(self, i, j):
        """Human-readable attributes two movies have in common."""
        a, b = self.attrs[i], self.attrs[j]
        out = []
        if a["director"] & b["director"]:
            out.append("director " + ", ".join(sorted(a["director"] & b["director"])))
        if a["genres"] & b["genres"]:
            out.append("genres " + ", ".join(sorted(a["genres"] & b["genres"])))
        if a["cast"] & b["cast"]:
            names = sorted(a["cast"] & b["cast"])
            out.append(("actor " if len(names) == 1 else "actors ") + ", ".join(names))
        if a["keywords"] & b["keywords"]:
            out.append("themes " + ", ".join(sorted(a["keywords"] & b["keywords"])[:3]))
        if a["language"] == b["language"] and a["language"] != "English":
            out.append("language " + a["language"])
        return out


# --------------------------------------------------------------------------
class CollabModel:
    K_USERS = 25

    def __init__(self, ratings, movie_ids, content_sim):
        self.content_sim = content_sim
        midx = {m: i for i, m in enumerate(movie_ids)}
        users = sorted(ratings["user_id"].unique()) if len(ratings) else []
        self.uidx = {u: i for i, u in enumerate(users)}
        n_u, n_m = len(users), len(movie_ids)
        R = np.full((n_u, n_m), np.nan)
        if len(ratings):
            R[ratings["user_id"].map(self.uidx).values, ratings["movie_id"].map(midx).values] = ratings["rating"].values
        self.R, self.M = R, ~np.isnan(R)
        Mf = self.M.astype(float)
        self.n_ratings = Mf.sum(1)
        self.global_mean = float(np.nanmean(R)) if self.M.any() else 3.0
        self.mean = np.where(self.n_ratings > 0, np.nansum(R, 1) / np.maximum(self.n_ratings, 1), self.global_mean)
        self.C = np.where(self.M, R - self.mean[:, None], 0.0)
        # Significance weighting: similarity from few co-rated movies is down-weighted.
        user_sim = cosine_similarity(self.C) if n_u > 1 else np.zeros((n_u, n_u))
        self.user_sim = user_sim * np.minimum(1.0, (Mf @ Mf.T) / 8.0)
        np.fill_diagonal(self.user_sim, 0.0)
        item_sim = cosine_similarity(self.C.T) if n_u > 0 else np.zeros((n_m, n_m))
        self.item_sim = item_sim * np.minimum(1.0, (Mf.T @ Mf) / 5.0)
        np.fill_diagonal(self.item_sim, 0.0)
        # Item bias: how far a movie sits above/below each rater's own mean (shrunk).
        self.bias = self.C.sum(0) / (Mf.sum(0) + 5.0)

    def top_neighbours(self, pu):
        sims = np.clip(self.user_sim[pu], 0, None)
        top = np.argsort(-sims)[: self.K_USERS]
        w = np.zeros_like(sims)
        w[top] = sims[top]
        return w

    def components(self, pu):
        """Per-movie rating deviations from this user's mean, from each
        source. NaN means 'no evidence'."""
        w = self.top_neighbours(pu)
        den_u = w @ self.M
        user_dev = np.where(den_u > 1e-6, (w @ self.C) / (den_u + 0.5), np.nan)
        support = (w > 0).astype(float) @ self.M

        rated = np.flatnonzero(self.M[pu])
        cu = self.C[pu, rated]
        S = np.clip(self.item_sim[rated], 0, None)
        den_i = S.sum(0)
        item_dev = np.where(den_i > 1e-6, (cu @ S) / (den_i + 0.5), np.nan)
        Sc = np.clip(self.content_sim[rated], 0, None) ** 2
        den_c = Sc.sum(0)
        content_dev = np.where(den_c > 1e-6, (cu @ Sc) / (den_c + 0.3), np.nan)
        return {"user": user_dev, "item": item_dev, "content": content_dev,
                "bias": self.bias, "support": support, "den_i": den_i, "weights": w}

    def predict(self, pu, comp=None):
        """Hybrid rating prediction plus a 0-100 confidence heuristic for every movie."""
        comp = comp or self.components(pu)
        stack = np.vstack([comp[k] for k in PRED_WEIGHTS])
        avail = ~np.isnan(stack)
        wts = np.array(list(PRED_WEIGHTS.values()))[:, None] * avail
        dev = (np.where(avail, stack, 0) * wts).sum(0) / np.maximum(wts.sum(0), 1e-9)
        pred = np.clip(self.mean[pu] + DEV_GAIN * dev, 1.0, 5.0)

        three, cnt = stack[:3], avail[:3].sum(0)
        mu = np.where(avail[:3], three, 0).sum(0) / np.maximum(cnt, 1)
        spread = np.sqrt((np.where(avail[:3], (three - mu) ** 2, 0)).sum(0) / np.maximum(cnt, 1))
        agree = np.where(cnt >= 2, 1 - np.minimum(1, spread), 0.5)
        data = min(1.0, self.n_ratings[pu] / 12.0)
        supp = np.clip((comp["support"] / 6.0 + np.clip(comp["den_i"], 0, 1.5) / 1.5) / 2, 0, 1)
        conf = 100 * (0.4 * data + 0.3 * supp + 0.3 * agree)
        return pred, conf


# --------------------------------------------------------------------------
class Recommender:
    def __init__(self, conn_factory):
        self._cf = conn_factory
        self._lock = threading.RLock()
        self._load_movies()
        self.refresh()

    # ---- data loading ---------------------------------------------------
    def _load_movies(self):
        conn = self._cf()
        m = pd.read_sql_query("SELECT * FROM movies ORDER BY id", conn)
        conn.close()
        for col in ("director", "actors", "language", "tags", "poster_url", "trailer_url", "streaming",
                    "watch_link", "streaming_region", "extras_at"):
            m[col] = m[col].fillna("")
        m["runtime"] = m["runtime"].fillna(0).astype(int)
        self.movies = m
        self.ids = m["id"].tolist()
        self.idx = {mid: i for i, mid in enumerate(self.ids)}
        self.rows = m.to_dict("records")
        for r in self.rows:
            r["genre_list"] = _pipe_tokens(r["genres"])
            r["tag_list"] = _pipe_tokens(r["tags"])
            r["_trailer"], r["_watch"] = self._links(r)
        self.content = ContentModel(m)
        self.people = nlp.build_people_index(m)
        self.all_genres = sorted({g for r in self.rows for g in r["genre_list"]})

    @staticmethod
    def _links(r):
        """Trailer and where-to-watch info. Exact data comes from fetch_posters.py --extras;
        otherwise we fall back to search links, which always work but are not exact."""
        https = lambda u: u if str(u).startswith("https://") else ""
        trailer = https(r["trailer_url"])
        query = quote_plus(f"{r['title']} {r['year']} official trailer")
        providers = {"flatrate": [], "rent": [], "buy": []}
        try:
            providers.update(json.loads(r["streaming"]) if r["streaming"] else {})
        except ValueError:
            pass
        region = (r["streaming_region"] or WATCH_REGION).lower()
        link = https(r["watch_link"])
        return (
            {"url": trailer or f"https://www.youtube.com/results?search_query={query}", "exact": bool(trailer)},
            {"url": link or f"https://www.justwatch.com/{region}/search?q={quote_plus(r['title'])}",
             "exact": bool(link), "region": region.upper(), "providers": providers,
             "has_providers": any(providers.values()), "updated": (r["extras_at"] or "")[:10]},
        )

    def refresh(self, collab=True):
        """Reload ratings and activity. collab=False is the cheap path for
        views / watchlist / favourites, which only affect trending."""
        with self._lock:
            conn = self._cf()
            if collab or not hasattr(self, "model"):
                self.ratings = pd.read_sql_query(
                    "SELECT user_id, movie_id, rating, rated_at FROM ratings", conn)
                self.model = CollabModel(self.ratings, self.ids, self.content.sim)
                self._build_stats()
            activity = pd.read_sql_query(
                "SELECT movie_id, 'rating' AS kind, rated_at AS ts FROM ratings UNION ALL "
                "SELECT movie_id, 'favorite', added_at FROM favorites UNION ALL "
                "SELECT movie_id, 'watchlist', added_at FROM watchlist UNION ALL "
                "SELECT movie_id, 'view', viewed_at FROM views", conn)
            conn.close()
            self._build_trending(activity)

    def _build_stats(self):
        stats = self.ratings.groupby("movie_id")["rating"].agg(["mean", "count"]).reindex(self.ids)
        gm = self.ratings["rating"].mean() if len(self.ratings) else 3.0
        count = stats["count"].fillna(0).values
        mean = stats["mean"].fillna(gm).values
        self.popularity = (count * mean + 5 * gm) / (count + 5)   # Bayesian average
        self.rating_count = count.astype(int)
        self.avg_rating = np.where(count > 0, mean, np.nan)

    def _build_trending(self, activity):
        score = np.zeros(len(self.ids))
        if len(activity):
            ts = pd.to_datetime(activity["ts"], format="ISO8601", errors="coerce", utc=True)   # seeded rows have no milliseconds, new rows do
            age = ((pd.Timestamp.now(tz="UTC") - ts).dt.total_seconds() / 86400.0).fillna(30.0).clip(lower=0)
            decay = np.exp(-math.log(2) * age / TREND_HALF_LIFE_DAYS)
            weights = activity["kind"].map(EVENT_WEIGHTS).values * decay.values
            for mid, val in zip(activity["movie_id"].values, weights):
                if mid in self.idx:
                    score[self.idx[mid]] += val
        self.trend = score

    # ---- cards ------------------------------------------------------------
    def _card(self, i, **extra):
        r = self.rows[i]
        avg = self.avg_rating[i]
        card = {
            "id": int(r["id"]), "title": r["title"], "year": int(r["year"]),
            "genres": r["genre_list"], "overview": r["overview"],
            "director": r["director"].replace("|", ", "),
            "cast": _pipe_tokens(r["actors"]), "language": r["language"],
            "runtime": r["runtime"], "runtime_text": fmt_runtime(r["runtime"]),
            "tags": [nlp.TAG_LABELS.get(t, t) for t in r["tag_list"]],
            "poster_url": r["poster_url"], "trailer": r["_trailer"], "watch": r["_watch"],
            "avg_rating": None if np.isnan(avg) else round(float(avg), 2),
            "num_ratings": int(self.rating_count[i]),
        }
        card.update({k: v for k, v in extra.items() if v is not None})
        return card

    def annotate(self, cards, user_id):
        """Add the signed-in user's rating / watchlist / favourite flags."""
        if not user_id:
            return cards
        conn = self._cf()
        ratings = {r[0]: r[1] for r in conn.execute("SELECT movie_id, rating FROM ratings WHERE user_id=?", (user_id,))}
        wl = {r[0] for r in conn.execute("SELECT movie_id FROM watchlist WHERE user_id=?", (user_id,))}
        fav = {r[0] for r in conn.execute("SELECT movie_id FROM favorites WHERE user_id=?", (user_id,))}
        conn.close()
        for c in cards:
            c["my_rating"] = ratings.get(c["id"])
            c["in_watchlist"] = c["id"] in wl
            c["is_favorite"] = c["id"] in fav
        return cards

    def get_movie(self, movie_id):
        i = self.idx.get(movie_id)
        return None if i is None else self._card(i)

    def cards(self, movie_ids):
        return [self._card(self.idx[m]) for m in movie_ids if m in self.idx]

    def titles(self):
        return [{"id": r["id"], "title": r["title"], "year": r["year"]} for r in self.rows]

    # ---- catalogue --------------------------------------------------------
    def browse(self, genre="", sort="popular", page=1, per_page=18):
        hits = [i for i, r in enumerate(self.rows) if not genre or genre in r["genre_list"]]
        avg = lambda i: 0 if np.isnan(self.avg_rating[i]) else self.avg_rating[i]
        keys = {"popular": lambda i: -self.popularity[i], "rating": lambda i: -avg(i),
                "year": lambda i: -self.rows[i]["year"], "title": lambda i: self.rows[i]["title"],
                "runtime": lambda i: self.rows[i]["runtime"]}
        hits.sort(key=keys.get(sort, keys["popular"]))
        start = (page - 1) * per_page
        return {"total": len(hits), "page": page, "per_page": per_page,
                "results": [self._card(i) for i in hits[start:start + per_page]]}

    def trending(self, n=12):
        order = np.lexsort((-self.popularity, -self.trend))
        return [self._card(i, trend=round(float(self.trend[i]), 2)) for i in order[:n]]

    def top_rated(self, n=12, min_votes=5):
        ok = np.flatnonzero(self.rating_count >= min_votes)
        order = ok[np.argsort(-self.popularity[ok])]
        return [self._card(i) for i in order[:n]]

    # ---- similar movies -----------------------------------------------------
    def similar(self, movie_id, n=8, exclude=()):
        i = self.idx.get(movie_id)
        if i is None:
            return []
        collab = np.clip(self.model.item_sim[i], 0, None)
        blended = 0.7 * self.content.sim[i] + 0.3 * collab
        blended[i] = -1
        out = []
        for j in np.argsort(-blended):
            if self.ids[j] in exclude:
                continue
            out.append(self._card(j, similarity=round(float(blended[j]) * 100),
                                  shared=self.content.shared(i, j)))
            if len(out) == n:
                break
        return out

    # ---- user signals -------------------------------------------------------
    def user_signals(self, user_id):
        conn = self._cf()
        q = lambda sql: conn.execute(sql, (user_id,)).fetchall()
        rated = q("SELECT movie_id, rating FROM ratings WHERE user_id=? ORDER BY rated_at DESC, rowid DESC")
        row = conn.execute("SELECT fav_genres FROM users WHERE id=?", (user_id,)).fetchone()
        s = {
            "user_id": user_id,
            "ratings": {r[0]: r[1] for r in rated},
            "recent_rated": [r[0] for r in rated],
            "favorites": {r[0] for r in q("SELECT movie_id FROM favorites WHERE user_id=?")},
            "watchlist": {r[0] for r in q("SELECT movie_id FROM watchlist WHERE user_id=?")},
            "views": {r[0]: r[1] for r in q("SELECT movie_id, count FROM views WHERE user_id=?")},
            "recent_views": [r[0] for r in q("SELECT movie_id FROM views WHERE user_id=? ORDER BY viewed_at DESC, rowid DESC")],
            "searches": [r[0] for r in q("SELECT query FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 30")],
            "fav_genres": _pipe_tokens((row[0] if row else "") or ""),
        }
        conn.close()
        return s

    @staticmethod
    def _empty_signals():
        return {"user_id": None, "ratings": {}, "recent_rated": [], "favorites": set(), "watchlist": set(),
                "views": {}, "recent_views": [], "searches": [], "fav_genres": []}

    def _weights(self, s):
        """Per-movie preference weight: explicit ratings (mean-centred) plus
        implicit signals from favourites, watchlist and views."""
        w = np.zeros(len(self.ids))
        vals = np.array(list(s["ratings"].values()), dtype=float)
        mu = vals.mean() if len(vals) > 1 and vals.std() > 0 else 3.0
        for m, v in s["ratings"].items():
            w[self.idx[m]] += v - mu
        for m in s["favorites"]:
            w[self.idx[m]] += 2.0
        for m in s["watchlist"]:
            w[self.idx[m]] += 0.8
        for m, c in s["views"].items():
            w[self.idx[m]] += min(0.9, 0.3 * c)
        return w

    def _genre_pref(self, s, w):
        pref = Counter()
        for g in s["fav_genres"]:
            pref[g] += 2.0
        for q in s["searches"][:20]:
            for g in nlp.parse_query(q, self.people).genres:
                pref[g] += 0.5
        for j in np.flatnonzero(w):
            for g in self.rows[j]["genre_list"]:
                pref[g] += 0.35 * w[j]
        return pref

    @staticmethod
    def _strength(s):
        return len(s["ratings"]) + 0.5 * (len(s["favorites"]) + len(s["watchlist"]))

    def _seen_idx(self, s):
        return {self.idx[m] for m in set(s["ratings"]) | s["favorites"] | s["watchlist"]}

    # ---- hybrid ranking ------------------------------------------------------
    def _score(self, s, model):
        n = len(self.ids)
        w = self._weights(s)
        pref = self._genre_pref(s, w)
        gb = np.array([sum(pref.get(g, 0) for g in r["genre_list"]) for r in self.rows])
        content = (cosine_similarity(csr_matrix(w) @ self.content.X, self.content.X).ravel()
                   if np.any(w) else np.zeros(n))
        pu = model.uidx.get(s["user_id"])
        comp, collab = None, np.full(n, np.nan)
        if pu is not None and model.n_ratings[pu] >= 2:
            comp = model.components(pu)
            both = np.vstack([comp["user"], comp["item"]])
            ok = ~np.isnan(both)
            collab = np.where(ok.any(0), np.where(ok, both, 0).sum(0) / np.maximum(ok.sum(0), 1), np.nan)
        return {"w": w, "pref": pref, "gb": gb, "content": content, "collab": collab, "comp": comp, "pu": pu}

    def _rank(self, s, model=None, pop=None):
        model = model or self.model
        pop = self.popularity if pop is None else pop
        n = len(self.ids)
        cand = np.ones(n, bool)
        seen = self._seen_idx(s)
        if seen:
            cand[list(seen)] = False
        sc = self._score(s, model)
        strength = self._strength(s)
        pop_n, gb_n = _mm(pop, cand), _mm(sc["gb"], cand)
        if strength < 1:
            strategy = "genres"
            score = 0.6 * gb_n + 0.4 * pop_n
        elif strength < 3:
            strategy = "content"
            score = 0.80 * _mm(sc["content"], cand) + 0.12 * gb_n + 0.08 * pop_n
        else:
            strategy = "hybrid"
            n_rated = len(s["ratings"])
            alpha = 0.65 if n_rated < 5 else 0.55 if n_rated < 15 else 0.45
            collab = np.where(np.isnan(sc["collab"]), 0.0, sc["collab"])
            blend = alpha * _mm(sc["content"], cand) + (1 - alpha) * _mm(collab, cand)
            score = 0.88 * blend + 0.07 * gb_n + 0.05 * pop_n
        return np.where(cand, score, -np.inf), sc, strategy, model

    def recommend(self, s, n=12):
        """Personalised ranking. Returns (strategy, cards)."""
        if self._strength(s) < 1 and not s["fav_genres"]:
            return "trending", self.trending(n)
        score, sc, strategy, model = self._rank(s)
        pred = conf = None
        if sc["comp"] is not None and len(s["ratings"]) >= 3:
            pred, conf = model.predict(sc["pu"], sc["comp"])
        out = []
        for j in np.argsort(-score)[:n]:
            extra = {"reasons": self._reasons(j, s, sc, model, strategy)}
            if pred is not None:
                extra.update(predicted_rating=round(float(pred[j]), 1), confidence=int(round(conf[j])),
                             match=int(round((pred[j] - 1) / 4 * 100)))
            out.append(self._card(j, **extra))
        return strategy, out

    def predict(self, s, movie_id):
        """Predicted rating for one movie, with confidence and reasons."""
        j = self.idx.get(movie_id)
        pu = self.model.uidx.get(s["user_id"])
        if j is None or pu is None or len(s["ratings"]) < 3:
            return None
        comp = self.model.components(pu)
        pred, conf = self.model.predict(pu, comp)
        sc = {"pref": self._genre_pref(s, self._weights(s)), "comp": comp, "pu": pu}
        return {"rating": round(float(pred[j]), 1), "confidence": int(round(conf[j])),
                "match": int(round((pred[j] - 1) / 4 * 100)),
                "reasons": self._explain(j, s, sc, self.model)}

    def _reasons(self, j, s, sc, model, strategy):
        if strategy == "genres":
            mine = [g for g in self.rows[j]["genre_list"] if g in s["fav_genres"]]
            return [f"Matches genres you picked: {', '.join(mine)}"] if mine else []
        return self._explain(j, s, sc, model)

    def _explain(self, j, s, sc, model):
        """Up to three plain-English reasons for recommending movie j."""
        reasons = []
        liked = [(m, r) for m, r in s["ratings"].items() if r >= 4]
        liked += [(m, None) for m in s["favorites"] if m not in s["ratings"]]
        if liked:
            m, r = max(liked, key=lambda x: self.content.sim[self.idx[x[0]], j])
            i = self.idx[m]
            if self.content.sim[i, j] > 0.05:
                shared = self.content.shared(i, j)
                how = (f"you rated {self.rows[i]['title']} {r:g}/5" if r
                       else f"{self.rows[i]['title']} is one of your favorites")
                reasons.append(f"Because {how}" + (f". Shares {'; '.join(shared[:2])}" if shared else ""))
        top = [g for g, v in sc["pref"].most_common(3) if v > 0]
        match = [g for g in self.rows[j]["genre_list"] if g in top]
        if match:
            reasons.append(f"Matches your favorite genre{'s' if len(match) > 1 else ''}: {', '.join(match)}")
        if sc.get("pu") is not None and sc.get("comp") is not None:
            nb = np.flatnonzero((sc["comp"]["weights"] > 0) & model.M[:, j])
            nb = nb[model.R[nb, j] >= 4] if len(nb) else nb
            if len(nb) >= 2:
                reasons.append(f"{len(nb)} viewers with similar taste rated it {model.R[nb, j].mean():.1f}/5")
        return reasons[:3]

    # ---- home page sections ----------------------------------------------------
    def because_you_watched(self, s, rows=2, n=8):
        anchors = [m for m in s["recent_rated"] if s["ratings"][m] >= 4][:rows]
        anchors += [m for m in s["favorites"] if m not in anchors][: rows - len(anchors)]
        seen = set(s["ratings"]) | s["favorites"] | s["watchlist"]
        return [{"anchor": self.rows[self.idx[m]]["title"],
                 "results": self.similar(m, n, exclude=seen | {m})} for m in anchors]

    def by_favorite_genres(self, s, rows=2, n=8):
        top = [g for g, v in self._genre_pref(s, self._weights(s)).most_common(rows) if v > 0]
        if not top:
            return []
        score, *_ = self._rank(s)
        pop = _mm(self.popularity)
        out = []
        for g in top:
            idxs = [i for i, r in enumerate(self.rows) if g in r["genre_list"] and np.isfinite(score[i])]
            idxs.sort(key=lambda i: -(0.6 * score[i] + 0.4 * pop[i]))
            out.append({"genre": g, "results": [self._card(i) for i in idxs[:n]]})
        return out

    def home(self, user_id=None):
        s = self.user_signals(user_id) if user_id else self._empty_signals()
        strength = self._strength(s)
        sections = []
        strategy, recs = self.recommend(s, 12)
        if strategy != "trending":
            sections.append({"id": "recommended", "title": "Recommended for you", "results": recs})
        if strength >= 1:
            for row in self.because_you_watched(s):
                if row["results"]:
                    sections.append({"id": "because", "title": f"Because you watched {row['anchor']}",
                                     "results": row["results"]})
        if strength >= 1 or s["fav_genres"]:
            for row in self.by_favorite_genres(s):
                sections.append({"id": "genre", "title": f"Based on your love of {row['genre']}",
                                 "results": row["results"]})
        if s["recent_views"]:
            sections.append({"id": "recent", "title": "Recently viewed", "results": self.cards(s["recent_views"][:10])})
        sections.append({"id": "trending", "title": "Trending now", "results": self.trending(10)})
        sections.append({"id": "top", "title": "Top rated", "results": self.top_rated(10)})
        for sec in sections:
            self.annotate(sec["results"], user_id)
        return {"strategy": strategy, "sections": sections, "strength": strength}

    # ---- profile --------------------------------------------------------------
    def profile(self, s):
        w = self._weights(s)
        pos = [(g, v) for g, v in self._genre_pref(s, w).most_common(6) if v > 0]
        top_v = pos[0][1] if pos else 1
        people = {"director": Counter(), "actors": Counter()}
        for j in np.flatnonzero(w > 0):
            for col in people:
                for name in _pipe_tokens(self.rows[j][col]):
                    people[col][name] += w[j]
        vals = list(s["ratings"].values())
        if not pos:
            summary = "Rate a few movies and your taste profile will appear here."
        else:
            summary = "You lean toward " + " and ".join(g for g, _ in pos[:2]) + "."
            if people["director"]:
                summary += f" You often enjoy films by {people['director'].most_common(1)[0][0]}."
        return {
            "counts": {"rated": len(vals), "favorites": len(s["favorites"]), "watchlist": len(s["watchlist"]),
                       "viewed": len(s["views"]), "searches": len(s["searches"])},
            "avg_rating_given": round(sum(vals) / len(vals), 2) if vals else None,
            "top_genres": [{"genre": g, "score": round(100 * v / top_v)} for g, v in pos],
            "top_directors": [n for n, _ in people["director"].most_common(3)],
            "top_actors": [n for n, _ in people["actors"].most_common(4)],
            "recent_searches": s["searches"][:8], "picked_genres": s["fav_genres"], "summary": summary,
        }

    # ---- moods ------------------------------------------------------------------
    def moods(self):
        return [{"key": k, "label": v["label"], "blurb": v["blurb"], "genres": list(v["genres"])}
                for k, v in nlp.MOODS.items()]

    def mood(self, key, s=None, n=12):
        spec = nlp.MOODS.get(key)
        if spec is None:
            return None
        raw = np.array([
            sum(spec["genres"].get(g, 0) for g in r["genre_list"])
            + sum(spec["tags"].get(t, 0) for t in r["tag_list"])
            - 0.8 * sum(spec["avoid"].get(t, 0) for t in r["tag_list"]) for r in self.rows])
        eligible = raw > 0
        personal = np.zeros(len(raw))
        if s and self._strength(s) >= 1:
            score, *_ = self._rank(s)
            personal = np.where(np.isfinite(score), score, 0)
            eligible &= np.isfinite(score)          # hide movies they've already rated/saved
        final = 0.65 * _mm(raw, eligible) + 0.25 * _mm(personal, eligible) + 0.10 * _mm(self.popularity, eligible)
        final = np.where(eligible, final, -np.inf)
        out = []
        for j in np.argsort(-final)[:n]:
            if not np.isfinite(final[j]):
                break
            r = self.rows[j]
            why = ([g for g in r["genre_list"] if g in spec["genres"]]
                   + [nlp.TAG_LABELS[t] for t in r["tag_list"] if t in spec["tags"]])
            out.append(self._card(j, reasons=[f"Fits a {spec['label'].lower()} mood: " + ", ".join(why)] if why else None))
        return {"mood": {"key": key, "label": spec["label"], "blurb": spec["blurb"]}, "results": out}

    # ---- smart / natural-language search -----------------------------------------
    def smart_search(self, q, limit=24):
        q = q.strip()
        p = nlp.parse_query(q, self.people)
        low = q.lower()
        # A query that is a whole-word phrase (or a prefix) of a title is a title lookup,
        # so "dark knight" finds The Dark Knight instead of being parsed as tone + actress.
        pattern = re.compile(r"\b" + re.escape(low) + r"\b")
        titles = [i for i, r in enumerate(self.rows)
                  if pattern.search(r["title"].lower()) or (len(low) >= 3 and r["title"].lower().startswith(low))]
        if titles:
            titles.sort(key=lambda i: (not self.rows[i]["title"].lower().startswith(low), -self.popularity[i]))
            return {"mode": "title", "understood": [], "relaxed": False,
                    "results": [self._card(i) for i in titles[:limit]]}

        n = len(self.rows)
        mask = np.ones(n, bool)
        for i, r in enumerate(self.rows):
            if p.year and ((p.year[0] and r["year"] < p.year[0]) or (p.year[1] and r["year"] > p.year[1])):
                mask[i] = False
            if p.runtime and ((p.runtime[0] and r["runtime"] < p.runtime[0])
                              or (p.runtime[1] and r["runtime"] > p.runtime[1])):
                mask[i] = False
            if p.language and r["language"] != p.language:
                mask[i] = False
            if p.people and not (set(p.people) & set(_pipe_tokens(r["director"]) + _pipe_tokens(r["actors"]))):
                mask[i] = False
        g_hit = np.array([sum(g in r["genre_list"] for g in p.genres) / len(p.genres) if p.genres else 0
                          for r in self.rows])
        t_hit = np.array([sum(t in r["tag_list"] for t in p.tags) / len(p.tags) if p.tags else 0
                          for r in self.rows])
        sim = np.zeros(n)
        if p.text:
            sim = cosine_similarity(self.content.text_vec.transform([p.text]), self.content.text_X).ravel()
        relaxed = False
        strict = mask & (g_hit > 0) if p.genres else mask.copy()
        if p.text and not p.has_constraints:
            strict &= sim > 0
        if p.genres and strict.sum() < 3:
            strict, relaxed = mask.copy(), True
        score = 0.40 * g_hit + 0.25 * t_hit + 0.25 * _mm(sim, strict) + 0.10 * _mm(self.popularity, strict)
        if p.people:
            score = score + 0.3
        if p.sort == "rating":
            score = _mm(self.popularity, strict) + 0.05 * score
        elif p.sort == "trending":
            score = _mm(self.trend, strict) + 0.05 * score
        score = np.where(strict, score, -np.inf)
        out = []
        for j in np.argsort(-score)[:limit]:
            if not np.isfinite(score[j]):
                break
            r = self.rows[j]
            why = ([g for g in p.genres if g in r["genre_list"]]
                   + [nlp.TAG_LABELS[t] for t in p.tags if t in r["tag_list"]])
            out.append(self._card(j, reasons=["Matches: " + ", ".join(why)] if why else None))
        notes = list(p.notes) + ([f"plot/themes: {p.text}"] if p.text else [])
        return {"mode": "natural", "understood": notes, "relaxed": relaxed, "results": out}

    # ---- offline evaluation ---------------------------------------------------------------------
    @staticmethod
    def _bayes_pop(train, ids):
        """Popularity from TRAINING ratings only (using all ratings would leak the test set)."""
        stats = train.groupby("movie_id")["rating"].agg(["mean", "count"]).reindex(ids)
        gm = train["rating"].mean()
        c, mu = stats["count"].fillna(0).values, stats["mean"].fillna(gm).values
        return (c * mu + 5 * gm) / (c + 5)

    def _algo_scores(self, s, model, pop, rng):
        """Score every unseen movie with each algorithm, for one user."""
        n = len(self.ids)
        cand = np.ones(n, bool)
        seen = self._seen_idx(s)
        if seen:
            cand[list(seen)] = False
        sc = self._score(s, model)
        tie = 1e-6 * _mm(pop, cand)                       # break ties by popularity
        fin = lambda a: np.where(cand, a, -np.inf)
        zero = lambda a: np.where(np.isnan(a), 0.0, a)
        comp = sc["comp"]
        out = {"popularity": fin(pop), "random": fin(rng.random(n)),
               "content": fin(sc["content"] + tie), "hybrid": self._rank(s, model, pop)[0]}
        if comp is not None:
            out["user_cf"] = fin(zero(comp["user"]) + tie)
            out["item_cf"] = fin(zero(comp["item"]) + tie)
            out["collab"] = fin(zero(sc["collab"]) + tie)
        else:                                              # no neighbours yet: nothing to filter on
            for k in ("user_cf", "item_cf", "collab"):
                out[k] = fin(pop)
        return out

    def evaluate(self, repeats=3, test_frac=0.2, ks=(5, 10, 20), seed=0, relevant=4.0):
        """Hold out 20% of each active user's ratings (repeated over several random splits), then
        compare rating predictors (RMSE/MAE) and top-K rankers (precision, recall, F1, NDCG, hit
        rate, coverage). A held-out rating >= `relevant` counts as a movie the user would like."""
        df = self.ratings
        k_main = 10 if 10 in ks else ks[0]
        pred_names = ["global_mean", "user_mean", "item_bias", "content_only", "user_cf", "item_cf", "hybrid"]
        pred_runs, rank_runs, pooled = [], [], {a: [] for a, _ in ALGOS}
        n_users, n_test, n_train = [], [], []
        for r in range(repeats):
            rng = np.random.default_rng(seed + r)
            tr_idx, te_idx = [], []
            for _, grp in df.groupby("user_id"):
                ids = grp.index.to_numpy().copy()
                rng.shuffle(ids)
                cut = int(len(ids) * test_frac) if len(ids) >= 10 else 0
                te_idx += ids[:cut].tolist()
                tr_idx += ids[cut:].tolist()
            train, test = df.loc[tr_idx], df.loc[te_idx]
            model, pop = CollabModel(train, self.ids, self.content.sim), self._bayes_pop(train, self.ids)
            n_test.append(len(test)); n_train.append(len(train))

            # -- rating prediction
            preds, actual, cache = {n: [] for n in pred_names}, [], {}
            for t in test.itertuples():
                pu, j = model.uidx[t.user_id], self.idx[t.movie_id]
                if pu not in cache:
                    comp = model.components(pu)
                    cache[pu] = (comp, model.predict(pu, comp)[0])
                comp, hyb = cache[pu]
                mu = model.mean[pu]
                pick = lambda key: float(np.clip(mu + DEV_GAIN * (0 if np.isnan(comp[key][j]) else comp[key][j]), 1, 5))
                preds["global_mean"].append(model.global_mean); preds["user_mean"].append(mu)
                preds["item_bias"].append(float(np.clip(mu + DEV_GAIN * comp["bias"][j], 1, 5)))
                preds["content_only"].append(pick("content")); preds["user_cf"].append(pick("user"))
                preds["item_cf"].append(pick("item")); preds["hybrid"].append(float(hyb[j]))
                actual.append(t.rating)
            actual = np.array(actual)
            pred_runs.append({n: (float(np.sqrt(np.mean((np.array(p) - actual) ** 2))),
                                  float(np.mean(np.abs(np.array(p) - actual)))) for n, p in preds.items()})

            # -- top-K ranking
            per = {a: {k: {m: [] for m in RANK_METRICS} for k in ks} for a, _ in ALGOS}
            top_sets = {a: {k: set() for k in ks} for a, _ in ALGOS}
            users = 0
            for uid, grp in test.groupby("user_id"):
                held = set(grp[grp["rating"] >= relevant]["movie_id"])
                if not held:
                    continue
                users += 1
                tr = train[train["user_id"] == uid]
                s = self._empty_signals()
                s.update(user_id=uid, ratings=dict(zip(tr["movie_id"], tr["rating"])))
                for algo, score in self._algo_scores(s, model, pop, rng).items():
                    order = np.argsort(-score)
                    for k in ks:
                        top = order[:k]
                        gains = [1.0 if self.ids[j] in held else 0.0 for j in top]
                        hits = sum(gains)
                        p_, r_ = hits / k, hits / len(held)
                        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
                        idcg = sum(1 / math.log2(i + 2) for i in range(min(len(held), k)))
                        row = per[algo][k]
                        row["precision"].append(p_); row["recall"].append(r_)
                        row["f1"].append(2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0)
                        row["ndcg"].append(dcg / idcg); row["hit"].append(1.0 if hits else 0.0)
                        top_sets[algo][k].update(self.ids[j] for j in top)
                    pooled[algo].append(per[algo][k_main]["precision"][-1])
            n_users.append(users)
            rank_runs.append({a: {k: {**{m: float(np.mean(per[a][k][m])) for m in RANK_METRICS},
                                      "coverage": len(top_sets[a][k]) / len(self.ids)} for k in ks} for a, _ in ALGOS})

        sd = lambda xs: float(np.std(xs, ddof=1)) if len(xs) > 1 else 0.0
        rating = {n: {"rmse": round(float(np.mean([r[n][0] for r in pred_runs])), 3),
                      "rmse_std": round(sd([r[n][0] for r in pred_runs]), 3),
                      "mae": round(float(np.mean([r[n][1] for r in pred_runs])), 3),
                      "mae_std": round(sd([r[n][1] for r in pred_runs]), 3)} for n in pred_names}
        ranking = {}
        for k in ks:
            ranking[str(k)] = {}
            for a, _ in ALGOS:
                row = {}
                for m in (*RANK_METRICS, "coverage"):
                    vals = [run[a][k][m] for run in rank_runs]
                    row[m], row[m + "_std"] = round(float(np.mean(vals)), 4), round(sd(vals), 4)
                ranking[str(k)][a] = row
        # Paired bootstrap over users: is the hybrid's precision@K gain over each other algorithm real?
        brng = np.random.default_rng(seed + 999)
        h = np.array(pooled["hybrid"])
        comparisons = {}
        for a, _ in ALGOS:
            if a == "hybrid" or not len(h):
                continue
            d = h - np.array(pooled[a])
            means = d[brng.integers(0, len(d), (2000, len(d)))].mean(1)
            lo, hi = np.percentile(means, [2.5, 97.5])
            comparisons[a] = {"mean_diff": round(float(d.mean()), 4), "ci_low": round(float(lo), 4),
                              "ci_high": round(float(hi), 4), "significant": bool(lo > 0 or hi < 0)}
        return {"config": {"repeats": repeats, "test_frac": test_frac, "ks": list(ks), "k": k_main,
                           "relevant": relevant, "users": int(round(np.mean(n_users))),
                           "test_ratings": int(round(np.mean(n_test))), "train_ratings": int(round(np.mean(n_train)))},
                "algorithms": [{"key": a, "label": l} for a, l in ALGOS],
                "rating_prediction": rating, "ranking": ranking, "comparisons": comparisons}
