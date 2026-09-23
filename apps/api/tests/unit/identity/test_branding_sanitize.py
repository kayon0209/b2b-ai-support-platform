"""Unit tests: the read-side repair for a stored display name.

`identity/branding._validate` refuses markup on *write*. These cover the other
half - what a reader sees for a value that was stored before that rule existed.

The case is not hypothetical. Measured 2026-09-23: the customer support window's
`<h1>` was the literal `<img src=x onerror=alert(2)>Acme & Co`, on desktop and
on mobile, for every customer of that tenant. React escapes it, so it was never
an executable XSS - it was a company name that read as source code, and a value
that would have been executed by any consumer rendering HTML (an email, a PDF).
"""

from __future__ import annotations

from platform_core.identity.branding import sanitize_display_name


def test_the_stored_value_that_caused_this_keeps_the_tenants_own_words() -> None:
    assert (
        sanitize_display_name("<img src=x onerror=alert(2)>Acme & Co") == "Acme & Co"
    )


def test_a_clean_name_is_untouched() -> None:
    """Including the characters a label legitimately carries."""
    for name in ("Acme & Co", "华秋电子", "O'Brien & Sons", "A/B Testing Co"):
        assert sanitize_display_name(name) == name


def test_surrounding_whitespace_is_trimmed() -> None:
    assert sanitize_display_name("  Acme  ") == "Acme"
    assert sanitize_display_name("<b>Acme</b>") == "Acme"


def test_control_characters_go_too() -> None:
    """A label has no use for a NUL or a newline either."""
    assert sanitize_display_name("Acme\x00Co") == "AcmeCo"
    assert sanitize_display_name("Acme\nCo") == "AcmeCo"


def test_only_markup_yields_none_so_the_caller_falls_back() -> None:
    """NOT an empty string.

    `None` is what makes `support_router` fall back to the tenant's own `name`.
    Returning "" would render an empty heading, which is a worse failure than
    the one being repaired.
    """
    assert sanitize_display_name("<img src=x>") is None
    assert sanitize_display_name("   ") is None
    assert sanitize_display_name(None) is None
