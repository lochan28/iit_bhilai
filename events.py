"""Data layer: fetch events from the event sites, normalize, geocode, save.

Run directly to refresh events.json:  python events.py
"""
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- Tunable values --------------------------------------------------------
GEMMA_SUMMARY_LIMIT = 0    # new Gemma summaries per refresh; the rest are made when someone opens an event
HTTP_TIMEOUT = 40          # seconds to wait for each API before giving up
REFRESH_HOURS = 6          # auto-refresh interval; lower = fresher events but more calls to the event sites

# ---- Fixed values ----------------------------------------------------------
# Where the data files live. A website can point this at persistent storage.
DATA_DIR = os.environ.get("HACKFINDR_DATA_DIR") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
for _name in ("events.json", "geocache.json", "summary_cache.json"):   # first run: start from the repo's data
    if not os.path.exists(os.path.join(DATA_DIR, _name)) and os.path.exists(os.path.join(HERE, _name)):
        shutil.copy(os.path.join(HERE, _name), DATA_DIR)
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
GEOCACHE_FILE = os.path.join(DATA_DIR, "geocache.json")
SUMMARY_CACHE_FILE = os.path.join(DATA_DIR, "summary_cache.json")
_file_lock = threading.RLock()      # many visitors at once must not write a file at the same time
_geocode_lock = threading.Lock()
UNSTOP_URL = "https://unstop.com/api/public/opportunity/search-result"
# Unstop category -> (our event type, the most pages of 100 to fetch).
# Order matters: an event listed in two categories keeps the first one's label.
UNSTOP_CATEGORIES = {"hackathons": ("hackathon", 5), "workshops": ("workshop", 3), "conferences": ("conference", 2),
                     "quizzes": ("quiz", 2), "competitions": ("competition", 12)}
DEVPOST_URL = "https://devpost.com/api/hackathons"
DEVPOST_PAGES = 5          # Devpost sends 9 events per page
DEVFOLIO_URL = "https://api.devfolio.co/api/search/hackathons"
HACKEREARTH_URL = "https://www.hackerearth.com/chrome-extension/events/"
MLH_URL = "https://mlh.io/seasons/2027/events"
KNOWAFEST_URL = "https://www.knowafest.com"
KNOWAFEST_PAGES = 20       # the most list pages to read; it shows about 12 events per page
HACK2SKILL_URL = "https://hack2skill.com/api/v1/innovator/public/event/list"
CODEFORCES_URL = "https://codeforces.com/api/contest.list?gym=false"
IST = timezone(timedelta(hours=5, minutes=30))     # contest times are shown as Indian dates
HEADERS = {"User-Agent": "Mozilla/5.0 (hackfindr student project)"}


# ---------- small helpers ----------
def load_json(path, default):
    with _file_lock:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return default


def save_json(path, data):
    """Write to a temporary file, then swap it in, so a crash can never leave half a file."""
    with _file_lock:
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp, path)


def parse_json(text):
    """Pull the first {...} block out of a Gemma reply. Raises ValueError if there is none."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("Gemma did not return JSON:\n" +(text or "")[:300])
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Gemma returned JSON, but not an object.")
    return data


def make_id(source, key):
    return source + "-" + hashlib.md5(str(key).encode()).hexdigest()[:8]


def haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance between two points on Earth, in km."""
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- geocoding (cached, 1 request/sec) ----------
_geocache = load_json(GEOCACHE_FILE, {})
_last_geocode = 0.0


def geocode(place):
    """City name -> (lat, lon) or (None, None). Cached in geocache.json."""
    if not place or not str(place).strip():
        return None, None
    key = str(place).strip().lower()
    if key in _geocache:
        return tuple(_geocache[key]) if _geocache[key] else (None, None)
    global _last_geocode
    try:
        from geopy.geocoders import Nominatim
        with _geocode_lock:                              # one lookup at a time, even with many visitors
            wait = 1.0 - (time.time() - _last_geocode)   # Nominatim rule: max 1 req/sec
            if wait > 0:
                time.sleep(wait)
            _last_geocode = time.time()
            loc = Nominatim(user_agent="hackfindr").geocode(place, timeout=10)
        _geocache[key] = [loc.latitude, loc.longitude] if loc else None
    except Exception as e:
        print("geocode failed for", place, "->", e)
        return None, None          # don't cache network errors
    save_json(GEOCACHE_FILE, _geocache)
    return tuple(_geocache[key]) if _geocache[key] else (None, None)


# ---------- Unstop ----------
def unstop_link(seo_url):
    """seo_url is sometimes a full link and sometimes only a path."""
    return seo_url if seo_url.startswith("http") else "https://unstop.com/" + seo_url.lstrip("/")


def guess_type(title, default):
    """Ideathons and webinars are often listed under other categories, so check the title."""
    low = title.lower()
    for word, kind in (("ideathon", "ideathon"), ("webinar", "webinar"), ("workshop", "workshop"),
                       ("bootcamp", "workshop")):
        if word in low:
            return kind
    return default


def get_unstop_page(params):
    """One page of Unstop results, trying twice because Unstop sometimes drops the connection."""
    for attempt in (1, 2):
        try:
            r = requests.get(UNSTOP_URL, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()["data"]
        except Exception as e:
            print("Unstop", params["opportunity"], "page", params["page"], "try", attempt, "failed ->", e)
            time.sleep(3)
    return None


def fetch_unstop(limit=None):
    events = []
    cats = UNSTOP_CATEGORIES
    if limit is not None:
        try:
            lim = int(limit)
            if lim <= 50:
                cats = {"hackathons": ("hackathon", 1)}
            elif lim <= 100:
                cats = {"hackathons": ("hackathon", 1), "workshops": ("workshop", 1)}
            elif lim <= 250:
                cats = {"hackathons": ("hackathon", 2), "workshops": ("workshop", 1), "competitions": ("competition", 2)}
            elif lim <= 500:
                cats = {"hackathons": ("hackathon", 3), "workshops": ("workshop", 2), "conferences": ("conference", 1), "competitions": ("competition", 5)}
            else:
                cats = {"hackathons": ("hackathon", 3), "workshops": ("workshop", 2), "conferences": ("conference", 1), "quizzes": ("quiz", 1), "competitions": ("competition", 8)}
        except (ValueError, TypeError):
            cats = UNSTOP_CATEGORIES

    for category, (event_type, pages) in cats.items():
        for page in range(1, pages + 1):
            params = {"opportunity": category, "oppstatus": "open", "per_page": 100, "page": page}
            result = get_unstop_page(params)
            if result is None:         # keep the pages we already have
                break
            events += unstop_events(result["data"], event_type)
            if limit and len(events) >= int(limit):
                break
            if page >= (result.get("last_page") or 1):   # a page can hold under 100 events and still not be the last
                break
            time.sleep(1)              # Unstop drops connections if asked too quickly
        if limit and len(events) >= int(limit):
            break
    if not events:
        raise RuntimeError("Unstop sent no events")
    # Many hackathons are also listed under competitions: keep the first, more specific label.
    return list({ev["id"]: ev for ev in reversed(events)}.values())[::-1]


def unstop_events(items, event_type):
    """Turn raw Unstop items into our event schema."""
    events = []
    for it in items:
        reg = it.get("regnRequirements") or {}
        if reg.get("reg_status") == "FINISHED":
            continue
        addr = it.get("address_with_country_logo") or {}
        mode = "offline" if (it.get("region") or "").lower() == "offline" else "online"
        city = ", ".join(x for x in [addr.get("city"), addr.get("state")] if x)
        team = f"Team size {reg.get('min_team_size', '?')}-{reg.get('max_team_size', '?')}"
        events.append({
            "id": make_id("unstop", it.get("id") or it.get("title")),
            "title": it.get("title", ""),
            "organiser": (it.get("organisation") or {}).get("name", ""),
            "type": guess_type(it.get("title", ""), event_type),
            "mode": mode,
            "city": city if mode == "offline" else "",
            "start": reg.get("start_regn_dt", ""),
            "end": reg.get("end_regn_dt", ""),
            "event_end": it.get("end_date") or "",    # when the event itself ends
            "is_free": not it.get("isPaid"),
            "description": "",
            "eligibility": "",
            "requirements": team,
            "team_size": team.replace("Team size ", ""),   # kept apart: the AI summary replaces requirements
            "register_url": unstop_link(it.get("seo_url") or ""),
            "poster_url": it.get("logoUrl2") or "",
            "source": "unstop",
            "details_html": it.get("details") or "",
        })
    return events


# ---------- Devpost ----------
def devpost_end(dates):
    """'Aug 31 - Oct 23, 2026' -> '2026-10-23'. Returns '' if the text looks different."""
    match = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})\s*$", dates or "")
    if not match:
        return ""
    try:
        return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date().isoformat()
    except ValueError:
        return ""


def fetch_devpost(limit=None):
    items = []
    pages = DEVPOST_PAGES
    if limit is not None:
        try:
            pages = max(1, min(15, int(limit) // 20))
        except (ValueError, TypeError):
            pages = DEVPOST_PAGES
    for page in range(1, pages + 1):
        params = {"status[]": ["upcoming", "open"], "page": page}
        try:
            r = requests.get(DEVPOST_URL, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            batch = r.json().get("hackathons", [])
            items += batch
            if not batch or (limit and len(items) >= int(limit)):
                break
        except Exception as e:
            print("Devpost page", page, "failed ->", e)
            break
    events = []
    for it in items:
        loc = (it.get("displayed_location") or {}).get("location", "")
        mode = "online" if loc.lower() in ("online", "") else "offline"
        thumb = it.get("thumbnail_url") or ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        themes = ", ".join(t.get("name", "") for t in it.get("themes", []))
        events.append({
            "id": make_id("devpost", it.get("id") or it.get("url")),
            "title": it.get("title", ""),
            "organiser": it.get("organization_name", ""),
            "type": guess_type(it.get("title", ""), "hackathon"),
            "mode": mode,
            "city": loc if mode == "offline" else "",
            "start": it.get("submission_period_dates", ""),
            "end": devpost_end(it.get("submission_period_dates", "")),
            "description": f"Themes: {themes}" if themes else "",
            "eligibility": "",
            "requirements": "",
            "register_url": it.get("url", ""),
            "event_end": "",
            "is_free": None,                          # Devpost does not say
            "poster_url": thumb,
            "source": "devpost",
        })
    return events


# ---------- Devfolio (Indian hackathons) ----------
def devfolio_event(it):
    """Turn one raw Devfolio hackathon into our event schema."""
    online = bool(it.get("is_online"))
    city = ", ".join(x for x in [it.get("city"), it.get("state")] if x) or it.get("location") or ""
    slug = it.get("slug", "")
    setting = it.get("hackathon_setting") or {}
    size = f"{it.get('team_min') or 1}-{it['team_size']}" if it.get("team_size") else ""
    return {
        "id": make_id("devfolio", slug or it.get("name")),
        "title": it.get("name", ""),
        "organiser": "",
        "type": guess_type(it.get("name", ""), "hackathon"),
        "mode": "online" if online else "offline",
        "city": "" if online else city,
        "start": (it.get("starts_at") or "")[:10],
        "end": (setting.get("reg_ends_at") or it.get("ends_at") or "")[:10],   # when registration closes
        "description": it.get("tagline") or "",
        "eligibility": "",
        "requirements": f"Team size {size}" if size else "",
        "team_size": size,
        "register_url": f"https://{slug}.devfolio.co" if slug else "https://devfolio.co/hackathons",
        "event_end": (it.get("ends_at") or "")[:10],
        "is_free": None,
        "poster_url": setting.get("logo") or "",
        "source": "devfolio",
    }


def fetch_devfolio():
    events = []
    for kind in ("application_open", "upcoming"):      # registration open now, or opening soon
        body = {"type": kind, "from": 0, "size": 100}
        r = requests.post(DEVFOLIO_URL, json=body, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        events += [devfolio_event(hit.get("_source", {})) for hit in r.json().get("hits", {}).get("hits", [])]
    return events


# ---------- HackerEarth (online challenges, hackathons, webinars) ----------
HACKEREARTH_TYPES = {"hackathon": "hackathon", "webinar": "webinar", "hiring": "hackathon"}


def fetch_hackerearth():
    r = requests.get(HACKEREARTH_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    events = []
    for it in r.json().get("response", []):
        if (it.get("status") or "").upper() == "ENDED":
            continue
        kind = (it.get("challenge_type") or "").lower()
        events.append({
            "id": make_id("hackerearth", it.get("url") or it.get("title")),
            "title": it.get("title", ""),
            "organiser": "HackerEarth",
            "type": next((v for k, v in HACKEREARTH_TYPES.items() if k in kind), "hackathon"),
            "mode": "online",
            "city": "",
            "start": (it.get("start_utc_tz") or it.get("start_tz") or "")[:10],
            "end": (it.get("end_utc_tz") or it.get("end_tz") or "")[:10],
            "description": (it.get("description") or "")[:300],
            "eligibility": "",
            "requirements": "",
            "register_url": it.get("url", ""),
            "event_end": "",
            "is_free": None,
            "poster_url": it.get("cover_image") or "",
            "source": "hackerearth",
        })
    return events


# ---------- MLH (Major League Hacking season page, HTML) ----------
def fetch_mlh():
    from bs4 import BeautifulSoup
    r = requests.get(MLH_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    for card in soup.select("div.event"):
        name = card.select_one(".event-name")
        link = card.select_one("a")
        if not name or not link:
            continue
        def text(sel):
            el = card.select_one(sel)
            return el.get_text(strip=True) if el else ""
        city = ", ".join(x for x in [text('[itemprop="addressLocality"]'),
                                     text('[itemprop="addressRegion"]')] if x)
        online = "digital" in text(".event-hybrid-notes").lower() or not city
        img = card.select_one(".image-wrap img") or card.select_one("img")
        events.append({
            "id": make_id("mlh", link.get("href")),
            "title": name.get_text(strip=True),
            "organiser": "MLH",
            "type": "hackathon",
            "mode": "online" if online else "offline",
            "city": "" if online else city,
            "start": text(".event-date"),
            "end": "",
            "description": "",
            "eligibility": "",
            "requirements": "",
            "register_url": link.get("href", ""),
            "event_end": "",
            "is_free": None,
            "poster_url": img.get("src", "") if img else "",
            "source": "mlh",
        })
    return events


# ---------- KnowAFest (Indian college events) ----------
def knowafest_slugs():
    """The page name of every listed event, reading list pages until one shows nothing new."""
    from bs4 import BeautifulSoup
    slugs = []
    for page in range(1, KNOWAFEST_PAGES + 1):
        try:
            r = requests.get(f"{KNOWAFEST_URL}/events", params={"page": page}, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:   # past the last page
                break
            r.raise_for_status()
        except Exception as e:
            if not slugs:              # nothing read at all: let the saved events be used instead
                raise
            print("KnowAFest page", page, "failed, using the pages read so far ->", e)
            break
        links = BeautifulSoup(r.text, "html.parser").select("a[href*='/event/']")
        found = {re.split("[/#?]", a["href"].split("/event/")[1])[0] for a in links}
        new = sorted(slug for slug in found if slug and slug not in slugs)
        if not new:
            break
        slugs += new
        time.sleep(1.5)
    return slugs


def knowafest_data(slug):
    """The schema.org Event block on an event's page (the data sites publish for search engines)."""
    from bs4 import BeautifulSoup
    r = requests.get(f"{KNOWAFEST_URL}/event/{slug}", headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    for block in BeautifulSoup(r.text, "html.parser").select('script[type="application/ld+json"]'):
        data = json.loads(block.string or "{}")
        if isinstance(data, dict) and data.get("@type") == "Event":
            return data
    return None


def knowafest_event(slug, it):
    """Turn one schema.org Event into our event schema."""
    place = it.get("location") or {}
    if isinstance(place, list):        # hybrid events list a venue and an online link
        place = next((p for p in place if p.get("@type") == "Place"), {})
    address = place.get("address")
    address = address if isinstance(address, dict) else {}
    city = ", ".join(x for x in [address.get("addressLocality"), address.get("addressRegion")] if x)
    online = "OnlineEvent" in str(it.get("eventAttendanceMode"))
    offers = it.get("offers") or {}
    offer = offers[0] if isinstance(offers, list) else offers
    price = offer.get("price")
    images = it.get("image") or ""
    text = " ".join(str(it.get("description") or "").split())
    title = it.get("name", "")
    return {
        "id": make_id("knowafest", slug),
        "title": title,
        "organiser": (it.get("organizer") or {}).get("name", ""),
        "type": guess_type(title, "hackathon" if "hackathon" in title.lower() else "conference"),
        "mode": "online" if online else "offline",
        "city": "" if online else city,
        "start": it.get("startDate", ""),
        "end": "",                         # KnowAFest only publishes the event's own dates
        "event_end": it.get("endDate") or it.get("startDate", ""),
        "is_free": None if price in (None, "") else float(price) == 0,
        "description": short_text(text),
        "details_text": text[:4000],       # lets Gemma summarize it when someone opens it
        "eligibility": "",
        "requirements": "",
        "register_url": offer.get("url") or it.get("url", ""),
        "poster_url": images[0] if isinstance(images, list) else images,
        "source": "knowafest",
    }


def fetch_knowafest():
    known = {ev["id"] for ev in load_events()}     # saved events are kept, so only open the new ones
    events = []
    for slug in knowafest_slugs():
        if make_id("knowafest", slug) in known:
            continue
        try:
            data = knowafest_data(slug)
            if data and "Cancelled" not in str(data.get("eventStatus")):
                events.append(knowafest_event(slug, data))
        except Exception as e:             # one odd page should not lose the others
            print("KnowAFest event skipped:", slug, "->", e)
        time.sleep(1.5)
    return events


# ---------- Hack2skill (public event list) ----------
def fetch_hack2skill():
    r = requests.get(HACK2SKILL_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    events = []
    for group in r.json().get("data", {}).values():    # flagship and community events
        for it in group:
            if not it.get("registrationEnd"):
                continue
            tags = it.get("tags") or {}
            team = tags.get("teamSize") or {}
            size = f"{team.get('min', 1)}-{team['max']}" if team.get("max") else ""
            title = it.get("title", "")
            events.append({
                "id": make_id("hack2skill", it.get("eventUrl") or title),
                "title": title,
                "organiser": "Hack2skill",
                "type": guess_type(title, "hackathon"),
                # hybrid events can be joined online; the list does not say where in-person ones are held
                "mode": "offline" if (tags.get("mode") or {}).get("value") == "IN_PERSON" else "online",
                "city": "",
                "start": "",               # only registration dates are published
                "end": it["registrationEnd"],
                "event_end": "",
                "is_free": True if (tags.get("ticket") or {}).get("value") == "FREE" else None,
                "description": "",
                "eligibility": "",
                "requirements": f"Team size {size}" if size else "",
                "team_size": size,
                "register_url": it.get("customEventUrl") or f"https://hack2skill.com/event/{it.get('eventUrl')}",
                "poster_url": it.get("thumbnail") or "",
                "source": "hack2skill",
            })
    return events


# ---------- Codeforces (coding contests, official API) ----------
def fetch_codeforces():
    r = requests.get(CODEFORCES_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    events = []
    for it in r.json().get("result", []):
        if it.get("phase") != "BEFORE" or not it.get("startTimeSeconds"):
            continue
        start = datetime.fromtimestamp(it["startTimeSeconds"], IST)
        hours = it.get("durationSeconds", 0) / 3600
        events.append({
            "id": make_id("codeforces", it.get("id")),
            "title": it.get("name", ""),
            "organiser": "Codeforces",
            "type": "contest",
            "mode": "online",
            "city": "",
            "start": start.isoformat(),
            "end": start.isoformat(),      # you can register until it starts
            "event_end": (start + timedelta(hours=hours)).isoformat(),
            "is_free": True,
            "description": f"Online coding contest on Codeforces, {hours:g} hours long.",
            "eligibility": "",
            "requirements": "",
            "register_url": f"https://codeforces.com/contests/{it.get('id')}",
            "poster_url": "",
            "source": "codeforces",
        })
    return events


# ---------- Gemma summaries of Unstop HTML (cached) ----------
def short_text(text, limit=220):
    """First sentence or two of a long text, cut at a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


SUMMARY_PROMPT = ("From this event description, return ONLY JSON with keys "
                  '"description" (2-3 sentence summary for a student), "eligibility" (short), '
                  '"requirements" (short).\n\n')


def apply_summary(ev, summary):
    """Copy Gemma's summary fields onto the event and mark it as AI-summarized."""
    for k in ("description", "eligibility", "requirements"):
        value = str(summary.get(k) or "").strip()
        if value and value.lower().rstrip(".") not in ("not specified", "not mentioned", "n/a", "none"):
            ev[k] = value
    ev["ai_summary"] = True


def summarize_one(ev):
    """Gemma summary for one event (used when someone opens an event that has none yet).
    Cached in summary_cache.json and saved into events.json, so it runs once per event."""
    cache = load_json(SUMMARY_CACHE_FILE, {})
    if ev["id"] not in cache:
        import gemma_client
        cache[ev["id"]] = parse_json(gemma_client.ask(SUMMARY_PROMPT + ev["details_text"]))
        save_json(SUMMARY_CACHE_FILE, cache)
    apply_summary(ev, cache[ev["id"]])
    with _file_lock:
        events = load_events()
        for i, saved in enumerate(events):
            if saved["id"] == ev["id"]:
                events[i] = {**saved, **{k: ev[k] for k in ("description", "eligibility", "requirements", "ai_summary")}}
        save_json(EVENTS_FILE, events)
    return ev


def summarize_details(events):
    """Ask Gemma to summarize the first N Unstop events. The rest get summarized when someone opens them."""
    cache = load_json(SUMMARY_CACHE_FILE, {})
    done = 0
    from bs4 import BeautifulSoup
    for ev in events:
        html = ev.pop("details_html", "")
        if not html:
            continue
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        ev["details_text"] = text[:4000]   # kept so Gemma can summarize it later, on first view
        if not ev["description"]:      # until Gemma summarizes it, show the start of the real text
            ev["description"] = short_text(text)
        if ev["id"] in cache:
            apply_summary(ev, cache[ev["id"]])
            continue
        if done >= GEMMA_SUMMARY_LIMIT:
            continue
        done += 1
        try:
            import gemma_client
            cache[ev["id"]] = parse_json(gemma_client.ask(SUMMARY_PROMPT + ev["details_text"]))
            save_json(SUMMARY_CACHE_FILE, cache)
            apply_summary(ev, cache[ev["id"]])
        except Exception as e:
            print("Gemma summary skipped for", ev["title"], "->", e)
    return events


# ---------- main entry points ----------
def add_coords(events):
    for ev in events:
        if ev.get("lat") is None and ev["mode"] == "offline":
            ev["lat"], ev["lon"] = geocode(ev["city"])
            if ev["lat"] is None and "," in ev["city"]:   # "Some Hall, Bengaluru" -> try "Bengaluru"
                ev["lat"], ev["lon"] = geocode(ev["city"].rsplit(",", 1)[1])
        elif ev["mode"] != "offline":
            ev["lat"], ev["lon"] = None, None
    return events


def is_over(ev):
    """True if the event (or its registration) has already ended."""
    today = date.today().isoformat()
    ends = [str(ev.get(k) or "")[:10] for k in ("event_end", "end")]
    return any(e and e < today for e in ends)


SOURCES = {
    "unstop": fetch_unstop,
    "devpost": fetch_devpost,
    "devfolio": fetch_devfolio,
    "hackerearth": fetch_hackerearth,
    "mlh": fetch_mlh,
    "knowafest": fetch_knowafest,
    "hack2skill": fetch_hack2skill,
    "codeforces": fetch_codeforces,
}


def same_title(title):
    """'Hack CG 2026!' and 'hack cg 2026' -> the same key, to spot one event listed on two sites."""
    return "".join(ch for ch in title.lower() if ch.isalnum())


def one_per_event(events):
    """Same title on two sites -> one event, so keep the first. Same title on one site -> different
    colleges' events (five separate "Robo Soccer" competitions), so keep them all."""
    first_site, kept = {}, []
    for ev in events:
        if first_site.setdefault(same_title(ev["title"]), ev["source"]) == ev["source"]:
            kept.append(ev)
    return kept


def refresh_events(limit=None):
    """Fetch all sources (one at a time, even if called from two places at once)."""
    with _lock:
        return _refresh(limit)


def _refresh(limit=None):
    """Fetch live data; on any failure, fall back to the saved events.json."""
    events, saved = [], load_events()
    for source, fetch in SOURCES.items():
        try:
            events += fetch(limit) if source in ("unstop", "devpost") else fetch()
        except Exception as e:     # one failing source should not lose its events
            print(fetch.__name__, "failed, keeping its saved events ->", e)
            events += [ev for ev in saved if ev.get("source") == source]
    if not events:
        print("Live fetch failed, using saved events.json")
        return load_events()
    by_id = {ev["id"]: ev for ev in saved}
    for ev in events:
        by_id[ev["id"]] = ev
    events = one_per_event(list(by_id.values()))
    for ev in events:                # saved Devpost events from older runs may lack the end date
        if ev["source"] == "devpost" and not ev.get("end"):
            ev["end"] = devpost_end(ev.get("start", ""))
    events = [ev for ev in events if not is_over(ev)]
    events = add_coords(summarize_details(events))
    # keep events added from posters across refreshes
    with _file_lock:                 # re-read, so a poster added during this long fetch is not lost
        ids = {e["id"] for e in events}
        events += [e for e in load_events() if e.get("source") == "poster" and e["id"] not in ids]
        save_json(EVENTS_FILE, events)
    return events


def start_auto_refresh(hours=REFRESH_HOURS):
    """Website mode: fetch new events in the background, so visitors never have to."""
    def loop():
        while True:
            age = time.time() - os.path.getmtime(EVENTS_FILE) if os.path.exists(EVENTS_FILE) else hours * 3600
            time.sleep(max(60, hours * 3600 - age))
            try:
                refresh_events()
            except Exception as e:
                print("Auto refresh failed ->", e)
    threading.Thread(target=loop, daemon=True).start()


def load_events():
    """Saved events, leaving out any that have ended since the last refresh."""
    return [ev for ev in load_json(EVENTS_FILE, []) if not is_over(ev)]


def add_event(ev):
    """Append one event (e.g. from a poster) to events.json and return it."""
    with _file_lock:
        events = load_events()
        events = [e for e in events if e["id"] != ev["id"]] + [ev]   # same poster twice -> keep one
        save_json(EVENTS_FILE, events)
    return ev


_lock = threading.Lock()   # only one refresh at a time


if __name__ == "__main__":
    evs = refresh_events()
    print(len(evs), "events,", sum(1 for e in evs if e.get("lat")), "with map coordinates")
    for e in evs[:5]:
        print("-", e["title"], "|", e["mode"], "|", e["city"] or "online")
