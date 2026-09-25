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

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[3] / "admin-web" / "src"
HOOK = WEB / "lib" / "urlState.ts"


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


def test_the_hook_replaces_rather_than_pushes_by_default() -> None:
    """A search box that pushes per keystroke makes Back useless.

    Walking Back through the letters of the last word typed is the specific
    failure; so is walking Back through every conversation an operator clicked.
    Neither is what anybody means by Back in a workbench.
    """
    body = _read(HOOK)
    assert "push = false" in body, "the default has to be replace, not push"
    assert "{ replace: !push }" in body, "the decision has to be wired to the option"


def test_an_empty_value_removes_the_parameter() -> None:
    """`?q=` looks like a filter set to nothing.

    A link carrying `?q=` is a different request from one carrying no parameter,
    and it renders as a filter the operator has to clear before it behaves.
    """
    body = _read(HOOK)
    assert "updated.delete(key)" in body, "an empty value must remove the parameter"
    assert re.search(r'next\s*===\s*null\s*\|\|\s*next\s*===\s*""', body), (
        "null and the empty string both have to count as absent"
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
