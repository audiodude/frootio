"""Entry point for GPU/CPU-heavy work, run in a subprocess by `jobs.run_heavy`.

    python -m frootio.heavy <task> '<json args>'

The task's return value (a JSON object) is the only thing written to the
original stdout; everything else the task (or its native libraries) prints is
redirected to stderr. Tasks are registered by dotted path so the subprocess
only imports what the requested task needs (e.g. torch for stems).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable
from typing import Any

# task name -> "module:function"; function(args: dict) -> dict
TASKS: dict[str, str] = {
    "echo": "frootio.heavy:echo",
    "align": "frootio.captions:run_align",
    "stems": "frootio.stems_task:run",
}


def echo(args: dict[str, Any]) -> dict[str, Any]:
    """Built-in no-op task: returns its arguments (used to test the runner)."""
    print("echo task running", flush=True)  # proves stdout noise is kept off the result
    return {"echo": args, "pid": os.getpid()}


def resolve(name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    try:
        target = TASKS[name]
    except KeyError:
        raise SystemExit(f"unknown heavy task {name!r}; known: {', '.join(sorted(TASKS))}") from None
    module_name, func_name = target.split(":")
    return getattr(importlib.import_module(module_name), func_name)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m frootio.heavy <task> <json-args>", file=sys.stderr)
        return 2
    name, raw = argv
    func = resolve(name)
    args = json.loads(raw)
    if not isinstance(args, dict):
        print("heavy task args must be a JSON object", file=sys.stderr)
        return 2

    # Keep the real stdout for the result only; route fd 1 (Python and C) to stderr.
    sys.stdout.flush()
    result_fd = os.dup(1)
    os.dup2(2, 1)
    result = func(args)
    sys.stdout.flush()
    with os.fdopen(result_fd, "w") as out:
        json.dump(result, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
