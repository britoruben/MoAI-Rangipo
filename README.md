# MoAI

A voice-enabled second brain inspired by the Moai of Rapa Nui — a stone guardian that watches over your own notes, connectors, and tools, and gives them a voice.

Your notes, connectors, and tools become a navigable 3D knowledge galaxy. Ask it questions and it answers from your own knowledge (or the web), flying the camera to the source; tell it to remember something and a new star is born live.

- **Docs:** [`ARCHITECTURE.md`](docs/en/ARCHITECTURE.md) (how it works)

## Run it

```bash
py -3 build.py    # generates/updates viewer/graph-data.js
py -3 server.py    # starts at http://localhost:4700
py -3 server.py --dev    # same, but the Dev console (local file operations) can be enabled
```

Dev mode is off-limits unless the server was started for it (`--dev`, or `MOAI_DEV=1`): otherwise nothing can turn it on over HTTP.

Open Chrome at `http://localhost:4700`. Paste your Anthropic API key into `config.json` (never in the chat).

## Stack

Python 3 standard library only (`build.py`, `server.py`) + a single-page viewer (`viewer/index.html`) using `3d-force-graph` and the browser's Web Speech API.

**What leaves the machine:** (1) your question plus relevant note excerpts → Anthropic API; (2) `3d-force-graph` and Three.js downloaded from esm.sh on every load; (3) web search queries → Anthropic infrastructure; (4) voice recognition may use the browser's cloud service. Your notes themselves stay local.

---
