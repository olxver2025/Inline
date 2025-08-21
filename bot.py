import io
import os
import textwrap
from typing import Optional
import ast



import nextcord
from nextcord.ui import View, Button
from nextcord.ext import commands
from dotenv import load_dotenv

from sandbox import run_code_in_docker, SandboxError, ensure_image, build_pip_install_command
import json
import asyncio
import shlex
import time
from pathlib import Path


# Load environment variables from .env file
load_dotenv()
TOKEN: Optional[str] = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN environment variable.")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
DEFAULT_TIMEOUT = float(os.getenv("SANDBOX_TIMEOUT", "25.0"))
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.11-alpine")
PULL_ON_STARTUP = os.getenv("SANDBOX_PULL_ON_STARTUP", "1") not in {"0", "false", "False"}
ECHO_LAST_EXPR = os.getenv("ECHO_LAST_EXPR", "1") not in {"0", "false", "False"}
IL_BASE_DIR = Path(os.getenv("IL_BASE_DIR", "./il_sandboxes")).resolve()
IL_TIMEOUT_SECONDS = float(os.getenv("IL_TIMEOUT_SECONDS", "30.0"))
IL_MEMORY = os.getenv("IL_MEMORY", "256m")
IL_CPUS = os.getenv("IL_CPUS", "1.0")
IL_RETENTION_SECONDS = int(os.getenv("IL_RETENTION_SECONDS", str(7 * 24 * 3600)))


intents = nextcord.Intents.default()
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def extract_code_block(raw: str) -> str:
    content = raw.strip()
    # Handle inline single-backtick code like `1+1`
    if content.startswith("`") and content.endswith("`") and len(content) >= 2:
        inner_inline = content[1:-1]
        return inner_inline.strip()
    if content.startswith("```") and content.endswith("```"):
        # strip triple backticks and optional language hint
        inner = content[3:-3]
        if inner.startswith("python\n") or inner.startswith("py\n"):
            inner = inner.split("\n", 1)[1] if "\n" in inner else ""
        return inner.strip()
    return content


def format_result(stdout: str, stderr: str, returncode: int, truncated: bool) -> str:
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        if stdout:
            parts.append("\n--- stderr ---\n" + stderr)
        else:
            parts.append(stderr)
    if not stdout and not stderr:
        parts.append(f"(no output, exit code {returncode})")
    if truncated:
        parts.append("\n[output truncated]")
    text = "".join(parts)
    # ensure message-friendly newlines
    return text


def maybe_echo_last_expr(code: str) -> str:
    """If enabled, append a print of the last expression's source.

    This mimics REPL behavior: `1+1` -> prints 2. Falls back gracefully
    if parsing fails or when the last statement isn't an expression.
    """
    if not ECHO_LAST_EXPR:
        return code
    try:
        tree = ast.parse(code, mode="exec")
    except Exception:
        return code
    if not tree.body:
        return code
    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        expr = last.value
        # Avoid echoing if the last expression is an explicit print(...) call
        try:
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "print":
                return code
        except Exception:
            pass
        # Try to recover the exact source of the last expression
        src = ast.get_source_segment(code, expr)
        if not src:
            # Fallback using node positions (Python 3.8+)
            try:
                lines = code.splitlines()
                start = getattr(expr, "lineno", None)
                end = getattr(expr, "end_lineno", None)
                scol = getattr(expr, "col_offset", 0)
                ecol = getattr(expr, "end_col_offset", None)
                if start is not None and end is not None:
                    if start == end:
                        seg = lines[start - 1][scol:ecol]
                    else:
                        parts = [lines[start - 1][scol:]]
                        for i in range(start, end - 1):
                            parts.append(lines[i])
                        parts.append(lines[end - 1][:ecol])
                        seg = "\n".join(parts)
                    src = seg.strip()
            except Exception:
                src = None
        if not src:
            # Final fallback: last non-empty line
            for line in reversed(code.splitlines()):
                if line.strip():
                    src = line.strip()
                    break
        if src:
            return f"{code}\nprint(repr(({src})))"
    return code


# --- Persistent per-user sandbox management ---

def _user_dir(user_id: int) -> Path:
    return IL_BASE_DIR / str(user_id)


def _meta_path(user_id: int) -> Path:
    return _user_dir(user_id) / ".meta.json"


def _load_meta(user_id: int) -> Optional[dict]:
    try:
        with open(_meta_path(user_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_meta(user_id: int, meta: dict) -> None:
    d = _user_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    p = _meta_path(user_id)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _now() -> float:
    return time.time()


def _ensure_sandbox(user_id: int) -> bool:
    d = _user_dir(user_id)
    if not d.exists():
        return False
    # Expiry
    meta = _load_meta(user_id)
    if not meta:
        return False
    last = float(meta.get("last_used", meta.get("created_at", 0)))
    if _now() - last > IL_RETENTION_SECONDS:
        # Expired; delete
        try:
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_file() or p.is_symlink():
                    p.unlink(missing_ok=True)
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            d.rmdir()
        except Exception:
            pass
        return False
    return True


def _create_sandbox(user_id: int) -> bool:
    d = _user_dir(user_id)
    if d.exists():
        return False
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "created_at": _now(),
        "last_used": _now(),
        "cwd": ".",
    }
    _save_meta(user_id, meta)
    return True


def _delete_sandbox(user_id: int) -> bool:
    d = _user_dir(user_id)
    if not d.exists():
        return False
    try:
        for p in sorted(d.rglob("*"), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
        for p in sorted(d.rglob("*"), reverse=True):
            if p.is_dir():
                p.rmdir()
        d.rmdir()
        return True
    except Exception:
        return False


def _update_last_used(user_id: int) -> None:
    meta = _load_meta(user_id)
    if not meta:
        return
    meta["last_used"] = _now()
    _save_meta(user_id, meta)


def _set_cwd(user_id: int, rel: str) -> bool:
    meta = _load_meta(user_id)
    if not meta:
        return False
    # Normalize path: prevent escaping outside sandbox
    rel = rel.strip() if rel else "."
    rel = rel.replace("\\", "/")
    if rel.startswith("/"):
        rel = rel[1:]
    base = _user_dir(user_id)
    target = (base / rel).resolve()
    try:
        # Ensure target is within base
        target.relative_to(base)
    except Exception:
        return False
    if not target.exists() or not target.is_dir():
        return False
    rel_norm = "." if target == base else str(target.relative_to(base))
    meta["cwd"] = rel_norm
    _save_meta(user_id, meta)
    return True


def _get_cwd(user_id: int) -> str:
    meta = _load_meta(user_id) or {}
    return meta.get("cwd", ".")


def _resolve_path(user_id: int, rel: str) -> Optional[Path]:
    base = _user_dir(user_id)
    rel = rel.strip().replace("\\", "/")
    if rel.startswith("/"):
        rel = rel[1:]
    p = (base / _get_cwd(user_id) / rel).resolve()
    try:
        p.relative_to(base)
    except Exception:
        return None
    return p


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    print(
        "Config: "
        f"ECHO_LAST_EXPR={ECHO_LAST_EXPR}, "
        f"PULL_ON_STARTUP={PULL_ON_STARTUP}, "
        f"SANDBOX_IMAGE={SANDBOX_IMAGE}"
    )
    if PULL_ON_STARTUP:
        try:
            ensure_image(SANDBOX_IMAGE, pull=True)
            print(f"Docker image ready: {SANDBOX_IMAGE}")
        except SandboxError as e:
            print(f"[WARN] Unable to ensure Docker image '{SANDBOX_IMAGE}': {e}")


@bot.slash_command(name="il", description="Interact with your personal sandbox.")
async def il(inter: nextcord.Interaction):
    pass

@il.subcommand(name="create", description="Create your personal sandbox (one per user).")
async def il_create(inter: nextcord.Interaction):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if _ensure_sandbox(uid):
        await inter.followup.send("You already have a sandbox.")
        return
    if _create_sandbox(uid):
        await inter.followup.send("Sandbox created. Use /il look to browse and /il py to run code.")
    else:
        await inter.followup.send("Failed to create sandbox. Try again.")


@il.subcommand(name="py", description="Run Python code in your sandbox (30s timeout).")
async def il_py(inter: nextcord.Interaction, code: str):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox found or it expired. Use /il create first.")
        return
    code_to_run = extract_code_block(code)
    to_exec = maybe_echo_last_expr(code_to_run)
    try:
        res = run_code_in_docker(
            to_exec,
            timeout_seconds=IL_TIMEOUT_SECONDS,
            image=SANDBOX_IMAGE,
            ensure_image_present=not PULL_ON_STARTUP,
            memory=IL_MEMORY,
            cpus=IL_CPUS,
            mount_dir=str(_user_dir(uid)),
            workdir_subpath=_get_cwd(uid),
            env={"PYTHONPATH": "/workspace/.site-packages"},
        )
    except SandboxError as e:
        await inter.followup.send(f"Sandbox error: {e}")
        return
    _update_last_used(uid)
    full_text = format_result(res.stdout, res.stderr, res.returncode, res.truncated)
    if len(full_text) <= 1900:
        await inter.followup.send(f"```\n{full_text}\n```")
    else:
        buf = io.BytesIO(full_text.encode("utf-8", errors="replace"))
        await inter.followup.send(
            content=f"Output too long (exit {res.returncode}).",
            file=nextcord.File(buf, filename="output.txt"),
        )


class PagedList(View):
    def __init__(self, lines: list[str], page_size: int = 20):
        super().__init__(timeout=60)
        self.lines = lines
        self.page_size = page_size
        self.page = 0
        self.total_pages = max(1, (len(lines) + page_size - 1) // page_size)
        self.prev_button = Button(label="Prev", style=nextcord.ButtonStyle.secondary)
        self.next_button = Button(label="Next", style=nextcord.ButtonStyle.secondary)
        self.prev_button.callback = self.on_prev
        self.next_button.callback = self.on_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    def render(self) -> str:
        start = self.page * self.page_size
        end = start + self.page_size
        body = "\n".join(self.lines[start:end]) or "(empty)"
        return f"Page {self.page+1}/{self.total_pages}\n\n{body}"

    async def on_prev(self, interaction: nextcord.Interaction):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(content=f"```\n{self.render()}\n```", view=self)

    async def on_next(self, interaction: nextcord.Interaction):
        if self.page + 1 < self.total_pages:
            self.page += 1
        await interaction.response.edit_message(content=f"```\n{self.render()}\n```", view=self)


@il.subcommand(name="look", description="Change/list current directory in your sandbox.")
async def il_look(
    inter: nextcord.Interaction,
    path: str = nextcord.SlashOption(description="Directory to view (relative)", required=False),
):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox found or it expired. Use /il create first.")
        return
    if path:
        if not _set_cwd(uid, path):
            await inter.followup.send("Invalid path. Stay in current directory.")
            return
    base = _user_dir(uid)
    cwd_rel = _get_cwd(uid)
    cur = (base / cwd_rel).resolve()
    entries = []
    for p in sorted(cur.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        kind = "/" if p.is_dir() else ""
        size = "" if p.is_dir() else f" ({p.stat().st_size} B)"
        entries.append(f"{p.name}{kind}{size}")
    lines = [f"cwd: /{cwd_rel if cwd_rel != '.' else ''}"] + entries
    view = PagedList(lines, page_size=20)
    _update_last_used(uid)
    await inter.followup.send(content=f"```\n{view.render()}\n```", view=view)


@il.subcommand(name="delete", description="Delete your sandbox and all files.")
async def il_delete(inter: nextcord.Interaction):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox to delete.")
        return
    ok = _delete_sandbox(uid)
    if ok:
        await inter.followup.send("Sandbox deleted.")
    else:
        await inter.followup.send("Failed to delete sandbox. Try again.")


@il.subcommand(name="write", description="Create/overwrite a file in current directory.")
async def il_write(
    inter: nextcord.Interaction,
    name: str = nextcord.SlashOption(description="File name", required=True),
    content: str = nextcord.SlashOption(description="File content", required=True),
):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox found or it expired. Use /il create first.")
        return
    p = _resolve_path(uid, name)
    if not p:
        await inter.followup.send("Invalid path.")
        return
    if p.exists() and p.is_dir():
        await inter.followup.send("A directory exists with that name.")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    _update_last_used(uid)
    await inter.followup.send(f"Wrote {p.name} ({len(content)} bytes).")


@il.subcommand(name="rm", description="Delete a file in current directory.")
async def il_rm(
    inter: nextcord.Interaction,
    name: str = nextcord.SlashOption(description="File or directory name", required=True),
    recursive: bool = nextcord.SlashOption(description="Remove directories recursively", required=False, default=False),
):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox found or it expired. Use /il create first.")
        return
    p = _resolve_path(uid, name)
    if not p or not p.exists():
        await inter.followup.send("Path not found.")
        return
    try:
        if p.is_dir():
            if not recursive:
                await inter.followup.send("Use recursive=true to remove directories.")
                return
            for q in sorted(p.rglob("*"), reverse=True):
                if q.is_file() or q.is_symlink():
                    q.unlink(missing_ok=True)
            for q in sorted(p.rglob("*"), reverse=True):
                if q.is_dir():
                    q.rmdir()
            p.rmdir()
        else:
            p.unlink(missing_ok=True)
    except Exception as e:
        await inter.followup.send(f"Failed to remove: {e}")
        return
    _update_last_used(uid)
    await inter.followup.send("Removed.")


@il.subcommand(name="pip", description="Install Python packages into your sandbox.")
async def il_pip(
    inter: nextcord.Interaction,
    packages: str = nextcord.SlashOption(description="Space-separated package names", required=True),
):
    await inter.response.defer(ephemeral=True)
    uid = inter.user.id
    if not _ensure_sandbox(uid):
        await inter.followup.send("No sandbox found or it expired. Use /il create first.")
        return
    pkgs = [p for p in packages.split() if p.strip()]
    if not pkgs:
        await inter.followup.send("Provide at least one package name.")
        return
    # Ensure image available if configured to do so at runtime
    try:
        if not PULL_ON_STARTUP:
            ensure_image(SANDBOX_IMAGE, pull=True)
    except SandboxError as e:
        await inter.followup.send(f"Sandbox error: {e}")
        return

    cmd = build_pip_install_command(
        mount_dir=str(_user_dir(uid)),
        packages=pkgs,
        memory=IL_MEMORY,
        cpus=IL_CPUS,
        image=SANDBOX_IMAGE,
    )

    # Start process and stream combined logs; edit message every 3 seconds
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    log_chunks: list[str] = []
    last_edit = asyncio.get_event_loop().time()
    msg = await inter.followup.send("Starting pip install...")

    async def maybe_edit(final: bool = False):
        nonlocal last_edit
        now = asyncio.get_event_loop().time()
        if not final and (now - last_edit) < 3.0:
            return
        text = "".join(log_chunks)
        # Show only the tail if too long
        display = text[-1800:]
        try:
            await msg.edit(content=f"```\n{display}\n```")
        except Exception:
            pass
        last_edit = now

    try:
        while True:
            if proc.stdout is None:
                break
            line = await proc.stdout.readline()
            if not line:
                break
            log_chunks.append(line.decode("utf-8", errors="replace"))
            await maybe_edit()
    finally:
        rc = await proc.wait()
        await maybe_edit(final=True)
        full_log = "".join(log_chunks)
        _update_last_used(uid)



@bot.slash_command(name="health", description="Show Docker sandbox health status.")
async def health_cmd(inter: nextcord.Interaction):
    await inter.response.defer(ephemeral=True)
    try:
        ensure_image(SANDBOX_IMAGE, pull=False)
        msg = f"Docker reachable. Image present: {SANDBOX_IMAGE}"
    except SandboxError as e:
        msg = f"Sandbox not ready: {e}"
    await inter.followup.send(msg)





def main():
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN environment variable.")
    try:
        bot.add_application_command(il)
    except Exception:
        pass
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
