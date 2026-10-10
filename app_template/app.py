"""A one-screen app you can copy and change. Run it from the repo folder:  python app_template/app.py

Two tabs: ask Gemma 4 about a picture, and an agent that can use small tools.
Look for the lines marked CHANGE THIS.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so we can import gemma_client

import html
import json

import folium
import gradio as gr

import events
import gemma_client
from harness import Agent
from harness.tools import filter_events

# CHANGE THIS: the rule that tells Gemma how to behave in your app.
SYSTEM_RULE = ("You help students find events. Use search_events, then get_event_details if needed. "
               "List titles, cities, distances, dates and register links. Keep answers short.")

# CHANGE THIS: the title shown at the top of the page.
TITLE = "CampusEvents (Gemma 4)"
DEFAULT_CITY = "Raipur"
DEFAULT_RADIUS = 300  # km shown when the app opens


def ask_about_picture(image_path, question):
    """Tab 1: works with a picture, or with text only."""
    if not question.strip():
        return "Please type a question first."
    try:
        if image_path:
            return gemma_client.ask_with_image(image_path, question, system=SYSTEM_RULE)
        return gemma_client.ask(question, system=SYSTEM_RULE)
    except Exception as error:
        return gemma_client.describe_error(error)


def ask_agent(question):
    """Tab 2: the agent may use tools. We show each step, then the answer."""
    if not question.strip():
        return "", "Please type a question first."
    agent = Agent(system=SYSTEM_RULE)
    try:
        answer = agent.run(question)
    except Exception as error:
        return "", gemma_client.describe_error(error)
    steps = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(agent.steps[:-1], 1))
    return steps or "(No tools were used.)", answer


# ---------- Events Map tab ----------

def popup_html(ev):
    """The box shown when you click a marker. html.escape stops odd text from breaking the page."""
    e = {k: html.escape(str(ev.get(k) or "")) for k in ev}
    img = f'<img src="{e["poster_url"]}" width="200"><br>' if ev.get("poster_url") else ""
    return (f'{img}<b>{e["title"]}</b><br>{e["city"]} ({ev.get("distance_km", "?")} km)<br>'
            f'{e["start"][:10]} to {e["end"][:10]}<br><i>{e["description"]}</i><br>'
            f'<b>Eligibility:</b> {e["eligibility"]}<br><b>Requirements:</b> {e["requirements"]}<br>'
            f'<a href="{e["register_url"]}" target="_blank">Register</a>')


def build_map(city, radius, mode):
    """Draw the map of offline events near the city, plus a table of online events."""
    lat, lon, near, online = filter_events(city, radius, mode)
    if lat is None:
        return f"<p style='color:red'>Could not find the city '{html.escape(city)}'.</p>", []
    m = folium.Map(location=[lat, lon], zoom_start=6)
    folium.Marker([lat, lon], tooltip=city, icon=folium.Icon(color="red", icon="home")).add_to(m)
    folium.Circle([lat, lon], radius=radius * 1000, fill=False).add_to(m)
    d = radius / 111  # about 111 km per degree, so the whole circle fits on screen
    m.fit_bounds([[lat - d, lon - d], [lat + d, lon + d]])
    for ev in near:
        folium.Marker([ev["lat"], ev["lon"]], tooltip=ev["title"],
                      popup=folium.Popup(popup_html(ev), max_width=260)).add_to(m)
    rows = [[e["title"], e["type"], e["start"][:10], e["end"][:10], e["register_url"]] for e in online]
    header = f"<p><b>{len(near)}</b> offline events within {radius} km, <b>{len(online)}</b> online.</p>"
    return header + m._repr_html_(), rows


def refresh(city, radius, mode):
    events.refresh_events()  # live fetch; falls back to the saved events.json
    return build_map(city, radius, mode)


POSTER_PROMPT = """Read this event poster. Return ONLY a JSON object with these keys:
"title", "type" (one of hackathon, ideathon, webinar, workshop), "date" (YYYY-MM-DD if possible),
"mode" (online or offline), "city", "eligibility", "requirements", "register_link".
Use "" for anything not on the poster."""


def add_poster(image_path, city, radius, mode):
    """One Gemma call: read the poster, check the JSON, save the event, redraw the map."""
    if not image_path:
        return "Please upload a poster image first.", *build_map(city, radius, mode)
    try:
        reply = gemma_client.ask_with_image(image_path, POSTER_PROMPT)
    except Exception as error:
        return "Could not reach Gemma: " + gemma_client.describe_error(error), *build_map(city, radius, mode)
    try:
        data = events.parse_json(reply)
    except ValueError as error:
        return f"Gemma's answer was not valid JSON.\n{error}", *build_map(city, radius, mode)
    data = {k: str(v or "").strip() for k, v in data.items()}  # every field as plain text
    if not data.get("title"):
        return "Gemma found no title on the poster. Raw output:\n" + json.dumps(data, indent=2), \
            *build_map(city, radius, mode)
    ev_mode = "online" if data.get("mode", "").lower() == "online" else "offline"
    ev = {
        "id": events.make_id("poster", data["title"] + data.get("date", "")),
        "title": data["title"], "organiser": "", "type": data.get("type") or "hackathon",
        "mode": ev_mode, "city": data.get("city", "") if ev_mode == "offline" else "",
        "start": data.get("date", ""), "end": "", "description": "Added from a poster.",
        "eligibility": data.get("eligibility", ""), "requirements": data.get("requirements", ""),
        "register_url": data.get("register_link", ""), "poster_url": "", "source": "poster",
    }
    events.add_coords([ev])
    events.add_event(ev)
    warn = "" if ev["lat"] or ev_mode == "online" else " (city not found, so no map marker)"
    return f"Added: {ev['title']} ({ev_mode}, {ev['city'] or 'no city'}){warn}\n{json.dumps(data, indent=2)}", \
        *build_map(city, radius, mode)


# CHANGE THIS: the layout. Add boxes, move things, rename the tabs.
with gr.Blocks(title=TITLE) as demo:
    gr.Markdown(f"# {TITLE}")

    with gr.Tab("Events Map"):
        with gr.Row():
            city = gr.Textbox(DEFAULT_CITY, label="City")
            radius = gr.Slider(10, 2000, DEFAULT_RADIUS, step=10, label="Radius (km)")
            mode = gr.Dropdown(["any", "offline", "online"], value="any", label="Mode")
            refresh_btn = gr.Button("Refresh", variant="primary")
        map_html = gr.HTML()
        online_table = gr.Dataframe(headers=["Online event", "Type", "Start", "End", "Register"],
                                    label="Online events")
        gr.Markdown("### Add an event from a poster")
        with gr.Row():
            poster = gr.Image(type="filepath", label="Poster")
            poster_status = gr.Textbox(label="Result", lines=8)
        poster_btn = gr.Button("Read poster with Gemma and add")

        map_inputs = [city, radius, mode]
        refresh_btn.click(refresh, map_inputs, [map_html, online_table])
        city.submit(build_map, map_inputs, [map_html, online_table])
        radius.release(build_map, map_inputs, [map_html, online_table])
        mode.change(build_map, map_inputs, [map_html, online_table])
        poster_btn.click(add_poster, [poster] + map_inputs, [poster_status, map_html, online_table])
        demo.load(build_map, map_inputs, [map_html, online_table])

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
    demo.launch(share=False)  # share=True would put your app on the public internet. Do not.
