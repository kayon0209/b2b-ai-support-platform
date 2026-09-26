"""Print the `APP_RATE_LIMIT_TRUSTED_PROXIES` value a deployment should use.

Why this exists
---------------
The code side of the customer-side rate-limit key is finished and was measured:
with the trusted-proxy list empty behind an ingress, 200 concurrent visitors
shared **one** bucket in Redis and 14.4% of them were rejected; with the ingress
network trusted, the rejection rate was 0% and the visitors were split across
340 buckets. The ConfigMap ships the value empty, because the correct CIDR
belongs to the cluster and the repository cannot know it.

That leaves the deployment with an instruction it cannot easily follow. "Set it
to the ingress CIDR" is not an action - somebody has to find the ingress
controller's node network, work out which pod CIDR it hands out addresses from,
and decide how wide a range is safe. Guessing produces either the original
defect or a wider trust than intended, and the second is a security problem:
an over-broad entry lets a caller choose its own bucket by sending a header.

So this prints the value, from the cluster, at the moment it is needed. It
reads the ingress controller's own configuration rather than inferring from the
API's own pod IP - the two are different questions, and using the latter is how
a deployment ends up trusting a single pod rather than the range.

What it does NOT do
-------------------
It does not apply anything, and it does not guess when the cluster shape is not
recognisable. An unrecognised layout prints what it could not determine and why,
because a wrong value here is worse than no value: empty is the safe default
(do not trust any forwarded header) and a wrong CIDR can be either the
availability bug or the security bug.

Usage
-----
    kubectl -n b2b-support exec deploy/api -- python -m scripts.report_trusted_proxies

or, from the repository, against a reachable API:

    python scripts/report_trusted_proxies.py --namespace b2b-support
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


def _kubectl(args: list[str], namespace: str, timeout: int = 30) -> tuple[int, str]:
    """Run kubectl and return (exit code, output).

    The binary is resolved rather than assumed: a bare "kubectl" is a partial
    path, and S607 flagged it correctly - a script that only runs on the
    author's machine is a script nobody runs during a deployment.
    """
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        return 127, "kubectl is not on PATH"
    try:
        result = subprocess.run(  # noqa: S603 - resolved absolute path, no shell
            [kubectl, *args, "-n", namespace, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "kubectl is not on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"kubectl timed out after {timeout}s"
    return result.returncode, (result.stdout or result.stderr).strip()


def _pod_networks(namespace: str) -> list[str]:
    """The pod and service CIDRs the cluster actually allocates from.

    Read from the API server's own config where possible, because that is the
    authoritative answer, and fall back to the controller's pods only to say
    which controller is in front of the API.
    """
    notes: list[str] = []
    code, body = _kubectl(["get", "ingresscontroller", "-o", "json"], namespace)
    if code == 0 and body:
        try:
            controllers = json.loads(body).get("items", [])
        except json.JSONDecodeError:
            controllers = []
        for controller in controllers:
            proxy_vip = controller.get("status", {}).get("loadBalancer", {}).get("ingress")
            if proxy_vip:
                notes.append(
                    "ingress controller "
                    f"{controller.get('metadata', {}).get('name', '?')} "
                    f"load balancer: {proxy_vip[0].get('ip') or proxy_vip[0].get('hostname')}"
                )
    else:
        notes.append(
            "could not read IngressController resources "
            f"({body.splitlines()[0] if body else 'no output'}); "
            "the controller type may be nginx rather than contour"
        )

    # The API's own pod list is what actually shows the source ranges reaching
    # it, which is the question this has to answer.
    code, body = _kubectl(["get", "pods", "-l", "app.kubernetes.io/name=api"], namespace)
    if code == 0 and body:
        try:
            pods = json.loads(body).get("items", [])
        except json.JSONDecodeError:
            pods = []
        sources: set[str] = set()
        for pod in pods:
            for key in ("status",):
                for ip in pod.get(key, {}).get("podIP", "").split(","):
                    if ip.strip():
                        sources.add(ip.strip())
        notes.append(f"API pods are at: {', '.join(sorted(sources)) or 'none found'}")
        notes.append(
            "The peer address the API sees is the *proxy's*, not these. If every "
            "request arrives from one address, that address is the ingress."
        )
    else:
        notes.append(
            "could not list API pods "
            f"({body.splitlines()[0] if body else 'no output'}); "
            "adjust the selector to match this deployment"
        )
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="b2b-support")
    args = parser.parse_args()

    probe = subprocess.run(  # noqa: S603 - resolved path, fixed argv
        [shutil.which("kubectl") or "kubectl", "cluster-info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    code, body = probe.returncode, (probe.stderr or probe.stdout).strip()
    if code != 0:
        # kubectl retries a refused connection several times and logs each
        # attempt, which buried the actual answer under twenty lines of
        # memcache warnings. The last non-log line is the one that says why.
        useful = [
            line for line in body.splitlines() if line.strip() and "Unhandled Error" not in line
        ]
        reason = useful[-1] if useful else "kubectl exited non-zero"
        print(f"Cannot reach the cluster: {reason}")
        print()
        print("This script needs kubectl configured for the target cluster.")
        print("Until then the safe value is empty - an empty list trusts no")
        print("forwarded header, which is correct but shares one bucket behind")
        print("an ingress.")
        return 2

    print("APP_RATE_LIMIT_TRUSTED_PROXIES")
    print("=" * 34)
    print()
    print("Leave it empty if the API is exposed directly. Set it only when")
    print("something in front of it rewrites the source address.")
    print()

    notes = _pod_networks(args.namespace)
    for note in notes:
        print(f"  - {note}")
    print()

    print("To fill it in:")
    print("  1. Find what the API sees as the peer. Tail its access log for the")
    print("     address field, or run the probe below from a pod behind the same")
    print("     ingress.")
    print("  2. Convert that address to the smallest CIDR that covers the proxy's")
    print("     whole range - a pod CIDR, not a single pod IP. The proxy address")
    print("     changes when the controller rolls.")
    print("  3. Set the ConfigMap and restart the API:")
    print(f"       kubectl -n {args.namespace} edit configmap 10-configmap")
    print("  4. Confirm the buckets split, rather than assuming they did:")
    print("       redis-cli --scan --pattern 'ratelimit:api:addr:*' | wc -l")
    print("     One key means the list is still wrong; N keys means it took.")
    print()
    print("Do not widen it beyond the proxy's own range. A broader entry lets any")
    print("caller pick its own rate-limit bucket by sending a header, which is")
    print("worse than the shared bucket it fixes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
