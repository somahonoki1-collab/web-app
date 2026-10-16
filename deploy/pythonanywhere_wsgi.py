"""Paste this file into the WSGI configuration linked from PythonAnywhere's Web tab."""
import sys
from pathlib import Path

project = str(Path.home() / "web-app")
if project not in sys.path:
    sys.path.insert(0, project)

from wsgi import application  # noqa: E402, F401
