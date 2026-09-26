"""Operator state that a colleague cannot be sent a link to is state you lose.

The gap
-------
The workbench already put `caseId` and `conversationRef` in the path, so half
the page was linkable and half was not. Which conversation a replay was
showing, which queue tab the workbench was on, and what had been typed into the
queue search were all component state: lost on refresh, absent from a pasted
link. An operator could share a case but not "my queue, filtered to this
customer", which is the view they actually want a second opinion on.

What is asserted
----------------
- Both pages route that state through the one hook, rather than each growing its
  own `useSearchParams` call with its own idea of when to push history.
- The hook keeps the four decisions that are easy to re-decide differently:
  replace-not-push by default, empty means remove rather than write `?q=`, and
  a value outside the known set falls back instead of being rendered.

The last one matters most. `?tab=whatever` is one hand-edit away, and a page
that renders a state no code path handles shows an empty panel with no
explanation - which reads as "there are no cases", not "that link is wrong".
"""

from __future__ import annotations

import json
import pathlib

WEB = pathlib.Path(__file__).resolve().parents[3] / "admin-web" / "src"
HOOK = WEB / "lib" / "urlState.ts"
# `WEB` ends in `.../admin-web/src`, so its parent is the package root. Named
# rather than recomputed inline, because getting this wrong produced a
# FileNotFoundError on a path that reads correctly - the kind of failure that
# looks like a missing file rather than a wrong one.
ADMIN_WEB = WEB.parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_hook_exists_and_both_pages_in_scope_use_it() -> None:
    """The hook is the mechanism *for these two pages*.

    Scoped deliberately. Six other pages (`Cases`, `Customers`, `GapQueue`,
    `Knowledge`, `Landing`, `SupportNotFound`) already call `useSearchParams`
    directly, and the first version of this test asserted that none of them may -
    which would have been a false claim about the codebase, and would have turned
    a scoped change into an unscheduled rewrite of six pages nobody had looked
    at. What is pinned is that the two pages in scope stopped growing their own
    idea of the behaviour.
    """
    assert HOOK.is_file(), "lib/urlState.ts is missing"
    for name in ("Conversations", "Workbench"):
        source = _read(WEB / "pages" / f"{name}.tsx")
        assert "useSearchParams" not in source, (
            f"{name} bypasses the shared hook; the history-replace decision "
            f"would then be made twice, differently"
        )
        assert "useUrlState" in source, f"{name} does not use the shared hook"


def test_the_behaviour_decisions_are_exercised_by_a_real_run() -> None:
    """The decisions live in `tests/url-state.test.mts`, and that file runs them.

    The three decisions this hook makes - replace-not-push, empty removes the
    parameter, an unknown value falls back - used to be guarded here by matching
    strings in the source. That guard was wrong in both directions, and both
    were measured rather than argued:

    - Renaming `push` to `replaceByDefault` **failed** it with the behaviour
      completely unchanged.
    - Replacing the removal branch with `updated.set(key, next ?? "")` - which
      leaves `?q=` behind for every cleared search - **passed** it.

    A guard that cries wolf on a rename and misses the real defect trains people
    to protect a variable name instead of a behaviour. So the decisions were
    extracted into `applyUrlValue`, `readUrlValue` and `isOneOf`, which need
    neither React nor a DOM, and are executed by Node's type stripping.

    What is asserted here is that the real tests exist and are wired into the
    project's own entry points - a test file nobody runs is a comment.
    """
    behaviour_test = ADMIN_WEB / "tests" / "url-state.test.mts"
    assert behaviour_test.is_file(), (
        "apps/admin-web/tests/url-state.test.mts is missing; the URL-state "
        "decisions have no executable guard left"
    )

    body = behaviour_test.read_text(encoding="utf-8")
    for subject, why in [
        ("applyUrlValue", "the write path"),
        ("readUrlValue", "the read path"),
        ("isOneOf", "the unknown-value fallback"),
    ]:
        assert subject in body, f"the behaviour test does not exercise {subject}: {why}"

    package = json.loads((ADMIN_WEB / "package.json").read_text(encoding="utf-8"))
    assert "test" in package["scripts"], (
        "apps/admin-web has no `npm test`, so the behaviour tests have no entry "
        "point and will silently stop running"
    )
    assert "--experimental-strip-types" in package["scripts"]["test"], (
        "the runner must strip types natively rather than need a build step"
    )

    ci = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.is_file() and "npm test" in ci.read_text(encoding="utf-8"), (
        "CI does not run `npm test`; a test that CI skips is a test that rots"
    )


def test_a_value_outside_the_known_set_falls_back() -> None:
    """A hand-edited `?tab=whatever` must not reach a render path.

    The alternative is a page in a state no branch handles, which shows as an
    empty panel - and an empty panel reads as "there is nothing here", not "this
    link is wrong".
    """
    body = _read(HOOK)
    assert "isOneOf" in body, "the membership check is part of the contract"
    workbench = _read(WEB / "pages" / "Workbench.tsx")
    assert "isOneOf(tabParam, ALL_TABS)" in workbench, (
        "the workbench tab is not validated against the known set"
    )


def test_both_pages_keep_their_operator_state_in_the_url() -> None:
    """The actual requirement, one line each.

    Asserted on the call rather than on the absence of `useState`, because the
    legitimate reason for `useState` here is unchanged and asserting its absence
    would forbid the next correct use of it.
    """
    conversations = _read(WEB / "pages" / "Conversations.tsx")
    assert 'useUrlState("conversation"' in conversations, (
        "the selected replay is still component state, so a replay cannot be linked"
    )
    workbench = _read(WEB / "pages" / "Workbench.tsx")
    assert 'useUrlState("tab"' in workbench, "the queue tab is not in the URL"
    assert 'useUrlState("q"' in workbench, "the queue search is not in the URL"
