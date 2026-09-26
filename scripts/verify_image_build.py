"""Verify that `infra/compose/api.Dockerfile` actually builds.

Why this exists
---------------
The Dockerfile gained a `postgresql-client` layer in phase four, for the backup
CronJob and the restore drill. The scripts were verified against an existing
image with the sources mounted, and the dependency list was checked by reading -
but the image build itself was never run, because the registry was unreachable
from the machine doing the work.

That is exactly the state "verified except we never ran it" describes, and it
is the state a deployment discovers at 3am. So the check is now a command with
an exit code rather than a sentence in a plan document.

What it does, in order
----------------------
1. `docker build` the image as written. This is the part that cannot be faked -
   it is the build.
2. Assert the artefacts the image claims to carry are actually in it:
   `pg_dump` resolves, the operational scripts travelled, the frontend `dist/`
   is present, and the runtime image has no Node toolchain.

Step 2 is why this is not just a shell alias for `docker build`. A build that
succeeds while missing the `COPY scripts` layer would look identical from the
outside, and the restore drill would then run whatever version happened to be
in a ConfigMap - a different recovery path from the one actually deployed, which
is the reason the layer is there in the first place.

Usage
-----
    python scripts/verify_image_build.py

Exits non-zero on any failure. Requires network access to the container
registry; a registry outage is reported as such rather than as a defect in the
Dockerfile, because the two need different responses.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "infra" / "compose" / "api.Dockerfile"
IMAGE_TAG = "platform-api-build-verify"

# Each entry: (label, shell command run inside the image, why it matters).
# Commands are deliberately simple - a probe that can itself be wrong is worse
# than no probe, and these are checked by eye.
PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "pg_dump is present",
        "command -v pg_dump",
        "the backup CronJob and the restore drill both call it",
    ),
    (
        "pg_restore is present",
        "command -v pg_restore",
        "the restore drill cannot verify an archive without it",
    ),
    (
        "psql is present",
        "command -v psql",
        "the drill compares schemas after restoring",
    ),
    (
        "operational scripts travelled",
        "test -f /app/scripts/backup.py && test -f /app/scripts/backup_restore_drill.py",
        "without these the drill tests a version pinned somewhere else",
    ),
    (
        "the frontend build is present",
        'test -d /app/apps/admin-web/dist && test -n "$(ls -A /app/apps/admin-web/dist)"',
        "an image with no dist leaves /support answering 401, which looks like "
        "an auth problem rather than a missing artefact",
    ),
    (
        "no Node toolchain in the runtime image",
        "! command -v node",
        "the Node stage exists so the runtime image carries nothing to patch",
    ),
    (
        "the source is importable",
        'python -c "import platform_core.main"',
        "PYTHONPATH has to actually resolve, or every probe above is measuring "
        "a container that cannot serve",
    ),
)


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    if not DOCKERFILE.is_file():
        print(f"FAIL {DOCKERFILE} does not exist")
        return 1

    print(f"Building {DOCKERFILE.relative_to(REPO_ROOT)} as {IMAGE_TAG}")
    print("This needs registry access. A pull failure is reported separately")
    print("from a build failure, because they need different responses.\n")

    build = _run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE_TAG, "."],
        timeout=3600,
    )

    if build.returncode != 0:
        output = (build.stdout + build.stderr).strip()
        if "context deadline exceeded" in output or "no such host" in output:
            print("BLOCKED the registry is unreachable from this machine.")
            print("The build did not run, so it is not verified - and saying")
            print("otherwise would be the one thing worse than admitting it.")
            print("\nRun this where the registry is reachable:")
            print("    python scripts/verify_image_build.py")
        else:
            print("FAIL the image did not build")
            print(output[-4000:])
        return 2 if "context deadline exceeded" in output else 1

    print("build OK\n")

    failures: list[str] = []
    for label, probe, why in PROBES:
        result = _run(
            ["docker", "run", "--rm", "--entrypoint", "sh", IMAGE_TAG, "-c", probe],
            timeout=180,
        )
        if result.returncode == 0:
            print(f"  OK      {label}")
        else:
            failures.append(f"{label} - {why}")
            print(f"  MISSING {label}")
            print(f"          {why}")
            detail = (result.stdout + result.stderr).strip()
            if detail:
                print(f"          {detail[:300]}")

    print()
    if failures:
        print(f"FAIL the image builds but {len(failures)} thing(s) are wrong in it:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(f"PASS build verified, {len(PROBES)} probes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
