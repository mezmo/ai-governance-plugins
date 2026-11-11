"""Tests for ledger.resolve_target: the target and access each ledger event is filed under."""

import pathlib
import sys
import unittest
from typing import Any, Dict, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402


def bash(command: str) -> Tuple[str, str, Dict[str, Any]]:
    """resolve_target's arguments for a Bash call."""
    return ledger.AGENT, "Bash", {"command": command}


# resolve_target's arguments, and the (target, access) it returns.
CASES = [
    (bash("git checkout main"), ("unknown", "read")),
    (bash("KUBECONFIG=/x kubectl delete svc checkout -n $(cat ns)"), ("unknown", "write")),
    (bash("kubectl -n prod logs deploy/checkout"), ("checkout/prod", "read")),
    (bash("kubectl get deploy 2>/dev/null"), ("unknown", "read")),
    (bash("kubectl get deploy checkout -n prod && kubectl delete ns scratch"), ("unknown", "write")),
    (bash("kubectl get deploy payments\ngrep -n replicas out.txt"), ("payments/prod", "read")),
    (
        bash("kubectl get deploy payments # don't change staging\nkubectl delete svc checkout # team's cleanup"),
        ("checkout/prod", "write"),
    ),
    (bash("kubectl get pods -n prod"), ("unknown", "read")),
    (
        bash("kubectl get pods -n prod && kubectl scale deployment payments --replicas=0 --namespace=staging"),
        ("payments/staging", "write"),
    ),
    (bash("kubectl get pods -n staging -l app=checkout"), ("checkout/staging", "read")),
    (bash("kubectl get deploy -o yaml payments -n prod"), ("payments/prod", "read")),
    (bash("kubectl get deploy --sort-by x payments -n prod"), ("payments/prod", "read")),
    (bash("kubectl get deploy -A payments -n prod"), ("payments/prod", "read")),
    (bash("kubectl delete deploy payments -n prod -n stage"), ("payments/stage", "write")),
    (bash("kubectl drain node-1"), ("unknown", "write")),
    (bash("kubectl label deploy payments tier=web -n prod"), ("payments/prod", "write")),
    (bash("kubectl taint nodes node-1 key=value:NoSchedule"), ("unknown", "write")),
    (bash("kubectl rollout status deploy/checkout -n prod"), ("checkout/prod", "read")),
    (bash("kubectl rollout undo deploy/checkout -n prod --to-revision=40"), ("checkout/prod", "write")),
    (bash("kubectl create service clusterip payments --tcp=80:80 -n staging"), ("payments/staging", "write")),
    (bash("kubectl create service nodeport payments --tcp=80:80 -n staging"), ("payments/staging", "write")),
    (bash("kubectl create service loadbalancer payments --tcp=80:80 -n staging"), ("payments/staging", "write")),
    (bash("kubectl create svc externalname payments --external-name x.io -n staging"), ("payments/staging", "write")),
    (bash("kubectl create service clusterip --tcp 80:80 payments -n staging"), ("payments/staging", "write")),
    (bash("kubectl create service clusterip -n staging"), ("unknown", "write")),
    (("demo_server", "get_service_failure_rates", {"service": "checkout"}), ("checkout/prod", "read")),
    (("demo_server", "get_update_history", {"service": "checkout"}), ("checkout/prod", "read")),
    (("demo_server", "getRecentUpdates", {"service": "checkout"}), ("checkout/prod", "read")),
    (("demo_server", "restart_service", {"env": "staging", "service_name": "payments"}), ("payments/staging", "write")),
    (("demo_server", "updateService", {"service": "checkout"}), ("checkout/prod", "write")),
]


class ResolveTargetTests(unittest.TestCase):
    def test_target_and_access(self) -> None:
        for call, expected in CASES:
            with self.subTest(call=call):
                self.assertEqual(ledger.resolve_target(*call), expected)

    def test_command_words_keep_quoted_hashes(self) -> None:
        self.assertEqual(
            ledger._command_words("echo '# not a comment' # real comment\nkubectl get pods"),
            ["echo", "# not a comment", "\n", "kubectl", "get", "pods"],
        )


if __name__ == "__main__":
    unittest.main()
