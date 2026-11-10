"""
The few Kubernetes API calls the tests need, as plain HTTPS with the pod's own service account: the tests image needs
no Kubernetes client. What a test may do is set by its Job's Role (k8s/chaos-job.yaml, k8s/upgrade-job.yaml).
"""
import json
import os
from pathlib import Path

import requests

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
STRATEGIC_MERGE = "application/strategic-merge-patch+json"   # lists with a key (containers, env) merge item by item
JSON_MERGE = "application/merge-patch+json"                  # a list in the patch replaces the whole list


class Kube:
    def __init__(self):
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {(SERVICE_ACCOUNT / 'token').read_text()}"
        self.http.verify = str(SERVICE_ACCOUNT / "ca.crt")
        namespace = (SERVICE_ACCOUNT / "namespace").read_text()
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        server = f"https://{host}:{port}"
        self.pods_url = f"{server}/api/v1/namespaces/{namespace}/pods"
        self.deployments_url = f"{server}/apis/apps/v1/namespaces/{namespace}/deployments"
        self.replica_sets_url = f"{server}/apis/apps/v1/namespaces/{namespace}/replicasets"

    def _list(self, url: str, selector: str) -> list[dict]:
        res = self.http.get(url, params={"labelSelector": selector}, timeout=10)
        res.raise_for_status()
        return res.json()["items"]

    def inference_pods(self) -> list[dict]:
        return self._list(self.pods_url, "app=inference")

    def inference_replica_sets(self) -> list[dict]:
        return self._list(self.replica_sets_url, "app=inference")

    def delete(self, name: str):
        self.http.delete(f"{self.pods_url}/{name}", timeout=10).raise_for_status()

    def deployment(self, name: str) -> dict:
        res = self.http.get(f"{self.deployments_url}/{name}", timeout=10)
        res.raise_for_status()
        return res.json()

    def patch_deployment(self, name: str, patch: dict, content_type: str = STRATEGIC_MERGE):
        self.http.patch(f"{self.deployments_url}/{name}", data=json.dumps(patch), timeout=10,
                        headers={"Content-Type": content_type}).raise_for_status()


def is_ready(pod: dict) -> bool:
    if pod["metadata"].get("deletionTimestamp"):        # being stopped: not counted, even while still answering
        return False
    return any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
