"""Make the plugin importable without starting MaiBot."""

from pathlib import Path

import sys

PLUGINS_DIR = str(Path(__file__).resolve().parents[2])
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)
