"""Audit probe: the read-side repair and the migration must agree.

Two implementations of one rule - `branding.sanitize_display_name` in Python and
migration 0053's `REGEXP_REPLACE` in SQL. If they disagree, a value changes
shape the first time it is written back, which is its own bug: a name that reads
as "Acme & Co" today becomes "img src=xAcme & Co" tomorrow.

Runs the same inputs through both, against the live database.

Run from the project root:
    .venv/Scripts/python.exe scripts/_audit_probe_branding.py
"""

import os
import sys

sys.path.insert(0, "apps/api/src")

import psycopg  # noqa: E402

from platform_core.identity.branding import sanitize_display_name  # noqa: E402

SAMPLES = [
    "<img src=x onerror=alert(2)>Acme & Co",
    "<img src=x>",
    "<b>Acme</b>",
    "Acme < Co",
    "Acme & Co",
    "华秋电子",
    "O'Brien & Sons",
    "a<b>c",
    "  spaced  ",
    "   ",
    "<script>alert(1)</script>Real Name",
    # No NUL-byte sample: Postgres text columns cannot contain one at all, so
    # it is not a value the two implementations could ever disagree about in
    # stored data. The control-character pass is still exercised by the
    # migration's WHERE clause and by the unit tests.
]

URL = os.environ.get(
    "PROBE_DB_URL",
    "postgresql://platform:platform@127.0.0.1:5435/platform",
)

SQL = """
SELECT NULLIF(BTRIM(REGEXP_REPLACE(
           REGEXP_REPLACE(
               REGEXP_REPLACE(%s, '<[^>]*>', '', 'g'),
               '[<>]', '', 'g'
           ),
           '[[:cntrl:]]', '', 'g'
       )), '')
"""

print(f"{'输入':<44} {'Python':<24} {'SQL':<24} 一致")
print("-" * 104)
mismatches = 0
with psycopg.connect(URL) as conn:
    for sample in SAMPLES:
        py = sanitize_display_name(sample)
        sql = conn.execute(SQL, (sample,)).fetchone()[0]
        same = py == sql
        if not same:
            mismatches += 1
        print(f"{sample!r:<44} {str(py)!r:<24} {str(sql)!r:<24} {'✓' if same else '✗ 不一致'}")

print()
print("不一致数量:", mismatches)
sys.exit(1 if mismatches else 0)
