"""Regression checks for the pod/container read-only-rootfs contract."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _pod_spec():
    doc = yaml.safe_load((ROOT / "k8s/statefulset.yaml").read_text())
    return doc["spec"]["template"]["spec"]


def test_every_container_has_least_privilege_security_context():
    spec = _pod_spec()
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["fsGroup"] == 1000

    containers = {c["name"]: c for c in spec["containers"]}
    assert set(containers) == {"web", "loop", "ipdrift"}
    for container in containers.values():
        security = container["securityContext"]
        assert security["runAsNonRoot"] is True
        assert security["runAsUser"] == 1000
        assert security["runAsGroup"] == 1000
        assert security["allowPrivilegeEscalation"] is False
        assert security["seccompProfile"] == {"type": "RuntimeDefault"}
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]
        mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
        assert mounts["/data"] == "data"


def test_writable_mounts_preserve_shared_lock_and_regen_scratch():
    spec = _pod_spec()
    containers = {c["name"]: c for c in spec["containers"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert set(volumes) == {"hermes-home", "web-tmp"}
    assert all(volume["emptyDir"] == {} for volume in volumes.values())

    for name in ("web", "loop"):
        mounts = {m["mountPath"]: m["name"] for m in containers[name]["volumeMounts"]}
        # These containers must share one volume: executor's entry-order flock
        # provides no protection if each container gets a private rootfs copy.
        assert mounts["/home/hermes/.hermes"] == "hermes-home"
    web_mounts = {m["mountPath"]: m["name"] for m in containers["web"]["volumeMounts"]}
    assert web_mounts["/tmp"] == "web-tmp"


def test_base_image_and_secret_store_are_persistent_and_reproducible():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(
        r"(?m)^FROM python:3\.11-slim@sha256:[0-9a-f]{64} AS base$",
        dockerfile,
    )
    configmap = yaml.safe_load((ROOT / "k8s/configmap.yaml").read_text())
    assert configmap["data"]["HERMES_SECRETS_FILE"] == "/data/.secrets.json"
