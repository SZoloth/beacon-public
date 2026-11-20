"""Where this install keeps things: the config file and the machine-specific paths in it.

Nothing personal is hardcoded here. A checkout uses `config.json` when it has one and
`config.example.json` otherwise; `BEACON_CONFIG` points somewhere else. Machine paths live in the
config's optional `paths` block. A path that is not set turns its feature off rather than guessing.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keys a `paths` block may hold, and what each one switches on.
PATH_KEYS = {
    "live_db": "the one database allowed to write backlog files, the reading inbox and Reader",
    "reading_inbox": "the markdown file human reads are appended to",
    "projects_dir": "the folder `beacon discover` scans for local projects",
    "systems_manifest": "a JSON manifest `beacon discover` reads project details from",
    "context_script": "a script that returns personal background for area follow-ups",
    "mymind_cli": "a mymind-cli checkout `beacon sync --source mymind` reads the owner's tags and notes through",
    "readwise_cli": "the readwise CLI `beacon sync --source readwise-notes` reads highlight notes and tags through",
    "granola_digests": "the folder of daily Granola idea digests `beacon sync --source granola-digests` reads",
}


def config_path():
    override = os.environ.get("BEACON_CONFIG")
    if override:
        return os.path.expanduser(override)
    own = os.path.join(ROOT, "config.json")
    return own if os.path.exists(own) else os.path.join(ROOT, "config.example.json")


def load(path=None):
    try:
        with open(path or config_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def path(key, config=None):
    """The configured path for `key`, with ~ expanded, or None when it is not set."""
    if key not in PATH_KEYS:
        raise KeyError(key)
    value = ((config if config is not None else load()).get("paths") or {}).get(key)
    return os.path.expanduser(value) if isinstance(value, str) and value.strip() else None


def owner(config=None):
    """The person Beacon works for, as prompts name them. Set `owner.name` in config."""
    value = ((config if config is not None else load()).get("owner") or {}).get("name")
    return value.strip() if isinstance(value, str) and value.strip() else "the user"


def owner_about(config=None):
    """One line on who the owner is and what they read, for the classifier. Optional."""
    value = ((config if config is not None else load()).get("owner") or {}).get("about")
    return value.strip() if isinstance(value, str) and value.strip() else ""
