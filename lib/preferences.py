"""Explicit, expiring reference-selection preferences; never a learned user profile."""
import hashlib
import json
import re
from datetime import datetime, timezone

FIELDS = {"version", "id", "revision", "purpose", "consumers", "confirmed", "expires_at", "statements"}


def read(path, *, consumer="beacon", now=None):
    with open(path, "rb") as handle:
        raw = handle.read(8193)
    if len(raw) > 8192:
        raise ValueError("Preference file exceeds 8 KiB.")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["revision"]) is not int or value["revision"] < 1
            or value["confirmed"] is not True or value["purpose"] != "reference-selection"
            or not isinstance(value["id"], str) or not 1 <= len(value["id"].strip()) <= 128
            or not isinstance(value["consumers"], list) or not value["consumers"]
            or any(not isinstance(c, str) or c not in {"beacon", "mosaic"} for c in value["consumers"])
            or len(set(value["consumers"])) != len(value["consumers"]) or consumer not in value["consumers"]
            or not isinstance(value["statements"], list) or not 1 <= len(value["statements"]) <= 4
            or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 300 for s in value["statements"])
            or not isinstance(value["expires_at"], str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["expires_at"])):
        raise ValueError("Use a confirmed v1 reference-selection profile for this consumer.")
    expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
    if expires <= (now or datetime.now(timezone.utc)):
        raise ValueError("Preference profile expired; reconfirm before reuse.")
    return {**value, "sha256": hashlib.sha256(raw).hexdigest()}
