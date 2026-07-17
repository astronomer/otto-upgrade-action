"""Extract Otto's structured upgrade result from its --mode json stdout stream.

Symmetric to otto-review-action's extract-verdict.py. ``astro otto`` is run with
``--output-schema`` (scripts/upgrade-schema.json), which registers a synthetic
``submit_final_answer`` tool whose argument is the structured result. The exact
event envelope depends on the runtime version, so we look in the same places
the review action does before falling back to the largest balanced JSON object
matching the required keys.

Expected result shape (scripts/upgrade-schema.json):
  {
    "summary": "...",                 # one-line description of what changed
    "changes_made": ["..."],          # edits/decisions about the user's code
    "manual_followups": ["..."],      # things a human must finish
    "files_changed": ["..."]          # paths the agent edited
  }

Reads JSONL on stdin, writes the extracted result object as JSON on stdout.
Empty output signals "no result found".
"""

from __future__ import annotations

import json
import sys
from typing import Any

REQUIRED_KEYS = {"summary", "changes_made", "manual_followups"}
FINAL_TOOL_NAMES = {"submit_final_answer", "final_answer", "submit_final"}
FINAL_EVENT_TYPES = {"final_result", "result", "submit_final_answer", "final_answer", "agent_result"}


def looks_like_result(obj: Any) -> bool:
    return isinstance(obj, dict) and REQUIRED_KEYS.issubset(obj.keys())


def candidate_payloads(event: dict[str, Any]) -> list[Any]:
    keys = ("result", "output", "answer", "input", "arguments", "parameters", "value", "data")
    return [event[k] for k in keys if k in event]


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def find_in_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for ev in events:
        ev_type = (ev.get("type") or ev.get("event") or "").lower()
        tool_name = (ev.get("tool") or ev.get("name") or ev.get("toolName") or "").lower()
        if ev_type in FINAL_EVENT_TYPES or tool_name in FINAL_TOOL_NAMES:
            for payload in candidate_payloads(ev):
                if looks_like_result(payload):
                    return payload
                if isinstance(payload, dict):
                    for v in payload.values():
                        if looks_like_result(v):
                            return v
                if isinstance(payload, str):
                    try:
                        parsed = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if looks_like_result(parsed):
                        return parsed
    for ev in events:
        for payload in candidate_payloads(ev):
            if looks_like_result(payload):
                return payload
            if isinstance(payload, dict):
                for v in payload.values():
                    if looks_like_result(v):
                        return v
    return None


def extract_balanced_json(text: str) -> dict[str, Any] | None:
    candidates: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, c in enumerate(text):
        if escape:
            escape = False
            continue
        if in_str and c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append((start, i + 1))
                    start = -1
    best: dict[str, Any] | None = None
    best_len = 0
    for s, e in candidates:
        try:
            obj = json.loads(text[s:e])
        except json.JSONDecodeError:
            continue
        if looks_like_result(obj) and (e - s) > best_len:
            best, best_len = obj, e - s
    return best


def main() -> int:
    raw = sys.stdin.read()
    result = find_in_events(parse_jsonl(raw)) or extract_balanced_json(raw)
    if result is None:
        return 0
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
