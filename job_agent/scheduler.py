"""launchd schedule for macOS (spec §11, milestone 10).

Agents (~/Library/LaunchAgents/com.nepotism.jobagent.*.plist), all in your login session:
  run-all   daily 08:00     discover → score → referrals → draft → digest
                            (LinkedIn steps only run inside the 08:00–10:30 weekday window)
  send      Mon–Fri 08:40   Lane B sender, self-paced, stops at 10:30 / caps (dry-run unless live)
  track     every 2 h       acceptance emails → Lane A drafts, follow-ups, Gmail token check
  bot       always on       Telegram commands (long polling)

If the Mac is asleep at a calendar time, launchd runs the job when it wakes; the LinkedIn
window still applies, so a late wake never browses LinkedIn outside 08:00–10:30.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from job_agent.db import PROJECT_ROOT

PREFIX = "com.nepotism.jobagent"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
WEEKDAYS = [1, 2, 3, 4, 5]


def _path_env() -> str:
    dirs = [str(Path(sys.executable).parent)]
    for tool in ("claude", "ollama"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def jobs() -> dict[str, dict]:
    return {
        "run-all": {"args": ["run-all"], "when": {"StartCalendarInterval": {"Hour": 8, "Minute": 0}}},
        "send": {"args": ["send"],
                 "when": {"StartCalendarInterval": [{"Weekday": d, "Hour": 8, "Minute": 40} for d in WEEKDAYS]}},
        "track": {"args": ["track"], "when": {"StartInterval": 7200, "RunAtLoad": False}},
        "bot": {"args": ["bot", "--loop"], "when": {"RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30}},
    }


def plist(name: str, spec: dict) -> dict:
    logs = PROJECT_ROOT / "logs"
    d = {
        "Label": f"{PREFIX}.{name}",
        "ProgramArguments": [sys.executable, "-m", "job_agent", *spec["args"]],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"PATH": _path_env(), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / f"launchd-{name}.log"),
        "StandardErrorPath": str(logs / f"launchd-{name}.log"),
        "ProcessType": "Background",
    }
    d.update(spec["when"])
    return d


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install() -> list[str]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    uid = os.getuid()
    done = []
    for name, spec in jobs().items():
        label = f"{PREFIX}.{name}"
        path = AGENTS_DIR / f"{label}.plist"
        _launchctl("bootout", f"gui/{uid}/{label}")          # replace if already loaded
        with open(path, "wb") as f:
            plistlib.dump(plist(name, spec), f)
        r = _launchctl("bootstrap", f"gui/{uid}", str(path))
        done.append(f"{label}: {'loaded' if r.returncode == 0 else 'FAILED ' + (r.stderr or r.stdout).strip()}")
    return done


def uninstall() -> list[str]:
    uid = os.getuid()
    out = []
    for name in jobs():
        label = f"{PREFIX}.{name}"
        _launchctl("bootout", f"gui/{uid}/{label}")
        path = AGENTS_DIR / f"{label}.plist"
        if path.exists():
            path.unlink()
        out.append(f"{label}: removed")
    return out


def status() -> list[str]:
    uid = os.getuid()
    out = []
    for name in jobs():
        label = f"{PREFIX}.{name}"
        r = _launchctl("print", f"gui/{uid}/{label}")
        if r.returncode != 0:
            out.append(f"{name:<8} not installed")
            continue
        info = {k.strip(): v.strip() for k, _, v in (ln.partition(" = ") for ln in r.stdout.splitlines()) if _}
        out.append(f"{name:<8} {info.get('state', '?'):<12} runs={info.get('runs', '?'):<4} "
                   f"last exit={info.get('last exit code', '-')}")
    return out
