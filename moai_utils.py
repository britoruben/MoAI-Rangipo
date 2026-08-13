# -*- coding: utf-8 -*-
"""
MoAI — moai_utils.py
Small helpers shared by build.py and server.py: tolerant JSON reads and
atomic writes.

Python 3 standard library only.
"""

import json
import os


def read_json_dict(path, default=None):
    """Parsed JSON object at `path`, or `default` if the file is missing,
    unreadable, malformed, or its root isn't an object. Never raises — a
    config/data file broken by hand must not take the galaxy down."""
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError
        return default
    return data if isinstance(data, dict) else default


def read_json_items(path, key):
    """List stored under `key` in the JSON object at `path` (connectors.json,
    tools.json); empty list on anything unexpected."""
    items = (read_json_dict(path, {}) or {}).get(key, [])
    return items if isinstance(items, list) else []


def write_text_atomic(path, text):
    """Write `text` via a temp file + os.replace, so no reader ever sees a
    half-written file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path, data):
    """Pretty-print `data` as JSON, atomically."""
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
