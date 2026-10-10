"""Discord Rich Presence for Claude Code: tokens burned + agents running. Never shows what you're working on.

Usage:
  python claude_presence.py            # push to Discord every 15s while Claude Code is running
  python claude_presence.py --spawn    # same, detached in the background (the SessionStart hook runs this)
  python claude_presence.py --print    # just print the status, no Discord
"""
import json, os, socket, subprocess, sys, time
from collections import Counter
from datetime import datetime
from pathlib import Path

# Defaults point at a shared Discord app ("ClaudeCode") that already has the logo uploaded.
# Override with your own app via the DISCORD_CLIENT_ID / DISCORD_ASSET environment variables.
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "1558281443560656916")
ASSET = os.environ.get("DISCORD_ASSET", "image-1791593760582")
LOCK_PORT = 47823
PROJECTS = Path.home() / ".claude" / "projects"
ACTIVE_SECS = 120   # an agent/session counts as "running" if its transcript changed this recently
RATE_WINDOW = 300   # tokens/min averaged over the last 5 minutes
_cache = {}         # path -> (mtime, {msg_id: (epoch, tokens)})


def parse(path):
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    msgs = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
                m = d["message"]
                u = m["usage"]
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            tok = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens",
                                              "cache_creation_input_tokens", "cache_read_input_tokens"))
            msgs[m.get("id") or d.get("uuid")] = (ts, tok)  # same msg id repeats per content block
    _cache[path] = (mtime, msgs)
    return msgs


def snapshot():
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = recent = 0
    agents, sessions = Counter(), 0
    for path in PROJECTS.glob("**/*.jsonl"):
        mtime = path.stat().st_mtime
        if mtime < midnight:
            continue
        for ts, tok in parse(path).values():
            if ts >= midnight:
                today += tok
            if ts >= now - RATE_WINDOW:
                recent += tok
        if now - mtime < ACTIVE_SECS:
            if path.parent.name == "subagents":
                try:
                    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
                    agents[meta.get("agentType", "agent")] += 1
                except (OSError, ValueError):
                    agents["agent"] += 1
            else:
                sessions += 1
    return today, recent * 60 // RATE_WINDOW, sessions, agents


def fmt(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def render(today, rate, sessions, agents):
    details = f"🔥 {fmt(today)} tokens · {fmt(rate)}/min"
    if agents:
        kinds = ", ".join(f"{c}× {k}" for k, c in agents.most_common(3))
        state = f"🤖 {sum(agents.values())} agents: {kinds}"
    else:
        state = f"{sessions} session{'s' * (sessions != 1)} · no subagents" if sessions else "idle"
    return details[:128], state[:128]


def claude_code_running():
    """True while any Claude Code CLI process (terminal, or the desktop app's Code tab) is alive."""
    if sys.platform == "win32":
        # skip the Claude desktop app's own Claude.exe, which lives under WindowsApps
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-Process claude -ErrorAction SilentlyContinue | ? { $_.Path -notmatch 'WindowsApps' }).Count"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        return out not in ("", "0")
    return subprocess.run(["pgrep", "-x", "claude"], capture_output=True).returncode == 0  # desktop app is "Claude"


def run(client_id):
    try:  # one instance at a time: holding this port is the lock
        lock = socket.socket()
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return
    from pypresence import Presence
    start = int(time.time())
    rpc, idle_checks = None, 0
    while idle_checks < 2:  # exit ~30s after the last Claude Code process closes
        idle_checks = 0 if claude_code_running() else idle_checks + 1
        try:
            if rpc is None:
                rpc = Presence(client_id)
                rpc.connect()
                print("\nConnected to Discord.")
            details, state = render(*snapshot())
            rpc.update(details=details, state=state, start=start,
                       large_image=ASSET, large_text="Claude Code")
            print(f"\r{details} | {state}".ljust(120), end="", flush=True)
        except Exception as e:  # Discord closed/restarted: keep retrying
            print(f"\nDiscord not reachable ({type(e).__name__}), retrying...", flush=True)
            rpc = None
        time.sleep(15)  # Discord rate-limits updates to ~1 per 15s
    if rpc:
        try:
            rpc.clear()
            rpc.close()
        except Exception:
            pass


def main():
    if sys.stdout is None:  # pythonw has no console; log to a file instead
        sys.stdout = sys.stderr = open(Path(__file__).with_name("presence.log"), "w", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    arg = sys.argv[1] if len(sys.argv) > 1 else CLIENT_ID
    if arg == "--print":
        print(*render(*snapshot()), sep="\n")
    elif arg == "--spawn":  # used by the SessionStart hook: start in the background and return immediately
        log = open(Path(__file__).with_name("presence.log"), "w")
        if sys.platform == "win32":
            exe = Path(sys.executable).with_name("pythonw.exe")
            kw = dict(creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            exe, kw = sys.executable, dict(start_new_session=True)
        subprocess.Popen([str(exe), __file__], cwd=Path(__file__).parent,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kw)
    else:
        run(arg)


if __name__ == "__main__":
    main()
