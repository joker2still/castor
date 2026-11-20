<p align="center"><img src="static/logo.png" alt="Castor" width="150"></p>

# Castor

[中文](README.md) | [English](README.eu.md)

A self-hosted AI assistant with multilingual chat, tool calling, long-term memory, a knowledge base, and scheduled tasks. Maintained by **joker2still**.

Use Castor through the terminal, web interface, or Telegram. Connect a local Ollama or LM Studio instance, or configure a cloud model provider in Settings. Chat history, settings, and memory are stored locally; cloud models and network tools send relevant requests to their respective services.

## Quick start

You need Python 3.11+, Git, and a running model service. On Windows, Git Bash is recommended for the assistant's shell commands.

### 1. Prepare Ollama

Install and start Ollama, then download the model configured as the code default:

```bash
ollama pull qwen3:8b
```

If Ollama is not already running as a service, run `ollama serve` in another terminal. Its default API endpoint is `http://localhost:11434/v1`.

### 2. Install Castor

```bash
git clone https://github.com/joker2still/castor.git
cd castor
```

Windows:

```powershell
.\setup.bat
.\.venv\Scripts\Activate.ps1
```

Linux / macOS:

```bash
./setup.sh
source .venv/bin/activate
```

The setup scripts create a virtual environment, install dependencies, and preload the embedding model. The first installation requires internet access. Alternatively, create a virtual environment manually and run `python -m pip install -e .`.

### 3. Start Castor

```bash
castor --web
```

Open `http://localhost:7860`. Follow the first-run setup to choose a name, timezone, language, and model. For Chinese conversations, set the assistant's language to Chinese. Setup may recommend a different model based on your hardware; the configuration you choose and save takes precedence.

```bash
castor              # Terminal chat
castor --doctor     # Check dependencies, database, and model connectivity
```

If the model is downloaded but Castor cannot connect, check that Ollama is running and that Settings selects provider `ollama` and model `qwen3:8b`.

## Features

- **Tool calling**: read and write files, run commands, and make network requests. Additional tools load on demand.
- **Memory and knowledge base**: SQLite stores conversations, while Qdrant stores vectors. Search files, web pages, and common office documents.
- **Browser and MCP**: browser automation, external tool services, and extensible skills.
- **Task execution**: scheduled tasks, persistent goals, execution records, and recovery after interruption.
- **Interfaces**: web, terminal, and Telegram, with optional voice, camera, and an HTML canvas.
- **Personality and models**: configure the assistant's name, language, personality, and model provider.

Browser automation requires Playwright and Chromium:

```bash
python -m pip install playwright
python -m playwright install chromium
```

For local speech recognition, install `python -m pip install -e '.[voice]'`. Camera and microphone access from a remote browser generally requires HTTPS; start it with `castor --web --ssl --port 7861`.

## Default configuration

The table below lists the initial defaults in `config.py` and the default provider selection. Provider and model selection also read saved database settings. Switch providers through the web settings or terminal `/model`; check saved settings on existing installations.

| Setting | Default | Purpose |
|---|---|---|
| Provider | `ollama` | Used when no provider has been saved |
| `CASTOR_LLM_URL` | `http://localhost:11434/v1` | Initial model API endpoint |
| `CASTOR_LLM_MODEL` | `qwen3:8b` | Initial chat model |
| `CASTOR_LLM_KEY` | `ollama` | Placeholder key for local Ollama |
| `CASTOR_DATA_DIR` | `~/.castor` | User data directory |
| `CASTOR_DB_PATH` | `castor.db` inside the data directory | SQLite database |
| `CASTOR_QDRANT_MODE` | `disk` | Vector storage mode; also supports `memory` and `server` |
| `CASTOR_QDRANT_PATH` | `memory/` inside the data directory | On-disk vector storage |
| `CASTOR_PASSWORD` | Empty | Web access password; set it yourself |
| Web port | `7860` | Override with `--port` |

To change the data directory:

```powershell
# Windows PowerShell
$env:CASTOR_DATA_DIR = "D:\castor-data"
castor --web
```

```bash
# Linux / macOS
export CASTOR_DATA_DIR="$HOME/.castor"
castor --web
```

Other settings can be changed and saved through the web interface. Native startup does not automatically read the project's `.env` file; Docker Compose loads that file.

Anonymous telemetry is off by default. No telemetry server or application key is bundled, and no telemetry network requests are sent unless you configure a collector.

## Docker deployment

Copy `.env.example` to `.env`, then configure the web password and model endpoint:

```bash
cp .env.example .env
docker compose up -d
```

In PowerShell, use `Copy-Item .env.example .env` to copy the file.

Compose uses `ghcr.io/joker2still/castor:latest`, which must first be published by the repository's Docker workflow. Before an image is available, comment out `image:` and enable `build: .` in `docker-compose.yml`, then run:

```bash
docker compose up -d --build
```

The Docker example connects to Ollama on the host at `http://host.docker.internal:11434/v1`, using `qwen3:8b`. The container must be able to reach the host service. On Linux, configure a host gateway or use a reachable host address. Ollama must also listen on an interface accessible to the container. After startup, set the Ollama URL to that address in the web model settings: provider presets or saved configuration override the initial endpoint from environment variables.

Persistent data lives in **`./castor-data`**, mounted at `/data` inside the container. Updating the image does not delete this directory. Back up the entire directory.

## Updates and development checks

Activate the virtual environment, then run:

```bash
git pull
python -m pip install -e . --upgrade
```

Development checks:

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/ -v
python -m ruff check .
python scripts/check_js.py
```

The JavaScript syntax check requires Node.js.

## Documentation and license

Feature guides are in [docs/](docs/README.md). Architecture notes, development instructions, and release history are collected separately in `project-notes/`.

Report security issues privately by following the [security policy](SECURITY.md). Castor is distributed under the [MIT license](LICENSE).
