"""The tools the model is allowed to ask for.

A tool is a plain Python function. Put @tool above it and the model can use it.
The tool's description comes from its docstring. Its inputs come from its type hints.
Type hints can be str, int, float or bool.

SAFETY: there is no shell tool, no network tool, and no file access outside notes/.
"""

import ast
import inspect
import operator
import typing
from datetime import datetime
from pathlib import Path

TOOLS = {}  # tool name -> {"func": the function, "declaration": what we tell the model}

_TYPE_NAMES = {str: "string", int: "integer", float: "number", bool: "boolean"}
NOTES_FILE = Path(__file__).resolve().parent.parent / "notes" / "notes.txt"


def tool(func):
    """Register a function as a tool the model can ask for."""
    hints = typing.get_type_hints(func)
    docstring = inspect.cleandoc(func.__doc__ or "")
    properties, required = {}, []
    for name, param in inspect.signature(func).parameters.items():
        py_type = hints.get(name, str)
        if py_type not in _TYPE_NAMES:
            raise TypeError(f"Tool '{func.__name__}': use str, int, float or bool for '{name}'.")
        # A docstring line like "expression: the sum to work out" describes that input.
        lines = [l.strip() for l in docstring.splitlines() if l.strip().startswith(name + ":")]
        description = lines[0].split(":", 1)[1].strip() if lines else name
        properties[name] = {"type": _TYPE_NAMES[py_type], "description": description}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    declaration = {"name": func.__name__, "description": docstring.split("\n\n")[0].replace("\n", " ")}
    if properties:
        declaration["parameters"] = {"type": "object", "properties": properties, "required": required}
    TOOLS[func.__name__] = {"func": func, "declaration": declaration}
    return func


def get_declarations():
    return [t["declaration"] for t in TOOLS.values()]


def _convert(py_type, value):
    """The model sends numbers and text loosely. Turn each value into the type we expect."""
    if py_type is bool and isinstance(value, str):
        return value.strip().lower() == "true"
    return py_type(value)


def run_tool(name, args):
    """Run a tool by name. Always returns text. Problems come back as text starting with 'Error'."""
    if name not in TOOLS:
        return f"Error: there is no tool called '{name}'."
    func = TOOLS[name]["func"]
    hints = typing.get_type_hints(func)
    try:
        clean = {key: _convert(hints.get(key, str), value) for key, value in (args or {}).items()}
        return str(func(**clean))
    except Exception as error:
        return f"Error: {error}"


# ---------- calculator: safe maths, never eval() ----------

_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
}


def _calc(node):
    """Work out a maths expression. Allows numbers, + - * / ** % and brackets. Nothing else."""
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _calc(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("That power is too big.")
        result = _OPERATORS[type(node.op)](left, right)
        if isinstance(result, int) and result.bit_length() > 4000:
            raise ValueError("That number is too big.")
        return result
    raise ValueError("Only numbers and + - * / ** % ( ) are allowed.")


@tool
def calculator(expression: str):
    """Work out a maths sum such as (12 + 8) * 3. Allows numbers, + - * / ** % and brackets.

    expression: the sum to work out, for example 2 * (3 + 4)
    """
    if len(expression) > 200:
        raise ValueError("That sum is too long.")
    return _calc(ast.parse(expression.strip(), mode="eval"))


@tool
def get_time():
    """Get the current date and time on this computer."""
    return datetime.now().strftime("%A %d %B %Y, %H:%M")


@tool
def save_note(text: str):
    """Save a short note. The note is added to notes/notes.txt.

    text: the note to save
    """
    NOTES_FILE.parent.mkdir(exist_ok=True)
    with NOTES_FILE.open("a", encoding="utf-8") as file:
        file.write(text[:500].replace("\n", " ") + "\n")
    return "Saved the note."


# ---------- CampusEvents: search the saved events.json ----------
# These tools only READ events.json. Looking up a city name may use the geocoder (cached in geocache.json).

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so we can import events.py
import events  # noqa: E402


def filter_events(city, radius_km=100, mode="any", event_type="any"):
    """Shared filter for the map tab and the agent.
    Returns (city_lat, city_lon, offline events sorted by distance, online events)."""
    lat, lon = events.geocode(city)
    near, online = [], []
    for ev in events.load_events():
        if event_type != "any" and ev.get("type") != event_type:
            continue
        if ev["mode"] == "online":
            if mode in ("any", "online"):
                online.append(ev)
        elif mode in ("any", "offline") and lat is not None and ev.get("lat") is not None:
            distance = events.haversine_km(lat, lon, ev["lat"], ev["lon"])
            if distance <= radius_km:
                near.append(dict(ev, distance_km=round(distance)))
    near.sort(key=lambda e: e["distance_km"])
    return lat, lon, near, online


@tool
def search_events(city: str, radius_km: int = 100, mode: str = "any", event_type: str = "any"):
    """Find upcoming student events (hackathons, ideathons, webinars, workshops) near a city. Returns id, title, city, distance, dates and register link.

    city: the city name, for example Raipur
    radius_km: only offline events within this many km of the city are returned
    mode: online, offline or any
    event_type: hackathon, ideathon, webinar, workshop or any
    """
    lat, _, near, online = filter_events(city, radius_km, mode.lower(), event_type.lower())
    if lat is None:
        raise ValueError(f"Could not find the city '{city}'.")
    keys = ("id", "title", "mode", "city", "distance_km", "start", "end", "register_url")
    found = [{k: e.get(k) for k in keys} for e in (near + online)[:15]]
    return found or "No events found. Try a bigger radius_km."


@tool
def get_event_details(event_id: str):
    """Get the full record of one event (description, eligibility, requirements, links) by the id from search_events.

    event_id: the id returned by search_events
    """
    for ev in events.load_events():
        if ev["id"] == event_id:
            return ev
    raise ValueError(f"No event with id {event_id}")
