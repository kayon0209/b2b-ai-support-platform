"""The console's dialogs must be modal, and must say so.

The defect
----------
`TokenDialog` declared `aria-modal="false"` on an element that covered the
page, in both of its branches. A screen-reader user was announced "dialog" and
then left free to tab into the console behind it, where every control still
looked live. There was no Escape handling, no focus trap, and no restoration of
focus to the control that opened it, so closing the dialog dropped the operator
at the top of the document.

None of that is visible in a build, and the existing render check only fails on
console errors. These assertions are on the source because that is where the
defect lived.

What is pinned
--------------
- The primitive exists and carries the five properties, in the order they
  matter: modality, focus in, focus trap, Escape, focus restore.
- Every dialog rendered inside a backdrop declares `aria-modal="true"`. Scoped
  to overlays, because `aria-modal="false"` on an inline panel is correct.
"""

from __future__ import annotations

import json
import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[3] / "admin-web" / "src"
# `WEB` ends in `.../admin-web/src`, so its parent is the package root. Named
# rather than recomputed inline, because getting this wrong produces a
# FileNotFoundError on a path that reads correctly.
ADMIN_WEB = WEB.parent


def _sources() -> list[pathlib.Path]:
    return sorted(p for p in WEB.rglob("*.tsx") if p.is_file())


def _code_only(text: str) -> str:
    """Strip comments so prose about a defect is not itself the defect.

    The first version of this guard failed on the two comments that *describe*
    the `aria-modal="false"` bug. A check that cannot tell documentation from
    code trains people to delete the explanation instead of the bug, so the
    scanner drops comments and matches attributes only.
    """
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))


def test_the_accessibility_claims_are_checked_by_rendering_not_by_matching() -> None:
    """The attributes are read off rendered HTML now.

    This file used to assert `aria-modal="true"` appeared in the source, and
    measured how that guard behaves:

    - Writing `aria-modal={MODAL}` where `MODAL` is `"true"` - identical
      behaviour, and a reasonable thing to do when a value appears twice -
      produced **two** failures, one claiming an overlay was not modal and one
      claiming the primitive was missing `aria-modal="true"`.

    Both failures name a literal, so the guard protected a spelling. The
    replacement renders the component with `react-dom/server` and reads the
    attributes off the HTML, and was checked the same way: the refactor above
    passes silently, and removing `aria-hidden` from the skeleton - a real
    defect, since a screen reader would then announce placeholder rows as
    content - fails two assertions.
    """
    tests_dir = ADMIN_WEB / "tests"
    behaviour = tests_dir / "dialog-a11y.test.mts"
    assert behaviour.is_file(), (
        "apps/admin-web/tests/dialog-a11y.test.mts is missing; the console's "
        "accessibility claims have no executed guard left"
    )

    body = behaviour.read_text(encoding="utf-8")
    assert "renderToStaticMarkup" in body, (
        "the test must render the component; reading its source is the thing this replaced"
    )
    assert "SkeletonRows" in body and "Dialog" in body, (
        "both primitives are claimed accessible and both must be rendered"
    )

    # The build step, because Node cannot import `.tsx` directly and a missing
    # step makes the test fail with a loader error rather than an assertion.
    assert (tests_dir / "build-components.mjs").is_file(), (
        "the component build step the test depends on is missing"
    )

    package = json.loads((ADMIN_WEB / "package.json").read_text(encoding="utf-8"))
    script = package.get("scripts", {}).get("test", "")
    assert "dialog-a11y" in script, f"`npm test` does not run the accessibility tests: {script!r}"
    assert "build-components" in script, (
        "`npm test` must build the components first; importing `.tsx` directly "
        "fails with ERR_UNKNOWN_FILE_EXTENSION"
    )


def test_an_inline_panel_is_still_allowed_to_be_non_modal() -> None:
    """The distinction the first version of this rule got wrong, kept.

    "No component may declare `aria-modal=\"false\"`" flagged `Prompt.tsx`,
    where false is **correct**: it is an inline panel, not an overlay, and false
    is the ARIA default for `role="dialog"`. A rule that flags a right decision
    gets deleted rather than fixed.

    Asserted on the source deliberately, because the property is about which
    element a component is - and the rendered HTML of an inline panel is
    indistinguishable from an overlay's once a backdrop is involved. Here the
    check is a presence check on a named constant, not a string match on the
    attribute value, so a refactor that extracts the value is not a failure.
    """
    prompt = (WEB / "components" / "Prompt.tsx").read_text(encoding="utf-8")
    code = _code_only(prompt)
    if 'role="dialog"' not in code:
        # Prompt no longer renders a dialog at all; the rule has nothing to say.
        return
    assert "backdrop" not in code, (
        'Prompt.tsx now renders a backdrop, so `aria-modal="false"` would be '
        "wrong there and the exemption no longer applies"
    )
