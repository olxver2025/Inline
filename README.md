# Inline

Run Python snippets inside a tightly constrained Docker container from Discord

## Features

- Per-user sandboxes: one sandbox directory per user, persisted on disk and auto-expiring after 1 week of inactivity.
- Slash commands under `/il`:
  - `/il create`: create your sandbox.
  - `/il py`: run Python code inside your sandbox (30s timeout).
  - `/il look [path]`: change/list current directory, with pagination.
  - `/il write name content`: create/overwrite a file.
  - `/il rm name [recursive]`: delete a file or a directory (with `recursive=true`).
  - `/il pip packages:"..."`: install Python packages into your sandbox with throttled log updates.
  - `/il delete`: delete your sandbox and all files.
- Secure execution in `python:3.11-alpine` with strict limits:
  - No network for code runs (`--network none`), read-only FS, tmpfs `/tmp`
  - Non-root user, drop all caps, `no-new-privileges`
  - CPU, memory, and pids limits; GPU not used
  - 30s execution timeout per `/il py`
- Package installs: `/il pip` allows network access only for installing to `/workspace/.site-packages`; logs are edited at most every 3 seconds to avoid Discord rate limits.
- Long outputs are truncated; when too large, the bot attaches the full output as a file.

## Prerequisites

- Docker Desktop (or Docker Engine) running and accessible as `docker`
- Python 3.9+
- A Discord Bot token

## Setup

1. Create and activate a virtual environment (optional but recommended):

   ```bash
   python -m venv .venv
   . .venv/bin/activate  # Windows: .venv\\Scripts\\activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Set your Discord bot token:

   ```bash
   # PowerShell
   $env:DISCORD_TOKEN = "YOUR_TOKEN_HERE"
   # bash/zsh
   export DISCORD_TOKEN="YOUR_TOKEN_HERE"
   ```

4. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

1) Create your sandbox

```
/il create
```

2) (Optional) Install packages into your sandbox

```
/il pip packages:"requests numpy"
```

3) Run code in your sandbox (code block or inline)

```
/il py code:"""
```python
import requests
print("requests:", requests.__version__)

```

4) Browse/change directory with pagination

```
/il look                 # list current directory
/il look path:subdir     # cd into subdir and list
```

5) Manage files

```
/il write name:main.py content:"print('hi')"
/il rm name:main.py
/il rm name:folder recursive:true
```

6) Delete your sandbox

```
/il delete
```

Notes
- `/il py` respects a 30s timeout, disables network, and limits CPU/RAM.
- `/il pip` allows network only for installation, stores packages in `/workspace/.site-packages`, and `/il py` sets `PYTHONPATH` accordingly.

## Configuration

Environment variables (optional):

- `SANDBOX_IMAGE`: Docker image (default `python:3.11-alpine`)
- `SANDBOX_PULL_ON_STARTUP`: pre-pull image on startup (default `1`)
- `IL_BASE_DIR`: host directory for sandboxes (default `./il_sandboxes`)
- `IL_TIMEOUT_SECONDS`: `/il py` timeout (default `30.0`)
- `IL_MEMORY`: memory limit for runs and pip (default `256m`)
- `IL_CPUS`: CPU limit for runs and pip (default `1.0`)
- `IL_RETENTION_SECONDS`: sandbox expiry in seconds (default `604800`)
- `ECHO_LAST_EXPR`: REPL-style echo of last expression (default `1`)
- `DOCKER_BINARY`: docker binary name/path (default `docker`)

Resource limits are enforced in `sandbox.py` (`--network none` for runs, `--cpus`, `--memory`, `--pids-limit`, non-root user). Adjust via env vars where exposed.

### First-run timeouts

If you see `Execution timed out` on the very first run, Docker was likely pulling the Python image and exceeded the short execution timeout. Either let the bot pre-pull (default), or run:

```
docker pull python:3.11-alpine
```

Then retry. Use `/health` to check whether Docker is reachable and the image is present.


## Notes on Security

This design aims for strong isolation for untrusted snippets using Docker. Still, treat it as best-effort isolation and avoid running the bot on hosts with sensitive data or elevated privileges. Keep Docker updated and prefer Linux containers.
