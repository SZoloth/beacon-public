"""Shared AI prompt runner with CLI fallback for Beacon."""

import json
import os
import subprocess

_CIRCUIT_THRESHOLD = 2
_consecutive_failures = {}


def reset_runner_circuit():
    """Clear per-process runner failure counts."""
    _consecutive_failures.clear()


def _runner_order():
    raw = os.environ.get("BEACON_AI_RUNNERS", "codex,claude")
    order = []
    for name in raw.split(","):
        normalized = name.strip().lower()
        if normalized and normalized not in order:
            order.append(normalized)
    return order


def _runner_command(name):
    if name == "claude":
        return ["claude", "--print", "--output-format", "text", "--tools", ""]
    if name == "codex":
        return ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"]
    return None


def _strip_code_fences(output):
    if output.startswith("```"):
        lines = output.split("\n")
        if lines and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return output


def run_json_prompt_result(prompt, timeout=120, runners=None):
    """Run a JSON-only prompt and return a structured outcome dict. `runners` overrides BEACON_AI_RUNNERS."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    errors = []
    runners = runners or _runner_order()
    available = [r for r in runners if _consecutive_failures.get(r, 0) < _CIRCUIT_THRESHOLD]
    if not available:
        available = runners

    for runner in available:
        command = _runner_command(runner)
        if not command:
            continue

        try:
            result = subprocess.run(
                command,
                input=prompt,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            _consecutive_failures[runner] = _consecutive_failures.get(runner, 0) + 1
            errors.append(f"{runner} not installed")
            continue
        except subprocess.TimeoutExpired:
            _consecutive_failures[runner] = _consecutive_failures.get(runner, 0) + 1
            errors.append(f"{runner} timed out")
            continue

        if result.returncode != 0:
            _consecutive_failures[runner] = _consecutive_failures.get(runner, 0) + 1
            detail = (result.stderr or "").strip() or f"exit {result.returncode}"
            errors.append(f"{runner} error: {detail}")
            continue

        output = _strip_code_fences((result.stdout or "").strip())
        try:
            parsed = json.loads(output)
            _consecutive_failures[runner] = 0
            return {"ok": True, "results": parsed, "errors": errors, "runner": runner}
        except json.JSONDecodeError as exc:
            _consecutive_failures[runner] = _consecutive_failures.get(runner, 0) + 1
            errors.append(f"{runner} returned invalid JSON: {exc}")

    return {"ok": False, "results": [], "errors": errors, "runner": None}


def run_json_prompt(prompt, timeout=120):
    """Run a JSON-only prompt through the first available AI CLI."""
    outcome = run_json_prompt_result(prompt, timeout=timeout)
    if not outcome["ok"]:
        for error in outcome["errors"]:
            print(f"  {error}")
    return outcome["results"]
