# CineMatch: a hybrid movie recommendation system

A full-stack Python project (Flask + SQLite + scikit-learn + vanilla JS). It recommends movies
with **content-based**, **collaborative** and **hybrid** filtering, explains every
recommendation, predicts how you'd rate a movie, and handles brand-new users.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                      # http://127.0.0.1:5000
pytest                             # 36 tests
python evaluate.py                 # offline algorithm comparison (writes evaluation_report.json)
```

First run creates `cinematch.db` (70 movies + 60 synthetic "demo" users so collaborative
filtering has data) and `.secret_key`. **Keep both**: they hold accounts and sign-in sessions.
Upgrading from an earlier version? Your existing `cinematch.db` is migrated automatically.

## Feature checklist

| Area | Feature | Where |
|---|---|---|
| Basics | Registration and login (hashed passwords, 30-day "keep me signed in") | `routes.py` |
| | Search by name, with partial titles ("inter" finds Interstellar) | `smart_search` |
| | Movie details: title, poster, year, genres, rating, duration, description, director, cast, language | detail dialog |
| | Genre browsing and sorting | Browse tab |
| | Ratings (1-5), **watchlist**, **favorites**, **recently viewed** | cards, detail dialog, My library |
| Recommenders | **Content-based**: genre, director, actors, keywords, plot, language | `ContentModel` |
| | **Collaborative**: user-based and item-based | `CollabModel` |
| | **Hybrid** blend, weights adapt to how much you've rated | `Recommender._rank` |
| Personalization | Taste profile from ratings, favorites, watchlist, views, searches and picked genres | My library, Taste profile |
| | Sections: Recommended for you, Because you watched…, Based on your favorite genres, Recently viewed, Trending, Top rated | For you tab |
| Advanced | **Mood** recommendations (Happy, Excited, Relaxed, Romantic, Thrilled, Thoughtful) | Mood tab |
| | **Natural-language search**: "funny movies with a happy ending", "sci-fi from the 90s", "Nolan under 2 hours" | search box |
| | **Similar movie finder** with shared director/genres/cast/themes | Find similar tab |
| | **Explainable** recommendations ("Because you rated Interstellar 5/5. Shares director Christopher Nolan") | every pick |
| | **Rating prediction** + **confidence** + match % | detail page, picks |
| | **Cold start**: pick genres at sign-up, otherwise trending/top-rated; content-only until 3+ signals | `recommend` |
| | **Trending**: recency-weighted activity (7-day half-life) | `_build_trending` |
| Community | **Reviews and replies** on every movie (one review per person, editable, deletable) | detail dialog |
| | **Trailer links** and **where to watch** (streaming providers by country) | detail dialog |
| Analytics | **Admin dashboard**: users, movies, genres, ratings, recommendation click and acceptance rates, activity, model metrics | Dashboard tab |
| | **Algorithm comparison**: Precision/Recall/F1/NDCG@K, RMSE/MAE, significance tests | Dashboard, `evaluate.py` |

## How the recommender works

1. **Content model.** One TF-IDF block per field, each L2-normalised and scaled by a weight
   (genres .28, keywords .22, plot .20, director .14, cast .12, language .04), concatenated,
   then compared with cosine similarity.
2. **Collaborative model.** Ratings are mean-centred per user. *User-based*: weighted average of
   the 25 most similar users' deviations. *Item-based*: movies similar to ones you rated.
   Similarities from few co-rated movies are down-weighted (significance weighting).
3. **Preference weights.** Your rating minus your average, +2 for a favorite, +0.8 for a watchlist
   item, up to +0.9 for repeated views. Searches and picked genres feed a genre profile.
4. **Hybrid ranking.** `alpha * content + (1 - alpha) * collaborative`, plus a small genre and
   popularity prior. Alpha is 0.65 with <5 ratings, 0.55 with <15, else 0.45. With under 3 signals
   it uses content only; with none it falls back to your picked genres, or trending.
5. **Rating prediction.** `your mean + gain * (0.10 user + 0.15 item + 0.65 content + 0.10 movie bias)`,
   clipped to 1-5. **Confidence** (0-100) combines how many ratings you've given, how many similar
   users/items back the prediction, and how much the sources agree. It is a heuristic, not a
   statistical probability.
6. **Natural language.** A rule-based parser extracts genres, mood/ending tags, decades, runtime,
   language, directors and actors; leftover words are matched against plot and keywords with TF-IDF.
   The search box shows what it understood.

## Comparing the algorithms

`python evaluate.py` (or the Dashboard tab) holds out 20% of each active user's ratings, repeated over
5 random splits, and compares seven approaches. A held-out rating of 4 or more counts as "the user would
like this". Popularity is computed from training ratings only, so the test set never leaks in.

Top-10 recommendations (mean over 5 splits, about 34 users per split):

| Algorithm | Precision@10 | Recall@10 | F1@10 | NDCG@10 | Hit rate |
|---|---|---|---|---|---|
| Random | 0.024 | 0.164 | 0.041 | 0.089 | 0.23 |
| Most popular | 0.033 | 0.217 | 0.056 | 0.132 | 0.31 |
| User-based CF | 0.043 | 0.284 | 0.073 | 0.164 | 0.38 |
| Item-based CF | 0.047 | 0.321 | 0.080 | 0.175 | 0.41 |
| Collaborative (user + item) | 0.044 | 0.294 | 0.074 | 0.178 | 0.39 |
| Content-based | 0.063 | 0.452 | 0.108 | **0.253** | **0.58** |
| **Hybrid (CineMatch)** | **0.064** | **0.460** | **0.110** | 0.241 | 0.56 |

Rating prediction (RMSE, lower is better): global average 1.007, user's own average 1.025,
collaborative-only about 1.08, content-based 0.900, **hybrid 0.896**.

**What this shows, honestly:**
- The hybrid is **significantly better** than random, most-popular and both collaborative-only methods
  (paired bootstrap over users, 95% confidence intervals exclude zero).
- The hybrid is **not significantly better than content-based alone** on this data (the confidence
  interval for the difference includes zero), and content-based is slightly ahead on NDCG@10 and at K = 5.
  That is expected here: the 60 demo users' ratings are generated from genre tastes, which favours content
  features. With real ratings (e.g. MovieLens) collaborative filtering usually contributes more, so
  re-run the comparison before claiming the hybrid wins.
- **Precision@K looks small by construction.** Each user has only a few held-out liked movies, so even a
  perfect top-10 could not reach 1.0 (two liked movies gives a maximum of 0.2). Compare algorithms against
  each other, and lean on recall, NDCG and hit rate.
- Coverage shows how much of the catalogue gets recommended: most-popular covers only about a quarter.

## Dashboard (admins)

The first real account created is an admin automatically. Add others with
`python make_admin.py USERNAME`, or set `ADMIN_USERS=alice,bob`. Admins get a **Dashboard** tab with:

- users (real and simulated), movies, ratings, reviews, average rating, active users (7 days)
- most popular genres, rating distribution, highest-rated movies
- **recommendation funnel:** impressions, clicks and acceptance, per section of the For you page.
  An *impression* is a movie shown once in a section; a *click* is opening it from there; *accepted*
  means rating it 4+ or adding it to favorites or the watchlist (counted once per movie, credited to
  one section). Counts build up from real use, so a fresh install shows zeros here.
- most recommended movies, and 30 days of user activity
- model performance with a K selector, all metrics above, significance tests and RMSE/MAE

## Reviews, trailers and where to watch

- Anyone can read reviews; signed-in users write one review per movie (editing replaces it), reply to
  others, and delete their own. Admins can delete any. Text is stored and shown as plain text.
- **Trailer** and **Where to watch** buttons always work: by default they open a YouTube search and a
  JustWatch search for your region (`WATCH_REGION`, default `in`). For exact trailers and streaming
  providers, run `python fetch_posters.py YOUR_KEY --extras --region IN` once (TMDB data, same retry and
  resume behaviour as posters). Provider availability changes often, so the page shows when it was fetched.

## Real movie posters

By default CineMatch draws coloured placeholder posters, because it ships without image files.
To show real posters:

1. Make a free account at themoviedb.org and request an API key (Settings > API, "API Key (v3 auth)").
2. Run `python fetch_posters.py YOUR_KEY` once (needs internet).
3. Restart the app and hard-refresh the browser (Ctrl+Shift+R).

If it stops with a network error, just run the same command again: it keeps its progress, skips
posters it already has and retries temporary failures. If it keeps failing, try another network
(a phone hotspot often works), toggle a VPN, or set your DNS to 1.1.1.1.

The script looks up each movie by title and year, saves the links to `app/data/posters.csv`
(so they survive a database reset) and updates the database. Posters load from
`image.tmdb.org`, so the browser needs internet access. Any movie it can't find keeps the
placeholder. Posters are TMDB's images; check their terms if you publish the project.

## Honest limitations

- **The ratings are synthetic.** The 60 demo users are generated from genre tastes, so
  content signals dominate and collaborative filtering adds little here. The blend weights were
  tuned on one split and checked on four others, but on real data (e.g. MovieLens) re-tune them.
  Treat the numbers above as a sanity check, not a benchmark.
- **Posters are generated art by default**; run `fetch_posters.py` for real ones (see above).
- **Review moderation is minimal**: length limits, a posting-rate limit and admin delete. Add a report
  button and profanity filtering before opening this to the public.
- **Mood and ending tags are hand-curated** for these 70 movies (`movies_meta.csv`). Directors,
  cast and runtimes are from memory of well-known films; spot-check before relying on them.
- Users who give every movie the same rating have no taste signal after mean-centring.
- No email verification or password reset. For production set `SECRET_KEY` and `COOKIE_SECURE=1`,
  serve over HTTPS and run behind gunicorn (`gunicorn "app:create_app()"`).

## Project layout

```
app/recommender.py   ContentModel, CollabModel, Recommender (all the ML)
app/nlp.py           natural-language parser and mood definitions
app/analytics.py     dashboard statistics
app/routes.py        JSON API and accounts
app/db.py            schema, migrations, seeding
app/data/            movies.csv, movies_meta.csv
app/static, app/templates   frontend
tests/test_app.py    36 tests (API, accounts, models, evaluation, reviews, dashboard, migration)
evaluate.py          offline evaluation
fetch_posters.py     optional: posters, trailers and streaming providers from TMDB
make_admin.py        grant or remove dashboard access
```

## API (JSON)

`POST /api/register` `/login` `/logout` · `GET /api/me` · `PUT /api/me/genres`
`GET /api/movies?genre=&sort=&page=` · `GET /api/movies/<id>` · `POST /api/movies/<id>/view`
`GET /api/search?q=&save=1` · `GET /api/titles` · `GET /api/similar/<id>`
`POST|DELETE /api/ratings` · `/api/watchlist/<id>` · `/api/favorites/<id>`
`GET /api/me/ratings|watchlist|favorites|recent|profile` · `DELETE /api/me/searches`
`GET /api/home` · `/api/recommendations` · `/api/trending` · `/api/top-rated`
`GET /api/moods` · `/api/mood/<key>` · `/api/model-info`
`GET|POST /api/movies/<id>/reviews` · `DELETE /api/reviews/<id>`
`GET /api/admin/stats?demo=0|1` · `/api/admin/evaluation` (admins only)
