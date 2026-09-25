"""Every sidebar entry has a route, and every route is reachable from the menu.

The defect this pins
--------------------
`pages/Agents.tsx` was written, exported cleanly, and had no route, no lazy
import and no sidebar entry. `tsc -b` passed, `vite build` passed, and the
bundle did not contain the page at all. Nothing failed, because every check in
the repository answers "does this code compile?" and none of them asked "can an
operator actually get to this?"

The route table in `main.tsx` carries a comment claiming it mirrors the sidebar.
Comments do not enforce anything, and this is the second time a hand-maintained
pair of lists has been able to drift.

What is asserted
----------------
Parsed out of the source rather than imported: the app is a browser bundle and
`main.tsx` mounts on import, so a test that imports it needs a DOM. The parse is
deliberately crude - a route string and a nav `to` string - and that is enough
because the failure being guarded is a missing string, not a subtle one.
"""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[3] / "admin-web" / "src"
MAIN = WEB / "main.tsx"
LAYOUT = WEB / "components" / "Layout.tsx"


def _string_literals(body: str, key: str) -> set[str]:
    """Every value assigned to `key`, in either quote style.

    Measured: a route written as `` path: `agents` `` instead of
    `path: "agents"` - a legitimate style, and one this file's own prose has
    used throughout - made the guard report `/admin/agents` as having no route.
    The route was there. The parser only understood double quotes.

    So the pattern accepts a backtick as readily as a quote. It is still a
    regex over source, deliberately: a route table *is* a text manifest, and the
    alternative - mounting the app in a DOM and reading `createBrowserRouter`'s
    routes - is a far larger change for a check that already catches the defect
    it was written for. What is not acceptable is a parser that fails on a
    spelling choice, because that is a guard which cries wolf and gets deleted.
    """
    return set(re.findall(rf"{key}\s*:\s*[`\"]([^`\"]+)[`\"]", body))


def _route_paths() -> set[str]:
    source = MAIN.read_text(encoding="utf-8")
    body = source.split("const OPERATOR_PAGES = [", 1)[1].split("\n];", 1)[0]
    return _string_literals(body, "path")


def _nav_paths() -> set[str]:
    source = LAYOUT.read_text(encoding="utf-8")
    # NAV is the full expanded menu. PRIMARY is deliberately a shortlist for
    # the narrow rail, so it is not part of the contract.
    body = source.split("const NAV = [", 1)[1].split("\n] as const", 1)[0]
    return {p for p in _string_literals(body, "to") if p.startswith("/admin")}


def test_every_menu_entry_has_a_route() -> None:
    """A link to nowhere is a broken console, and the sidebar is the only map."""
    routes = {f"admin/{p}" for p in _route_paths()}
    missing = sorted(p for p in _nav_paths() if p.lstrip("/") not in routes)
    assert not missing, f"sidebar entries with no route: {missing}"


def test_every_route_is_in_the_menu() -> None:
    """The other direction, and the one that actually bit.

    A page with a route but no menu entry is reachable only by typing the URL,
    which is how the roster page would have stayed invisible even after the
    route was added. Both directions are asserted because either one alone
    leaves a console that looks complete and is not.
    """
    # `OPERATOR_PAGES` entries are relative ("quality") and the router prefixes
    # them with `admin/`, so the menu's absolute "/admin/quality" is the thing
    # to compare against.
    routes = {f"/admin/{p}" for p in _route_paths() if not p.startswith("workbench")}
    missing = sorted(p for p in routes if p not in _nav_paths())
    assert not missing, f"routes with no sidebar entry: {missing}"


def test_every_page_module_is_imported_somewhere() -> None:
    """The cheapest guard against dead page code.

    A page that nothing imports bundles to nothing, passes the build, and
    cannot be found. This catches it in a second, without a browser.
    """
    sources = "\n".join(p.read_text(encoding="utf-8") for p in WEB.rglob("*.ts*") if p.is_file())
    orphans = sorted(
        p.stem for p in (WEB / "pages").glob("*.tsx") if p.stem not in sources.replace(str(p), "")
    )
    assert not orphans, f"page modules nothing references: {orphans}"
