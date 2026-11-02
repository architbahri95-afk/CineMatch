"use strict";
const $ = (s) => document.querySelector(s);
const state = { dashDemo: true, dashK: 10, dashMetric: "precision", user: null, genres: [], titles: null, tab: "home", lib: "watchlist", genre: "", sort: "popular", page: 1, q: "", mood: null, similarId: null };

// DOM helper: builds elements from text only, so movie/user data can never inject HTML.
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  kids.flat().forEach((c) => c != null && c !== false && el.append(c.nodeType ? c : document.createTextNode(c)));
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || "Something went wrong. Try again."), { status: res.status });
  return data;
}

// ---- posters and cards ------------------------------------------------------------
const GENRE_HUE = { Action: 8, Adventure: 32, Animation: 170, Biography: 215, Comedy: 45, Crime: 270, Drama: 220, Family: 150, Fantasy: 290, History: 25, Horror: 350, Music: 320, Mystery: 245, Romance: 335, "Sci-Fi": 195, Thriller: 0, War: 90 };
function posterColor(genres) {
  const hue = GENRE_HUE[genres[0]] ?? 210;
  return `linear-gradient(160deg, hsl(${hue} 58% 40%), hsl(${(hue + 28) % 360} 62% 22%))`;
}

// Generated artwork by default; a real image is used when the movie has a poster_url.
function poster(m) {
  const el = h("div", { class: "poster", style: `background:${posterColor(m.genres)}` });
  if (m.poster_url) { const img = h("img", { src: m.poster_url, alt: "", loading: "lazy" }); img.onerror = () => img.remove(); el.append(img); }
  el.append(...[h("span", { class: "year" }, m.year), h("span", { class: "name" }, m.title),
    m.avg_rating ? h("span", { class: "score" }, `★ ${m.avg_rating.toFixed(1)}`) : null,
    m.my_rating ? h("span", { class: "mine" }, `You: ${m.my_rating}★`) : null].filter(Boolean));   // append(null) would print "null"
  return el;
}

async function toggleList(kind, m, btn) {
  if (!state.user) return openAuth("register");
  const key = kind === "watchlist" ? "in_watchlist" : "is_favorite", on = !m[key];
  await api(`/api/${kind}/${m.id}`, { method: on ? "POST" : "DELETE" });
  m[key] = on; btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", on);
  btn.setAttribute("aria-label", `${on ? "Remove from" : "Add to"} ${kind === "watchlist" ? "watchlist" : "favorites"}`);
}

function card(m, src) {
  const quick = h("div", { class: "quick" },
    qbtn("favorites", m, "♥", "favorites"), qbtn("watchlist", m, "＋", "watchlist"));
  const pred = m.predicted_rating ? h("span", { class: "pred" }, `★ ${m.predicted_rating.toFixed(1)} predicted `, h("small", {}, `${m.confidence}% confidence`)) : null;
  const note = m.shared?.length ? h("span", { class: "shared" }, "Shares " + m.shared.slice(0, 2).join(" · "))
    : m.reasons?.length ? h("span", { class: "reason" }, m.reasons[0]) : null;
  return h("div", { class: "card" },
    h("button", { class: "card-main", onclick: () => openDetail(m.id, src), "aria-label": m.title },
      poster(m), h("span", { class: "meta" }, m.genres.slice(0, 2).join(", ") + (m.runtime_text ? ` · ${m.runtime_text}` : "")),
      pred, note),
    quick);
}
function qbtn(kind, m, icon, noun) {
  const on = kind === "watchlist" ? m.in_watchlist : m.is_favorite;
  const b = h("button", { class: "q" + (on ? " on" : ""), "aria-pressed": !!on, "aria-label": `${on ? "Remove from" : "Add to"} ${noun}`, title: noun === "favorites" ? "Favorite" : "Watchlist" }, icon);
  b.addEventListener("click", (e) => { e.stopPropagation(); toggleList(kind, m, b).catch(() => {}); });
  return b;
}

const grid = (movies, empty, src) => h("div", { class: "grid" }, movies.length ? movies.map((m) => card(m, src)) : h("p", { class: "empty" }, empty));
const scroller = (movies, src) => h("div", { class: "scroller" }, movies.map((m) => card(m, src)));
const section = (title, body, sub) => h("section", { class: "section" }, h("h2", {}, title), sub ? h("p", { class: "sub" }, sub) : null, body);
const intro = (title, text) => h("section", { class: "intro" }, h("h1", {}, title), text ? h("p", {}, text) : null);
const view = (...nodes) => $("#view").replaceChildren(...nodes.flat().filter(Boolean));

function genrePicker(selected, onChange) {
  const chosen = new Set(selected);
  return h("div", { class: "chips" }, state.genres.map((g) => {
    const b = h("button", { type: "button", class: "chip" + (chosen.has(g) ? " is-on" : ""), "aria-pressed": chosen.has(g) }, g);
    b.addEventListener("click", () => { chosen.has(g) ? chosen.delete(g) : chosen.add(g); b.classList.toggle("is-on"); b.setAttribute("aria-pressed", chosen.has(g)); onChange?.([...chosen]); });
    return b;
  }));
}

// ---- hero ---------------------------------------------------------------------------
function hero(m, strategy, src) {
  const chips = [];
  if (m.predicted_rating) chips.push(`★ ${m.predicted_rating.toFixed(1)} predicted · ${m.match}% match · ${m.confidence}% confidence`);
  const el = h("section", { class: "hero", style: `--tint:${posterColor(m.genres)}` },
    h("div", {},
      m.reasons?.[0] ? h("span", { class: "why" }, m.reasons[0]) : strategy === "trending" ? h("span", { class: "why" }, "Trending right now") : null,
      h("h2", {}, m.title),
      h("div", { class: "meta" }, [m.year, m.runtime_text, m.genres.join(", "), m.avg_rating ? `★ ${m.avg_rating.toFixed(1)}` : null].filter(Boolean).join(" · ")),
      h("p", { class: "lead" }, m.overview),
      chips.map((c) => h("p", { class: "meta" }, c)),
      h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => openDetail(m.id, src) }, "View details and rate"))),
    poster(m));
  return el;
}

// ---- views ------------------------------------------------------------------------------
const HOME_COPY = {
  trending: (u) => u ? [`Welcome, ${u.name}`, "Pick a few genres below, or rate three movies, and your recommendations switch on."]
    : ["What should you watch tonight?", "Here's what's trending and top rated. Create an account and rate three movies to get picks built around your taste."],
  genres: () => ["Starter picks from your genres", "Rate movies, add favorites or save to your watchlist and these sharpen quickly."],
  content: () => ["Early picks for you", "Based on what you've rated and saved so far. A few more ratings unlock collaborative filtering."],
  hybrid: (u) => [`Picked for ${u.name}`, "A hybrid of what each film is about and what viewers with similar taste rated highly."],
};

async function showHome() {
  const data = await api("/api/home");
  const [title, text] = HOME_COPY[data.strategy](state.user);
  const nodes = [intro(title, text)];
  if (state.user && data.strength < 1 && !state.user.fav_genres?.length) {
    let picked = [];
    const save = h("button", { class: "primary", onclick: async () => { await api("/api/me/genres", { method: "PUT", body: { genres: picked } }); await refreshUser(); show(); } }, "Save and personalize");
    nodes.push(h("div", { class: "banner" }, h("div", {}, h("h3", {}, "New here? Tell us what you like"), h("p", {}, "Pick a few genres to get recommendations right away (cold-start).")), save,
      genrePicker([], (g) => { picked = g; })));
  }
  const [first, ...rest] = data.sections;
  const lead = first.results[0];
  if (lead) nodes.push(hero(lead, data.strategy, first.id));
  data.sections.forEach((sec, i) => {
    const items = i === 0 ? sec.results.slice(1) : sec.results;
    if (items.length) nodes.push(section(sec.title, scroller(items, sec.id)));
  });
  view(nodes);
}

async function showBrowse() {
  const data = await api("/api/movies?" + new URLSearchParams({ genre: state.genre, sort: state.sort, page: state.page }));
  const sort = h("select", { "aria-label": "Sort by", onchange: (e) => { state.sort = e.target.value; state.page = 1; show(); } },
    [["popular", "Most popular"], ["rating", "Highest rated"], ["year", "Newest"], ["title", "A to Z"], ["runtime", "Shortest"]]
      .map(([v, l]) => h("option", { value: v, selected: v === state.sort }, l)));
  const chips = h("div", { class: "chips" }, ["", ...state.genres].map((g) =>
    h("button", { class: "chip" + (g === state.genre ? " is-on" : ""), onclick: () => { state.genre = g; state.page = 1; show(); } }, g || "All")));
  const pages = Math.max(1, Math.ceil(data.total / data.per_page));
  const pager = pages > 1 ? h("div", { class: "pager" },
    h("button", { disabled: data.page <= 1, onclick: () => { state.page--; show(); } }, "Previous"), h("span", {}, `Page ${data.page} of ${pages}`),
    h("button", { disabled: data.page >= pages, onclick: () => { state.page++; show(); } }, "Next")) : null;
  view(intro("Browse all movies", `${data.total} movie${data.total === 1 ? "" : "s"}${state.genre ? ` in ${state.genre}` : ""}.`),
    h("div", { class: "toolbar" }, sort), chips, grid(data.results, "No movies here."), pager);
}

async function showSearch(save = false) {
  const data = await api(`/api/search?q=${encodeURIComponent(state.q)}${save ? "&save=1" : ""}`);
  view(intro(`Results for “${state.q}”`, data.mode === "title" ? "Matching titles." : "Understood as a plain-English request."),
    data.understood.length ? h("div", { class: "understood" }, "Understood:", data.understood.map((n) => h("span", { class: "tag" }, n))) : null,
    data.relaxed ? h("p", { class: "note" }, "Few movies matched every part, so some results only match partly.") : null,
    grid(data.results, "Nothing matched. Try different words, like a genre, a decade or an actor."));
}

const EXAMPLES = ["funny movies with a happy ending", "sci-fi from the 90s", "movies by Christopher Nolan under 2 hours", "scary movies after 2015", "feel-good animated family", "mind-bending thrillers", "Ryan Gosling romance"];
function runSearch(text) { state.q = text; $("#search").value = text; state.tab = "search"; syncTabs(); showSearch(true).catch(showError); }

async function showMood() {
  const moods = await api("/api/moods");
  const tiles = h("div", { class: "moods" }, moods.map((m) => {
    const t = h("button", { class: "mood-tile", "aria-pressed": state.mood === m.key }, h("strong", {}, m.label), h("span", {}, m.blurb), h("span", {}, m.genres.slice(0, 3).join(" · ")));
    t.addEventListener("click", () => { state.mood = m.key; show(); });
    return t;
  }));
  const nodes = [intro("What's your mood?", "Pick how you feel and CineMatch maps it to genres and tone, then ranks the matches by your taste."), tiles,
    h("div", { class: "try" }, "Or ask in plain English:", EXAMPLES.slice(0, 4).map((e) => h("button", { class: "chip", onclick: () => runSearch(e) }, e)))];
  if (state.mood) {
    const r = await api(`/api/mood/${state.mood}`);
    nodes.push(section(`Feeling ${r.mood.label.toLowerCase()}`, grid(r.results, "Nothing to show."), r.mood.blurb));
  }
  view(nodes);
}

async function showSimilar() {
  state.titles ||= await api("/api/titles");
  const label = (t) => `${t.title} (${t.year})`;
  const list = h("datalist", { id: "titleList" }, state.titles.map((t) => h("option", { value: label(t) })));
  const input = h("input", { list: "titleList", placeholder: "Start typing a movie title", "aria-label": "Movie title" });
  const go = () => { const t = state.titles.find((x) => label(x).toLowerCase() === input.value.trim().toLowerCase()) || state.titles.find((x) => x.title.toLowerCase() === input.value.trim().toLowerCase()); if (t) { state.similarId = t.id; show(); } };
  input.addEventListener("change", go);
  const nodes = [intro("Find similar movies", "Choose a movie you love. Results blend content similarity (genre, director, cast, themes, plot) with what other viewers rated alike."),
    h("div", { class: "findbox" }, input, h("button", { class: "primary", onclick: go }, "Find similar"), list)];
  if (state.similarId) {
    const [src, sims] = await Promise.all([api(`/api/movies/${state.similarId}`), api(`/api/similar/${state.similarId}`)]);
    input.value = label(src);
    nodes.push(h("div", { class: "simhead" }, poster(src), h("div", {}, h("h2", {}, src.title), h("p", { class: "meta" }, `${src.director} · ${src.genres.join(", ")}`), h("p", {}, src.overview))));
    nodes.push(h("div", { class: "grid" }, sims.map((m) => h("div", {}, card(m), h("div", { class: "simbar", title: `Similarity ${m.similarity}` }, h("i", { style: `width:${Math.min(100, m.similarity * 1.6)}%` })), h("span", { class: "meta" }, `Similarity score ${m.similarity}`)))));
  }
  view(nodes);
}

const LIB_TABS = [["watchlist", "Watchlist"], ["favorites", "Favorites"], ["recent", "Recently viewed"], ["ratings", "My ratings"], ["profile", "Taste profile"]];
async function showLibrary() {
  if (!state.user) return view(intro("Your library", "Sign in to keep a watchlist, mark favorites, see what you viewed recently and explore your taste profile."),
    h("button", { class: "primary", onclick: () => openAuth("register") }, "Create a free account"));
  const chips = h("div", { class: "chips" }, LIB_TABS.map(([k, l]) => h("button", { class: "chip" + (k === state.lib ? " is-on" : ""), onclick: () => { state.lib = k; show(); } }, l)));
  const empty = { watchlist: "Your watchlist is empty. Tap ＋ on any movie to save it for later.", favorites: "No favorites yet. Tap ♥ on any movie.", recent: "Movies you open will appear here.", ratings: "You haven't rated anything yet." };
  if (state.lib === "profile") return view(intro("Your library"), chips, await profilePanel());
  const path = { watchlist: "/api/me/watchlist", favorites: "/api/me/favorites", recent: "/api/me/recent", ratings: "/api/me/ratings" }[state.lib];
  const movies = await api(path);
  view(intro("Your library"), chips, grid(movies, empty[state.lib]));
}

async function profilePanel() {
  const p = await api("/api/me/profile");
  const c = p.counts;
  const stat = (n, l) => h("div", { class: "stat" }, h("b", {}, n ?? "–"), h("span", {}, l));
  let picked = state.user.fav_genres || [];
  const saved = h("span", { class: "meta", role: "status" });
  return h("div", {},
    h("div", { class: "stats" }, stat(c.rated, "rated"), stat(p.avg_rating_given, "avg rating you give"), stat(c.favorites, "favorites"), stat(c.watchlist, "on watchlist"), stat(c.viewed, "viewed"), stat(c.searches, "searches")),
    h("div", { class: "panel" }, h("h3", {}, "What we've learned"), h("p", {}, p.summary),
      p.top_genres.map((g) => h("div", { class: "bar" }, h("span", {}, g.genre), h("span", { class: "track" }, h("i", { style: `width:${g.score}%` })), h("span", { class: "meta" }, g.score))),
      p.top_directors.length ? h("p", { class: "meta" }, "Directors you like: ", p.top_directors.join(", ")) : null,
      p.top_actors.length ? h("p", { class: "meta" }, "Actors you like: ", p.top_actors.join(", ")) : null),
    h("div", { class: "panel" }, h("h3", {}, "Genres you picked"), genrePicker(picked, (g) => { picked = g; }),
      h("div", { class: "actions-row" }, h("button", { class: "primary", onclick: async () => { await api("/api/me/genres", { method: "PUT", body: { genres: picked } }); await refreshUser(); saved.textContent = "Saved."; } }, "Save genres"), saved)),
    h("div", { class: "panel" }, h("h3", {}, "Recent searches"),
      p.recent_searches.length ? h("div", { class: "pills" }, p.recent_searches.map((q) => h("button", { class: "pill", onclick: () => runSearch(q) }, q))) : h("p", { class: "meta" }, "Searches you run (press Enter) are remembered and nudge your recommendations."),
      p.recent_searches.length ? h("div", { class: "actions-row" }, h("button", { class: "ghost", onclick: async () => { await api("/api/me/searches", { method: "DELETE" }); show(); } }, "Clear history")) : null));
}

// ---- routing -----------------------------------------------------------------------------
const VIEWS = { home: showHome, browse: showBrowse, mood: showMood, similar: showSimilar, library: showLibrary, dashboard: showDashboard, search: () => showSearch(false) };
function showError(e) { view(h("p", { class: "empty" }, e.message)); }
function show() { return VIEWS[state.tab]().catch(showError); }
function syncTabs() { document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === state.tab)); }
function go(tab) { state.tab = tab; state.page = 1; syncTabs(); return show(); }

// ---- movie detail ---------------------------------------------------------------------
const DETAIL_PRED = (m) => {
  if (m.predicted && !m.my_rating) return h("div", { class: "predbox" }, h("strong", {}, `Predicted for you: ★ ${m.predicted.rating.toFixed(1)}`),
    ` · ${m.predicted.match}% match · ${m.predicted.confidence}% confidence`, m.predicted.reasons.length ? h("ul", {}, m.predicted.reasons.map((r) => h("li", {}, r))) : null);
  if (!state.user) return h("p", { class: "meta" }, "Create an account to see your predicted rating for this movie.");
  if (!m.my_rating && !m.predicted) return h("p", { class: "meta" }, "Rate at least three movies to see a predicted rating here.");
  return null;
};

async function openDetail(id, source) {
  const m = await api(`/api/movies/${id}`);
  if (state.user) api(`/api/movies/${id}/view`, { method: "POST", body: { source: source || "" } }).catch(() => {});
  const dlg = $("#detail");
  const stars = h("div", { class: "stars", role: "group", "aria-label": "Your rating" });
  const note = h("p", { class: "meta", role: "status" });
  const paint = (v) => [...stars.children].forEach((b, i) => b.classList.toggle("on", i < v));
  const predSlot = h("div", {}, DETAIL_PRED(m));
  for (let n = 1; n <= 5; n++) {
    stars.append(h("button", { class: "star", "aria-label": `${n} star${n > 1 ? "s" : ""}`, onclick: async () => {
      if (!state.user) { dlg.close(); return openAuth("register"); }
      await api("/api/ratings", { method: "POST", body: { movie_id: id, rating: n } });
      m.my_rating = n; paint(n); predSlot.replaceChildren(); refreshUser(); note.textContent = "Saved to your account. Your picks have been updated.";
    } }, "★"));
  }
  paint(m.my_rating || 0);
  const clear = h("button", { class: "ghost", onclick: async () => { await api(`/api/ratings/${id}`, { method: "DELETE" }); m.my_rating = null; paint(0); refreshUser(); note.textContent = "Rating removed."; } }, "Remove rating");
  const tog = (kind, key, on, off) => { const b = h("button", { class: "toggle" + (m[key] ? " on" : ""), "aria-pressed": !!m[key] }, m[key] ? on : off);
    b.addEventListener("click", async () => { await toggleList(kind, m, b).catch(() => {}); if (state.user) { b.classList.toggle("on", m[key]); b.textContent = m[key] ? on : off; } }); return b; };
  const fact = (k, v) => v ? [h("dt", {}, k), h("dd", {}, v)] : [];

  dlg.replaceChildren(h("div", { class: "detail" },
    h("button", { class: "close", "aria-label": "Close", onclick: () => dlg.close() }, "×"),
    poster(m),
    h("div", {},
      h("h2", {}, m.title),
      h("p", { class: "meta" }, [m.year, m.runtime_text, m.language].filter(Boolean).join(" · ") + (m.avg_rating ? ` · ★ ${m.avg_rating.toFixed(1)} from ${m.num_ratings} ratings` : "")),
      h("div", { class: "pills" }, m.genres.map((g) => h("span", { class: "pill" }, g))),
      m.tags.length ? h("div", { class: "tags" }, m.tags.map((t) => h("span", { class: "tag" }, t))) : null,
      h("p", {}, m.overview),
      h("dl", { class: "facts" }, fact("Director", m.director), fact("Cast", m.cast.join(", "))),
      watchBox(m),
      h("div", { class: "actions-row" }, tog("favorites", "is_favorite", "♥ Favorited", "♡ Favorite"), tog("watchlist", "in_watchlist", "✓ On watchlist", "＋ Watchlist"),
        h("button", { class: "toggle", onclick: () => { dlg.close(); state.similarId = id; go("similar"); } }, "Find similar")),
      predSlot,
      h("strong", {}, state.user ? "Your rating" : "Create a free account to rate this movie"), stars, state.user && m.my_rating ? clear : null, note),
    reviewsSection(m),
    h("div", { class: "similar" }, h("h3", {}, "More like this"), scroller(m.similar))));
  if (!dlg.open) dlg.showModal();
}
$("#detail").addEventListener("close", () => { if (["home", "library", "browse", "search"].includes(state.tab)) show(); });

// ---- account header ------------------------------------------------------------------------
function renderAccount() {
  const u = state.user;
  $("#account").replaceChildren(...(u
    ? [h("span", { class: "avatar", "aria-hidden": "true" }, u.name[0].toUpperCase()),
       h("span", { class: "who" }, h("span", {}, u.name), h("small", {}, `${u.num_ratings} rated`)),
       h("button", { class: "ghost", onclick: signOut }, "Sign out")]
    : [h("button", { class: "ghost", onclick: () => openAuth("login") }, "Sign in"),
       h("button", { class: "primary", onclick: () => openAuth("register") }, "Join free")]));
  $("#dashTab").hidden = !(u && u.is_admin);
}
async function refreshUser() { const r = await api("/api/me"); state.user = r.user; if (r.genres) state.genres = r.genres; renderAccount(); }
async function signOut() { await api("/api/logout", { method: "POST" }); state.user = null; renderAccount(); go("home"); }

// ---- sign in / create account ---------------------------------------------------------------
let regGenres = [];
function showAuthMode(mode) {
  const login = mode === "login";
  $("#loginForm").hidden = !login; $("#registerForm").hidden = login;
  $("#segLogin").setAttribute("aria-selected", login); $("#segRegister").setAttribute("aria-selected", !login);
  $("#authTitle").textContent = login ? "Welcome back" : "Create your account";
  $("#liError").textContent = $("#rgError").textContent = "";
  $(login ? "#liId" : "#rgName").focus();
}
function openAuth(mode) { if (!$("#auth").open) $("#auth").showModal(); showAuthMode(mode); }
$("#segLogin").onclick = () => showAuthMode("login");
$("#segRegister").onclick = () => showAuthMode("register");
$("#authClose").onclick = () => $("#auth").close();
$("#auth").addEventListener("click", (e) => { if (e.target === $("#auth")) $("#auth").close(); });
document.querySelectorAll(".reveal").forEach((b) => b.addEventListener("click", () => {
  const input = $("#" + b.dataset.for), reveal = input.type === "password";
  input.type = reveal ? "text" : "password"; b.textContent = reveal ? "Hide" : "Show";
}));
$("#rgPw").addEventListener("input", (e) => {
  const p = e.target.value, variety = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(p)).length;
  const score = !p ? 0 : p.length < 8 ? 1 : Math.min(4, 1 + (p.length >= 12 ? 1 : 0) + (variety >= 3 ? 1 : 0) + (variety === 4 && p.length >= 10 ? 1 : 0));
  $("#meterBar").style.width = `${score * 25}%`;
  $("#meterBar").style.background = ["#ff8a8a", "#ff8a8a", "#f5a524", "#a6d96a", "#4fd18b"][score];
  $("#meterText").textContent = ["At least 8 characters", "Too short: use at least 8 characters", "Okay", "Good", "Strong"][score];
});
async function submitAuth(form, path, body, errorEl) {
  const btn = form.querySelector("button[type=submit]");
  errorEl.textContent = ""; btn.disabled = true;
  try {
    await api(path, { method: "POST", body });
    form.reset(); $("#meterBar").style.width = "0"; regGenres = []; buildRegGenres();
    $("#auth").close(); await refreshUser(); go("home");
  } catch (err) { errorEl.textContent = err.message; } finally { btn.disabled = false; }
}
$("#loginForm").addEventListener("submit", (e) => { e.preventDefault();
  submitAuth(e.target, "/api/login", { identifier: $("#liId").value, password: $("#liPw").value, remember: $("#liRemember").checked }, $("#liError")); });
$("#registerForm").addEventListener("submit", (e) => { e.preventDefault();
  if ($("#rgPw").value !== $("#rgPw2").value) { $("#rgError").textContent = "The two passwords don't match."; return; }
  submitAuth(e.target, "/api/register", { username: $("#rgName").value, email: $("#rgEmail").value, password: $("#rgPw").value, remember: $("#rgRemember").checked, genres: regGenres }, $("#rgError")); });
function buildRegGenres() { $("#rgGenres").replaceChildren(...genrePicker([], (g) => { regGenres = g; }).children); }

// ---- trailer and where to watch ------------------------------------------------------------
const safeUrl = (u) => (typeof u === "string" && u.startsWith("https://") ? u : null);
function watchBox(m) {
  const t = m.trailer, w = m.watch, groups = [["flatrate", "Stream"], ["rent", "Rent"], ["buy", "Buy"]].filter(([k]) => w.providers[k]?.length);
  const link = (text, url) => safeUrl(url) ? h("a", { class: "toggle linkbtn", href: url, target: "_blank", rel: "noopener noreferrer" }, text) : null;
  return h("div", { class: "watch" },
    h("div", { class: "actions-row" }, link(t.exact ? "▶ Watch trailer" : "▶ Search trailer on YouTube", t.url),
      link(w.has_providers ? `Open on JustWatch (${w.region})` : `Find where to watch (${w.region})`, w.url)),
    w.has_providers ? h("div", { class: "providers" }, groups.map(([k, label]) => h("div", {}, h("span", { class: "meta" }, label + ": "), w.providers[k].map((n) => h("span", { class: "pill" }, n)))),
      h("p", { class: "meta" }, `Availability in ${w.region}${w.updated ? ` as of ${w.updated}` : ""}, from TMDB/JustWatch. It changes often, so check before you plan.`)) : null);
}

// ---- reviews and replies ---------------------------------------------------------------------
function reviewsSection(movie) {
  const box = h("section", { class: "reviews" });
  const post = async (body, parent_id) => api(`/api/movies/${movie.id}/reviews`, { method: "POST", body: { body, parent_id } });
  async function load() {
    const data = await api(`/api/movies/${movie.id}/reviews`);
    const mine = data.reviews.find((r) => r.mine);
    const err = h("span", { class: "error", role: "alert" });
    const ta = h("textarea", { rows: 3, maxlength: 1000, placeholder: "Share your thoughts (please avoid spoilers)", "aria-label": "Your review" }, mine?.body || "");
    const send = h("button", { class: "primary", onclick: async () => {
      if (!state.user) return openAuth("register");
      try { await post(ta.value); await load(); } catch (e) { err.textContent = e.message; }
    } }, mine ? "Update my review" : "Post review");
    const del = (r) => r.can_delete ? h("button", { class: "linkish", onclick: async () => { await api(`/api/reviews/${r.id}`, { method: "DELETE" }); load(); } }, "Delete") : null;
    const item = (r) => {
      const slot = h("div", {});
      const reply = h("button", { class: "linkish", onclick: () => {
        if (!state.user) return openAuth("register");
        const t = h("textarea", { rows: 2, maxlength: 1000, placeholder: `Reply to ${r.author}`, "aria-label": "Your reply" });
        const e2 = h("span", { class: "error", role: "alert" });
        slot.replaceChildren(t, h("div", { class: "actions-row" }, h("button", { class: "primary", onclick: async () => { try { await post(t.value, r.id); await load(); } catch (e) { e2.textContent = e.message; } } }, "Reply"),
          h("button", { class: "ghost", onclick: () => slot.replaceChildren() }, "Cancel"), e2)); t.focus();
      } }, "Reply");
      return h("article", { class: "review" },
        h("div", { class: "rhead" }, h("b", {}, r.author), r.rating ? h("span", { class: "tag" }, `★ ${r.rating}`) : null, h("span", { class: "meta" }, r.created_at)),
        h("p", {}, r.body), h("div", { class: "actions-row" }, reply, del(r)), slot,
        r.replies.length ? h("div", { class: "replies" }, r.replies.map((x) => h("div", { class: "reply" },
          h("div", { class: "rhead" }, h("b", {}, x.author), h("span", { class: "meta" }, x.created_at), del(x)), h("p", {}, x.body)))) : null);
    };
    box.replaceChildren(h("h3", {}, `Reviews (${data.count})`),
      state.user ? h("div", { class: "composer" }, ta, h("div", { class: "actions-row" }, send, err))
                 : h("p", { class: "meta" }, "Sign in to write a review or reply."),
      ...(data.reviews.length ? data.reviews.map(item) : [h("p", { class: "meta" }, "No reviews yet. Be the first.")]));
  }
  load().catch(() => box.replaceChildren(h("p", { class: "meta" }, "Reviews couldn't be loaded.")));
  return box;
}

// ---- tables and charts (plain HTML/SVG, no libraries) ----------------------------------------
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, props = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(props)) el.setAttribute(k, v);
  kids.forEach((c) => el.append(c.nodeType ? c : document.createTextNode(c)));
  return el;
}
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const dtable = (head, rows, bestCols = []) => h("div", { class: "wrap" }, h("table", { class: "dtable" },
  h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
  h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => h("td", { class: bestCols[i] === r[i] ? "best" : "" }, c)))))));
function hbars(items, { max, fmt = (v) => v, highlight } = {}) {
  const top = max ?? Math.max(...items.map((i) => i.value), 1e-9);
  return h("div", {}, items.map((i) => h("div", { class: "bar" }, h("span", {}, i.label),
    h("span", { class: "track" }, h("i", { style: `width:${Math.max(1, (100 * i.value) / top)}%` + (highlight && i.key === highlight ? ";background:var(--accent)" : "") })),
    h("span", { class: "meta" }, fmt(i.value)))));
}
const SERIES = [["ratings", "Ratings", "#f5a524"], ["views", "Movie views", "#8f7bff"], ["saves", "Watchlist + favorites", "#4fd1c5"], ["reviews", "Reviews", "#ff7a59"], ["searches", "Searches", "#ff8ac4"]];
function lineChart(activity) {
  const W = 860, H = 240, L = 44, B = 28, T = 10, R = 10, n = activity.days.length;
  const max = Math.max(1, ...SERIES.flatMap(([k]) => activity[k]));
  const x = (i) => L + (i * (W - L - R)) / (n - 1), y = (v) => T + (H - T - B) * (1 - v / max);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", class: "linechart", "aria-label": `Daily activity for the last ${n} days, peak ${max} events in a day` });
  [0, 0.5, 1].forEach((f) => { const v = Math.round(max * f);
    g.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "currentColor", "stroke-opacity": ".15" }), svg("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" }, v)); });
  [0, Math.floor(n / 2), n - 1].forEach((i) => g.append(svg("text", { x: x(i), y: H - 8, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle", class: "axis" }, activity.days[i].slice(5))));
  SERIES.forEach(([k, , c]) => g.append(svg("polyline", { fill: "none", stroke: c, "stroke-width": 2.5, "stroke-linejoin": "round", points: activity[k].map((v, i) => `${x(i)},${y(v)}`).join(" ") })));
  return g;
}

// ---- model performance (shared by the dashboard and the "How it works" dialog) ----------------
const RATING_NAMES = { global_mean: "Global average", user_mean: "User's own average", item_bias: "Movie bias", content_only: "Content-based only", user_cf: "User-based CF only", item_cf: "Item-based CF only", hybrid: "Hybrid (CineMatch)" };
function ratingTable(ev) {
  const r = ev.rating_prediction, best = (k) => Math.min(...Object.values(r).map((v) => v[k]));
  return dtable(["Method", "RMSE", "MAE"], Object.entries(r).map(([k, v]) => [RATING_NAMES[k], `${v.rmse.toFixed(3)} ± ${v.rmse_std.toFixed(3)}`, `${v.mae.toFixed(3)} ± ${v.mae_std.toFixed(3)}`]),
    [null, `${best("rmse").toFixed(3)} ± ${Object.values(r).find((v) => v.rmse === best("rmse")).rmse_std.toFixed(3)}`, `${best("mae").toFixed(3)} ± ${Object.values(r).find((v) => v.mae === best("mae")).mae_std.toFixed(3)}`]);
}
const METRICS = [["precision", "Precision"], ["recall", "Recall"], ["f1", "F1"], ["ndcg", "NDCG"], ["hit", "Hit rate"], ["coverage", "Coverage"]];
function rankingTable(ev, k) {
  const rows = ev.ranking[k], best = METRICS.map(([m]) => Math.max(...ev.algorithms.map((a) => rows[a.key][m])).toFixed(3));
  return h("div", { class: "wrap" }, h("table", { class: "dtable" },
    h("thead", {}, h("tr", {}, ["Algorithm", ...METRICS.map(([, l]) => `${l}@${k}`)].map((c) => h("th", {}, c)))),
    h("tbody", {}, ev.algorithms.map((a) => h("tr", { class: a.key === "hybrid" ? "hybrid-row" : "" }, h("td", {}, a.label),
      METRICS.map(([m], i) => { const v = rows[a.key][m].toFixed(3); return h("td", { class: v === best[i] ? "best" : "" }, v); }))))));
}
function comparisonNotes(ev) {
  const k = ev.config.k, label = Object.fromEntries(ev.algorithms.map((a) => [a.key, a.label]));
  return h("ul", { class: "notes" }, Object.entries(ev.comparisons).map(([a, v]) => h("li", {},
    `Hybrid vs ${label[a]}: ${v.mean_diff >= 0 ? "+" : ""}${v.mean_diff.toFixed(3)} Precision@${k} (95% CI ${v.ci_low.toFixed(3)} to ${v.ci_high.toFixed(3)}) — `,
    h("b", {}, v.significant ? (v.mean_diff > 0 ? "hybrid is significantly better" : "hybrid is significantly worse") : "no significant difference"))));
}

function renderEval(slot, ev) {
  const c = ev.config, kStr = String(state.dashK), metric = state.dashMetric, [, mLabel] = METRICS.find(([m]) => m === metric);
  const chips = (items, cur, on) => h("div", { class: "chips" }, items.map(([v, l]) => h("button", { class: "chip" + (String(v) === String(cur) ? " is-on" : ""), onclick: () => on(v) }, l)));
  const rows = ev.ranking[kStr];
  slot.replaceChildren(h("h3", {}, "Model performance and algorithm comparison"),
    h("p", { class: "meta" }, `${c.users} users per split · ${c.repeats} random ${Math.round((1 - c.test_frac) * 100)}/${Math.round(c.test_frac * 100)} splits · a held-out rating ≥ ${c.relevant} counts as "liked" · values are means across splits`),
    h("h4", {}, "Top-K recommendation quality"),
    h("div", { class: "toolbar" }, h("span", { class: "meta" }, "K:"), chips(c.ks.map((k) => [k, `K = ${k}`]), state.dashK, (k) => { state.dashK = Number(k); renderEval(slot, ev); })),
    rankingTable(ev, kStr),
    h("h4", {}, `${mLabel}@${kStr} by algorithm`),
    chips(METRICS.map(([m, l]) => [m, l]), metric, (m) => { state.dashMetric = m; renderEval(slot, ev); }),
    hbars(ev.algorithms.map((a) => ({ key: a.key, label: a.label, value: rows[a.key][metric] })), { max: metric === "hit" || metric === "coverage" ? 1 : undefined, fmt: (v) => v.toFixed(3), highlight: "hybrid" }),
    h("h4", {}, `Is the hybrid really better? (Precision@${c.k}, paired bootstrap over users)`), comparisonNotes(ev),
    h("h4", {}, "Rating prediction error (lower is better, mean ± std across splits)"), ratingTable(ev),
    h("p", { class: "meta" }, `Precision@K is small by construction: each user has only a few held-out liked movies, so even a perfect list could not reach 1.0 (e.g. with 2 liked movies the maximum Precision@10 is 0.2). Compare algorithms against each other rather than against 1.0. Recall, NDCG and hit rate are easier to read. The ratings are synthetic, generated from genre tastes, so content-based methods have an advantage here; re-run on real data (e.g. MovieLens) before drawing conclusions.`));
}

// ---- admin dashboard ---------------------------------------------------------------------------
async function showDashboard() {
  const st = await api(`/api/admin/stats?demo=${state.dashDemo ? 1 : 0}`);
  const t = st.totals, f = st.funnel;
  const kpi = (n, label, sub) => h("div", { class: "stat" }, h("b", {}, n ?? "–"), h("span", {}, label), sub ? h("small", { class: "sub2" }, sub) : null);
  const demo = h("input", { type: "checkbox", checked: state.dashDemo, onchange: (e) => { state.dashDemo = e.target.checked; show(); } });
  const evalSlot = h("section", { class: "panel" }, h("h3", {}, "Model performance and algorithm comparison"), h("p", { class: "meta" }, "Running offline evaluation…"));
  const live = f.all.impressions > 0;
  const funnelTiles = h("div", { class: "stats" }, kpi(f.personalised.impressions, "recommendations shown", "personalised sections"), kpi(f.personalised.clicks, "opened (clicks)"), kpi(f.personalised.accepted, "accepted"),
    kpi(pct(f.personalised.ctr), "click-through rate"), kpi(pct(f.personalised.acceptance), "acceptance rate"));
  view(
    h("section", { class: "intro" }, h("h1", {}, "Recommendation analytics"), h("p", {}, "Usage, model quality and algorithm comparison. Only admins can see this page.")),
    h("div", { class: "toolbar" }, h("label", { class: "check" }, demo, " Include the 60 simulated demo users in totals and charts"), h("button", { class: "ghost", onclick: show }, "Refresh")),
    h("div", { class: "stats" }, kpi(t.users_real + t.users_demo, "users", `${t.users_real} real · ${t.users_demo} demo`), kpi(t.movies, "movies"), kpi(t.ratings, "ratings", `${st.ratings_per_user ?? "–"} per rater`),
      kpi(t.reviews, "reviews", `${t.comments} replies`), kpi(st.avg_rating, "average rating"), kpi(st.active_users_7d, "active users", "last 7 days")),
    h("div", { class: "dash-grid" },
      h("section", { class: "panel" }, h("h3", {}, "Most popular genres"), h("p", { class: "meta" }, "Number of ratings, with the average rating"),
        h("div", {}, st.genres.slice(0, 10).map((g, i, all) => h("div", { class: "bar wide" }, h("span", {}, g.genre),
          h("span", { class: "track" }, h("i", { style: `width:${Math.max(1, (100 * g.ratings) / all[0].ratings)}%` })), h("span", { class: "meta" }, `${g.ratings} · ★${g.avg_rating ?? "–"}`))))),
      h("section", { class: "panel" }, h("h3", {}, "Ratings"), h("p", { class: "meta" }, "How people rate (all ratings)"),
        hbars(st.rating_distribution.map((d) => ({ label: `${d.rating} ★`, value: d.count })), { fmt: (v) => v }),
        h("h4", {}, "Highest rated (5+ ratings)"), h("ol", { class: "notes" }, st.top_rated.map((m) => h("li", {}, `${m.title}: ★ ${m.avg_rating} (${m.ratings})`))))),
    h("section", { class: "panel" }, h("h3", {}, "Recommendations in live use"),
      h("p", { class: "meta" }, "Counted for real accounts only. An impression is a movie shown once in a section of the For you page; a click is opening it from there; accepted means rating it 4+ or adding it to favorites or the watchlist (once per movie)."),
      funnelTiles,
      live ? dtable(["Section", "Shown", "Clicks", "Accepted", "Click-through", "Acceptance"], f.by_source.map((r) => [r.label + (r.personalised ? "" : " (not personalised)"), r.impressions, r.clicks, r.accepted, pct(r.ctr), pct(r.acceptance)]))
           : h("p", { class: "note" }, "No live data yet. Sign in as a real user, open the For you tab, and open or rate some recommendations: these numbers build up as people use the app."),
      st.most_recommended.length ? [h("h4", {}, "Most recommended movies (personalised sections)"), dtable(["Movie", "Shown to (users)", "Opened by", "Accepted by"], st.most_recommended.map((m) => [m.title, m.shown_to, m.clicks, m.accepted]))] : null),
    h("section", { class: "panel" }, h("h3", {}, "User activity, last 30 days"),
      h("div", { class: "legend" }, SERIES.map(([k, l, c]) => h("span", {}, h("i", { style: `background:${c}` }), `${l} (${st.activity[k].reduce((a, b) => a + b, 0)})`))),
      lineChart(st.activity), h("p", { class: "meta" }, `New real sign-ups in this period: ${st.activity.new_users.reduce((a, b) => a + b, 0)}. Users with 3+ ratings (get personalised picks): ${st.personalised_users}.`)),
    evalSlot);
  api("/api/admin/evaluation").then((r) => renderEval(evalSlot, r.evaluation)).catch((e) => evalSlot.append(h("p", { class: "error" }, e.message)));
}

// ---- how it works (public) ----------------------------------------------------------------------------
$("#aboutBtn").addEventListener("click", async () => {
  const dlg = $("#about");
  const shell = (...kids) => h("div", { class: "about" }, h("button", { class: "close", "aria-label": "Close", onclick: () => dlg.close() }, "×"), h("h2", {}, "How CineMatch works"), ...kids);
  dlg.replaceChildren(shell(h("p", {}, "Running a quick offline evaluation…")));
  dlg.showModal();
  try {
    const m = await api("/api/model-info"), e = m.evaluation, k = String(e.config.k), rows = e.ranking[k];
    dlg.replaceChildren(shell(
      h("p", {}, `${m.catalogue} movies and ${m.ratings} ratings. Each recommendation combines two ideas:`),
      h("ul", {}, h("li", {}, "Content-based: TF-IDF over genres, keywords, plot, director, cast and language, compared with cosine similarity."),
        h("li", {}, "Collaborative: user-based and item-based filtering on mean-centred ratings (people with similar taste liked this)."),
        h("li", {}, "Your profile also uses favorites, watchlist, views and searches as weaker signals. New users start from picked genres or trending movies.")),
      h("h3", {}, `Rating prediction error (lower is better; ${e.config.repeats} random splits)`), ratingTable(e),
      h("h3", {}, `Top-${k} recommendations: does it surface movies you'd rate ${e.config.relevant}+?`),
      dtable(["Algorithm", "Precision", "Recall", "F1", "Hit rate"], e.algorithms.map((a) => [a.label, ...["precision", "recall", "f1", "hit"].map((x) => rows[a.key][x].toFixed(3))])),
      h("p", {}, "Measured on synthetic demo users, so treat the numbers as a sanity check. Confidence is a heuristic based on how much data and agreement supports a prediction, not a statistical probability.")));
  } catch (err) { dlg.querySelector(".about p").textContent = err.message; }
});

// ---- wiring --------------------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => { if (t.dataset.tab !== "search") { $("#search").value = ""; state.q = ""; } go(t.dataset.tab); }));
let timer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(timer);
  const q = e.target.value.trim();
  timer = setTimeout(() => { if (!q) return go("home"); state.q = q; state.tab = "search"; syncTabs(); showSearch(false).catch(showError); }, 350);
});
$("#search").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.value.trim()) { clearTimeout(timer); runSearch(e.target.value.trim()); } });

(async function init() {
  await refreshUser();      // restores a saved sign-in from the session cookie
  buildRegGenres();
  show();
})();
