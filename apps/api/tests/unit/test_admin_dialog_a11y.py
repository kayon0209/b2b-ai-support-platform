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

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[3] / "admin-web" / "src"


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


def test_every_dialog_rendered_as_an_overlay_declares_itself_modal() -> None:
    """A backdrop is a modal, so it has to say so.

    Scoped to dialogs rendered inside a `*backdrop*` element rather than to the
    attribute as such. `Prompt.tsx` declares `aria-modal="false"` on a
    `role="dialog"` panel, and that is **correct**: it is an inline panel, not
    an overlay, and the ARIA default for `role="dialog"` is exactly that. A
    blunt "no `false` anywhere" rule would have flagged a right decision and
    taught somebody to delete it.

    `TokenDialog` was the real instance. It covered the page - it has a
    backdrop, it swallows clicks - and announced the opposite, so assistive
    technology treated the console behind it as still live while a keyboard user
    tabbed straight into it.
    """
    offenders: list[str] = []
    for path in _sources():
        code = _code_only(path.read_text(encoding="utf-8"))
        # A backdrop element and its dialog child are on adjacent lines in
        # every case here; the window is generous enough for the indented form.
        for match in re.finditer(r'className="[^"]*backdrop[^"]*"', code):
            window = code[match.start() : match.start() + 600]
            if 'role="dialog"' in window and 'aria-modal="true"' not in window:
                offenders.append(str(path.relative_to(WEB)))
                break
    assert not offenders, f"overlay dialogs that are not modal: {sorted(set(offenders))}"


def test_the_dialog_primitive_carries_every_modality_property() -> None:
    """Asserted as presence, not behaviour - the point is that none is dropped.

    A dialog that traps focus but does not restore it is the common partial
    fix, and it is invisible until somebody closes one by keyboard and loses
    their place. The comment on the primitive records why each is here.
    """
    source = (WEB / "components" / "ui.tsx").read_text(encoding="utf-8")
    body = source.split("export function Dialog(", 1)[1]
    for needed, why in [
        ('aria-modal="true"', "tells assistive technology the rest of the page is inert"),
        ("openerRef.current?.focus", "returns focus to the control that opened it"),
        ('event.key === "Escape"', "the only way out for a user who cannot see a close button"),
        ('event.key !== "Tab"', "the focus trap, which is what makes it modal for a keyboard"),
        ("document.body.style.overflow", "stops the page scrolling behind under a trackpad"),
    ]:
        assert needed in body, f"Dialog is missing {needed!r}: {why}"


def test_skeleton_placeholders_are_hidden_from_assistive_technology() -> None:
    """A skeleton is a picture of content that does not exist yet.

    Announcing the placeholder rows would be noise; the live region beside them
    is what actually says "loading", and that is asserted here too so the two
    cannot be separated by a later edit.
    """
    source = (WEB / "components" / "ui.tsx").read_text(encoding="utf-8")
    body = source.split("export function SkeletonRows(", 1)[1]
    assert 'aria-hidden="true"' in body, "skeleton rows would be announced as content"
    assert 'role="status"' in body, "nothing tells a screen reader the page is loading"
