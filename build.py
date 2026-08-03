# -*- coding: utf-8 -*-
"""
MoAI — build.py
Walks docs/ (skipping docs/en/ and docs/es/ which are project documentation),
connectors.json and tools.json, and writes viewer/graph-data.js with:
const GRAPH = {nodes: [...], links: [...]}

Node types by location under docs/:
  docs/ideas/    -> idea
  docs/projects/ -> project
  everything else -> note

Python 3 standard library only.

Critical rule: each node's id is numeric and equal to its position in the
nodes array — future phases look up nodes by index.
"""

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(ROOT, "docs")
DOCS_IDEAS_DIR = os.path.join(DOCS_DIR, "ideas")
DOCS_PROJECTS_DIR = os.path.join(DOCS_DIR, "projects")
DOCS_SKIP = {"en", "es"}  # project docs — not user notes
CONNECTORS_FILE = os.path.join(ROOT, "connectors.json")
TOOLS_FILE = os.path.join(ROOT, "tools.json")
OUT_FILE = os.path.join(ROOT, "viewer", "graph-data.js")
HUB_PATH = "docs/projects/example-project-moai-galaxy.md"

EXTRACT_LEN = 700
MIN_ALIAS_LEN = 4  # shorter aliases generate junk links

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def clean_markdown(text):
    """Markdown -> approximate plain text for the excerpt."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = WIKILINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [text](url)
    text = re.sub(r"[*_`>#]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_md(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def md_label(raw, fallback_stem):
    # excluding code blocks: a "# something" inside ``` is not a heading
    no_code = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
    m = HEADING_RE.search(no_code)
    if m:
        return m.group(1).strip()
    return fallback_stem.replace("-", " ").capitalize()


def collect_md_nodes():
    """Returns a list of dicts with the raw info for each .md file."""
    entries = []

    if not os.path.isdir(DOCS_DIR):
        return entries

    abs_ideas = os.path.abspath(DOCS_IDEAS_DIR)
    abs_projects = os.path.abspath(DOCS_PROJECTS_DIR)

    for dirpath, dirnames, filenames in os.walk(DOCS_DIR):
        # at the docs/ root, prune en/ and es/ so os.walk never descends into them
        if os.path.abspath(dirpath) == os.path.abspath(DOCS_DIR):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in DOCS_SKIP)
        else:
            dirnames.sort()

        abs_dp = os.path.abspath(dirpath)
        is_idea = abs_dp == abs_ideas or abs_dp.startswith(abs_ideas + os.sep)
        is_project = abs_dp == abs_projects or abs_dp.startswith(abs_projects + os.sep)

        for fn in sorted(filenames):
            if not fn.lower().endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            stem = os.path.splitext(fn)[0]
            raw = read_md(path)
            if is_idea:
                node_type = "idea"
            elif is_project:
                node_type = "project"
            else:
                node_type = "note"
            entries.append({
                "label": md_label(raw, stem),
                "type": node_type,
                "stem": stem,
                "path": rel,
                "raw": raw,
            })

    return entries


def collect_json_nodes(path, key, node_type):
    """Empty list (no exception) on missing file, invalid JSON, or an
    unexpected root/list shape — a connectors.json/tools.json broken by
    hand shouldn't take down build() (and with it, every /remember)."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get(key, [])
    if not isinstance(items, list):
        return []

    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        desc = item.get("description", "")
        status = item.get("status", "")
        raw = " ".join(filter(None, [item.get("label", ""), desc, status]))
        entries.append({
            "label": item.get("label", item.get("id", "?")),
            "type": node_type,
            "stem": item.get("id", ""),
            # unique path per entry (the viewer uses it as a stable key)
            "path": "%s#%s" % (os.path.basename(path), item.get("id", "?")),
            "raw": raw,
            "status": status,
        })
    return entries


def build():
    entries = collect_md_nodes()
    entries += collect_json_nodes(CONNECTORS_FILE, "connectors", "connector")
    entries += collect_json_nodes(TOOLS_FILE, "tools", "tool")

    # --- nodes (id == index in the array) ---
    nodes = []
    for i, e in enumerate(entries):
        excerpt = clean_markdown(e["raw"])
        if len(excerpt) > EXTRACT_LEN:
            excerpt = excerpt[:EXTRACT_LEN].rsplit(" ", 1)[0] + "…"
        node = {
            "id": i,
            "label": e["label"],
            "type": e["type"],
            "group": e["type"],
            "excerpt": excerpt,
            "path": e["path"],
        }
        if e.get("status"):
            node["status"] = e["status"]
        nodes.append(node)

    # --- aliases for each node for mention detection ---
    alias_map = []  # index -> set of lowercase aliases
    for e in entries:
        aliases = set()
        for a in (e["label"], e["stem"]):
            a = (a or "").strip().lower()
            if len(a) >= MIN_ALIAS_LEN:
                aliases.add(a)
        alias_map.append(aliases)

    texts = [e["raw"].lower() for e in entries]
    wikilinks = [
        {w.strip().lower() for w in WIKILINK_RE.findall(e["raw"])}
        for e in entries
    ]

    # per-alias pattern with word boundaries: "idea" shouldn't match inside "ideal"
    alias_patterns = [
        {a: re.compile(r"(?<!\w)" + re.escape(a) + r"(?!\w)") for a in aliases}
        for aliases in alias_map
    ]

    # --- links ---
    seen = set()
    links = []

    def add_link(a, b):
        if a == b:
            return
        key = (min(a, b), max(a, b))
        if key in seen:
            return
        seen.add(key)
        links.append({"source": key[0], "target": key[1]})

    n = len(entries)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            # 1) wikilink from i to j (by stem or label)
            if wikilinks[i] & alias_map[j]:
                add_link(i, j)
                continue
            # 2) i mentions j's title / id in its text (whole word)
            for alias, pattern in alias_patterns[j].items():
                if pattern.search(texts[i]):
                    add_link(i, j)
                    break

    # Every capability belongs to the central MoAI Galaxy project. This is a
    # structural relationship, not a text-mention heuristic: tools and
    # connectors remain discoverable even when their descriptions change.
    hub_id = next((i for i, e in enumerate(entries) if e.get("path") == HUB_PATH), None)
    if hub_id is not None:
        for i, e in enumerate(entries):
            if e.get("type") in {"tool", "connector"}:
                add_link(i, hub_id)

    # --- degree (for star size in the viewer) ---
    degree = [0] * n
    for l in links:
        degree[l["source"]] += 1
        degree[l["target"]] += 1
    for node in nodes:
        node["val"] = 1 + degree[node["id"]]

    # mtime lets the viewer detect if its copy of the galaxy went stale
    # (another tab, a manual build.py run) even if the ids still fall
    # within range
    graph = {"nodes": nodes, "links": links, "mtime": time.time()}

    # atomic write: nobody ever reads a half-written graph-data.js
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("// Generated by build.py — do not edit by hand.\n")
        f.write("const GRAPH = ")
        f.write(json.dumps(graph, ensure_ascii=False, indent=2))
        f.write(";\n")
    os.replace(tmp, OUT_FILE)

    print("MoAI build: %d nodes, %d links -> %s" % (
        len(nodes), len(links), os.path.relpath(OUT_FILE, ROOT)))
    by_type = {}
    for node in nodes:
        by_type[node["type"]] = by_type.get(node["type"], 0) + 1
    for node_type, count in sorted(by_type.items()):
        print("  %-12s %d" % (node_type, count))


if __name__ == "__main__":
    sys.exit(build())
