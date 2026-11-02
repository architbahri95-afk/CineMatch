"""Rule-based natural-language understanding for movie search, plus moods.

"funny movies from the 90s with a happy ending" is turned into structured
constraints (genre, decade, ending tag). Any words left over are matched
semantically against plot and keywords by the recommender (TF-IDF).
"""
import re
from dataclasses import dataclass, field

GENRE_SYNONYMS = {
    "science fiction": "Sci-Fi", "sci-fi": "Sci-Fi", "scifi": "Sci-Fi", "sci fi": "Sci-Fi",
    "funny": "Comedy", "comedy": "Comedy", "comedies": "Comedy", "hilarious": "Comedy",
    "laugh": "Comedy", "humorous": "Comedy",
    "scary": "Horror", "horror": "Horror", "creepy": "Horror", "terrifying": "Horror", "spooky": "Horror",
    "action": "Action", "adventure": "Adventure", "animated": "Animation", "animation": "Animation",
    "cartoon": "Animation", "romantic": "Romance", "romance": "Romance", "love story": "Romance",
    "thriller": "Thriller", "suspenseful": "Thriller", "suspense": "Thriller",
    "mystery": "Mystery", "whodunit": "Mystery", "crime": "Crime", "gangster": "Crime",
    "drama": "Drama", "fantasy": "Fantasy", "war": "War", "biopic": "Biography",
    "biography": "Biography", "historical": "History", "history": "History",
    "musical": "Music", "music": "Music", "family": "Family", "kids": "Family",
}

# phrase -> tag stored in movies.tags
TAG_SYNONYMS = {
    "happy ending": "happy_ending", "good ending": "happy_ending", "positive ending": "happy_ending",
    "feel good": "feel_good", "feel-good": "feel_good", "uplifting": "feel_good",
    "heartwarming": "feel_good", "wholesome": "feel_good", "bittersweet": "bittersweet",
    "dark": "dark", "gritty": "dark", "mind bending": "mind_bending", "mind-bending": "mind_bending",
    "twist": "mind_bending", "trippy": "mind_bending", "confusing": "mind_bending",
    "true story": "true_story", "true events": "true_story", "real events": "true_story",
    "based on a true story": "true_story", "sad": "tragic", "tearjerker": "tragic",
    "heartbreaking": "tragic", "tragic": "tragic", "violent": "violent", "bloody": "violent",
    "family friendly": "family_friendly", "family-friendly": "family_friendly",
}

LANGUAGES = {"english": "English", "japanese": "Japanese", "korean": "Korean", "french": "French"}

TAG_LABELS = {
    "happy_ending": "happy ending", "feel_good": "feel-good", "bittersweet": "bittersweet",
    "dark": "dark tone", "mind_bending": "mind-bending", "true_story": "true story",
    "tragic": "sad / tragic", "violent": "violent", "family_friendly": "family friendly",
}

# mood -> genre weights, preferred tags, tags to avoid
MOODS = {
    "happy": {"label": "Happy", "blurb": "Light, funny and feel-good",
              "genres": {"Comedy": 1.0, "Family": 0.8, "Animation": 0.6, "Music": 0.5},
              "tags": {"feel_good": 1.0, "happy_ending": 0.8}, "avoid": {"dark": 1.0, "tragic": 1.0}},
    "excited": {"label": "Excited", "blurb": "Fast, loud and full of action",
                "genres": {"Action": 1.0, "Adventure": 0.9, "Sci-Fi": 0.6},
                "tags": {"happy_ending": 0.2}, "avoid": {}},
    "relaxed": {"label": "Relaxed", "blurb": "Calm, warm and easy to sink into",
                "genres": {"Drama": 1.0, "Romance": 0.6, "Family": 0.5, "Animation": 0.4},
                "tags": {"feel_good": 0.7, "happy_ending": 0.5}, "avoid": {"violent": 1.0, "dark": 0.8}},
    "romantic": {"label": "Romantic", "blurb": "Love stories and slow burns",
                 "genres": {"Romance": 1.0, "Drama": 0.3, "Comedy": 0.3},
                 "tags": {"bittersweet": 0.3, "feel_good": 0.3}, "avoid": {"violent": 0.8}},
    "thrilled": {"label": "Thrilled", "blurb": "Tense, twisty and edge-of-seat",
                 "genres": {"Thriller": 1.0, "Mystery": 0.8, "Horror": 0.7, "Crime": 0.5},
                 "tags": {"dark": 0.4, "mind_bending": 0.4}, "avoid": {"family_friendly": 1.0}},
    "thoughtful": {"label": "Thoughtful", "blurb": "Ideas, real stories and big questions",
                   "genres": {"Drama": 0.8, "Biography": 0.8, "History": 0.8, "Mystery": 0.5, "Sci-Fi": 0.4},
                   "tags": {"mind_bending": 0.9, "true_story": 0.7}, "avoid": {}},
}

STOPWORDS = set("""show me find give some any movie movies film films with that and the a an of in on for from to
about like want watch something recommend please good great really very who whose which are is was has have had
there their them they this these those i my we you your it its be been being do does did can could would should
tonight today looking look need get gimme""".split())

NAME_BLACKLIST = {"stone", "hill", "day", "long", "love", "young", "king", "mark", "scott"}


@dataclass
class ParsedQuery:
    genres: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    year: tuple = None            # (lo, hi) inclusive, either may be None
    runtime: tuple = None         # minutes
    language: str = None
    people: list = field(default_factory=list)   # canonical names
    sort: str = None              # "rating" | "trending"
    text: str = ""                # leftover words for semantic matching
    notes: list = field(default_factory=list)    # human-readable "what I understood"

    @property
    def has_constraints(self):
        return bool(self.genres or self.tags or self.year or self.runtime
                    or self.language or self.people or self.sort)


def build_people_index(movies):
    """name -> canonical name, and lastname -> {canonical names}."""
    full, last = {}, {}
    for col in ("director", "actors"):
        for cell in movies[col]:
            for name in filter(None, cell.split("|")):
                full[name.lower()] = name
                tail = name.split()[-1].lower()
                if len(tail) >= 4 and tail not in NAME_BLACKLIST:
                    last.setdefault(tail, set()).add(name)
    return {"full": full, "last": last}


def _take(pattern, text):
    """Search; return (match, text with the matched span blanked out)."""
    m = re.search(pattern, text)
    if not m:
        return None, text
    return m, text[:m.start()] + " " + text[m.end():]


def _hours_to_min(value, unit):
    v = float(value)
    return int(v * 60) if unit.startswith("h") else int(v)


def parse_query(q, people):
    p = ParsedQuery()
    t = " " + re.sub(r"\s+", " ", q.lower().strip()) + " "

    # people first (so "Nolan" is not mistaken for leftover text)
    for low, name in sorted(people["full"].items(), key=lambda kv: -len(kv[0])):
        m, t2 = _take(r"\b" + re.escape(low) + r"\b", t)
        if m:
            t = t2
            if name not in p.people:
                p.people.append(name)
    for tail, names in people["last"].items():
        m, t2 = _take(r"\b" + re.escape(tail) + r"\b", t)
        if m and len(names) <= 2:
            t = t2
            p.people += [n for n in sorted(names) if n not in p.people]
    if p.people:
        p.notes.append("with " + " / ".join(p.people[:3]))

    # tags then genres (longest phrase first so "feel good" beats "good")
    for phrase, tag in sorted(TAG_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        m, t2 = _take(r"\b" + re.escape(phrase) + r"\b", t)
        if m:
            t = t2
            if tag not in p.tags:
                p.tags.append(tag)
    for phrase, genre in sorted(GENRE_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        m, t2 = _take(r"\b" + re.escape(phrase) + r"\b", t)
        if m:
            t = t2
            if genre not in p.genres:
                p.genres.append(genre)
    if p.genres:
        p.notes.append("genre: " + ", ".join(p.genres))
    if p.tags:
        p.notes.append("feel: " + ", ".join(TAG_LABELS[x] for x in p.tags))

    # years
    m, t = _take(r"\bbetween (\d{4}) and (\d{4})\b", t)
    if m:
        p.year = (int(m.group(1)), int(m.group(2)))
    if not p.year:
        m, t = _take(r"\b(?:after|since|newer than)\s+(\d{4})\b", t)
        if m:
            p.year = (int(m.group(1)) + (1 if "after" in m.group(0) else 0), None)
    if not p.year:
        m, t = _take(r"\b(?:before|older than|until)\s+(\d{4})\b", t)
        if m:
            p.year = (None, int(m.group(1)) - 1)
    if not p.year:
        m, t = _take(r"\b(19\d0|20\d0)s\b", t)
        if m:
            d = int(m.group(1)); p.year = (d, d + 9)
    if not p.year:
        m, t = _take(r"\b(\d0)s\b", t)
        if m:
            dd = int(m.group(1)); d = 1900 + dd if dd >= 30 else 2000 + dd
            p.year = (d, d + 9)
    if not p.year:
        m, t = _take(r"\b((?:19|20)\d{2})\b", t)
        if m:
            p.year = (int(m.group(1)), int(m.group(1)))
    if not p.year:
        m, t = _take(r"\b(classics?|old school|vintage)\b", t)
        if m:
            p.year = (None, 1990)
    if not p.year:
        m, t = _take(r"\b(recent|new|latest|modern)\b", t)
        if m:
            p.year = (2015, None)
    if p.year:
        lo, hi = p.year
        p.notes.append("released " + (f"{lo}-{hi}" if lo and hi and lo != hi else str(lo) if lo and hi
                                      else f"after {lo - 1}" if lo else f"before {hi + 1}"))

    # runtime
    unit = r"(hours?|hrs?|h|minutes?|mins?|m)"
    m, t = _take(r"\b(?:under|less than|shorter than|below)\s+(\d+(?:\.\d+)?)\s*" + unit + r"\b", t)
    if m:
        p.runtime = (None, _hours_to_min(m.group(1), m.group(2)))
    else:
        m, t = _take(r"\b(?:over|more than|longer than|above)\s+(\d+(?:\.\d+)?)\s*" + unit + r"\b", t)
        if m:
            p.runtime = (_hours_to_min(m.group(1), m.group(2)), None)
        else:
            m, t = _take(r"\b(short)\b", t)
            if m:
                p.runtime = (None, 100)
            else:
                m, t = _take(r"\b(long|lengthy)\b", t)
                if m:
                    p.runtime = (150, None)
    if p.runtime:
        lo, hi = p.runtime
        p.notes.append(f"under {hi} min" if hi else f"over {lo} min")

    # language
    for word, lang in LANGUAGES.items():
        m, t2 = _take(r"\b" + word + r"\b", t)
        if m:
            t = t2; p.language = lang
            p.notes.append(f"language: {lang}")
            break

    # sort intent
    m, t = _take(r"\b(best|top rated|highest rated|top|greatest|acclaimed)\b", t)
    if m:
        p.sort = "rating"; p.notes.append("sorted by rating")
    else:
        m, t = _take(r"\b(trending|popular|hot)\b", t)
        if m:
            p.sort = "trending"; p.notes.append("sorted by trending")

    words = [w for w in re.findall(r"[a-z][a-z'-]{2,}", t) if w not in STOPWORDS]
    p.text = " ".join(words)
    return p
