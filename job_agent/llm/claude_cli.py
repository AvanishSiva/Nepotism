"""Headless Claude Code (`claude -p`). Uses the Pro subscription: call sparingly."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile

log = logging.getLogger(__name__)


class ClaudeError(Exception):
    pass


class ClaudeUsageLimit(ClaudeError):
    pass


# Neutral working dir: no project CLAUDE.md / settings get loaded into the call.
_CWD = tempfile.mkdtemp(prefix="job_agent_claude_")


def run(prompt: str, *, binary: str = "claude", model: str | None = None, system: str | None = None,
        timeout_s: float = 120, retries: int = 1) -> str:
    """Return the `result` text from `claude -p --output-format json` (prompt on stdin).

    Locked down: no tools, no MCP servers, no saved session, neutral cwd.
    """
    cmd = [binary, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    if system:
        cmd += ["--system-prompt", system]
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=timeout_s, cwd=_CWD)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            last = e
            continue
        out = (p.stdout or "") + (p.stderr or "")
        try:
            env = json.loads(p.stdout)
        except ValueError:
            env = None
        failed = p.returncode != 0 or env is None or env.get("is_error")
        # Only inspect error output for limits: a successful reply may legitimately
        # mention "rate limit" (e.g. a JD about API rate limiting).
        if failed and re.search(r"usage limit|rate limit|limit reached|quota", out, re.I):
            raise ClaudeUsageLimit(out[:300])
        if env is None:
            last = ClaudeError(f"bad envelope (rc={p.returncode}): {out[:200]}")
            continue
        if env.get("is_error"):
            last = ClaudeError(str(env.get("result"))[:300])
            continue
        try:
            return env["result"]
        except KeyError as e:
            last = ClaudeError(f"bad envelope: {e}")
    raise ClaudeError(str(last))


def run_json(prompt: str, **kw) -> dict:
    """Like run(), but parses the first JSON object in the reply."""
    text = run(prompt + "\n\nReply with ONLY a JSON object.", **kw)
    start = text.find("{")
    if start < 0:
        raise ClaudeError(f"no JSON in reply: {text[:200]}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])   # first object only; ignore trailing text
    return obj
