"""Starts HackFindr. Hosting services such as Hugging Face Spaces run this file."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "app_template" / "app.py"), run_name="__main__")
