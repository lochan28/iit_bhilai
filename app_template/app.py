"""HackFindr app. Run it from the repo folder:  python app_template/app.py

One page: a header with search and filters, the map beside the event list,
a details panel (with AI chat) that slides over the page, and poster upload.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so we can import gemma_client

import html
import json
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from datetime import date
from urllib.parse import quote

import folium
from folium.plugins import MarkerCluster
import gradio as gr

import events
import gemma_client
from harness.tools import filter_events

TITLE = "HackFindr"
# HACKFINDR_PUBLIC=1 turns on the website rules: visitor limits and no manual fetching.
PUBLIC = os.environ.get("HACKFINDR_PUBLIC") == "1"
AI_PER_VISITOR_HOUR = 30   # Gemma calls one visitor may use per hour
AI_PER_DAY = 500           # Gemma calls the whole website may use per day
POSTERS_PER_VISITOR_HOUR = 5
DEFAULT_CITY = "Raipur"
DEFAULT_RADIUS = 300  # km shown when the app opens
FETCH_CHOICES = [
    ("Fetch 50 events", 50),
    ("Fetch 100 events", 100),
    ("Fetch 200 events", 200),
    ("Fetch 300 events", 300),
    ("Fetch 500 events", 500),
    ("Fetch 1000 events", 1000),
]
DEFAULT_FETCH_COUNT = 100
CHIPS = ["All", "Saved", "Hackathons", "Workshops", "Free", "This week", "Open to 1st year"]
PAGE = 24             # cards shown at first; "Show more" adds this many again
RADIUS_CHOICES = [("50 km", 50), ("150 km", 150), ("300 km", 300), ("600 km", 600), ("1000 km", 1000), ("2000 km", 2000)]
# Every event type belongs to one of four colour groups, used for pins, thumbnails and labels.
TYPE_GROUP = {"hackathon": "hack", "ideathon": "hack", "workshop": "work", "webinar": "work",
              "competition": "comp", "conference": "conf"}
PIN_COLORS = {"hack": "#6d5dfc", "work": "#10b981", "comp": "#f59e0b", "conf": "#0ea5e9", "other": "#94a3b8"}
LEGEND = [("hack", "Hackathon"), ("work", "Workshop"), ("comp", "Competition"), ("conf", "Conference")]
ACCENT, HERE = "#6d5dfc", "#ff4d6d"  # radius circle, "you are here" dot
SOURCE_NAMES = {"unstop": "Unstop", "devpost": "Devpost", "poster": "Unverified poster", "sample": "Sample"}


# ---------- website limits (only when PUBLIC) ----------

_usage = defaultdict(list)   # (kind, visitor) -> times of recent uses; kept in memory only


def visitor_id(request):
    """Who is asking. Behind a website proxy the real address is in x-forwarded-for."""
    if request is None:
        return "local"
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or getattr(request.client, "host", "unknown")


def limit_message(request, kind="ai", cost=1):
    """Count one use. Returns "" if allowed, or a friendly message if a limit was reached."""
    if not PUBLIC:
        return ""
    now, visitor = time.time(), visitor_id(request)
    per_hour = POSTERS_PER_VISITOR_HOUR if kind == "poster" else AI_PER_VISITOR_HOUR
    mine = _usage[kind, visitor] = [t for t in _usage[kind, visitor] if now - t < 3600]
    everyone = _usage["day", "all"] = [t for t in _usage["day", "all"] if now - t < 86400]
    if len(everyone) + cost > AI_PER_DAY:
        return "HackFindr has used today's AI allowance. Please try again tomorrow."
    if len(mine) + cost > per_hour:
        return "You have reached the hourly limit. Please try again later."
    mine.extend([now] * cost)
    everyone.extend([now] * cost)
    return ""


def safe_url(url):
    """Only real web links may become clickable (a poster could contain a harmful javascript: link)."""
    url = str(url or "").strip()
    return url if re.match(r"https?://", url, re.IGNORECASE) else ""


# ---------- small date helpers ----------

def to_date(text):
    """'2026-10-14T09:00:00+05:30' -> date(2026, 10, 14). Anything else -> None."""
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def nice_date(text):
    """Short date like '14 Oct'. Devpost gives text like 'Oct 20 - Dec 21, 2026', which we keep."""
    d = to_date(text)
    return d.strftime("%d %b").lstrip("0") if d else str(text or "")


def days_left(ev):
    """Days until registration closes, or None if we do not know."""
    d = to_date(ev.get("end"))
    return (d - date.today()).days if d else None


def event_date(ev):
    """The best date we have for the event itself."""
    return ev.get("event_end") or ev.get("start") or ""


# ---------- filtering for the chips ----------

FIRST_YEAR = re.compile(r"1st|first[- ]year|all years|any year|freshers?", re.IGNORECASE)


def chip_match(ev, chip, saved=()):
    if chip == "Saved":
        return ev["id"] in saved
    if chip == "Hackathons":
        return ev.get("type") in ("hackathon", "ideathon")
    if chip == "Workshops":
        return ev.get("type") in ("workshop", "webinar")
    if chip == "Free":
        return ev.get("is_free") is True
    if chip == "This week":
        left = days_left(ev)
        when = to_date(event_date(ev))
        soon = when is not None and 0 <= (when - date.today()).days <= 7
        return soon or (left is not None and 0 <= left <= 7)
    if chip == "Open to 1st year":
        text = " ".join(str(ev.get(k) or "") for k in ("eligibility", "description", "requirements"))
        return bool(FIRST_YEAR.search(text))
    return True


def find_events(city, radius, chip, sort, saved=(), query=""):
    """Offline events near the city first, then online ones, filtered by the chip and the title search."""
    lat, lon, near, online = filter_events(city, int(radius))
    found = [e for e in near + online if chip_match(e, chip, saved)]
    words = str(query or "").lower().split()
    if words:   # every word must appear in the title or organiser
        text = lambda e: (str(e.get("title") or "") + " " + str(e.get("organiser") or "")).lower()
        found = [e for e in found if all(w in text(e) for w in words)]
    if sort == "Closing soon":
        found.sort(key=lambda e: days_left(e) if days_left(e) is not None else 9999)
    return lat, lon, found


# ---------- HTML pieces ----------

ICON_IMG = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l2.4 5.6L20 10l-5.6 2.4L12 18l-2.4-5.6L4 10l5.6-2.4z"/></svg>')
ICON_PIN = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z"/>'
            '<circle cx="12" cy="9" r="2.5"/></svg>')
LOGO_HTML = (f'<div class="ce-logo"><span class="ce-logo-box">{ICON_PIN}</span>'
             f'<span class="ce-logo-word">Hack<b>Findr</b></span></div>')


def esc(value):
    return html.escape(str(value or ""))


def group(ev):
    """Which of the four kinds an event belongs to; decides its colour everywhere."""
    return TYPE_GROUP.get(ev.get("type"), "other")


def pin_color(ev):
    return PIN_COLORS[group(ev)]


def closes_text(ev):
    left = days_left(ev)
    if left is None or left < 0:
        return ""
    return "closes today" if left == 0 else f"closes in {left} day{'s' if left != 1 else ''}"


def meta_line(ev, with_closes=True):
    """City · km · date · closes in N days (each part only if we know it)."""
    parts = [ev.get("city") or "Online"]
    if ev.get("distance_km") is not None:
        parts.append(f"{ev['distance_km']} km")
    if event_date(ev):
        parts.append(nice_date(event_date(ev)))
    line = " · ".join(esc(p) for p in parts)
    if with_closes and closes_text(ev):
        line += f' · <span class="ce-closes">{closes_text(ev)}</span>'
    return line


def tint(ev):
    return group(ev)


def type_html(ev):
    kind = ev.get("type")
    return f'<span class="ce-type {group(ev)}">{esc(kind.capitalize())}</span>' if kind else ""


def tags_html(ev, with_type=False):
    source = ev.get("source", "")
    tags = []
    if with_type and ev.get("type"):
        tags.append((ev["type"].capitalize(), "type " + group(ev)))
    if ev.get("is_free") is True:
        tags.append(("Free", "free"))
    tags.append((SOURCE_NAMES.get(source, source), "src " + source))
    return "".join(f'<span class="ce-tag {cls}">{esc(text)}</span>' for text, cls in tags if text)


def thumb_html(ev, cls="ce-thumb"):
    if safe_url(ev.get("poster_url")):
        return f'<div class="{cls} {tint(ev)}"><img src="{esc(ev["poster_url"])}" alt="" loading="lazy"></div>'
    return f'<div class="{cls} {tint(ev)}">{ICON_IMG}</div>'


def card_html(ev):
    closes = closes_text(ev)
    hot = days_left(ev) is not None and days_left(ev) <= 3
    badge = f'<span class="ce-badge{" hot" if hot else ""}">{closes}</span>' if closes else ""
    summary = ev.get("description")
    summary_html = f'<div class="ce-summary">{esc(summary)}</div>' if summary else ""
    star = f'<button class="ce-star" data-id="{esc(ev["id"])}" aria-label="Save this event">★</button>'
    return (f'{thumb_html(ev)}<div class="ce-card-body"><div class="ce-card-top">{type_html(ev)}{badge}</div>'
            f'<div class="ce-card-title">{esc(ev["title"])}</div>'
            f'<div class="ce-meta">{meta_line(ev, with_closes=False)}</div>{summary_html}'
            f'<div class="ce-tags">{tags_html(ev)}</div></div>{star}<span class="ce-chev" aria-hidden="true">›</span>')


def deadline_strip(found):
    """Events whose registration closes within 7 days, soonest first."""
    soon = sorted((e for e in found if days_left(e) is not None and 0 <= days_left(e) <= 7), key=days_left)[:10]
    if not soon:
        return ""
    def item(e):
        left = days_left(e)
        when = "today" if left == 0 else f"{left}d"
        return (f'<button class="ce-dl{" hot" if left <= 2 else ""}" data-id="{esc(e["id"])}">'
                f'<b>{when}</b>{esc(e["title"])}</button>')
    return f'<div class="ce-strip"><span class="ce-strip-label">Closing this week</span>{"".join(item(e) for e in soon)}</div>'


def bullets(text):
    """Split 'a; b, c' style text into a bullet list."""
    items = [p.strip(" .") for p in re.split(r"[;\n•]|, (?=[A-Z])", str(text or "")) if p.strip(" .")]
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>" if items else ""


def details_html(ev, note=""):
    """The details panel. Tiles and sections without data are left out."""
    tiles = [("Event date", nice_date(event_date(ev))), ("Where", ev.get("city") or "Online"),
             ("Registration closes", nice_date(ev.get("end"))), ("Team size", ev.get("team_size"))]
    tiles_html = "".join(f'<div class="ce-tile"><div class="ce-tile-label">{k}</div>'
                         f'<div class="ce-tile-value">{esc(v)}</div></div>' for k, v in tiles if v)
    sections = ""
    if ev.get("description"):
        heading = '<span class="ce-ai">✦ AI summary</span>' if ev.get("ai_summary") else "About"
        sections += f'<h4>{heading}</h4><p>{esc(ev["description"])}</p>'
    for label in ("eligibility", "requirements"):
        if ev.get(label):
            sections += f"<h4>{label.capitalize()}</h4>{bullets(ev[label])}"
    source = SOURCE_NAMES.get(ev.get("source"), "the website")
    register = ""
    if safe_url(ev.get("register_url")):
        register = (f'<div class="ce-register-card"><a class="ce-register" href="{esc(ev["register_url"])}" '
                    f'target="_blank" rel="noopener">Register on {esc(source)} ↗</a>'
                    f'<div class="ce-caption">Opens the official registration page</div></div>')
    closes = closes_text(ev)
    org = f'<div class="ce-org">by {esc(ev["organiser"])}</div>' if ev.get("organiser") else ""
    badge = f'<span class="ce-badge">{closes}</span>' if closes else ""
    # data-id lets the browser-only "Copy link" button know which event is open (gr.State never reaches the browser)
    return (f'<div class="ce-details" data-id="{esc(ev["id"])}">{thumb_html(ev, "ce-poster")}'
            f'<div class="ce-tags">{tags_html(ev, True)}{badge}</div>'
            f'<h2>{esc(ev["title"])}</h2>{org}<div class="ce-tiles">{tiles_html}</div>{register}{sections}{note}</div>')


MAP_STYLE = """<style>
  :root { --bg:#eef0f4; --card:#ffffff; --border:#e6e5ee; --muted:#6e6b80; --title:#15141c; --hot:#e5484d; }
  html.dark { --bg:#0c0b11; --card:#18171f; --border:#2a2935; --muted:#9c99ad; --title:#f1f0f6; --hot:#ff6b70; }
  .leaflet-container { background: var(--bg) !important; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .leaflet-tile-pane { filter: saturate(.25) brightness(1.05) contrast(.95); }
  html.dark .leaflet-tile-pane { filter: grayscale(1) invert(1) brightness(.62) contrast(1.15); }
  .leaflet-tile-container img { width: 256.6px !important; height: 256.6px !important; }  /* hides tile seams */
  .leaflet-tooltip { background: var(--card) !important; color: var(--muted) !important;
    border: 1px solid var(--border) !important; border-radius: 14px !important; padding: 10px 12px !important;
    white-space: normal !important; width: max-content !important; max-width: 280px !important;
    font-size: 12px !important; line-height: 1.45 !important; box-shadow: 0 12px 32px rgba(20, 16, 50, .18) !important; }
  .leaflet-tooltip b { color: var(--title) !important; font-size: 14px !important; font-weight: 600 !important;
    display: block !important; margin-bottom: 2px !important; }
  .leaflet-tooltip-top:before { border-top-color: var(--card) !important; }
  .ce-tip { display: flex; gap: 10px; align-items: flex-start; }
  .ce-tip img { width: 44px; height: 44px; flex: none; border-radius: 10px; object-fit: cover; }
  .ce-closes { color: var(--hot); font-weight: 500; }
  .leaflet-interactive { cursor: pointer; }
  .marker-cluster, .marker-cluster div { background: rgba(109, 93, 252, .25) !important; }
  .marker-cluster div { background: #6d5dfc !important; color: #fff !important; font-weight: 600; }
  .leaflet-bar { border: 1px solid var(--border) !important; border-radius: 12px !important; overflow: hidden;
    box-shadow: 0 6px 18px rgba(20, 16, 50, .12) !important; }
  .leaflet-bar a { background: var(--card) !important; color: var(--title) !important; border-color: var(--border) !important;
    width: 34px !important; height: 34px !important; line-height: 34px !important; }
  .leaflet-control-attribution { background: transparent !important; color: var(--muted) !important; font-size: 10px !important; }
  .leaflet-control-attribution a { color: var(--muted) !important; }
</style>
<script>
  // copy day/night from the page around the map, whenever the theme button changes it
  function ceSync() { document.documentElement.classList.toggle("dark", !!parent.document.querySelector(".dark")); }
  ceSync();
  try { new MutationObserver(ceSync).observe(parent.document.body, {attributes: true, attributeFilter: ["class"]}); } catch (e) {}
  setInterval(ceSync, 1500);
</script>"""


def map_html(lat, lon, radius, found):
    """Map with a dashed radius circle and coloured pins, inside an iframe we size ourselves."""
    m = folium.Map(location=[lat, lon], tiles="OpenStreetMap", zoom_control=True)
    # Free OpenStreetMap tiles, recoloured with a CSS filter (dark tile services now need a key).
    # The map lives in its own frame, so it has its own day/night colours and copies the page's mode.
    m.get_root().header.add_child(folium.Element(MAP_STYLE))
    folium.Circle([lat, lon], radius=radius * 1000, color=ACCENT, weight=1.5, dash_array="5 7",
                  fill=True, fill_color=ACCENT, fill_opacity=0.06).add_to(m)
    folium.CircleMarker([lat, lon], radius=14, color=HERE, weight=0, fill=True, fill_color=HERE,
                        fill_opacity=0.18).add_to(m)
    folium.CircleMarker([lat, lon], radius=6, color="white", weight=2.5, fill=True, fill_color=HERE, fill_opacity=1,
                        tooltip="You are here").add_to(m)
    # close pins merge into a numbered bubble that splits apart as you zoom in
    cluster = MarkerCluster(options={"showCoverageOnHover": False, "maxClusterRadius": 45,
                                     "spiderfyOnMaxZoom": True}).add_to(m)
    pins = []  # (folium's JavaScript name for the pin, event id)
    for ev in found:
        if ev.get("lat") is None:
            continue
        logo = f'<img src="{esc(ev["poster_url"])}" alt="">' if safe_url(ev.get("poster_url")) else ""
        tip = f'<div class="ce-tip">{logo}<div><b>{esc(ev["title"])}</b>{meta_line(ev)}</div></div>'
        pin = folium.CircleMarker([ev["lat"], ev["lon"]], radius=8, color="white", weight=2.5, fill=True,
                                  fill_color=pin_color(ev), fill_opacity=1,
                                  tooltip=folium.Tooltip(tip, direction="top", offset=(0, -6))).add_to(cluster)
        pins.append((pin.get_name(), ev["id"]))
    d = radius / 111  # about 111 km per degree, so the whole circle fits on screen
    bounds = [[lat - d, lon - d], [lat + d, lon + d]]
    m.fit_bounds(bounds)
    # Clicking a pin writes its event id into the hidden #ce-pin-id box on the page, which opens the details.
    # The time is added so clicking the same pin twice still counts as a change.
    clicks = "".join(f"{name}.on('click', () => pick({json.dumps(event_id)}));" for name, event_id in pins)
    # re-measure once the frame has its final size, so tiles fill the whole map
    # re-measure once the frame has its final size, and again whenever the frame is resized
    refit = f"{m.get_name()}.invalidateSize(); {m.get_name()}.fitBounds({json.dumps(bounds)});"
    clicks += f"setTimeout(() => {{ {refit} }}, 300); window.addEventListener('resize', () => {{ {refit} }});"
    # Pointing at a card grows its pin (the page sends {hl: id}); pointing at a pin outlines its card.
    # A pin hidden inside a cluster is not on the map, so it is only brought forward when it is showing.
    lookup = ",".join(f"{json.dumps(i)}: {name}" for name, i in pins)
    clicks += ("const PINS = {" + lookup + "};"
               "window.addEventListener('message', e => {"
               "  if (!e.data || !('hl' in e.data)) return;"
               "  Object.entries(PINS).forEach(([id, p]) => p.setRadius(id === e.data.hl ? 13 : 8));"
               "  const p = PINS[e.data.hl]; if (p && p._map) p.bringToFront(); });"
               "function hl(id, on) {"
               "  const c = parent.document.querySelector('.ce-card-html[data-id=\"' + id + '\"]');"
               "  if (c) c.closest('.ce-card').classList.toggle('ce-hl', on); }")
    clicks += "".join(f"{name}.on('mouseover', () => hl({json.dumps(i)}, true));"
                      f"{name}.on('mouseout', () => hl({json.dumps(i)}, false));" for name, i in pins)
    m.get_root().html.add_child(folium.Element(
        "<script>window.addEventListener('load', () => {"
        "function pick(id) { const box = parent.document.querySelector('#ce-pin-id textarea');"
        " box.value = id + '|' + Date.now(); box.dispatchEvent(new Event('input', {bubbles: true})); }"
        + clicks + "});</script>"))
    page = html.escape(m.get_root().render(), quote=True)
    legend = "".join(f'<span><i style="background:{PIN_COLORS[k]}"></i>{label}</span>' for k, label in LEGEND)
    legend = f'<div class="ce-legend">{legend}<span><i class="here" style="background:{HERE}"></i>You</span></div>'
    return f'<div class="ce-map"><iframe srcdoc="{page}" title="Map of events"></iframe>{legend}</div>'


# ---------- what the buttons do ----------

def saved_ids(saved):
    """The hidden #ce-saved box holds the browser's saved ids as JSON text."""
    try:
        ids = json.loads(saved or "[]")
    except ValueError:
        return set()
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def show_events(city, radius, chip, sort, fetch_count=None, saved="[]", query="", *args):
    """Redraw the map, the card list and the deadline strip."""
    lat, lon, found = find_events(city, radius, chip, sort, saved_ids(saved), query)
    if lat is None:
        return (f"<p class='ce-error'>Couldn't find the city “{esc(city)}”. Check the spelling or try a bigger city nearby.</p>",
                [], "No events", PAGE, "")
    near = sum(1 for e in found if e.get("distance_km") is not None)
    header = f"**{near}** nearby · **{len(found) - near}** online"
    return map_html(lat, lon, int(radius), found), found, header, PAGE, deadline_strip(found)


def refresh(city, radius, chip, sort, fetch_count=DEFAULT_FETCH_COUNT, saved="[]", query="", *args):
    if not PUBLIC:   # on the website a background job fetches, so visitors cannot overload the sources
        events.refresh_events(limit=int(fetch_count))  # live fetch with selected event count
        gr.Info("Live events fetched.")
    return show_events(city, radius, chip, sort, fetch_count, saved, query)


def ics_file(ev):
    """A tiny calendar file: the event day and the registration deadline."""
    def clean(text):  # .ics text escapes backslashes, commas and semicolons
        return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HackFindr//EN"]
    for kind, day, label in (("event", to_date(event_date(ev)), ev["title"]),
                             ("deadline", to_date(ev.get("end")), "Registration closes: " + ev["title"])):
        if not day:
            continue
        # each entry needs its own UID, or calendars merge the two when both fall on the same day
        lines += ["BEGIN:VEVENT", f"UID:{ev['id']}-{kind}-{day:%Y%m%d}@hackfindr",
                  f"DTSTAMP:{date.today():%Y%m%d}T000000Z", f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                  f"SUMMARY:{clean(label)}"]
        if safe_url(ev.get("register_url")):
            lines.append(f"URL:{ev['register_url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(ev["id"]))   # the id becomes part of a file name
    path = Path(tempfile.gettempdir()) / f"hackfindr-{safe_id}.ics"
    path.write_text("\r\n".join(lines), encoding="utf-8")
    return str(path)


def nearest_city(latlon):
    """The city, among cities that have events, closest to the browser's location."""
    try:
        lat, lon = map(float, str(latlon).split(",")[:2])
    except ValueError:
        return gr.update()
    places = {(e["city"], e["lat"], e["lon"]) for e in events.load_events()
              if e.get("city") and e.get("lat") is not None}
    if not places:
        return gr.update()
    k = math.cos(math.radians(lat))
    name = min(places, key=lambda p: (p[1] - lat) ** 2 + ((p[2] - lon) * k) ** 2)[0]
    gr.Info(f"Showing events near {name}")
    return name


def open_details(event_id, found, request=None):
    """Show the details view for one event."""
    ev = next((e for e in found if e["id"] == event_id), None)
    ev = ev or next((e for e in events.load_events() if e["id"] == event_id), None)
    if ev is None:   # e.g. an old share link: say so in the panel, the list underneath keeps working
        return (gr.update(), gr.update(visible=True), "<p class='ce-error'>Event not found. It may have closed.</p>",
                event_id, gr.update(visible=False))
    note = ""
    if not ev.get("ai_summary") and ev.get("details_text"):
        blocked = limit_message(request)
        try:
            if blocked:
                raise RuntimeError(blocked)
            ev = events.summarize_one(dict(ev))  # one Gemma call, then saved for everyone
            for i, e in enumerate(found):        # so the card list shows it too
                if e["id"] == event_id:
                    found[i] = {**e, **ev}
        except Exception as error:
            note = f"<p class='ce-caption'>AI summary not available right now: {esc(gemma_client.describe_error(error))}</p>"
    # the list stays where it was underneath; the details panel opens over it
    has_dates = bool(to_date(event_date(ev)) or to_date(ev.get("end")))
    cal = gr.update(value=ics_file(ev) if has_dates else None, visible=has_dates)
    return gr.update(), gr.update(visible=True), details_html(ev, note), event_id, cal


def open_from_pin(value, found, request: gr.Request = None):
    """A map pin was clicked: the hidden box holds 'event_id|time'."""
    if not value:
        return (gr.update(),) * 5
    return open_details(value.split("|")[0], found, request)


def open_from_link(found, request: gr.Request = None):
    """Opening /?event=<id> goes straight to that event's details."""
    event_id = request.query_params.get("event", "") if request else ""
    if not event_id:
        return (gr.update(),) * 5          # same length as the "opens" outputs list
    return open_details(event_id[:100], found, request)


def ask_about_event(question, history, event_id, request: gr.Request = None):
    """Chat on the details page: one Gemma call, answering only from this event's details."""
    history = history or []
    if not question.strip():
        yield "", history
        return
    question = question[:500]
    blocked = limit_message(request)
    if blocked:
        yield "", history + [{"role": "user", "content": question}, {"role": "assistant", "content": blocked}]
        return
    ev = next((e for e in events.load_events() if e["id"] == event_id), None)
    if ev is None:
        yield "", history + [{"role": "user", "content": question},
                             {"role": "assistant", "content": "Open an event first."}]
        return
    facts = {k: ev.get(k) for k in ("title", "organiser", "type", "mode", "city",
                                    "is_free", "description", "eligibility", "requirements", "register_url")}
    # clear names help Gemma find the facts
    facts["team_size"] = ev.get("team_size")
    facts.update({"registration_opens": ev.get("start"), "registration_closes": ev.get("end"),
                  "event_ends": ev.get("event_end"), "today": str(date.today())})
    facts["full_details"] = ev.get("details_text", "")[:1200]  # shorter prompt = faster answer
    chat = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
    prompt = (f"Event details (JSON):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
              f"Conversation so far:\n{chat}\n\nStudent's question: {question}")
    rule = ("You answer a student's questions about ONE event, using only the event details given. "
            "If the answer is not in the details, say so and suggest checking the register link. "
            "Answer in 1-3 short sentences.")
    history = history + [{"role": "user", "content": question}, {"role": "assistant", "content": "…"}]
    yield "", history  # show the question straight away
    # Short questions do not need Gemma's long thinking: "minimal" answers in ~2 s instead of ~7 s.
    # The answer is streamed, so words appear as Gemma writes them.
    from google.genai import types
    config = types.GenerateContentConfig(system_instruction=rule,
                                         thinking_config=types.ThinkingConfig(thinking_level="minimal"))
    answer = ""
    for attempt in (1, 2):   # streaming skips gemma_client's retries, so try once more if Google is busy
        try:
            for chunk in gemma_client.get_client().models.generate_content_stream(
                    model=gemma_client.MODEL, contents=prompt, config=config):
                answer += chunk.text or ""
                history[-1]["content"] = answer or "…"
                yield "", history
            return
        except Exception as error:
            if attempt == 1 and not answer and gemma_client._is_busy(error):
                time.sleep(2)
                continue
            history[-1]["content"] = gemma_client.describe_error(error)
            yield "", history
            return


def ask_gemma(prompt, rule):
    """One short Gemma answer (not streamed), with the same quick 'minimal' thinking as the chat.
    Like the chat it skips gemma_client's retries, so it tries once more if Google is busy."""
    from google.genai import types
    config = types.GenerateContentConfig(system_instruction=rule,
                                         thinking_config=types.ThinkingConfig(thinking_level="minimal"))
    for attempt in (1, 2):
        try:
            reply = gemma_client.get_client().models.generate_content(
                model=gemma_client.MODEL, contents=prompt, config=config)
            return (reply.text or "").strip()
        except Exception as error:
            if attempt == 2 or not gemma_client._is_busy(error):
                raise
            time.sleep(2)


def event_facts(event_id):
    """The parts of one event that Gemma needs to judge it, or None if the id is unknown."""
    ev = next((e for e in events.load_events() if e["id"] == event_id), None)
    keys = ("title", "type", "mode", "city", "is_free", "eligibility", "requirements", "team_size", "description")
    return {k: ev.get(k) for k in keys} if ev else None


FIT_RULE = ("You help a student decide if ONE event suits them. Use only the event details and what the student "
            "says about themselves. First line: exactly 'Good fit', 'Maybe' or 'Not a fit' in bold. Then 2-3 short "
            "bullet reasons. Then one line starting 'Tip:' on how to prepare. If a detail is missing, say so.")


def fit_check(me, event_id, request: gr.Request = None):
    """One Gemma call: does this event suit the student? Also returns the profile so the browser keeps it."""
    me = str(me or "").strip()[:300]
    if not me:
        return "Write your year and skills first, for example “1st year CSE · Python”.", me
    blocked = limit_message(request)
    if blocked:
        return blocked, me
    facts = event_facts(event_id)
    if facts is None:
        return "Open an event first.", me
    prompt = f"Event (JSON): {json.dumps(facts, ensure_ascii=False)}\nStudent: {me}"
    try:
        return ask_gemma(prompt, FIT_RULE), me
    except Exception as error:
        return gemma_client.describe_error(error), me


TEAM_RULE = ("Write a short, friendly WhatsApp message (under 70 words) from a student inviting friends to form a "
             "team for this event. Include the name, date, team size and the register link if given. "
             "Plain text, at most one emoji. Use only the details given.")


def team_message(event_id, request: gr.Request = None):
    """One Gemma call: a ready-to-send WhatsApp invite, plus an 'Open in WhatsApp' link."""
    blocked = limit_message(request)
    if blocked:
        return blocked, ""
    facts = event_facts(event_id)
    if facts is None:
        return "Open an event first.", ""
    ev = next(e for e in events.load_events() if e["id"] == event_id)
    facts.update(date=nice_date(event_date(ev)), closes=nice_date(ev.get("end")), link=safe_url(ev.get("register_url")))
    try:
        text = ask_gemma(json.dumps(facts, ensure_ascii=False), TEAM_RULE)
    except Exception as error:
        return gemma_client.describe_error(error), ""
    if not text:
        return "Gemma sent back no text. Press the button again.", ""
    return text, wa_link(text)


def wa_link(text):
    """'Open in WhatsApp' with the message filled in. A click handler in HEAD swaps in the edited text."""
    text = str(text or "").strip()
    if not text:
        return ""
    return (f'<a class="ce-wa" href="https://wa.me/?text={quote(text)}" target="_blank" rel="noopener">'
            f'Open in WhatsApp ↗</a>')


CMP_RULE = ("Compare TWO events for a student, using only the details given. Give a markdown table whose first "
            "header cell is empty and whose other two header cells are the two event titles (shortened if long); "
            "it has exactly 3 rows: Date, Who can join, Team size. Then one line starting 'Pick:' that names the "
            "event to choose and says why. Call the events by their titles, never 'Event A' or 'Event B'. "
            "If the student described themselves, use that.")


def compare_choices(found, event_id):
    """Fill 'Compare with' from the events in view, never listing the one that is open."""
    others = [(e["title"][:60], e["id"]) for e in (found or []) if e["id"] != event_id][:60]
    return gr.update(choices=others, value=None)


def compare_events(a_id, b_id, me, request: gr.Request = None):
    """One Gemma call: the open event side by side with the chosen one."""
    if not b_id:
        return "Pick a second event to compare with."
    blocked = limit_message(request)
    if blocked:
        return blocked
    a, b = event_facts(a_id), event_facts(b_id)
    if not a or not b:
        return "One of those events is gone. Refresh and try again."
    for facts, eid in ((a, a_id), (b, b_id)):   # give Gemma the dates in plain words, like the team message
        ev = next(e for e in events.load_events() if e["id"] == eid)
        facts.update(date=nice_date(event_date(ev)), registration_closes=nice_date(ev.get("end")))
    prompt = (f"Event A: {json.dumps(a, ensure_ascii=False)}\nEvent B: {json.dumps(b, ensure_ascii=False)}\n"
              f"Student: {str(me or '')[:300]}")
    try:
        return ask_gemma(prompt, CMP_RULE) or "Gemma sent back no text. Press Compare again."
    except Exception as error:
        return gemma_client.describe_error(error)


def make_opener(event_id):
    """The click handler for one card's Details button."""
    def opener(found, request: gr.Request = None):
        return open_details(event_id, found, request)
    return opener


def back_to_events():
    return gr.update(), gr.update(visible=False)


# ---------- poster upload: step 1 extract, step 2 add ----------

POSTER_PROMPT = """Read this event poster. Return ONLY a JSON object with these keys:
"title", "type" (one of hackathon, ideathon, webinar, workshop, competition, conference), "date" (YYYY-MM-DD if possible),
"mode" (online or offline), "city", "eligibility", "requirements", "register_link".
Use "" for anything not on the poster."""
POSTER_FIELDS = ["title", "type", "date", "mode", "city", "eligibility", "requirements", "register_link"]


def extract_poster(image_path, request: gr.Request = None):
    """One Gemma call: read the poster and fill the editable fields."""
    empty = [""] * len(POSTER_FIELDS)
    if not image_path:
        return "Please upload a poster image first.", *empty
    blocked = limit_message(request)
    if blocked:
        return blocked, *empty
    try:
        reply = gemma_client.ask_with_image(image_path, POSTER_PROMPT)
    except Exception as error:
        return "Could not reach Gemma: " + gemma_client.describe_error(error), *empty
    try:
        data = events.parse_json(reply)
    except ValueError as error:
        return f"Gemma's answer was not valid JSON.\n{error}", *empty
    data = {k: str(v or "").strip() for k, v in data.items()}  # every field as plain text
    if not data.get("title"):
        return "Gemma found no title on the poster. Raw output:\n" + json.dumps(data, indent=2), *empty
    return "Check the fields below, fix anything wrong, then press Add to map.", \
        *[data.get(k, "") for k in POSTER_FIELDS]


def save_poster(title, ev_type, when, mode, city, eligibility, requirements, link,
                map_city, radius, chip, sort, fetch_count=None, saved="[]", query="", request: gr.Request = None):
    """Save the (possibly edited) poster event to events.json and redraw."""
    if not title.strip():
        return "The event needs a title.", *show_events(map_city, radius, chip, sort, fetch_count, saved, query)
    blocked = limit_message(request, "poster")
    if blocked:
        return blocked, *show_events(map_city, radius, chip, sort, fetch_count, saved, query)
    title, city = title[:150], city[:100]
    eligibility, requirements = eligibility[:500], requirements[:500]
    ev_mode = "online" if mode.strip().lower() == "online" else "offline"
    ev = {
        "id": events.make_id("poster", title + when),
        "title": title.strip(), "organiser": "", "type": ev_type.strip().lower() or "hackathon",
        "mode": ev_mode, "city": city.strip() if ev_mode == "offline" else "",
        "start": when.strip(), "end": "", "description": "Added from a poster.",
        "eligibility": eligibility, "requirements": requirements,
        "register_url": safe_url(link), "poster_url": "", "source": "poster",
    }
    events.add_coords([ev])
    events.add_event(ev)
    warn = "" if ev["lat"] or ev_mode == "online" else " (city not found, so no map marker)"
    return f"Added: {ev['title']} ({ev_mode}, {ev['city'] or 'no city'}){warn}", \
        *show_events(map_city, radius, chip, sort, fetch_count, saved, query)


# ---------- look ----------

# Runs in the page <head>: brings back the saved day/night choice, and Esc closes the details panel.
HEAD = """<style>
  /* page-level rules live here because Gradio scopes everything in css= to inside the app */
  body, gradio-app { background: var(--ce-bg, #f5f5f8); }
  body { -webkit-font-smoothing: antialiased; margin: 0; }
  .gradio-container { max-width: 1440px !important; min-width: 0 !important; margin: 0 auto !important;
                      padding: 0 clamp(12px, 2.4vw, 28px) 24px !important; overflow-x: hidden; }
  .gradio-container > .main { padding-left: 0 !important; padding-right: 0 !important; }
  footer { display: none !important; }
  @media (max-width: 860px) {
    html, body { overflow-x: hidden; }
    .gradio-container { width: 100% !important; max-width: 100vw !important; padding: 0 12px 20px !important; }
  }
</style>
<script>
(function () {
  function saved() { try { return localStorage.getItem("hf-theme"); } catch (e) { return null; } }
  function apply() { var m = saved(); if (m && document.body) document.body.classList.toggle("dark", m === "dark"); }
  document.addEventListener("DOMContentLoaded", apply);
  var n = 0, t = setInterval(function () { apply(); if (++n > 25) clearInterval(t); }, 120);  // Gradio sets its own mode while booting
  // ---- saved events: ids live in this browser only ----
  var KEY = "hf-saved";
  function savedIds() { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; } }
  // fill the stars again after every redraw of the list
  new MutationObserver(function () {
    var ids = savedIds();
    document.querySelectorAll(".ce-star").forEach(function (s) { s.classList.toggle("on", ids.indexOf(s.dataset.id) >= 0); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  // capture phase: the star wins over the card's own "open details" button
  document.addEventListener("click", function (e) {
    var s = e.target.closest && e.target.closest(".ce-star");
    if (!s) return;
    e.preventDefault(); e.stopPropagation();
    var ids = savedIds(), id = s.dataset.id;
    ids = ids.indexOf(id) >= 0 ? ids.filter(function (x) { return x !== id; }) : ids.concat(id);
    try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch (err) {}
    s.classList.toggle("on", ids.indexOf(id) >= 0);
    var box = document.querySelector("#ce-saved textarea");
    if (box) { box.value = JSON.stringify(ids); box.dispatchEvent(new Event("input", { bubbles: true })); }
  }, true);
  // "Open in WhatsApp": send whatever is in the message box now, so the student's edits are kept
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.ce-wa");
    var box = document.querySelector("#ce-team textarea");
    if (a && box && box.value.trim()) a.href = "https://wa.me/?text=" + encodeURIComponent(box.value.trim());
  }, true);
  // pointing at a card grows its pin on the map (the map lives in its own frame, so we post a message)
  var hovered = null;
  document.addEventListener("mouseover", function (e) {
    var card = e.target.closest && e.target.closest(".ce-card");
    var inner = card && card.querySelector(".ce-card-html");
    var id = inner ? inner.dataset.id : null;
    if (id === hovered) return;
    hovered = id;
    var frame = document.querySelector(".ce-map iframe");
    if (frame && frame.contentWindow) frame.contentWindow.postMessage({ hl: id }, "*");
  });
  // "Closing this week" strip: a tap opens that event, the same way a map pin does
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".ce-dl[data-id]");
    if (!b) return;
    var box = document.querySelector("#ce-pin-id textarea");
    if (box) { box.value = b.dataset.id + "|" + Date.now(); box.dispatchEvent(new Event("input", { bubbles: true })); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var b = document.querySelector("#ce-back"); if (b && b.offsetParent) b.click();
  });
})();
</script>"""

THEME_JS = """() => {
  const night = !document.body.classList.contains('dark');
  document.querySelectorAll('.dark').forEach(el => el.classList.remove('dark'));
  if (night) document.body.classList.add('dark');
  try { localStorage.setItem('hf-theme', night ? 'dark' : 'light'); } catch (e) {}
}"""

CSS = """
/* ---------- colours: day (default) and night (.dark, set by the system or the theme button) ---------- */
:root {
  --ce-bg: #f5f5f8; --ce-card: #ffffff; --ce-inner: #f1f0f6; --ce-border: #e6e5ee; --ce-border-2: #d9d7e4;
  --ce-text: #15141c; --ce-muted: #6e6b80; --ce-accent: #6d5dfc; --ce-accent-2: #a35cff; --ce-accent-ink: #5443e8;
  --ce-accent-bg: #efedff; --ce-on-accent: #ffffff; --ce-hot: #e5484d; --ce-hot-bg: #fdecec;
  --ce-glass: rgba(245, 245, 248, .82); --ce-shadow: 0 1px 2px rgba(20, 16, 50, .04), 0 8px 24px rgba(20, 16, 50, .06);
  --ce-hack: #5443e8; --ce-hack-bg: #efedff;   --ce-work: #0b8a66; --ce-work-bg: #e2f6ee;
  --ce-comp: #b26b00; --ce-comp-bg: #fcf1dc;   --ce-conf: #0a77b3; --ce-conf-bg: #e1f1fb;
  --ce-other: #6e6b80; --ce-other-bg: #f1f0f6;
}
.dark {
  --ce-bg: #0c0b11; --ce-card: #15141c; --ce-inner: #1d1c26; --ce-border: #26252f; --ce-border-2: #34333f;
  --ce-text: #f1f0f6; --ce-muted: #9c99ad; --ce-accent: #7c6dff; --ce-accent-2: #b072ff; --ce-accent-ink: #b3a9ff;
  --ce-accent-bg: #211e3d; --ce-on-accent: #ffffff; --ce-hot: #ff7a7e; --ce-hot-bg: #3a1d21;
  --ce-glass: rgba(12, 11, 17, .78); --ce-shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 10px 30px rgba(0, 0, 0, .35);
  --ce-hack: #b3a9ff; --ce-hack-bg: #221f3d;   --ce-work: #4fd8a6; --ce-work-bg: #10291f;
  --ce-comp: #fbc54a; --ce-comp-bg: #2d2410;   --ce-conf: #5cc6fa; --ce-conf-bg: #0c2533;
  --ce-other: #9c99ad; --ce-other-bg: #1d1c26;
}
/* hand our colours to Gradio's own components too */
:root, .dark {
  --body-background-fill: var(--ce-bg); --background-fill-primary: var(--ce-card);
  --background-fill-secondary: var(--ce-inner); --block-background-fill: var(--ce-card);
  --block-border-color: var(--ce-border); --border-color-primary: var(--ce-border); --border-color-accent: var(--ce-accent);
  --input-background-fill: var(--ce-card); --input-background-fill-focus: var(--ce-card); --input-border-color: var(--ce-border);
  --input-border-color-focus: var(--ce-accent); --input-shadow-focus: 0 0 0 3px var(--ce-accent-bg);
  --body-text-color: var(--ce-text); --body-text-color-subdued: var(--ce-muted);
  --block-label-text-color: var(--ce-muted); --block-title-text-color: var(--ce-muted); --color-accent: var(--ce-accent);
  --color-accent-soft: var(--ce-accent-bg); --checkbox-label-background-fill-selected: var(--ce-text);
  --button-primary-background-fill: var(--ce-accent); --button-primary-background-fill-hover: var(--ce-accent);
  --button-primary-text-color: var(--ce-on-accent); --button-primary-border-color: transparent;
  --button-secondary-background-fill: var(--ce-card); --button-secondary-background-fill-hover: var(--ce-inner);
  --button-secondary-text-color: var(--ce-text); --button-secondary-border-color: var(--ce-border);
  --block-radius: 16px; --container-radius: 16px; --input-radius: 12px; --button-large-radius: 999px; --button-small-radius: 999px;
  --layout-gap: 12px; --block-shadow: none; --shadow-drop: none; --shadow-drop-lg: none;
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
}
#ce-pin-id, #ce-saved, #ce-coords { display: none !important; }
#ce-near { border-radius: 999px !important; height: 42px; white-space: nowrap; }
.ce-star { position: relative; z-index: 3; align-self: flex-start; border: 0; background: none; padding: 2px;
           font-size: 19px; line-height: 1; color: var(--ce-border-2); cursor: pointer; }
.ce-star:hover { color: var(--ce-muted); }
.ce-star.on { color: #f5b000; }
.ce-card.ce-hl { border-color: var(--ce-accent) !important; box-shadow: 0 0 0 3px var(--ce-accent-bg); }
/* closing-this-week strip */
#ce-strip-box { padding: 0 !important; background: transparent !important; border: none !important;
                contain: inline-size; min-width: 0 !important; max-width: 100%; }   /* scroll inside, never widen the page */
.ce-strip { display: flex; gap: 8px; align-items: center; overflow-x: auto; scrollbar-width: none; padding: 2px 0; }
.ce-strip-label { font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
                  color: var(--ce-muted); white-space: nowrap; }
.ce-dl { display: inline-flex; gap: 7px; align-items: center; white-space: nowrap; font-size: 13px; cursor: pointer;
         background: var(--ce-card); border: 1px solid var(--ce-border); border-radius: 999px; padding: 4px 12px 4px 4px;
         color: var(--ce-text); max-width: 280px; overflow: hidden; text-overflow: ellipsis; flex: none; }
.ce-dl b { font: 600 11px ui-monospace, monospace; padding: 2px 7px; border-radius: 999px; background: var(--ce-inner); color: var(--ce-muted); }
.ce-dl.hot b { background: var(--ce-hot-bg); color: var(--ce-hot); }
/* title search above the list */
#ce-q { margin-bottom: 8px; }
#ce-q input, #ce-q textarea { border-radius: 999px !important; height: 40px !important; padding: 9px 16px !important;
                              background: var(--ce-card) !important; border: 1px solid var(--ce-border) !important; resize: none; }
/* calendar + share buttons in the details panel */
#ce-detail-actions { gap: 8px; margin: 4px 0 12px; }
#ce-detail-actions button, #ce-detail-actions a { border-radius: 999px !important; }
* { scrollbar-width: thin; scrollbar-color: var(--ce-border-2) transparent; }
.ce-hidden-text { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }

/* ---------- header ---------- */
/* the header stays on top while the page scrolls, with a frosted background */
#ce-header { position: sticky; top: 0; z-index: 50; background: var(--ce-glass); backdrop-filter: saturate(1.4) blur(14px);
             -webkit-backdrop-filter: saturate(1.4) blur(14px); padding: 12px 0 10px; gap: 10px !important;
             border-bottom: 1px solid var(--ce-border); }
#ce-top { align-items: center; gap: 12px !important; flex-wrap: nowrap !important; }
#ce-top > :first-child { flex: 0 0 auto !important; width: auto !important; min-width: max-content !important; }
#ce-top > button { flex: 0 0 auto !important; }
#ce-top .block, #ce-filters .block, #ce-list-head .block { background: transparent !important; border: none !important;
                                                          padding: 0 !important; box-shadow: none !important; }
#ce-top .form, #ce-filters .form, #ce-list-head .form { background: transparent !important; border: none !important;
                                                        box-shadow: none !important; gap: 10px; overflow: visible; }
.ce-logo { display: flex; align-items: center; gap: 10px; white-space: nowrap; }
.ce-logo-box { width: 36px; height: 36px; flex: none; border-radius: 11px; display: inline-flex; align-items: center;
               justify-content: center; background: linear-gradient(135deg, var(--ce-accent), var(--ce-accent-2));
               box-shadow: 0 6px 16px color-mix(in srgb, var(--ce-accent) 35%, transparent); }
.ce-logo-word { font-size: 19px; font-weight: 600; letter-spacing: -.02em; color: var(--ce-text); }
.ce-logo-word b { font-weight: 600; color: var(--ce-accent-ink); }

/* city search */
#ce-city { flex: 1 1 220px !important; max-width: 340px; min-width: 0 !important; }
#ce-city .input-container { width: 100%; }
#ce-city input, #ce-city textarea { height: 42px !important; line-height: 20px; box-sizing: border-box; resize: none;
                  border-radius: 999px !important; border: 1px solid var(--ce-border) !important; color: var(--ce-text) !important;
                  background: var(--ce-card) !important; padding: 11px 18px 11px 40px !important; font-size: 14px;
                  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%238a87a0' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='M20 20l-3.5-3.5'/%3E%3C/svg%3E") !important;
                  background-repeat: no-repeat !important; background-position: 15px center !important; }
#ce-city input:focus, #ce-city textarea:focus { border-color: var(--ce-accent) !important; box-shadow: 0 0 0 3px var(--ce-accent-bg) !important; }

/* segmented controls: radius and sort */
#ce-radius .wrap, #ce-sort .wrap { display: inline-flex; flex-wrap: nowrap; gap: 2px; padding: 3px; border-radius: 999px;
                                   background: var(--ce-inner); border: 1px solid var(--ce-border); }
#ce-radius label, #ce-sort label { border: none !important; background: transparent !important; box-shadow: none !important;
                                   border-radius: 999px !important; padding: 6px 12px !important; font-size: 13px;
                                   color: var(--ce-muted) !important; white-space: nowrap; min-height: 0; cursor: pointer; }
#ce-radius label.selected, #ce-sort label.selected { background: var(--ce-card) !important; color: var(--ce-text) !important;
                                   font-weight: 600; box-shadow: 0 1px 3px rgba(20, 16, 50, .12) !important; }
#ce-radius { flex: 0 0 auto !important; width: auto !important; overflow: visible !important; }
#ce-radius input[type=radio], #ce-sort input[type=radio], #ce-chips input[type=radio] { display: none; }

/* header buttons */
.ce-spacer { flex: 1 1 0 !important; min-width: 0 !important; }
/* the theme button shows a moon by day and a sun by night */
#ce-theme-btn { width: 42px; min-width: 42px !important; height: 42px; padding: 0 !important; border-radius: 999px !important;
                font-size: 0 !important; color: transparent !important; border: 1px solid var(--ce-border) !important;
                background: var(--ce-card) no-repeat center / 18px url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2315141c' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'/%3E%3C/svg%3E") !important; }
.dark #ce-theme-btn { background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23f1f0f6' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='4.5'/%3E%3Cpath d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'/%3E%3C/svg%3E") !important; }
#ce-poster-btn { height: 42px; padding: 0 18px !important; border-radius: 999px !important; border: none !important;
                 font-weight: 600; white-space: nowrap; color: var(--ce-on-accent) !important;
                 background: linear-gradient(135deg, var(--ce-accent), var(--ce-accent-2)) !important;
                 box-shadow: 0 6px 18px color-mix(in srgb, var(--ce-accent) 30%, transparent); }
#ce-poster-btn:hover { filter: brightness(1.06); }

/* filter chips */
#ce-chips .wrap { display: flex; gap: 8px; flex-wrap: wrap; }
#ce-chips label { border: 1px solid var(--ce-border) !important; background: var(--ce-card) !important; box-shadow: none !important;
                  border-radius: 999px !important; padding: 6px 14px !important; font-size: 13px; color: var(--ce-text) !important;
                  min-height: 0; cursor: pointer; transition: background .15s, border-color .15s; }
#ce-chips label:hover { border-color: var(--ce-border-2) !important; }
#ce-chips label.selected { background: var(--ce-text) !important; color: var(--ce-bg) !important; border-color: var(--ce-text) !important;
                           font-weight: 600; }

/* ---------- body: map + list ---------- */
#ce-body { gap: 18px !important; margin-top: 16px; align-items: flex-start !important; }
#ce-map-col { position: sticky; top: 132px; min-width: 0 !important; }
#ce-map-col .block, #ce-map-col .html-container, #ce-map-col .prose { padding: 0 !important; border: none !important; background: transparent !important; }
.ce-map { position: relative; border-radius: 20px; overflow: hidden; border: 1px solid var(--ce-border); background: var(--ce-inner);
          box-shadow: var(--ce-shadow); }
.ce-map iframe { width: 100%; height: calc(100vh - 160px); min-height: 460px; border: 0; display: block; }
.ce-legend { position: absolute; left: 12px; bottom: 12px; display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center;
             background: var(--ce-glass); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); color: var(--ce-text);
             border: 1px solid var(--ce-border); border-radius: 14px; padding: 7px 12px; font-size: 12px; max-width: calc(100% - 24px); }
.ce-legend span { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.ce-legend i { width: 9px; height: 9px; border-radius: 50%; box-shadow: 0 0 0 2px var(--ce-card); }
.ce-error { color: var(--ce-hot); padding: 24px; background: var(--ce-card); border: 1px solid var(--ce-border); border-radius: 16px; margin: 0; }

#ce-list-col { min-width: 0 !important; gap: 10px !important; }
#ce-list-head { align-items: center; justify-content: space-between; flex-wrap: nowrap !important; gap: 10px; }
#ce-list-head > * { flex: 0 1 auto !important; min-width: 0 !important; width: auto !important; }
#ce-count, #ce-count p { color: var(--ce-muted); font-size: 13px; margin: 0; }
#ce-count strong { color: var(--ce-text); font-weight: 600; }

/* event cards: the whole card is the button */
.ce-card { position: relative; border: 1px solid var(--ce-border) !important; border-radius: 18px !important;
           background: var(--ce-card) !important; padding: 14px !important; gap: 0 !important; box-shadow: none;
           transition: border-color .15s, box-shadow .15s, transform .15s; }
.ce-card:hover { border-color: var(--ce-border-2) !important; box-shadow: var(--ce-shadow); transform: translateY(-1px); }
.ce-card:focus-within { outline: 2px solid var(--ce-accent); outline-offset: 2px; }
.ce-card.selected { border-color: var(--ce-accent) !important; box-shadow: 0 0 0 3px var(--ce-accent-bg); }
.ce-card > .block, .ce-card .html-container, .ce-card .prose { padding: 0 !important; border: none !important; background: transparent !important; }
.ce-card-html { display: flex; gap: 14px; align-items: flex-start; }
button.ce-details-btn { position: absolute !important; inset: 0; width: 100%; height: 100%; max-width: none; margin: 0 !important;
                        opacity: 0; border-radius: 18px !important; cursor: pointer; z-index: 2; }
.ce-thumb { width: 64px; height: 64px; flex: none; border-radius: 14px; overflow: hidden; display: flex; align-items: center;
            justify-content: center; }
.ce-thumb img, .ce-poster img { width: 100%; height: 100%; object-fit: cover; }
.ce-thumb.hack, .ce-poster.hack { background: var(--ce-hack-bg); color: var(--ce-hack); }
.ce-thumb.work, .ce-poster.work { background: var(--ce-work-bg); color: var(--ce-work); }
.ce-thumb.comp, .ce-poster.comp { background: var(--ce-comp-bg); color: var(--ce-comp); }
.ce-thumb.conf, .ce-poster.conf { background: var(--ce-conf-bg); color: var(--ce-conf); }
.ce-thumb.other, .ce-poster.other { background: var(--ce-other-bg); color: var(--ce-other); }
.ce-card-body { flex: 1; min-width: 0; }
.ce-card-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.ce-type { font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
.ce-type.hack { color: var(--ce-hack); } .ce-type.work { color: var(--ce-work); } .ce-type.comp { color: var(--ce-comp); }
.ce-type.conf { color: var(--ce-conf); } .ce-type.other { color: var(--ce-other); }
.ce-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--ce-inner); color: var(--ce-muted); }
.ce-badge.hot { background: var(--ce-hot-bg); color: var(--ce-hot); }
.ce-card-title { font-weight: 600; font-size: 15.5px; line-height: 1.3; letter-spacing: -.01em; color: var(--ce-text); }
.ce-meta { color: var(--ce-muted); font-size: 13px; margin-top: 3px; }
.ce-closes { color: var(--ce-hot); font-weight: 500; }
.ce-summary { color: var(--ce-muted); font-size: 13px; line-height: 1.45; margin-top: 6px; display: -webkit-box;
              -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ce-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 9px; align-items: center; }
.ce-tag { border-radius: 999px; padding: 2px 9px; font-size: 11.5px; font-weight: 500; background: var(--ce-inner); color: var(--ce-muted); }
.ce-tag.free { background: var(--ce-work-bg); color: var(--ce-work); }
.ce-tag.src.poster { background: var(--ce-comp-bg); color: var(--ce-comp); }
.ce-tag.type.hack { background: var(--ce-hack-bg); color: var(--ce-hack); } .ce-tag.type.work { background: var(--ce-work-bg); color: var(--ce-work); }
.ce-tag.type.comp { background: var(--ce-comp-bg); color: var(--ce-comp); } .ce-tag.type.conf { background: var(--ce-conf-bg); color: var(--ce-conf); }
.ce-chev { align-self: center; color: var(--ce-muted); font-size: 22px; line-height: 1; transition: transform .15s; }
.ce-card:hover .ce-chev { transform: translateX(2px); color: var(--ce-accent-ink); }
.ce-empty { text-align: center; padding: 40px 20px; border: 1px dashed var(--ce-border-2); border-radius: 18px; color: var(--ce-muted); }
.ce-empty b { display: block; color: var(--ce-text); font-size: 15px; margin-bottom: 4px; }
#ce-more { border-radius: 999px !important; }
.ce-note { color: var(--ce-muted); font-size: 12px; text-align: center; }

/* ---------- details: a panel that slides in from the right ---------- */
#ce-drawer { position: fixed !important; inset: 0; z-index: 100; padding: 0 !important; margin: 0 !important; gap: 0 !important;
             background: transparent !important; border: none !important; }
#ce-scrim { position: absolute !important; inset: 0; width: 100%; height: 100%; max-width: none; border: none !important;
            font-size: 0 !important; color: transparent !important;
            border-radius: 0 !important; background: rgba(8, 6, 20, .38) !important; backdrop-filter: blur(2px);
            cursor: default; animation: ce-fade .2s ease; }
#ce-panel { position: absolute !important; top: 0; right: 0; bottom: 0; width: min(560px, 100%); overflow-y: auto;
            background: var(--ce-bg) !important; border-left: 1px solid var(--ce-border); padding: 14px 22px 28px !important;
            gap: 14px !important; box-shadow: -20px 0 60px rgba(8, 6, 20, .25); animation: ce-slide .25s cubic-bezier(.2, .8, .2, 1); }
@keyframes ce-slide { from { transform: translateX(40px); opacity: 0; } }
@keyframes ce-fade { from { opacity: 0; } }
#ce-back { align-self: flex-start; width: auto; max-width: none; border-radius: 999px !important; padding: 6px 14px !important;
           background: var(--ce-card) !important; border: 1px solid var(--ce-border) !important; }
#ce-panel > .block { background: transparent !important; border: none !important; padding: 0 !important; }
.ce-details { color: var(--ce-text); }
.ce-poster { aspect-ratio: 16 / 7; border-radius: 18px; display: flex; align-items: center; justify-content: center; overflow: hidden;
             margin-bottom: 14px; }
.ce-poster svg { width: 44px; height: 44px; }
.ce-details h2 { font-size: 26px; line-height: 1.2; letter-spacing: -.02em; font-weight: 650; margin: 10px 0 2px; color: var(--ce-text); }
.ce-org { color: var(--ce-muted); font-size: 14px; }
.ce-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 16px; }
.ce-tile { background: var(--ce-card); border: 1px solid var(--ce-border); border-radius: 14px; padding: 10px 14px; }
.ce-tile-label { color: var(--ce-muted); font-size: 12px; }
.ce-tile-value { font-weight: 600; font-size: 14.5px; color: var(--ce-text); margin-top: 1px; }
.ce-register-card { margin-top: 14px; }
.ce-register { display: block; text-align: center; border-radius: 999px; padding: 12px; font-weight: 600; font-size: 15px;
               color: var(--ce-on-accent) !important; text-decoration: none !important;
               background: linear-gradient(135deg, var(--ce-accent), var(--ce-accent-2));
               box-shadow: 0 8px 22px color-mix(in srgb, var(--ce-accent) 32%, transparent); }
.ce-register:hover { filter: brightness(1.06); }
.ce-caption { color: var(--ce-muted); font-size: 12px; margin-top: 6px; text-align: center; }
.ce-details h4 { font-size: 13px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--ce-muted);
                 margin: 22px 0 6px; }
.ce-details p, .ce-details li { color: var(--ce-text); font-size: 14.5px; line-height: 1.6; }
.ce-details ul { margin: 0; padding-left: 18px; }
.ce-ai { color: var(--ce-accent-ink); }

/* event chat */
#ce-chat { border: 1px solid var(--ce-border) !important; border-radius: 18px !important; padding: 14px !important;
           background: var(--ce-card) !important; gap: 10px !important; }
#ce-chat .ce-chat-head { font-weight: 600; font-size: 14px; }
#ce-chat .ce-chat-head span { color: var(--ce-accent-ink); }
#ce-chat .ce-chat-head small { display: block; color: var(--ce-muted); font-weight: 400; font-size: 12px; margin-top: 2px; }
#ce-chat .block { border: none !important; background: transparent !important; }
#ce-fit { border: 1px solid var(--ce-border) !important; border-radius: 18px !important; padding: 14px !important;
          background: var(--ce-card) !important; gap: 10px !important; margin-bottom: 14px; }
#ce-fit .ce-chat-head { font-weight: 600; font-size: 14px; }
#ce-fit .ce-chat-head span { color: var(--ce-accent-ink); }
#ce-fit .ce-chat-head small { display: block; color: var(--ce-muted); font-weight: 400; font-size: 12px; margin-top: 2px; }
#ce-fit .block { border: none !important; background: transparent !important; }
#ce-fit-btn { border-radius: 999px !important; align-self: flex-start; }
#ce-fit-out:empty { display: none; }
#ce-team-card { border: 1px solid var(--ce-border) !important; border-radius: 18px !important; padding: 14px !important;
                background: var(--ce-card) !important; gap: 10px !important; margin-bottom: 14px; }
#ce-team-card .ce-chat-head { font-weight: 600; font-size: 14px; }
#ce-team-card .ce-chat-head span { color: var(--ce-accent-ink); }
#ce-team-card .ce-chat-head small { display: block; color: var(--ce-muted); font-weight: 400; font-size: 12px; margin-top: 2px; }
#ce-team-card .block { border: none !important; background: transparent !important; }
#ce-team-btn { border-radius: 999px !important; align-self: flex-start; }
.ce-wa { display: inline-block; background: #25d366; color: #fff !important; text-decoration: none !important;
         font-weight: 600; font-size: 13px; border-radius: 999px; padding: 8px 16px; }
.ce-wa:hover { filter: brightness(1.05); }
#ce-cmp-card { border: 1px solid var(--ce-border) !important; border-radius: 18px !important; padding: 14px !important;
               background: var(--ce-card) !important; gap: 10px !important; margin-bottom: 14px; }
#ce-cmp-card .ce-chat-head { font-weight: 600; font-size: 14px; }
#ce-cmp-card .ce-chat-head span { color: var(--ce-accent-ink); }
#ce-cmp-card .ce-chat-head small { display: block; color: var(--ce-muted); font-weight: 400; font-size: 12px; margin-top: 2px; }
#ce-cmp-card .block { border: none !important; background: transparent !important; }
#ce-cmp-btn { border-radius: 999px !important; align-self: flex-end; }
#ce-cmp-out:empty { display: none; }
#ce-cmp-out table { font-size: 13px; width: 100%; }
#ce-cmp-out th, #ce-cmp-out td { word-break: normal; overflow-wrap: break-word; }
#ce-cmp-out td:first-child { white-space: nowrap; font-weight: 600; }
#ce-chat-q textarea, #ce-chat-q input { border-radius: 14px !important; }

/* poster panel */
#ce-poster-panel { border: 1px solid var(--ce-border) !important; border-radius: 20px !important; padding: 18px !important;
                   background: var(--ce-card) !important; margin-top: 14px; box-shadow: var(--ce-shadow); }
#ce-poster-panel .ce-panel-title { font-size: 17px; font-weight: 600; letter-spacing: -.01em; }
#ce-poster-panel .ce-panel-title small { display: block; font-size: 13px; font-weight: 400; color: var(--ce-muted); margin-top: 2px; }
#ce-poster-panel .form, #ce-poster-panel .block { border: none !important; background: transparent !important; box-shadow: none !important; }
#ce-poster-panel textarea, #ce-poster-panel input[type=text] { background: var(--ce-inner) !important; border: 1px solid var(--ce-border) !important;
                  border-radius: 12px !important; padding: 9px 12px !important; color: var(--ce-text) !important; }
#ce-poster-panel textarea:focus, #ce-poster-panel input[type=text]:focus { border-color: var(--ce-accent) !important; }
#ce-poster-panel .ce-field-row { gap: 10px !important; }
#ce-poster-panel .image-container, #ce-poster-panel [data-testid=image] { border-radius: 14px; background: var(--ce-inner); }
#ce-poster-panel .ce-close { border-radius: 999px !important; }

/* live data (local only) */
#ce-data { margin-top: 18px; border: 1px solid var(--ce-border) !important; border-radius: 16px !important; background: var(--ce-card) !important; }
#ce-data .form { background: transparent !important; border: none !important; }
#ce-refresh-btn { border-radius: 999px !important; white-space: nowrap; }

/* loading: dim the area being redrawn */
#ce-body .pending, #ce-body .generating { opacity: .55; transition: opacity .2s; }

/* ---------- phone ---------- */
/* laptops and tablets: the radius buttons need ~480px, so they move to their own line */
@media (min-width: 861px) and (max-width: 1180px) {
  #ce-top { flex-wrap: wrap !important; row-gap: 10px !important; }
  #ce-city { flex: 1 1 220px !important; max-width: none; min-width: 200px !important; }
  #ce-radius { order: 5; flex: 1 1 100% !important; }
}
@media (max-width: 860px) {
  /* never wider than the screen: Gradio writes min-widths inline, so release them here */
  [style*="min-width"], .block, .form { min-width: 0 !important; }
  #ce-top > * { flex-basis: auto !important; max-width: 100% !important; }
  #ce-list-head { flex-wrap: wrap !important; }
  #ce-header { padding-top: 10px; top: 0; }
  #ce-top { flex-wrap: wrap !important; row-gap: 10px !important; }
  #ce-top > :first-child { order: 0; }
  .ce-spacer { display: none !important; }
  #ce-theme-btn { order: 1; margin-left: auto; }
  #ce-poster-btn { order: 2; width: 42px; min-width: 42px !important; padding: 0 !important; font-size: 0 !important; }
  #ce-poster-btn::before { content: "+"; font-size: 22px; font-weight: 500; }
  #ce-near { order: 1; width: 42px; min-width: 42px !important; padding: 0 !important; font-size: 0 !important; }   /* icon only */
  #ce-near::before { content: "◎"; font-size: 20px; }
  #ce-city { order: 3; flex: 1 1 100% !important; max-width: none; }
  #ce-radius { order: 4; flex: 1 1 100% !important; width: 100% !important; overflow: hidden !important; }
  #ce-radius .wrap { display: flex; overflow-x: auto; width: 100%; max-width: 100%; scrollbar-width: none; }
  #ce-radius, #ce-chips { max-width: calc(100vw - 24px) !important; }
  #ce-radius label { flex: 1 0 auto; text-align: center; justify-content: center; padding: 8px 10px !important; }
  #ce-chips .wrap { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; }
  #ce-chips label { flex: none !important; padding: 8px 14px !important; }
  #ce-body { flex-direction: column !important; margin-top: 12px; }
  #ce-body > * { width: 100% !important; flex: none !important; }
  #ce-map-col { position: static; }
  .ce-map iframe { height: 42vh; min-height: 280px; }
  .ce-legend { font-size: 11px; gap: 3px 9px; padding: 6px 10px; left: auto; right: 10px; top: 10px; bottom: auto; max-width: 62%; }
  #ce-list-col { border-top: 1px solid var(--ce-border); padding-top: 18px; position: relative; }
  #ce-list-col::before { content: ""; position: absolute; top: 7px; left: 50%; transform: translateX(-50%); width: 40px; height: 4px;
                         border-radius: 4px; background: var(--ce-border-2); }
  .ce-summary { display: none; }
  .ce-thumb { width: 52px; height: 52px; border-radius: 12px; }
  .ce-card { padding: 12px !important; }
  .ce-card-title { font-size: 15px; }
  #ce-panel { width: 100%; border-left: none; padding: 10px 16px 0 !important; }
  .ce-details h2 { font-size: 22px; }
  .ce-register-card { position: sticky; bottom: 0; z-index: 5; margin: 14px -16px 0; padding: 12px 16px;
                      background: var(--ce-glass); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
                      border-top: 1px solid var(--ce-border); }
  #ce-poster-panel .row { flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
"""


# ---------- layout ----------

# The first map and list are built once at startup, so the page never opens empty.
FIRST_MAP, FIRST_FOUND, FIRST_COUNT, _, FIRST_STRIP = show_events(DEFAULT_CITY, DEFAULT_RADIUS, "All", "Nearest")

with gr.Blocks(title=TITLE) as demo:
    found_state = gr.State(FIRST_FOUND)   # the events currently shown
    selected_state = gr.State("")         # id of the last opened event
    limit_state = gr.State(PAGE)          # how many cards the list shows
    profile = gr.BrowserState("", storage_key="hf-profile")   # "About you", remembered in this browser

    with gr.Column():
        # ----- main view -----
        with gr.Column() as main_view:
            with gr.Column(elem_id="ce-header"):
                with gr.Row(elem_id="ce-top"):
                    gr.HTML(LOGO_HTML, scale=0, min_width=150)
                    city = gr.Textbox(DEFAULT_CITY, show_label=False, placeholder="Search a city", scale=2,
                                      elem_id="ce-city", min_width=160, container=False)
                    near_btn = gr.Button("◎ Near me", elem_id="ce-near", scale=0, min_width=110)
                    radius = gr.Radio(RADIUS_CHOICES, value=DEFAULT_RADIUS, show_label=False, scale=0,
                                      elem_id="ce-radius", container=False)
                    gr.HTML("", elem_classes="ce-spacer", scale=1, min_width=0)
                    theme_btn = gr.Button("Switch day or night", elem_id="ce-theme-btn", scale=0, min_width=42)
                    poster_btn = gr.Button("+ Add from poster", elem_id="ce-poster-btn", scale=0, min_width=150)
                chip = gr.Radio(CHIPS, value="All", show_label=False, elem_id="ce-chips", container=False)
                strip_box = gr.HTML(FIRST_STRIP, elem_id="ce-strip-box")

            with gr.Column(visible=False, elem_id="ce-poster-panel") as poster_panel:
                with gr.Row(equal_height=False):
                    gr.HTML('<div class="ce-panel-title">Add an event from a poster'
                            '<small>Gemma reads the poster. Check what it found, then add it to the map.</small></div>')
                    close_btn = gr.Button("✕ Close", size="sm", scale=0, min_width=90, elem_classes="ce-close")
                with gr.Row(equal_height=False):
                    with gr.Column(scale=4, min_width=240):
                        poster_img = gr.Image(type="filepath", label="Poster", height=300)
                        extract_btn = gr.Button("✦ Extract with Gemma", variant="primary")
                        poster_status = gr.Markdown("Upload a poster, then press Extract.")
                    with gr.Column(scale=6, min_width=260):
                        poster_boxes = []
                        for pair in (POSTER_FIELDS[i:i + 2] for i in range(0, len(POSTER_FIELDS), 2)):
                            with gr.Row(elem_classes="ce-field-row"):   # two editable boxes per row
                                poster_boxes += [gr.Textbox(label=name.replace("_", " ").capitalize(), min_width=160)
                                                 for name in pair]
                        add_btn = gr.Button("Add to map", variant="primary")

            with gr.Row(elem_id="ce-body", equal_height=False):
                with gr.Column(scale=7, elem_id="ce-map-col", min_width=0):
                    map_box = gr.HTML(FIRST_MAP)
                with gr.Column(scale=5, elem_id="ce-list-col", min_width=320):
                    q = gr.Textbox(placeholder="Search titles, e.g. AI or IIT", show_label=False,
                                   container=False, elem_id="ce-q", max_length=80)
                    with gr.Row(elem_id="ce-list-head"):
                        count = gr.Markdown(FIRST_COUNT, elem_id="ce-count")
                        sort = gr.Radio(["Nearest", "Closing soon"], value="Nearest", show_label=False,
                                        elem_id="ce-sort", container=False, scale=0)

                    @gr.render(inputs=[found_state, selected_state, limit_state])
                    def render_cards(found, selected, limit):
                        if not found:
                            gr.HTML('<div class="ce-empty"><b>No events match</b>Try a bigger radius or another filter.</div>')
                        for ev in found[:limit]:
                            classes = ["ce-card", "selected"] if ev["id"] == selected else ["ce-card"]
                            with gr.Column(elem_classes=classes):
                                gr.HTML(f'<div class="ce-card-html" data-id="{esc(ev["id"])}">{card_html(ev)}</div>')
                                details_btn = gr.Button(f"Open {ev['title'][:60]}", elem_classes="ce-details-btn", size="sm")
                                details_btn.click(make_opener(ev["id"]), found_state, opens)
                        if len(found) > limit:
                            more_btn = gr.Button(f"Show {min(PAGE, len(found) - limit)} more", elem_id="ce-more")
                            more_btn.click(lambda n: n + PAGE, limit_state, limit_state)
                            gr.HTML(f'<div class="ce-note">Showing {limit} of {len(found)} events</div>')

            with gr.Accordion("Live data · fetch new events", open=False, visible=not PUBLIC, elem_id="ce-data"):
                with gr.Row():
                    fetch_count = gr.Dropdown(FETCH_CHOICES, value=DEFAULT_FETCH_COUNT, show_label=False,
                                              scale=2, min_width=160, elem_id="ce-fetch-count")
                    refresh_btn = gr.Button("↻ Fetch now", elem_id="ce-refresh-btn", scale=1, min_width=120)
                gr.Markdown("Fetching from Unstop and Devpost takes a while. Nothing starts until you press the button.")

        # ----- details: a panel over the page -----
        with gr.Column(visible=False, elem_id="ce-drawer") as details_view:
            scrim = gr.Button("Close", elem_id="ce-scrim")
            with gr.Column(elem_id="ce-panel"):
                back_btn = gr.Button("← Back to events", elem_id="ce-back", size="sm")
                details_box = gr.HTML()
                with gr.Row(elem_id="ce-detail-actions"):
                    cal_btn = gr.DownloadButton("📅 Add to calendar", visible=False, elem_id="ce-cal", size="sm")
                    share_btn = gr.Button("🔗 Copy link", elem_id="ce-share", size="sm")
                with gr.Column(elem_id="ce-fit"):
                    gr.HTML('<div class="ce-chat-head"><span>✦</span> Is this for me?'
                            '<small>Gemma compares this event with what you write about yourself.</small></div>')
                    me_box = gr.Textbox(label="About you", placeholder="1st year B.Tech CSE · Python, ESP32, a bit of ML",
                                        max_length=300, elem_id="ce-me")
                    fit_btn = gr.Button("✦ Is this for me?", variant="primary", size="sm", elem_id="ce-fit-btn")
                    fit_out = gr.Markdown(elem_id="ce-fit-out")
                with gr.Column(elem_id="ce-team-card"):
                    gr.HTML('<div class="ce-chat-head"><span>✉</span> Find teammates'
                            '<small>Gemma drafts a short invite you can edit, copy or send on WhatsApp.</small></div>')
                    team_btn = gr.Button("✉ Draft a team message", size="sm", elem_id="ce-team-btn")
                    team_text = gr.Textbox(label="Message", lines=4, buttons=["copy"], elem_id="ce-team",
                                           interactive=True, max_length=1000)   # editable before sending
                    team_link = gr.HTML(elem_id="ce-team-link")
                with gr.Column(elem_id="ce-cmp-card"):
                    gr.HTML('<div class="ce-chat-head"><span>⇄</span> Compare with another event'
                            '<small>Gemma puts the two side by side and says which to pick.</small></div>')
                    with gr.Row(equal_height=True):
                        other = gr.Dropdown(label="Compare with", choices=[], scale=3, elem_id="ce-other")
                        cmp_btn = gr.Button("✦ Compare", scale=1, size="sm", elem_id="ce-cmp-btn", min_width=110)
                    cmp_out = gr.Markdown(elem_id="ce-cmp-out")
                with gr.Column(elem_id="ce-chat"):
                    gr.HTML('<div class="ce-chat-head"><span>✦</span> Ask about this event'
                            '<small>Answers use only this event’s details.</small></div>')
                    chat_box = gr.Chatbot(height=260, show_label=False)
                    chat_q = gr.Textbox(placeholder="e.g. Can first-year students join? Is there a fee?",
                                        show_label=False, submit_btn=True, elem_id="ce-chat-q", max_length=500)

        pin_box = gr.Textbox(elem_id="ce-pin-id", show_label=False, container=False)  # hidden, filled by map clicks
        saved_box = gr.Textbox("[]", elem_id="ce-saved", show_label=False, container=False)  # hidden, filled by the browser
        coords = gr.Textbox(elem_id="ce-coords", show_label=False, container=False)  # hidden, "lat,lon,time" from the browser

        # ----- wiring -----
        list_inputs = [city, radius, chip, sort, fetch_count, saved_box, q]
        list_outputs = [map_box, found_state, count, limit_state, strip_box]
        opens = [main_view, details_view, details_box, selected_state, cal_btn]   # everything that opens details
        city.submit(show_events, list_inputs, list_outputs)
        radius.change(show_events, list_inputs, list_outputs)
        chip.change(show_events, list_inputs, list_outputs)
        sort.change(show_events, list_inputs, list_outputs)
        refresh_btn.click(refresh, list_inputs, list_outputs)   # the count dropdown no longer starts a fetch by itself
        back_btn.click(back_to_events, None, [main_view, details_view])
        scrim.click(back_to_events, None, [main_view, details_view])
        chat_q.submit(ask_about_event, [chat_q, chat_box, selected_state], [chat_q, chat_box])
        selected_state.change(lambda: [], None, chat_box)  # new event, fresh chat
        fit_btn.click(fit_check, [me_box, selected_state], [fit_out, profile])
        demo.load(lambda p: p, profile, me_box)                 # fill "About you" from last time
        selected_state.change(lambda: "", None, fit_out)        # new event, fresh verdict
        team_btn.click(team_message, selected_state, [team_text, team_link])
        selected_state.change(lambda: ("", ""), None, [team_text, team_link])   # new event, fresh message
        selected_state.change(compare_choices, [found_state, selected_state], other)
        selected_state.change(lambda: "", None, cmp_out)                          # new event, fresh comparison
        cmp_btn.click(compare_events, [selected_state, other, me_box], cmp_out)
        # The day/night switch runs only in the browser and is remembered (see HEAD).
        theme_btn.click(None, js=THEME_JS)
        # a star click only needs a redraw when the Saved chip is showing
        saved_box.change(lambda *a: show_events(*a) if a[2] == "Saved" else (gr.skip(),) * len(list_outputs),
                         list_inputs, list_outputs)
        demo.load(None, None, saved_box,
                  js="() => { try { return localStorage.getItem('hf-saved') || '[]'; } catch (e) { return '[]'; } }")
        pin_box.change(open_from_pin, [pin_box, found_state], opens)
        q.change(show_events, list_inputs, list_outputs, trigger_mode="always_last", show_progress="hidden")
        share_btn.click(None, None, None, js="""() => {
          const open = document.querySelector("#ce-drawer .ce-details[data-id]");
          if (!open) return;
          const id = open.dataset.id;
          const url = location.origin + location.pathname + "?event=" + encodeURIComponent(id);
          const done = () => { const b = document.querySelector("#ce-share"); if (b) { const t = b.innerText;
            b.innerText = "✓ Link copied"; setTimeout(() => b.innerText = t, 1500); } };
          if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(url).then(done, () => prompt("Copy this link", url));
          else prompt("Copy this link", url);
        }""")
        demo.load(open_from_link, found_state, opens)
        near_btn.click(None, None, coords, js="""() => new Promise(done => {
          if (!navigator.geolocation) return done("");
          navigator.geolocation.getCurrentPosition(
            p => done(p.coords.latitude + "," + p.coords.longitude + "," + Date.now()),
            () => done(""), { timeout: 8000 });
        })""")
        coords.change(nearest_city, coords, city).then(show_events, list_inputs, list_outputs)
        poster_btn.click(lambda: gr.update(visible=True), None, poster_panel)
        close_btn.click(lambda: gr.update(visible=False), None, poster_panel)
        extract_btn.click(extract_poster, poster_img, [poster_status] + poster_boxes)
        add_btn.click(save_poster, poster_boxes + list_inputs, [poster_status] + list_outputs)


def lan_address():
    """This computer's address on the Wi-Fi, e.g. 10.50.33.81 (no data is actually sent)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


if __name__ == "__main__":
    # python app_template/app.py --lan   lets phones and laptops on the same Wi-Fi open the app.
    lan = "--lan" in sys.argv
    print("Starting... open this address in your browser: http://127.0.0.1:7860")
    if lan:
        print(f"On the same Wi-Fi, open: http://{lan_address()}:7860")
    # share=True would put your app on the public internet. Do not.
    if PUBLIC:
        events.start_auto_refresh()
    demo.queue(max_size=40)      # extra visitors wait in line instead of overloading the app
    demo.launch(share=False, server_name="0.0.0.0" if lan else None,
                server_port=int(os.environ.get("GRADIO_SERVER_PORT", 7860)),
                css=CSS, head=HEAD, theme=gr.themes.Base(), max_file_size="8mb", ssr_mode=False)
