"""Lets the tools import app/db.py when run as `python tools/<tool>.py`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import db  # noqa: E402,F401
