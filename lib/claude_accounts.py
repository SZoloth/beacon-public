"""Which Claude subscription a headless `claude` run uses. When one hits its usage limit, the next takes over.

Config `claude_accounts` lists them in order. "default" is the login Claude Code uses with no CLAUDE_CONFIG_DIR;
any other entry is a CLAUDE_CONFIG_DIR. An account that hit a limit waits COOLDOWN before it is tried first again,
across processes (`.claude-limits.json` beside the database), so each weekly stage doesn't spend a call rediscovering it.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import db, settings

DEFAULT = "default"
COOLDOWN = 6 * 3600
LIMIT = re.compile(r"hit your .{0,20}limit|usage limit|out of usage credits", re.IGNORECASE)
STATE = None  # beside the database, so a test database never sees the live limit record


def accounts():
    listed = settings.load().get("claude_accounts")
    found = [entry.strip() for entry in listed if isinstance(entry, str) and entry.strip()] if isinstance(listed, list) else []
    return found or [DEFAULT]


def _state_path():
    return STATE or Path(db.current_path()).parent / ".claude-limits.json"


def _cooling():
    try:
        state = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {account: until for account, until in state.items() if isinstance(until, (int, float)) and until > now} \
        if isinstance(state, dict) else {}


def mark_limited(account):
    state = _cooling() | {account: time.time() + COOLDOWN}
    try:
        path = _state_path()
        scratch = path.with_suffix(".tmp")
        scratch.write_text(json.dumps(state))
        scratch.replace(path)
    except OSError:
        pass  # without the record, the next process spends one call finding the limit again


def ordered():
    """Every account, the ones cooling down last: when all are limited, each is still tried."""
    cooling, listed = _cooling(), accounts()
    return [account for account in listed if account not in cooling] + [account for account in listed if account in cooling]


def env_for(account, env):
    env = dict(env)
    if account == DEFAULT:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(account)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # a token in the environment would sign in as the default account
    return env


def hit_limit(result):
    """True when a finished `claude` run reports a usage limit instead of an answer."""
    out = result.stdout or ""
    try:
        payload = json.loads(out)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        text = str(payload.get("result") or "") if payload.get("is_error") else ""
    else:
        text = out if len(out) < 400 else ""  # an answer can mention limits; a limit notice is short
    return bool(LIMIT.search(text) or LIMIT.search(result.stderr or ""))


def run(command, *, env, run=subprocess.run, **kwargs):
    """`run(command)` on the first account not at its limit. Callers pass their own `subprocess.run`."""
    result = None
    for account in ordered():
        result = run(command, env=env_for(account, env), **kwargs)
        if not hit_limit(result):
            return result
        mark_limited(account)
    return result
