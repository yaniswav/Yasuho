#!/usr/bin/env python3
"""Generate requirements.lock: the fully-resolved RUNTIME dependency closure.

Walks the transitive dependency tree of every top-level package declared in
requirements.txt (honouring extras such as sonolink[speed]) and prints an exact
`Name==version` pin for each, using the versions currently installed in the
active interpreter. Dev/CI-only tooling is intentionally not a root, so it never
appears in the lock.

Usage (from the project venv):
    ./.venv/bin/python tools/gen_lock.py > requirements.lock
"""

from __future__ import annotations

import importlib.metadata as md

from packaging.requirements import Requirement

# Top-level runtime roots (canonical names) with the extras we install.
# Mirror requirements.txt; update here when a runtime top-level dep is added.
RUNTIME_ROOTS: dict[str, set[str]] = {
    "aiohttp": set(),
    "async-timeout": set(),
    "asyncpg": set(),
    "discord.py": set(),
    "pynacl": set(),
    "python-levenshtein": set(),
    "requests": set(),
    "sonolink": {"speed"},
    "pillow": set(),
    "pyfiglet": set(),
    "topggpy": set(),
    "parsedatetime": set(),
    "python-dateutil": set(),
    "wikipedia": set(),
    "lyricsgenius": set(),
    "cryptography": set(),
    "babel": set(),
}

HEADER = """\
# Yasuho - fully-resolved RUNTIME dependency lock (generated, do not edit by hand)
#
# What this is:
#   Exact, transitively-resolved pins of every package the bot imports at
#   runtime - the full dependency closure of requirements.txt (including the
#   sonolink[speed] extra). It exists for reproducibility and as an audit
#   record; the security floors that motivated this file are Pillow >= 12.3.0
#   and PyNaCl >= 1.6.2.
#
# What this is NOT:
#   The install source. run.sh and setup.sh install from requirements.txt (the
#   human-maintained ~= pins), NOT from this lock - so the lock never affects
#   the auto-update path and a stale lock can never crashloop a restart. Dev and
#   CI-only tooling (pytest, pytest-asyncio, pytest-cov, ruff, pip-audit) is
#   deliberately excluded: it is not part of the deployed bot.
#
# How to regenerate (after any requirements.txt bump), from the project venv:
#   ./.venv/bin/python tools/gen_lock.py > requirements.lock
#
# Resolved for: CPython 3.13 on Linux x86_64.\
"""


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def resolve() -> dict[str, str]:
    dist_names = {
        _norm(d.metadata["Name"]): d.metadata["Name"] for d in md.distributions()
    }
    resolved: dict[str, str] = {}
    visited: dict[str, set[str]] = {}

    def walk(name: str, extras: set[str]) -> None:
        cn = _norm(name)
        if cn in visited and extras <= visited[cn]:
            return
        try:
            dist = md.distribution(dist_names.get(cn, name))
        except md.PackageNotFoundError:
            return
        visited[cn] = visited.get(cn, set()) | extras
        resolved[dist.metadata["Name"]] = dist.version
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None:
                envs = [{"extra": e} for e in extras] or []
                envs.append({"extra": ""})
                if not any(req.marker.evaluate(env) for env in envs):
                    continue
            walk(req.name, set(req.extras))

    for root, extras in RUNTIME_ROOTS.items():
        walk(root, extras)
    return resolved


def main() -> None:
    print(HEADER)
    for name, version in sorted(resolve().items(), key=lambda kv: kv[0].lower()):
        print(f"{name}=={version}")


if __name__ == "__main__":
    main()
