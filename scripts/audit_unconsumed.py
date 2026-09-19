"""Audit: find module-level names in platform_core that no other module uses.

This repo's most productive defect class is "a value or capability that exists
and is tested, but that no production path reads or writes". It has produced
AgentRun.started_at, token_usage, sweep_expired_data, credential_ref,
health_check, NEEDS_REAUTH, SyncCursor, DeadLetterItem, flag_service.evaluate,
the reranker, release_check's CI caller, drain_outbox_once, _now,
ABSTAIN_AMBIGUOUS_IDENTITY, DraftAnswer.claim_texts, and more. Run it after
adding anything new.

Usage:
    .venv/Scripts/python.exe scripts/audit_unconsumed.py
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "apps" / "api" / "src" / "platform_core"
SEARCH_ROOTS = [ROOT / "apps", ROOT / "packages", ROOT / "tests", ROOT / "scripts"]
SKIP_PARTS = {"__pycache__", ".venv", "node_modules"}

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _iter_python_files():
    seen: set[pathlib.Path] = set()
    for base in SEARCH_ROOTS:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _declared() -> dict[str, list[tuple[str, int]]]:
    """Module-level functions, classes and UPPER_CASE constants in platform_core."""
    decls: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        module = _rel(path)
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper()]
            for name in names:
                decls.setdefault(name, []).append((module, node.lineno))
    return decls


def _identifier_index() -> dict[str, tuple[set[str], int]]:
    """identifier -> (modules mentioning it, total occurrences) in one pass."""
    modules: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for path in _iter_python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        module = _rel(path)
        seen = IDENT.findall(text)
        for name, n in _count(seen).items():
            modules.setdefault(name, set()).add(module)
            counts[name] = counts.get(name, 0) + n

    return {k: (modules[k], counts[k]) for k in counts}


def _count(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        out[name] = out.get(name, 0) + 1
    return out


def main() -> int:
    decls = _declared()
    index = _identifier_index()

    never_used: list[tuple[str, list[tuple[str, int]]]] = []
    module_local: list[tuple[str, list[tuple[str, int]]]] = []

    for name, where in sorted(decls.items()):
        if name.startswith("_") or len(name) < 4:
            continue
        mods, total = index.get(name, (set(), 0))
        # Every declaration line mentions the name once; anything beyond that is
        # a real reference. Zero extra occurrences means nothing consumes it.
        extra = total - len(where)
        defmods = {m for m, _ in where}
        if extra <= 0:
            never_used.append((name, where))
        elif not (mods - defmods):
            module_local.append((name, where))

    print("== declared and never referenced at all ==")
    if never_used:
        for name, where in never_used:
            locs = ", ".join(f"{m}:{n}" for m, n in where)
            print(f"  {name}  <- {locs}")
    else:
        print("  none")

    print(f"\n== referenced only inside the defining module ({len(module_local)}) ==")
    print("   (usually legitimate: in-module helpers, Pydantic models bound by FastAPI,")
    print("    names reached through strings/reflection. Listed for completeness.)")
    for name, where in module_local[:40]:
        locs = ", ".join(f"{m}:{n}" for m, n in where)
        print(f"  {name}  <- {locs}")
    if len(module_local) > 40:
        print(f"  ... and {len(module_local) - 40} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
