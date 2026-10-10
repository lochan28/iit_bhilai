---
name: hackfindr
description: Find upcoming student hackathons, ideathons, webinars, workshops, competitions and conferences near a city, filtered by distance, and show their details and register links.
license: MIT
---
# HackFindr agent skill

Tools (in `harness/tools.py`):
- `search_events(city, radius_km=100, mode="any", event_type="any")`: offline events near the city, sorted by distance, plus online events.
- `get_event_details(event_id)`: the full record of one event.

Steps:
1. Call `search_events` with the user's city and radius. Use `mode="offline"` if they ask for events "near" or "within" a distance.
2. Call `get_event_details` for any event the user wants to know more about.
3. Answer with title, city, distance, dates and the register link. Never make up events.
