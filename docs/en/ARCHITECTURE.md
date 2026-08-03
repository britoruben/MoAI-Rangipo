# MoAI — Technical Architecture

*Reference document written so that any session or model can continue the project without prior context. This file explains HOW what's already built actually works.*

---

## 1. Overview

```mermaid
graph TD
    subgraph Sources["Data sources (plain text, hand-editable)"]
        DOCS["docs/*.md<br/>(+ docs/ideas/, docs/projects/, docs/captures/)"]
        CONN["connectors.json"]
        TOOLS["tools.json"]
    end

    BUILD["build.py"]
    GRAPHDATA["viewer/graph-data.js<br/>const GRAPH = {nodes, links, mtime}"]
    VIEWER["viewer/index.html<br/>3D galaxy + chat + voice"]
    SERVER["server.py — port 4700"]
    CONFIG["config.json<br/>api_key + model list"]
    PREFS["preferences.json<br/>lang + addressed name"]
    ANTHROPIC["Anthropic API<br/>Messages + web_search"]

    DOCS --> BUILD
    CONN --> BUILD
    TOOLS --> BUILD
    BUILD -->|"atomic write (os.replace)"| GRAPHDATA
    GRAPHDATA -->|"loaded on page start"| VIEWER
    VIEWER <-->|"REST API — /chat /remember /preferences /connectors /tools ... (full list in server.py's module docstring)"| SERVER
    SERVER -->|reads| CONFIG
    SERVER -->|reads/writes| PREFS
    SERVER -->|"calls with the key (never reaches the browser)"| ANTHROPIC
    SERVER -.->|"after /remember, rebuilds"| BUILD
    SERVER -.->|"serves ONLY this folder"| VIEWER
```

Everything runs locally. The only thing that leaves the machine are `server.py`'s calls to the Anthropic API (with the question and the relevant nodes) — never the API key, never directly from the browser.

---

## 2. Responsibility of each file

| File | What it does |
|---|---|
| `build.py` | Walks `docs/` (skipping `docs/en/`, `docs/es/`), `connectors.json`, `tools.json`; generates `viewer/graph-data.js`. Standard library only. |
| `server.py` | HTTP server (`http.server`, standard library only). Serves `viewer/` and exposes a dozen-plus endpoints (`/chat`, `/remember`, `/preferences`, `/connectors`, `/tools`, `/runtime`, ... — full list in the module docstring at the top of the file). |
| `viewer/index.html` | The entire frontend in a single file: 3D galaxy (3d-force-graph via esm.sh), chat bar, voice (Web Speech API), node panel, powers panel. |
| `viewer/graph-data.js` | **Generated, do not edit by hand.** Overwritten on every `build.py` run or `/remember` call. |
| `config.json` | `api_key` + `models` (list of selectable models). Outside `viewer/`, in `.gitignore`. |
| `preferences.json` | `lang` (`es`/`en`) + addressed name, read by `build_system_prompt()`. Also `.gitignore` — per-installation state, not shared config. |
| `connectors.json` / `tools.json` | Each entry is a `connector`/`tool` type node. The `status` field (`active`/`placeholder`) decides whether it shows as "active" in the Council of Powers (Phase 7). |
| `docs/ideas/`, `docs/projects/`, `docs/captures/`, rest of `docs/` | Real content in Markdown. `docs/ideas/` → `idea` type; `docs/projects/` → `project` type; `docs/captures/` → notes born from `/remember`; everything else under `docs/` (except `docs/en/`, `docs/es/`) → `note` type. |
| `viewer/images/moai-clean.png` | Real photo of a Moai — base for Phase 9 (animated face). The only `images/` folder actually served — `server.py` serves `viewer/` only. |

---

## 3. `/chat` flow (with web search)

```mermaid
sequenceDiagram
    participant U as Matatoa
    participant V as viewer/index.html
    participant S as server.py
    participant A as Anthropic API

    U->>V: types or dictates a question
    V->>S: POST /chat {question, session_id, model}
    S->>S: load_graph() (cached by mtime)
    S->>S: score_nodes(question, graph) -> top 6
    S->>S: load_preferences() -> lang, name
    S->>S: build_system_prompt(graph, node_ids, lang, name)
    S->>A: Messages API + tools=[web_search]
    alt the model decides to search the web
        A->>A: runs the search (100% server-side)
        A-->>S: interleaved text + web_search_tool_result blocks
    else answers only from notes or chats
        A-->>S: single text block
    end
    S->>S: concatenate ALL text blocks + normalize whitespace
    S->>S: parse_marker() -> type: nodes / chat / web
    Note over S: if type != nodes, clear node_ids<br/>if type != web, clear sources
    S-->>V: {answer, nodes, sources, graph_mtime, model}
    V->>V: compare graph_mtime with own DATA.mtime
    alt matches and there are nodes
        V->>V: flyToNode() (1-3 nodes) or highlightCluster() (4+)
    else doesn't match, or chat/web with no nodes
        V->>V: camera stays put, warns if the galaxy is out of date
    end
    V->>U: shows the answer and reads it aloud (speechSynthesis)
```

**Why the `[[nodes]]/[[chat]]/[[web]]` marker exists:** it's the only way the server knows whether it should move the camera. The system prompt forces the model to prepend it; `parse_marker()` looks for it anywhere in the response (not just the start, because a web search can prepend a sentence of intent before the marker) and, if it finds none but there are real sources, it infers `web` rather than blindly assuming `nodes`.

---

## 4. `/remember` flow

```mermaid
sequenceDiagram
    participant U as Matatoa
    participant V as viewer/index.html
    participant S as server.py
    participant FS as docs/captures/*.md
    participant B as build.py

    U->>V: "remember that..." (voice or text)
    V->>S: POST /remember {text}
    activate S
    Note over S: with _build_lock — two /remember calls at once<br/>can't step on each other or corrupt graph-data.js
    S->>S: write_capture(text) -> title + slug
    S->>FS: writes the .md note
    S->>B: build.build()
    B->>B: walks docs/+connectors.json+tools.json<br/>reassigns ALL ids (id == index)
    B-->>S: graph-data.js rewritten (atomic)
    alt build fails
        S->>FS: deletes the note just written (no orphans)
        S-->>V: {error}
    else build OK
        S->>S: locate new_id and related_id (highest-degree neighbor)
        S-->>V: {full graph, new_id, related_id, title}
    end
    deactivate S
    V->>V: preserves the positions of already-existing stars
    V->>V: places the new one next to related_id + glow pulse
    V->>V: Graph.graphData(new DATA) + flyToNode()
    V->>U: confirms out loud
```

---

## 5. Critical rules and non-obvious decisions

- **`id == index` in `GRAPH.nodes`.** Explicit design decision since Phase 1: "future features depend on looking up nodes by index." Direct consequence: `build.py` **reassigns ALL ids on every run** (alphabetical order within each folder, made deterministic with `dirnames.sort()`), so a `/remember` call can shift the ids of notes that already existed, not just append one at the end. The `mtime` field in `GRAPH` (and `graph_mtime` in the `/chat` response) exist precisely so the viewer can detect when its copy went stale and avoid flying to the wrong star — but it doesn't fix it by itself: if you see the "outdated galaxy" warning, reload the page.
- **`py -3`, not `python`.** On Matatoa's machine, plain `python` points to the Microsoft Store alias.
- **esm.sh, not unpkg.** The `.mjs` bundle from unpkg for `3d-force-graph` doesn't bundle its dependencies (`three`); esm.sh does resolve them via `?deps=`. The `three` version imported in the module and the one in `?deps=` must match exactly, or the graph and sprites won't share the same instance.
- **Locks in `server.py`.** It's a `ThreadingHTTPServer`: it handles requests in parallel. `_build_lock` serializes note writes + graph rebuilds. `_session_locks` (one per `session_id`) serializes the history of a single conversation, so that two near-simultaneous questions don't break the `user`/`assistant` alternation required by the Messages API.
- **Atomic write.** `build.py` writes to a `.tmp` file and does `os.replace()` at the end — nobody ever reads a half-written `graph-data.js`.
- **The API key never reaches the browser.** It lives only in `config.json` (root, outside `viewer/`, in `.gitignore`). The server serves EXCLUSIVELY the `viewer/` folder — requesting `/config.json` from the browser gets a 404.
- **Graceful degradation on corrupt JSON.** `load_json_list()` (server.py) and `collect_json_nodes()` (build.py) return an empty list on a missing file, invalid JSON, or an unexpected root/list shape — they never raise an uncaught exception. This matters especially in `build.py`, because `/remember` calls `build.build()` and, if it failed, the note just written would be deleted as a rollback.

---

## 6. Accepted, not fixed

Things found during audits and left as-is on purpose — don't rediscover them as if they were new:

- **Id drift across tabs.** If you have two tabs open and one does `/remember`, the other is left with potentially shifted ids until reloaded. `graph_mtime` prevents this from moving the camera to the wrong star, but it doesn't fix it by itself. Fixing it at the root would mean abandoning the "id == index" rule — an explicit design decision, not an oversight.
- **`_sessions` grows without bound** (in-memory chat history, never purged by age). Low impact: local use, single user.
- **`_read_body` doesn't cap `Content-Length` size.** Mitigated by the server only listening on `127.0.0.1`.

---

## 7. How to run the project

```bash
py -3 build.py       # generates/updates viewer/graph-data.js
py -3 server.py       # starts at http://localhost:4700
```

Open Chrome at `http://localhost:4700`. Paste the API key into `config.json` (never in the chat).

---

## 8. File structure

```
moai/
├── README.md                   # quick start
├── config.json                 # api_key + models — NEVER in git (.gitignore)
├── config.example.json         # safe template — copy to config.json
├── preferences.json            # lang + addressed name — NEVER in git (.gitignore)
├── preferences.example.json    # safe template
├── connectors.json             # "connector" type nodes
├── tools.json                  # "tool" type nodes
├── build.py                    # indexer: docs/+JSON -> graph-data.js
├── server.py                   # server — see its module docstring for every endpoint
├── docs/
│   ├── ideas/*.md               # -> "idea" type nodes
│   ├── projects/*.md            # -> "project" type nodes
│   ├── captures/*.md            # notes born by voice (/remember)
│   ├── en/ARCHITECTURE.md       # this document (skipped by build.py)
│   └── es/                      # Spanish project docs (also skipped by build.py)
├── viewer/
│   ├── index.html               # the entire frontend
│   ├── graph-data.js            # generated — DO NOT edit by hand
│   └── images/                  # the only images/ folder actually served
└── tests/                       # offline unit tests, no network/API calls
```

---

## 9. Frontend implementation reference (`viewer/index.html`)

The entire UI is a single HTML file (CSS + HTML + JS, no build step, no
framework). This section is the detailed reference; the project's own
fast-path context doc keeps only a short index pointing here.

### CSS variables (theme)
```css
--space: #060b0e        /* background */
--note: #8fae6f         /* green */
--connector: #45c4b0    /* teal */
--tool: #c98a5e         /* orange */
--project: #d9aa4b      /* gold */
--idea: #f2e8d5         /* cream */
--face-standby: #e8ede9
--face-thinking: #e8c22e
--face-talking: #4fa3e0
--face-tools: #4caf6e
--face-error: #e0554a
```

### Moai face

`#moai-face` is a `180×270px` container (bottom-right) with:
- `<img id="moai-photo">` — `moai-clean.png` with `object-fit: cover; object-position: top center`
- `<svg id="moai-svg" viewBox="0 0 72 108">` — SVG overlay drawn on top
  - Eyes (`eye-l`, `eye-r` rects), pupils (`eye-pupil` circles), nose mark, mouth line
  - Chest ring + inner fill circle + glyph accent lines
  - `face-outline` rect (thin perimeter border around the face)
- **5 CSS states** driven by `setFaceState(state)`:
  - `s-idle` — opacity 0.1 (nearly invisible in standby)
  - `s-listening` — dim white
  - `s-thinking` — gold animated pulse
  - `s-tools` — green animated pulse
  - `s-talking` — blue animated pulse (same animation as thinking)
  - `s-error` — red

`setFaceState` is called from: `setStatus()` (thinking/tools), `rec.onstart` (listening), `speakBrowser` / `speakElevenLabs` start/end events (talking).

### Splash screen

`#splash` covers the full viewport with `images/web.jpg`.
- First visit: 5 seconds minimum delay.
- Subsequent visits: 300ms.
- Click-to-skip: `splashEl.addEventListener('click', e => { if (e.button === 0) hideSplashNow(); }, { once: true })`.
- `hideSplashNow()` directly adds `.hide` class (opacity → 0) then removes the element after 1.2s.
- **Do NOT use `maybeHideSplash()` for click-to-skip** — it requires both `splashMinDelayDone` AND `splashAppReady` flags to be true, so clicking before the galaxy engine finishes would do nothing.

### Conversation mode

Button `#conv-mode` toggles `conversationMode` flag (persisted in `localStorage`).
When ON: after `speak()` resolves, `startListening()` is called automatically.
`speak()` returns a `Promise` — both `speakBrowser()` and `speakElevenLabs()` are async and resolve when audio finishes.

### Bilingual preferences

Language (`es`/`en`) and the addressed name are stored server-side in `preferences.json` (GET/POST `/preferences`), not `localStorage` — every other setting on this page uses `localStorage`, this one deliberately doesn't, so it survives across browsers/reinstalls and is human-editable outside the browser, matching `config.json`'s pattern. On first run (`lang` is `null`), the viewer shows a one-time prompt (`#lang-prompt`) after the splash hides asking for a name and Español/English; after that it's changeable anytime via the `#lang-pick` toggle in the chat bar (voice + recognition switch live, no reload) or the name field in the Actions panel. `build_system_prompt()` in `server.py` reads `preferences.json` itself (not a client-supplied value) and selects between `_system_prompt_parts_es`/`_system_prompt_parts_en`. The "IORANA" greeting stays fixed in both languages — Rapa Nui brand identity, not an interface-language string — only the addressed name changes.

**Load-order gotcha (real incident):** `PREFS` must be declared before any code that reads it can run — `pickVoice()` reads `PREFS.lang` and is called synchronously at page load (`speechSynthesis.onvoiceschanged` + an immediate call), well before the rest of the Phase 16 block would otherwise declare it. Declaring `PREFS` there put it in the temporal dead zone, threw a `ReferenceError` on every load, and silently killed the entire script — the splash never hid and nothing after it ran, with no visible console error until deep inspection. Fixed by declaring `let PREFS` immediately after `let DATA = GRAPH;`, near the top of the script. If you add new state that early-running functions depend on, declare it that early too.

### Voice (speech recognition)

`SpeechRecognition` with `rec.lang` set to `es-ES` or `en-US` from the language preference. Single-shot (not continuous). In conversation mode, `rec.onend` restarts it after a 400ms delay.

### Voice (speech synthesis)

Auto-selects a voice matching the language preference — for Spanish it scores named voices (prefers "Álvaro", "Natural", "Pablo"); for English it just prefers a "natural/online" voice, since named voices aren't standardized enough across OSes to hardcode a short list. Manual override via `#voice-pick` dropdown. Voice preference saved to `localStorage` as `moai-voice-name` (this one setting stays client-side, unlike `lang`/`name`).

### ElevenLabs TTS — CURRENTLY DISABLED

`config-el.json` was deleted (user has free plan, free plan cannot call the TTS API). `POST /tts` returns `{"error": "ElevenLabs not configured"}`. The circuit breaker in `speakElevenLabs()` sets `elevenLabsDisabledUntil` for 5 minutes on any failure, so it doesn't retry on every answer.

**To re-enable:** create `config-el.json` from `config-el.example.json` with a real key and voice id (requires a paid ElevenLabs plan).

### Legend type filter

`#legend` in bottom-left corner. Each item has `data-type` attribute and `cursor: pointer`.
`toggleTypeFilter(type, color)`:
- First click: scales matching nodes ×1.6, dims others to 0.3 opacity, zooms toward centroid (min distance 150 to avoid extreme zoom on single nodes like "Projects").
- Same click again: deselects, restores original scales, zooms out with `Graph.zoomToFit(1600, 90)`.
- `originalNodeScales` Map preserves the baseline scales before filtering.
- `currentTypeFilter` tracks the active filter (only one at a time — multi-filter is deferred).

### Chat bar

- `<textarea id="question">` (auto-grow via `autoGrowQuestion()` on `input` event, max-height 120px).
- `Enter` submits; `Shift+Enter` inserts newline.
- Successful answers go to the **Log panel** (right side panel), NOT the floating answer box.
- Floating `#answer-box` is reserved for errors and "outdated galaxy" warnings only.
- Log auto-opens on the first message (`logAutoOpened` flag, one-time only).

### Navigator's Log

`localStorage` key `moai-log`, max 300 entries. Each entry: `{ts, type, question, answer, nodes, sources}`. Stars in the log are clickable (fly to node). Sources are filtered for `https?://` before rendering.

### 3D galaxy

`3d-force-graph` loaded from `esm.sh`. Key imports:
```js
import ForceGraph3D from 'https://esm.sh/3d-force-graph@1.73.3?deps=three@0.180.0'
import * as THREE   from 'https://esm.sh/three@0.180.0'
import { UnrealBloomPass } from 'https://esm.sh/three@0.180.0/examples/jsm/postprocessing/UnrealBloomPass.js?deps=three@0.180.0'
```

> **esm.sh, not unpkg.** unpkg's `.mjs` bundle doesn't include dependencies; esm.sh resolves them via `?deps=`. The `three` version in the import and in `?deps=` must match exactly, or nodes and sprites won't share the same Three.js instance.

Bloom: `UnrealBloomPass(resolution, strength=0.9, radius=0.4, threshold=0.12)` added via `Graph.postProcessingComposer().addPass(bloomPass)`.

### Slash commands

Handled client-side in `handleSlashCommand()`:

| Command | Action |
|---|---|
| `/remember <text>` | Opens save flow, calls `POST /remember` |
| `/remember-edit <title>` | Opens edit flow |
| `/list-notes [q]` | Lists notes (optional search) |
| `/list-connectors` | Lists connectors |
| `/list-tools` | Lists tools |
| `/web-search <query>` | Falls through to `/chat` (server triggers web_search tool) |

Command menu (`#slash-menu`) appears when the user types `/` in the textarea.
