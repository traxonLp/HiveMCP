"""Internal consistency of the Kubernetes manifests.

Not a substitute for applying them to a real cluster — nothing here talks to an API
server. What it catches is the class of mistake that survives review and then fails at
deploy time wearing someone else's face: a Deployment mounting a path the ConfigMap does
not name, an ingress host that disagrees with HIVE_PUBLIC_URL so every download link
resolves nowhere, an fsGroup that no longer matches the uid baked into the image.

These are all cross-file relationships, which is exactly what a human reviewer reading
one file at a time is worst at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to parse the manifests")

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "deploy" / "k8s"
IMAGE_UID = 10001
CONTAINER_PORT = 8080


@pytest.fixture(scope="module")
def objects() -> dict[tuple[str, str], dict]:
    if not MANIFEST_DIR.is_dir():
        pytest.skip("manifests are not in this build context")
    found: dict[tuple[str, str], dict] = {}
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document:
                found[(document["kind"], document["metadata"]["name"])] = document
    return found


@pytest.fixture
def deployment(objects) -> dict:
    return objects[("Deployment", "hivemcp")]


@pytest.fixture
def config(objects) -> dict[str, str]:
    return objects[("ConfigMap", "hivemcp-config")]["data"]


@pytest.fixture
def pod_spec(deployment) -> dict:
    return deployment["spec"]["template"]["spec"]


@pytest.fixture
def container(pod_spec) -> dict:
    return pod_spec["containers"][0]


# --------------------------------------------------------------------------- #
# Everything parses and the expected objects exist
# --------------------------------------------------------------------------- #


def test_every_file_is_valid_yaml() -> None:
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("PersistentVolumeClaim", "hivemcp-data"),
        ("PersistentVolumeClaim", "hivemcp-templates"),
        ("ConfigMap", "hivemcp-config"),
        ("Secret", "hivemcp-secrets"),
        ("Deployment", "hivemcp"),
        ("Service", "hivemcp"),
        ("Ingress", "hivemcp"),
        ("HorizontalPodAutoscaler", "hivemcp"),
        ("PodDisruptionBudget", "hivemcp"),
        ("NetworkPolicy", "hivemcp"),
    ],
)
def test_the_expected_objects_are_defined(objects, kind: str, name: str) -> None:
    assert (kind, name) in objects


def test_files_are_ordered_so_dependencies_come_first() -> None:
    """`kubectl apply -f <dir>` processes files in lexical order.

    Applying the Deployment before its ConfigMap works — Kubernetes retries — but the
    pod spends its first minute in CreateContainerConfigError, which reads like a real
    failure and sends people debugging the wrong thing.
    """
    names = sorted(path.name for path in MANIFEST_DIR.glob("*.yaml"))
    position = {name.split("-")[0]: index for index, name in enumerate(names)}
    assert position["00"] < position["02"], "storage must precede the deployment"
    assert position["01"] < position["02"], "config must precede the deployment"


# --------------------------------------------------------------------------- #
# Cross-file agreement
# --------------------------------------------------------------------------- #


def test_the_deployment_mounts_the_paths_the_config_names(container, config) -> None:
    mounted = {mount["mountPath"] for mount in container["volumeMounts"]}
    assert config["HIVE_DATA_DIR"] in mounted
    assert config["HIVE_TEMPLATES_DIR"] in mounted


def test_every_claim_referenced_actually_exists(pod_spec, objects) -> None:
    claimed = {
        volume["persistentVolumeClaim"]["claimName"]
        for volume in pod_spec["volumes"]
        if "persistentVolumeClaim" in volume
    }
    defined = {name for kind, name in objects if kind == "PersistentVolumeClaim"}
    assert claimed <= defined


def test_the_ingress_host_matches_the_public_url(objects, config) -> None:
    """Two halves of one setting.

    The ingress is where requests arrive; HIVE_PUBLIC_URL is what download links are
    built from. A mismatch produces links that resolve nowhere, and the failure surfaces
    in the user's browser rather than in any log here.
    """
    host = objects[("Ingress", "hivemcp")]["spec"]["rules"][0]["host"]
    assert host in config["HIVE_PUBLIC_URL"]


def test_the_service_targets_the_container_port(objects, container) -> None:
    port = objects[("Service", "hivemcp")]["spec"]["ports"][0]
    name = container["ports"][0]["name"]
    assert port["targetPort"] == name
    assert container["ports"][0]["containerPort"] == CONTAINER_PORT


def test_the_selector_matches_the_pod_labels(deployment) -> None:
    assert (
        deployment["spec"]["selector"]["matchLabels"]
        == deployment["spec"]["template"]["metadata"]["labels"]
    )


def test_the_service_selector_matches_the_pods(objects, deployment) -> None:
    selector = objects[("Service", "hivemcp")]["spec"]["selector"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(key) == value for key, value in selector.items())


def test_the_network_policy_selects_the_same_pods(objects, deployment) -> None:
    selector = objects[("NetworkPolicy", "hivemcp")]["spec"]["podSelector"]["matchLabels"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(key) == value for key, value in selector.items())


# --------------------------------------------------------------------------- #
# Security posture
# --------------------------------------------------------------------------- #


def test_the_pod_runs_as_the_uid_baked_into_the_image(pod_spec) -> None:
    """fsGroup is what makes both PVCs writable without root.

    An empty volume mounted at a path the image never created is owned by root; the
    container runs as 10001 and cannot write. That surfaced once already, as a 500 on
    the first template upload.
    """
    security = pod_spec["securityContext"]
    assert security["runAsUser"] == IMAGE_UID
    assert security["fsGroup"] == IMAGE_UID
    assert security["runAsNonRoot"] is True


def test_the_container_drops_everything_it_can(container) -> None:
    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]


def test_a_writable_tmp_exists_because_the_root_filesystem_is_not(container) -> None:
    """python-pptx and openpyxl both use tempfile while saving.

    With readOnlyRootFilesystem and no /tmp volume, every render fails at the write —
    late, and with an error that points at the library rather than at the pod spec.
    """
    assert "/tmp" in {mount["mountPath"] for mount in container["volumeMounts"]}


def test_the_deployed_environment_is_prod(config) -> None:
    """dev would mount /_debug, which reflects the session token back to the caller."""
    assert config["HIVE_ENVIRONMENT"] == "prod"


def test_mcp_binds_on_all_interfaces(config) -> None:
    """The SDK default of 127.0.0.1 enables DNS rebinding protection, which rejects the
    Host header of anything arriving through a Service."""
    assert config["HIVE_MCP_HOST"] == "0.0.0.0"


def test_no_real_signing_key_is_committed(objects) -> None:
    secret = objects[("Secret", "hivemcp-secrets")]["stringData"]
    assert "CHANGE-ME" in secret["HIVE_SIGNING_KEY"]


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


def test_all_three_probes_are_defined(container) -> None:
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    # Readiness checks the volumes, so a pod with a broken mount leaves the Service
    # instead of failing every render sent to it.
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"


def test_the_disruption_budget_can_be_satisfied(objects, deployment) -> None:
    """minAvailable above the replica count would block every node drain forever."""
    minimum = objects[("PodDisruptionBudget", "hivemcp")]["spec"]["minAvailable"]
    hpa = objects[("HorizontalPodAutoscaler", "hivemcp")]["spec"]
    assert minimum <= hpa["minReplicas"] <= deployment["spec"]["replicas"]
    assert hpa["minReplicas"] <= hpa["maxReplicas"]


def test_rolling_updates_keep_capacity(deployment) -> None:
    rolling = deployment["spec"]["strategy"]["rollingUpdate"]
    assert rolling["maxUnavailable"] == 0


def test_render_concurrency_is_bounded(config) -> None:
    assert 1 <= int(config["HIVE_MAX_RENDER_CONCURRENCY"]) <= 8


def test_the_ingress_accepts_a_body_larger_than_the_upload_limit(objects, config) -> None:
    """A spec with an inline image is large, and base64 inflates it by 4/3.

    If the ingress limit is the lower one, the request dies before reaching the app and
    the model gets a transport error instead of a message it can act on.
    """
    annotations = objects[("Ingress", "hivemcp")]["metadata"]["annotations"]
    body_size = annotations["nginx.ingress.kubernetes.io/proxy-body-size"]
    assert body_size.endswith("m")
    assert int(body_size[:-1]) > int(config["HIVE_MAX_UPLOAD_MB"])


def test_the_ingress_waits_longer_than_brief_mode(objects, config) -> None:
    annotations = objects[("Ingress", "hivemcp")]["metadata"]["annotations"]
    read_timeout = int(annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"])
    assert read_timeout > float(config["HIVE_LLM_TIMEOUT_SECONDS"])


# --------------------------------------------------------------------------- #
# Version consistency
# --------------------------------------------------------------------------- #


def project_version() -> str:
    text = (MANIFEST_DIR.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    pytest.fail("no version in pyproject.toml")


def test_the_deployment_pins_the_current_version(deployment, container) -> None:
    """CI rewrites both of these with sed on every release.

    A sed that silently matches nothing is the failure mode worth guarding: the release
    would still publish, the manifest would keep pointing at an older image, and the
    next `kubectl apply` would quietly deploy the wrong one.
    """
    version = project_version()
    assert container["image"].endswith(f":{version}")
    assert deployment["metadata"]["labels"]["app.kubernetes.io/version"] == version


def test_the_deployment_image_is_pinned_not_latest(container) -> None:
    """`:latest` in a Deployment means a rollback has nothing to roll back to."""
    assert not container["image"].endswith(":latest")


def test_egress_allows_dns(objects) -> None:
    """Easy to forget, and everything else in the policy silently fails without it."""
    egress = objects[("NetworkPolicy", "hivemcp")]["spec"]["egress"]
    ports = [port for rule in egress for port in rule.get("ports", [])]
    assert any(port["port"] == 53 for port in ports)
