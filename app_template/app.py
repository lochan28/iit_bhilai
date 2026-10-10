"""CampusEvents app. Run it from the repo folder:  python app_template/app.py

Three tabs: the Events page (map + list + details + poster upload),
ask Gemma 4 about a picture, and an agent that can use the event tools.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so we can import gemma_client

import html
import json
import re
from datetime import date

import folium
import gradio as gr

import events
import gemma_client
from harness import Agent
from harness.tools import filter_events

SYSTEM_RULE = ("You help students find events. Use search_events, then get_event_details if needed. "
               "List titles, cities, distances, dates and register links. Keep answers short.")

TITLE = "CampusEvents"
DEFAULT_CITY = "Raipur"
DEFAULT_RADIUS = 300  # km shown when the app opens
RADIUS_CHOICES = [(f"Within {km} km", km) for km in (50, 100, 300, 500, 1000, 2000)]
CHIPS = ["All", "Hackathons", "Workshops", "Free", "This week", "Open to 1st year"]
MAX_CARDS = 30        # cards shown in the list (more makes the page slow)
PURPLE, GREEN = "#5b4fd6", "#1fa37a"
SOURCE_NAMES = {"unstop": "Unstop", "devpost": "Devpost", "poster": "Poster", "sample": "Sample"}


# ---------- Other tabs (unchanged) ----------

def ask_about_picture(image_path, question):
    """Works with a picture, or with text only."""
    if not question.strip():
        return "Please type a question first."
    try:
        if image_path:
            return gemma_client.ask_with_image(image_path, question, system=SYSTEM_RULE)
        return gemma_client.ask(question, system=SYSTEM_RULE)
    except Exception as error:
        return gemma_client.describe_error(error)


def ask_agent(question):
    """The agent may use tools. We show each step, then the answer."""
    if not question.strip():
        return "", "Please type a question first."
    agent = Agent(system=SYSTEM_RULE)
    try:
        answer = agent.run(question)
    except Exception as error:
        return "", gemma_client.describe_error(error)
    steps = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(agent.steps[:-1], 1))
    return steps or "(No tools were used.)", answer


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


def chip_match(ev, chip):
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


def find_events(city, radius, chip, sort):
    """Offline events near the city first, then online ones, filtered by the chip."""
    lat, lon, near, online = filter_events(city, int(radius))
    found = [e for e in near + online if chip_match(e, chip)]
    if sort == "Closing soon":
        found.sort(key=lambda e: days_left(e) if days_left(e) is not None else 9999)
    return lat, lon, found


# ---------- HTML pieces ----------

ICON_IMG = ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#8b80f0" stroke-width="1.8">'
            '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/>'
            '<path d="M21 15l-5-5L5 21"/></svg>')
ICON_PIN = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2">'
            '<path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z"/><circle cx="12" cy="9" r="2.5"/></svg>')
LOGO_HTML = f'<div class="ce-logo"><span class="ce-logo-box">{ICON_PIN}</span><span>{TITLE}</span></div>'


def esc(value):
    return html.escape(str(value or ""))


def pin_color(ev):
    return GREEN if ev.get("type") in ("workshop", "webinar") else PURPLE


def meta_line(ev):
    """City · km · date · closes in N days (each part only if we know it)."""
    parts = [ev.get("city") or "Online"]
    if ev.get("distance_km") is not None:
        parts.append(f"{ev['distance_km']} km")
    if event_date(ev):
        parts.append(nice_date(event_date(ev)))
    left = days_left(ev)
    if left is not None and left >= 0:
        parts.append("closes today" if left == 0 else f"closes in {left} day{'s' if left != 1 else ''}")
    return " · ".join(esc(p) for p in parts)


def tags_html(ev, with_type=False):
    tags = [SOURCE_NAMES.get(ev.get("source"), ev.get("source", ""))]
    if with_type and ev.get("type"):
        tags.append(ev["type"].capitalize())
    if ev.get("is_free") is True:
        tags.append("Free")
    return "".join(f'<span class="ce-tag">{esc(t)}</span>' for t in tags if t)


def thumb_html(ev, cls="ce-thumb"):
    if ev.get("poster_url"):
        return f'<div class="{cls}"><img src="{esc(ev["poster_url"])}" alt=""></div>'
    return f'<div class="{cls}">{ICON_IMG}</div>'


def card_html(ev):
    summary = ev.get("description")
    summary_html = f'<div class="ce-summary"><span>✦</span> {esc(summary)}</div>' if summary else ""
    return (f'{thumb_html(ev)}<div class="ce-card-body"><div class="ce-card-title">{esc(ev["title"])}</div>'
            f'<div class="ce-meta">{meta_line(ev)}</div>{summary_html}'
            f'<div class="ce-tags">{tags_html(ev)}</div></div>')


def bullets(text):
    """Split 'a; b, c' style text into a bullet list."""
    items = [p.strip(" .") for p in re.split(r"[;\n•]|, (?=[A-Z])", str(text or "")) if p.strip(" .")]
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>" if items else ""


def details_html(ev):
    """The details page. Tiles and sections without data are left out."""
    tiles = [("Date", nice_date(event_date(ev))), ("Location", ev.get("city") or "Online"),
             ("Registration closes", nice_date(ev.get("end")))]
    tiles_html = "".join(f'<div class="ce-tile"><div class="ce-tile-label">{k}</div>'
                         f'<div class="ce-tile-value">{esc(v)}</div></div>' for k, v in tiles if v)
    sections = ""
    if ev.get("description"):
        sections += f'<h4>Description</h4><p>{esc(ev["description"])}</p>'
    for label in ("eligibility", "requirements"):
        if ev.get(label):
            sections += f"<h4>{label.capitalize()}</h4>{bullets(ev[label])}"
    source = SOURCE_NAMES.get(ev.get("source"), "the website")
    register = ""
    if ev.get("register_url"):
        register = (f'<div class="ce-register-card"><a class="ce-register" href="{esc(ev["register_url"])}" '
                    f'target="_blank" rel="noopener">Register on {esc(source)} ↗</a>'
                    f'<div class="ce-caption">Opens the official registration page</div></div>')
    return (f'<div class="ce-details"><div class="ce-details-left">{thumb_html(ev, "ce-poster")}{register}</div>'
            f'<div class="ce-details-right"><div class="ce-tags">{tags_html(ev, True)}</div>'
            f'<h2>{esc(ev["title"])}</h2><div class="ce-tiles">{tiles_html}</div>{sections}</div></div>')


def map_html(lat, lon, radius, found):
    """Dark folium map with a dashed radius circle and coloured pins, inside an iframe we size ourselves."""
    m = folium.Map(location=[lat, lon], tiles="OpenStreetMap", zoom_control=True)
    # Free OpenStreetMap tiles, turned dark with a colour filter (dark tile services now need a key).
    m.get_root().header.add_child(folium.Element(
        "<style>.leaflet-tile-pane{filter:grayscale(1) invert(1) brightness(.75) contrast(1.1)}"
        ".leaflet-container{background:#141414;font-family:system-ui,sans-serif}"
        # tiny overlap hides the thin lines between map tiles
        ".leaflet-tile-container img{width:256.6px!important;height:256.6px!important}"
        # hover card: dark, white title, wraps long names
        ".leaflet-tooltip{background:#1c1c1c!important;color:#a0a0a0!important;border:1px solid #3a3a3a!important;border-radius:12px!important;"
        "padding:10px 12px!important;white-space:normal!important;width:max-content!important;max-width:290px!important;font-size:12px!important;"
        "box-shadow:0 6px 20px rgba(0,0,0,.5)!important}"
        ".leaflet-tooltip b{color:#fff!important;font-size:14px!important;font-weight:600!important;display:block!important;margin-bottom:3px!important}"
        ".ce-tip{display:flex;gap:10px;align-items:flex-start}"
        ".ce-tip img{width:44px;height:44px;flex:none;border-radius:8px;object-fit:cover;background:#262640}"
        ".leaflet-tooltip-top:before{border-top-color:#3a3a3a!important}</style>"))
    folium.Circle([lat, lon], radius=radius * 1000, color="#7b70e8", weight=2, dash_array="6 6",
                  fill=True, fill_color=PURPLE, fill_opacity=0.08).add_to(m)
    folium.CircleMarker([lat, lon], radius=6, color=PURPLE, fill=True, fill_opacity=1,
                        tooltip="You are here").add_to(m)
    pins = []  # (folium's JavaScript name for the pin, event id)
    for ev in found:
        if ev.get("lat") is None:
            continue
        logo = f'<img src="{esc(ev["poster_url"])}" alt="">' if ev.get("poster_url") else ""
        tip = f'<div class="ce-tip">{logo}<div><b>{esc(ev["title"])}</b>{meta_line(ev)}</div></div>'
        pin = folium.CircleMarker([ev["lat"], ev["lon"]], radius=8, color="white", weight=1, fill=True,
                                  fill_color=pin_color(ev), fill_opacity=1,
                                  tooltip=folium.Tooltip(tip, direction="top", offset=(0, -6))).add_to(m)
        pins.append((pin.get_name(), ev["id"]))
    d = radius / 111  # about 111 km per degree, so the whole circle fits on screen
    m.fit_bounds([[lat - d, lon - d], [lat + d, lon + d]])
    # Clicking a pin writes its event id into the hidden #ce-pin-id box on the page, which opens the details.
    # The time is added so clicking the same pin twice still counts as a change.
    clicks = "".join(f"{name}.on('click', () => pick({json.dumps(event_id)}));" for name, event_id in pins)
    m.get_root().html.add_child(folium.Element(
        "<script>window.addEventListener('load', () => {"
        "function pick(id) { const box = parent.document.querySelector('#ce-pin-id textarea');"
        " box.value = id + '|' + Date.now(); box.dispatchEvent(new Event('input', {bubbles: true})); }"
        + clicks + "});</script>"
        "<style>.leaflet-interactive{cursor:pointer}</style>"))
    page = html.escape(m.get_root().render(), quote=True)
    legend = (f'<div class="ce-legend"><span style="background:{PURPLE}"></span>Hackathon'
              f'<span style="background:{GREEN}"></span>Workshop</div>')
    return f'<div class="ce-map"><iframe srcdoc="{page}"></iframe>{legend}</div>'


# ---------- what the buttons do ----------

def show_events(city, radius, chip, sort):
    """Redraw the map and the card list."""
    lat, lon, found = find_events(city, radius, chip, sort)
    if lat is None:
        return f"<p class='ce-error'>Could not find the city '{esc(city)}'.</p>", [], "0 events nearby"
    near = sum(1 for e in found if e.get("distance_km") is not None)
    header = f"{near} events nearby · {len(found) - near} online"
    return map_html(lat, lon, int(radius), found), found, header


def refresh(city, radius, chip, sort):
    events.refresh_events()  # live fetch; falls back to the saved events.json
    return show_events(city, radius, chip, sort)


def open_details(event_id, found):
    """Show the details view for one event."""
    ev = next((e for e in found if e["id"] == event_id), None)
    ev = ev or next((e for e in events.load_events() if e["id"] == event_id), None)
    if ev is None:
        return gr.update(), gr.update(), "<p class='ce-error'>Event not found.</p>", event_id
    return gr.update(visible=False), gr.update(visible=True), details_html(ev), event_id


def open_from_pin(value, found):
    """A map pin was clicked: the hidden box holds 'event_id|time'."""
    if not value:
        return gr.update(), gr.update(), gr.update(), gr.update()
    return open_details(value.split("|")[0], found)


def back_to_events():
    return gr.update(visible=True), gr.update(visible=False)


# ---------- poster upload: step 1 extract, step 2 add ----------

POSTER_PROMPT = """Read this event poster. Return ONLY a JSON object with these keys:
"title", "type" (one of hackathon, ideathon, webinar, workshop, competition, conference), "date" (YYYY-MM-DD if possible),
"mode" (online or offline), "city", "eligibility", "requirements", "register_link".
Use "" for anything not on the poster."""
POSTER_FIELDS = ["title", "type", "date", "mode", "city", "eligibility", "requirements", "register_link"]


def extract_poster(image_path):
    """One Gemma call: read the poster and fill the editable fields."""
    empty = [""] * len(POSTER_FIELDS)
    if not image_path:
        return "Please upload a poster image first.", *empty
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
                map_city, radius, chip, sort):
    """Save the (possibly edited) poster event to events.json and redraw."""
    if not title.strip():
        return "The event needs a title.", *show_events(map_city, radius, chip, sort)
    ev_mode = "online" if mode.strip().lower() == "online" else "offline"
    ev = {
        "id": events.make_id("poster", title + when),
        "title": title.strip(), "organiser": "", "type": ev_type.strip().lower() or "hackathon",
        "mode": ev_mode, "city": city.strip() if ev_mode == "offline" else "",
        "start": when.strip(), "end": "", "description": "Added from a poster.",
        "eligibility": eligibility, "requirements": requirements,
        "register_url": link.strip(), "poster_url": "", "source": "poster",
    }
    events.add_coords([ev])
    events.add_event(ev)
    warn = "" if ev["lat"] or ev_mode == "online" else " (city not found, so no map marker)"
    return f"Added: {ev['title']} ({ev_mode}, {ev['city'] or 'no city'}){warn}", \
        *show_events(map_city, radius, chip, sort)


# ---------- look ----------

CSS = """
:root, .dark {
  --body-background-fill: #141414; --background-fill-primary: #141414; --background-fill-secondary: #1c1c1c;
  --block-background-fill: #1c1c1c; --block-border-color: #2a2a2a; --border-color-primary: #2a2a2a;
  --input-background-fill: #1c1c1c; --input-border-color: #2a2a2a;
  --body-text-color: #f2f2f2; --body-text-color-subdued: #a0a0a0; --block-label-text-color: #a0a0a0;
  --color-accent: #5b4fd6; --button-primary-background-fill: #5b4fd6; --button-primary-background-fill-hover: #6b60e0;
  --button-secondary-background-fill: #1c1c1c; --button-secondary-text-color: #f2f2f2;
  --button-secondary-border-color: #2a2a2a; --block-radius: 14px; --layout-gap: 12px;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
body, gradio-app, .gradio-container { background: #141414 !important; color: #f2f2f2; }
.gradio-container { max-width: 1280px !important; margin: 0 auto !important; padding: 8px 16px !important; }
.gradio-container > .main, .gradio-container .wrap.contain { margin: 0 auto !important; width: 100% !important; }
footer { display: none !important; }
#ce-pin-id { display: none !important; }

/* top bar */
#ce-top { align-items: center; gap: 12px; flex-wrap: nowrap; }
.ce-logo { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 18px; white-space: nowrap; }
.ce-logo-box { background: #5b4fd6; width: 34px; height: 34px; border-radius: 9px;
               display: inline-flex; align-items: center; justify-content: center; }
#ce-top .block { border: none !important; background: transparent !important; padding: 0 !important; }
#ce-top input, #ce-top .wrap { border-radius: 12px !important; }
#ce-poster-btn, #ce-refresh-btn { border-radius: 12px; white-space: nowrap; }

/* chips */
#ce-chips, #ce-chips *, #ce-sort, #ce-list-head, #ce-list-head .block {
  background-color: transparent; border-color: transparent; box-shadow: none !important; }
#ce-chips { padding: 0 !important; }
#ce-chips .wrap { gap: 8px; flex-wrap: wrap; }
#ce-chips label { border: 1px solid #3a3a3a !important; border-radius: 999px !important; flex: none !important;
                  height: auto !important; padding: 6px 14px !important; }
#ce-sort .wrap { border: 1px solid #2a2a2a !important; border-radius: 10px !important; }
#ce-chips label.selected { background: #2b2860 !important; border-color: #7b70e8 !important; }
#ce-chips input[type=radio] { display: none; }

/* map */
.ce-map { position: relative; border-radius: 14px; overflow: hidden; border: 1px solid #2a2a2a; }
.ce-map iframe { width: 100%; height: 620px; border: 0; display: block; }
.ce-legend { position: absolute; left: 12px; bottom: 12px; background: #1c1c1c; border: 1px solid #2a2a2a;
             border-radius: 10px; padding: 6px 10px; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.ce-legend span { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-left: 6px; }
.ce-error { color: #ff7b7b; }

/* list */
#ce-list-col { max-height: 680px; overflow-y: auto; flex-direction: column !important; flex-wrap: nowrap !important; }
#ce-list-col > * { width: 100% !important; flex: none !important; }
* { scrollbar-width: thin; scrollbar-color: #3a3a3a transparent; }
#ce-chips .wrap { scrollbar-width: none; }
#ce-list-head { align-items: center; }
#ce-count { color: #a0a0a0; }
#ce-sort { max-width: 170px; }
.ce-card { border: 1px solid #2a2a2a !important; border-radius: 14px !important; background: #1c1c1c !important;
           padding: 14px !important; margin-bottom: 10px; gap: 8px !important; }
.ce-card.selected { border-color: #7b70e8 !important; }
.ce-card > .html-container, .ce-card .prose { padding: 0 !important; }
.ce-card .ce-row { display: flex; gap: 14px; }
.ce-thumb { width: 70px; height: 70px; flex: none; background: #262640; border-radius: 10px;
            display: flex; align-items: center; justify-content: center; overflow: hidden; }
.ce-thumb img, .ce-poster img { width: 100%; height: 100%; object-fit: cover; }
.ce-card-html { display: flex; gap: 14px; }
.ce-card-body { flex: 1; min-width: 0; }
.ce-card-title { font-weight: 600; font-size: 16px; margin-bottom: 2px; }
.ce-meta { color: #a0a0a0; font-size: 13px; margin-bottom: 8px; }
.ce-summary { background: #262626; border-radius: 10px; padding: 8px 10px; font-size: 13px; margin-bottom: 8px; }
.ce-summary span { color: #8b80f0; }
.ce-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.ce-tag { border: 1px solid #3a3a3a; border-radius: 8px; padding: 2px 8px; font-size: 12px; color: #d0d0d0; }
.ce-details-btn { align-self: flex-end; max-width: 130px; border-radius: 12px !important;
                  border: 1px solid #3a3a3a !important; padding: 8px 16px !important; font-weight: 600; }
.ce-details-btn:hover { background: #262626 !important; }
/* Gradio wraps inputs in a grey "form" box; remove it around the chips and the sort menu */
.form:has(#ce-chips), .form:has(#ce-sort), .form:has(#ce-count) {
  background: transparent !important; border: none !important; box-shadow: none !important; }

/* details page */
#ce-back { max-width: 180px; border-radius: 999px; }
.ce-details { display: grid; grid-template-columns: 38% 1fr; gap: 24px; }
.ce-poster { aspect-ratio: 1; background: #262640; border-radius: 14px; display: flex;
             align-items: center; justify-content: center; overflow: hidden; }
.ce-poster svg { width: 40px; height: 40px; }
.ce-register-card { border: 1px solid #2a2a2a; border-radius: 14px; padding: 16px; margin-top: 14px; text-align: center; }
.ce-register { display: block; border: 1px solid #3a3a3a; border-radius: 10px; padding: 10px;
               color: #f2f2f2 !important; text-decoration: none !important; font-weight: 600; }
.ce-register:hover { background: #262626; }
.ce-caption { color: #a0a0a0; font-size: 13px; margin-top: 8px; }
.ce-details-right h2 { font-size: 28px; font-weight: 600; margin: 10px 0 14px; }
.ce-details-right h4 { font-weight: 600; margin: 16px 0 6px; }
.ce-details-right p, .ce-details-right li { color: #d0d0d0; }
.ce-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.ce-tile { background: #262626; border-radius: 10px; padding: 10px 14px; }
.ce-tile-label { color: #a0a0a0; font-size: 13px; }
.ce-tile-value { font-weight: 600; }

/* poster panel */
#ce-poster-panel { border: 1px solid #2a2a2a; border-radius: 14px; padding: 16px; background: #1c1c1c; }

/* phone */
@media (max-width: 768px) {
  #ce-top { flex-wrap: wrap; }
  #ce-top > :first-child { min-width: 40px !important; flex: 0 0 40px !important; }
  #ce-top > :nth-child(2) { flex: 1 1 calc(100% - 52px) !important; min-width: 0 !important; }
  .ce-logo span:last-child { display: none; }
  #ce-chips .wrap { flex-wrap: nowrap; overflow-x: auto; }
  #ce-body { flex-direction: column; }
  .ce-map iframe { height: 40vh; }
  #ce-list-col { max-height: none; border-radius: 18px 18px 0 0; border-top: 1px solid #2a2a2a;
                 padding-top: 14px; position: relative; }
  #ce-list-col::before { content: ""; position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
                         width: 40px; height: 4px; border-radius: 4px; background: #3a3a3a; }
  .ce-summary { display: none; }
  .ce-details { display: flex; flex-direction: column; gap: 16px; }
  .ce-details-left { display: contents; }          /* lets the Register card move to the end */
  .ce-poster { order: 0; aspect-ratio: 16 / 10; }
  .ce-details-right { order: 1; }
  .ce-register-card { order: 2; position: sticky; bottom: 0; background: #141414; z-index: 5; margin: 0; }
}
"""


# ---------- layout ----------

# The first map and list are built once at startup, so the page never opens empty.
FIRST_MAP, FIRST_FOUND, FIRST_COUNT = show_events(DEFAULT_CITY, DEFAULT_RADIUS, "All", "Nearest")

with gr.Blocks(title=TITLE) as demo:
    found_state = gr.State(FIRST_FOUND)   # the events currently shown
    selected_state = gr.State("")    # id of the last opened event

    with gr.Tab("Events"):
        # ----- main view -----
        with gr.Column() as main_view:
            with gr.Row(elem_id="ce-top"):
                gr.HTML(LOGO_HTML, scale=0, min_width=210)
                city = gr.Textbox(DEFAULT_CITY, show_label=False, placeholder="🔍 City", scale=3)
                radius = gr.Dropdown(RADIUS_CHOICES, value=DEFAULT_RADIUS, show_label=False, scale=1, min_width=150)
                refresh_btn = gr.Button("↻", elem_id="ce-refresh-btn", scale=0, min_width=48)
                poster_btn = gr.Button("📷 Add from poster", elem_id="ce-poster-btn", scale=0, min_width=170)
            chip = gr.Radio(CHIPS, value="All", show_label=False, elem_id="ce-chips")

            with gr.Column(visible=False, elem_id="ce-poster-panel") as poster_panel:
                with gr.Row():
                    gr.Markdown("### Add an event from a poster")
                    close_btn = gr.Button("✕ Close", size="sm", scale=0, min_width=90)
                with gr.Row():
                    poster_img = gr.Image(type="filepath", label="Poster", height=320)
                    with gr.Column():
                        extract_btn = gr.Button("Extract with Gemma", variant="primary")
                        poster_status = gr.Markdown("Upload a poster, then press Extract.")
                        poster_boxes = [gr.Textbox(label=name.replace("_", " ").capitalize())
                                        for name in POSTER_FIELDS]
                        add_btn = gr.Button("Add to map", variant="primary")

            with gr.Row(elem_id="ce-body", equal_height=False):
                with gr.Column(scale=58):
                    map_box = gr.HTML(FIRST_MAP)
                with gr.Column(scale=42, elem_id="ce-list-col"):
                    with gr.Row(elem_id="ce-list-head"):
                        count = gr.Markdown(FIRST_COUNT, elem_id="ce-count")
                        sort = gr.Dropdown(["Nearest", "Closing soon"], value="Nearest",
                                           show_label=False, elem_id="ce-sort", min_width=150)

                    @gr.render(inputs=[found_state, selected_state])
                    def render_cards(found, selected):
                        if not found:
                            gr.Markdown("No events match. Try a bigger radius or another chip.")
                        for ev in found[:MAX_CARDS]:
                            classes = ["ce-card", "selected"] if ev["id"] == selected else ["ce-card"]
                            with gr.Column(elem_classes=classes):
                                gr.HTML(f'<div class="ce-card-html">{card_html(ev)}</div>')
                                details_btn = gr.Button("Details →", elem_classes="ce-details-btn", size="sm")
                                details_btn.click(lambda f, i=ev["id"]: open_details(i, f), found_state,
                                                  [main_view, details_view, details_box, selected_state])
                        if len(found) > MAX_CARDS:
                            gr.Markdown(f"Showing the first {MAX_CARDS} of {len(found)} events.")

        # ----- details view -----
        with gr.Column(visible=False) as details_view:
            back_btn = gr.Button("← Back to events", elem_id="ce-back", size="sm")
            details_box = gr.HTML()

        pin_box = gr.Textbox(elem_id="ce-pin-id", show_label=False, container=False)  # hidden, filled by map clicks

        # ----- wiring -----
        list_inputs = [city, radius, chip, sort]
        list_outputs = [map_box, found_state, count]
        for trigger in (city.submit, radius.change, chip.change, sort.change):
            trigger(show_events, list_inputs, list_outputs)
        refresh_btn.click(refresh, list_inputs, list_outputs)
        back_btn.click(back_to_events, None, [main_view, details_view])
        pin_box.change(open_from_pin, [pin_box, found_state],
                      [main_view, details_view, details_box, selected_state])
        poster_btn.click(lambda: gr.update(visible=True), None, poster_panel)
        close_btn.click(lambda: gr.update(visible=False), None, poster_panel)
        extract_btn.click(extract_poster, poster_img, [poster_status] + poster_boxes)
        add_btn.click(save_poster, poster_boxes + list_inputs, [poster_status] + list_outputs)

    with gr.Tab("Ask about a picture"):
        picture = gr.Image(type="filepath", label="Picture (optional)")
        question = gr.Textbox(label="Your question", value="What can you tell me about this?")
        submit = gr.Button("Submit")
        answer = gr.Textbox(label="Answer", lines=6)
        submit.click(ask_about_picture, [picture, question], answer)

    with gr.Tab("Agent with tools"):
        agent_question = gr.Textbox(label="Your question", value="hackathons within 200 km of Raipur")
        agent_submit = gr.Button("Ask the agent")
        agent_steps = gr.Textbox(label="Tool steps", lines=6)
        agent_answer = gr.Textbox(label="Final answer", lines=4)
        agent_submit.click(ask_agent, agent_question, [agent_steps, agent_answer])

if __name__ == "__main__":
    print("Starting... open this address in your browser: http://127.0.0.1:7860")
    # share=True would put your app on the public internet. Do not.
    demo.launch(share=False, css=CSS, theme=gr.themes.Base())
