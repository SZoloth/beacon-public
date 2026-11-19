"""Read-only save review against explicitly supplied, dated context."""
import hashlib
import html
import json
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import review, useful

BATCH_POLICY = "save-review-batch-v1"


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} requires 1–{limit} characters.")
    return value.strip()


def _time(value):
    return useful.parse_utc(value, "Context dates must be timezone-aware ISO timestamps.")


def context_packet(raw, now):
    if not isinstance(raw, dict) or set(raw) != {"version", "task", "conditions", "efforts"} or type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("Use a version-1 task/conditions/efforts packet.")
    useful.task_terms(raw["task"])
    result = {"task": raw["task"], "conditions": [], "efforts": [], "excluded": []}
    for kind in ("conditions", "efforts"):
        rows = raw[kind]
        if not isinstance(rows, list) or len(rows) > 8:
            raise ValueError("Supply at most eight conditions and eight efforts.")
        seen = set()
        for row in rows:
            fields = {"id", "project", "owner", "source_ref", "observed_at", "expires_at", "when" if kind == "conditions" else "summary"}
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError(f"Invalid {kind} fields.")
            row = {key: _text(value, key, 1600 if key in {"when", "summary"} else 500) for key, value in row.items()}
            if row["id"] in seen:
                raise ValueError("Context IDs must be unique within each kind.")
            seen.add(row["id"])
            observed, expires = _time(row["observed_at"]), _time(row["expires_at"])
            if expires <= observed:
                raise ValueError("Context expiry must follow observation.")
            if not observed <= now < expires or now - observed > timedelta(days=7):
                result["excluded"].append({"kind": kind, "id": row["id"], "reason": "stale_or_future_snapshot"})
            else:
                result[kind].append(row)
    return result


def build_request(item, context):
    questions = {}
    for i, _ in enumerate(context["conditions"]):
        questions[f"when_{i}"] = {"type": "noul", "instructions": review.UNTRUSTED +
            f"Does state.item explicitly describe a capability useful when state.conditions[{i}].when occurs? This is a future-use fit check, not evidence that the condition is currently met. Shared topic words are insufficient."}
    for i, _ in enumerate(context["efforts"]):
        questions[f"collision_{i}"] = {"type": "choice", "criteria": review.RELATIONSHIPS,
            "instructions": review.UNTRUSTED + f"Compare the documented capability of state.item with the work in state.efforts[{i}].summary for state.task. Duplicate means potentially the same effort, not proof to merge projects. Alternative means overlapping intent with material differences. Missing evidence is insufficient."}
    state = {"task": context["task"], "item": review._source(item),
             "conditions": [{"project": r["project"], "when": r["when"]} for r in context["conditions"]],
             "efforts": [{"project": r["project"], "summary": r["summary"]} for r in context["efforts"]]}
    return review._request(state, questions)


def interpret(item, context, request, response=None):
    answers = review._answers(request, response) if response is not None else {}
    eligible = [(answers.get(f"when_{i}", {}).get("noul", 0), r) for i, r in enumerate(context["conditions"])]
    eligible.sort(key=lambda row: (-row[0], row[1]["id"]))
    supported = bool(eligible and eligible[0][0] >= .8 and (len(eligible) == 1 or eligible[0][0] - eligible[1][0] >= .15))
    future = {"state": "proposed" if supported else "hold", "condition": eligible[0][1] if supported else None,
              "text": f"You will want this when {eligible[0][1]['when']}" if supported else None,
              "fit": eligible[0][0] if response is not None and eligible else None,
              "currently_met": "not_evaluated", "routing_allowed": False}
    collisions = []
    for i, target in enumerate(context["efforts"]):
        answer = answers.get(f"collision_{i}")
        confident = answer and review._certain(answer) and answer["choice"] != "insufficient"
        collisions.append({"target": target, "judgment": answer, "relationship": answer["choice"] if confident else "unassessed",
                           "review_with_owner": target["owner"], "merge_allowed": False})
    return {**review._metadata(request, response), "item": item, "future_use": future, "collisions": collisions,
            "excluded_context": context["excluded"], "context_authority": "supplied assertions; not independently verified",
            "note": "No route, merge, schedule, notification or source lifecycle change. Recheck the owner and current source before action."}


def _revision(path, item_id):
    with closing(review._connect(path)) as conn:
        row = conn.execute("SELECT id,source,external_id,title,body,url,author,status FROM items WHERE id=?", (item_id,)).fetchone()
    return hashlib.sha256(json.dumps(dict(row) if row else None, sort_keys=True).encode()).hexdigest()


def generate(item_id, db_path, context_path, *, provider="local", preview=False, allow_upload=False, clock=None):
    if provider not in {"local", "typesafe"} or (preview and provider != "typesafe"):
        raise ValueError("Use local or typesafe; preview requires typesafe.")
    path = Path(context_path)
    with path.open("rb") as handle:
        data = handle.read(24001)
    if len(data) > 24000:
        raise ValueError("Context packet exceeds 24 KB.")
    raw = json.loads(data)
    clock = clock or (lambda: datetime.now(timezone.utc))
    context = context_packet(raw, clock())
    revision = _revision(db_path, item_id)
    item = review.read_items(db_path, item_ids=[item_id])[0]
    request = build_request(item, context)
    if preview:
        return {"mode": "preview", "request": request, "excluded_context": context["excluded"], "network_calls": 0, "database_writes": 0}
    response = useful.call_typesafe(request, allow_upload=allow_upload) if request and provider == "typesafe" else None
    with path.open("rb") as handle:
        current_data = handle.read(24001)
    if revision != _revision(db_path, item_id) or data != current_data or context != context_packet(raw, clock()):
        raise ValueError("Source or context changed/expired during review; discard the result.")
    return interpret(item, context, request, response)


def generate_many(item_ids, db_path, context_path, *, preview=False, allow_upload=False,
                  replay=None, max_calls=20, clock=None):
    """Batch independent save judgments; preserve each item's original interpretation."""
    useful._limit(len(item_ids), 20)
    useful._limit(max_calls, 20)
    if replay is not None and (preview or allow_upload):
        raise ValueError("Replay cannot be combined with preview or upload.")
    path = Path(context_path)
    with path.open("rb") as handle:
        raw_bytes = handle.read(24001)
    if len(raw_bytes) > 24000:
        raise ValueError("Context packet exceeds 24 KB.")
    raw = json.loads(raw_bytes)
    clock = clock or (lambda: datetime.now(timezone.utc))
    context = context_packet(raw, clock())
    revisions = {item_id: _revision(db_path, item_id) for item_id in item_ids}
    items = review.read_items(db_path, item_ids=item_ids)
    entries = [{"item": item, "request": build_request(item, context)} for item in items]
    def packet(batch):
        states, questions = [], {}
        for i, entry in enumerate(batch):
            request = entry["request"]
            states.append({**request["state"], "source_revision": revisions[entry["item"]["id"]]})
            for key, question in request["questions"].items():
                questions[f"item_{entry['item']['id']}_{key}"] = {
                    **question, "instructions": question["instructions"].replace("state.", f"reviews[{i}].")}
        return review._request({"reviews": states, "policy": BATCH_POLICY}, questions)
    batches = useful.pack_batches([e for e in entries if e["request"] is not None], packet)
    requests = [packet(batch) for batch in batches]
    if len(requests) > max_calls:
        raise ValueError("Save review exceeds max_calls; no requests sent.")
    if replay is not None:
        review.validate_replay(requests, replay)
    result = {"mode": "batched_save_review", "requests": requests if preview else [],
              "request_hashes": [review.request_hash(r) for r in requests], "reviews": [],
              "network_calls": 0, "replayed_calls": 0, "database_writes": 0,
              "automation_allowed": False, "usage": {"input_tokens": 0, "output_tokens": 0}}
    responses, batch_hashes = {}, {}
    for index, (batch, request) in enumerate(zip(batches, requests)):
        if preview or not (allow_upload or replay is not None):
            continue
        if any(revisions[i] != _revision(db_path, i) for i in item_ids):
            error = ValueError("Source changed before batch dispatch.")
            error.network_calls = result["network_calls"]
            raise error
        result["replayed_calls" if replay is not None else "network_calls"] += 1
        try:
            response = replay[index]["response"] if replay is not None else useful.call_typesafe(request, allow_upload=allow_upload)
            answers = review._answers(request, response)
        except (ValueError, OSError) as error:
            error.network_calls = result["network_calls"]
            raise
        for key in result["usage"]:
            result["usage"][key] += response["usage"][key]
        for entry in batch:
            item_id = entry["item"]["id"]
            batch_hashes[item_id] = review.request_hash(request)
            responses[item_id] = {"model": response["model"],
                "answers": {key: answers[f"item_{item_id}_{key}"] for key in entry["request"]["questions"]},
                "usage": {"input_tokens": 0, "output_tokens": 0}}
    with path.open("rb") as handle:
        current_bytes = handle.read(24001)
    if (current_bytes != raw_bytes or context != context_packet(raw, clock())
            or any(revisions[i] != _revision(db_path, i) for i in item_ids)):
        error = ValueError("Source or context changed/expired during batch; no reviews accepted.")
        error.network_calls = result["network_calls"]
        raise error
    for entry in entries:
        result["reviews"].append({**interpret(entry["item"], context, entry["request"], responses.get(entry["item"]["id"])),
                                  "live_model_calls": 0,
                                  "request_sha256": batch_hashes.get(entry["item"]["id"]),
                                  "source_revision": revisions[entry["item"]["id"]],
                                  "usage_scope": "Shared batch usage is accounted once in the outer result."})
    return result


def render_html(result):
    esc = lambda s: html.escape(str(s), quote=True)
    cards = result.get("cards", [result["item"]] if "item" in result else [])
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Beacon review</title><main><h1>Beacon review</h1><p>Read-only advice. Nothing was routed, merged, deleted or scheduled.</p>"]
    if profile := result.get("preferences"):
        parts.append(f"<h2>Explicit preferences</h2><p>{esc(profile['id'])} · revision {esc(profile['revision'])} · expires {esc(profile['expires_at'])}</p>")
        parts.extend(f"<p>{esc(statement)}</p>" for statement in profile["statements"])
    for card in cards:
        parts.append(f"<article><h2>{esc(card['title'])}</h2><p>{esc(card.get('action', 'save review'))}</p><blockquote>{esc(card['excerpt'])}</blockquote>")
        url = card.get("url") or ""
        parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password:
            parts.append(f"<a href='{esc(url)}' rel='noreferrer'>Open saved source</a>")
        parts.append("</article>")
    if "future_use" in result:
        future = result["future_use"]
        parts.append(f"<h2>Future use</h2><p>{esc(future['text'] or 'Hold: no supported authored condition.')}</p><p>Condition currently met: not evaluated. Automatic routing: off.</p>")
        if future["condition"]:
            parts.append(f"<p>Authored context: {esc(future['condition']['owner'])}; {esc(future['condition']['source_ref'])}</p>")
        for collision in result["collisions"]:
            target = collision["target"]
            parts.append(f"<h2>{esc(collision['relationship'])}: {esc(target['project'])}</h2><p>{esc(target['summary'])}</p><p>Review with {esc(target['owner'])}; source {esc(target['source_ref'])}; expires {esc(target['expires_at'])}. No merge authorized.</p>")
        parts.append(f"<p>{len(result['excluded_context'])} stale/future context entries excluded.</p>")
    else:
        parts.append(f"<p>{len(result.get('review_ids', []))} need review; {len(result.get('ignored_ids', []))} not useful for this task but still saved; {result.get('additional_match_count', result.get('additional_candidate_count', 0))} more eligible; {result.get('unexamined_count', 0)} unexamined.</p>")
    return "\n".join([*parts, "</main></html>"])
