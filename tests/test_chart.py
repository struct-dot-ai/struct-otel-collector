"""The Helm chart lints, renders, and what it renders runs on the image.

Helm is run from a pinned container image, so no local install is needed. The
rendered DaemonSet and Deployment are then taken at their word: the arguments
and environment each one gives the collector are passed to the collector's own
`validate` on the image this checkout builds.

Requires Docker.

Usage:
    uv run python tests/test_chart.py
    # Or: uv run pytest tests/test_chart.py -v
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml
from harness import (
    CONFIG_DIR,
    CHART_DIR,
    PACKAGE_DIR,
    PRESETS_DIR,
    RUN_ID,
    build_image,
    docker,
    docker_command,
    env_args,
    kubernetes_args,
    wait_for,
    remove_image,
    remove_work_dir,
    run_tests,
    validate,
)

HELM_IMAGE = "alpine/helm:3.16.4"
KEY = "pk-chart-test"
READ_ONLY_VERBS = {"get", "list", "watch"}


def helm(*args: str):
    """Run Helm on the chart, named `/chart` in the arguments.

    Uses a `helm` on PATH when there is one (GitHub's runners ship it), so CI pulls
    nothing for it; otherwise the pinned image. STRUCT_TEST_HELM_IMAGE=1 forces the image.
    """
    if shutil.which("helm") and not os.environ.get("STRUCT_TEST_HELM_IMAGE"):
        local = [str(CHART_DIR) if arg == "/chart" else arg for arg in args]
        return subprocess.run(["helm", *local], check=False, capture_output=True, text=True, timeout=120)
    return docker("run", "--rm", "-v", f"{CHART_DIR}:/chart:ro", HELM_IMAGE, *args)


def render(*set_args: str, expect_failure: bool = False) -> list[dict[str, Any]]:
    flags: list[str] = []
    for item in set_args:
        flags += (["--set-json", item[5:]] if item.startswith("json:") else ["--set", item])
    result = helm("template", "demo", "/chart", "--namespace", "struct", *flags)
    if expect_failure:
        assert result.returncode != 0, "the chart rendered when it should have refused"
        return [{"error": result.stderr}]
    assert result.returncode == 0, f"helm template failed:\n{result.stdout}\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def one(docs: list[dict], kind: str, suffix: str = "") -> dict:
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    assert len(matches) == 1, f"expected one {kind} ending {suffix!r}, got {[d['metadata']['name'] for d in matches]}"
    return matches[0]


def container(workload: dict) -> dict:
    containers = workload["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def env_by_name(workload: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in container(workload)["env"]}


def literal_env(workload: dict) -> dict[str, str]:
    """The container's environment as the collector would see it, with stand-ins for
    the values Kubernetes fills in from the Secret and from the pod's own fields."""
    stand_ins = {"metadata.name": "pod-1", "metadata.namespace": "struct", "spec.nodeName": "node-1",
                 "status.hostIP": "10.255.255.2"}
    env: dict[str, str] = {}
    for name, entry in env_by_name(workload).items():
        if "value" in entry:
            # As Kubernetes does: $(NAME) is a variable defined earlier in the list.
            env[name] = re.sub(r"\$\(([A-Z0-9_]+)\)", lambda found: env.get(found.group(1), found.group(0)), entry["value"])
        elif "secretKeyRef" in entry["valueFrom"]:
            env[name] = KEY
        else:
            env[name] = stand_ins[entry["valueFrom"]["fieldRef"]["fieldPath"]]
    return env


def test_chart_lints() -> None:
    result = helm("lint", "/chart", "--strict", "--set", f"ingest.key={KEY}")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_default_render_has_a_collector_per_node_and_one_for_the_cluster() -> None:
    docs = render(f"ingest.key={KEY}", "environment=production", "clusterName=prod-1")
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["ClusterRole", "ClusterRole", "ClusterRoleBinding", "ClusterRoleBinding", "DaemonSet",
                     "Deployment", "Secret", "ServiceAccount", "ServiceAccount"], kinds

    node, cluster = one(docs, "DaemonSet"), one(docs, "Deployment")
    assert container(node)["args"] == [
        "--config=/etc/struct-otel-collector/config.yaml",
        "--config=/etc/struct-otel-collector/presets/kubernetes-node.yaml",
    ]
    assert container(cluster)["args"] == [
        "--config=/etc/struct-otel-collector/config.yaml",
        "--config=/etc/struct-otel-collector/presets/kubernetes-cluster.yaml",
    ]
    assert cluster["spec"]["replicas"] == 1, "a second cluster collector would send everything twice"

    secret = one(docs, "Secret")
    assert secret["stringData"] == {"ingest-key": KEY}
    for workload in (node, cluster):
        env = env_by_name(workload)
        # The key reaches the pod from the Secret, never as a literal in the pod spec.
        assert env["STRUCT_INGEST_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": secret["metadata"]["name"], "key": "ingest-key"}
        assert KEY not in yaml.safe_dump(workload)
        assert env["STRUCT_INGEST_URL"]["value"] == "https://ingest.struct.ai"
        security = container(workload)["securityContext"]
        assert security["allowPrivilegeEscalation"] is False and security["readOnlyRootFilesystem"] is True
        mounts = {m["mountPath"] for m in container(workload)["volumeMounts"]}
        assert "/var/lib/struct-otel-collector" in mounts, "a read-only root filesystem needs the state directory mounted"

    node_env = env_by_name(node)
    assert node_env["K8S_NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert node_env["K8S_NODE_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
    node_mounts = {m["mountPath"]: m for m in container(node)["volumeMounts"]}
    assert node_mounts["/var/log/pods"]["readOnly"] is True
    assert cluster["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True


def test_identity_is_the_node_for_the_node_collector_and_the_cluster_for_the_cluster_collector() -> None:
    docs = render(f"ingest.key={KEY}", "environment=production", "clusterName=prod-1")
    node, cluster = one(docs, "DaemonSet"), one(docs, "Deployment")
    shared = "deployment.environment.name=production,deployment.environment=production,k8s.cluster.name=prod-1"

    # The node collector names its node, which Kubernetes fills in from the pod's own field.
    # `env` is the first detector, so this wins over the pod name the system detector finds.
    node_env = env_by_name(node)
    assert node_env["OTEL_RESOURCE_ATTRIBUTES"]["value"] == (
        f"{shared},k8s.node.name=$(K8S_NODE_NAME),host.name=$(K8S_NODE_NAME)")
    names = [entry["name"] for entry in container(node)["env"]]
    assert names.index("K8S_NODE_NAME") < names.index("OTEL_RESOURCE_ATTRIBUTES"), (
        "Kubernetes only expands $(K8S_NODE_NAME) when it is defined earlier in the list")
    assert "STRUCT_COLLECTOR_RESOURCE_DETECTORS" not in node_env, "on a node the cloud detectors find the node"

    # The cluster collector carries no host at all: only what the environment says.
    cluster_env = env_by_name(cluster)
    assert cluster_env["OTEL_RESOURCE_ATTRIBUTES"]["value"] == shared
    assert cluster_env["STRUCT_COLLECTOR_RESOURCE_DETECTORS"]["value"] == "[env]"
    base = yaml.safe_load((CONFIG_DIR / "config.yaml").read_text())
    detectors = base["processors"]["resource_detection/struct"]["detectors"]
    assert detectors == "${env:STRUCT_COLLECTOR_RESOURCE_DETECTORS:-[env, system, ec2, ecs, gcp, azure]}", detectors

    # With no environment and no cluster name the node still names itself.
    bare = render(f"ingest.key={KEY}")
    assert env_by_name(one(bare, "DaemonSet"))["OTEL_RESOURCE_ATTRIBUTES"]["value"] == (
        "k8s.node.name=$(K8S_NODE_NAME),host.name=$(K8S_NODE_NAME)")
    assert "OTEL_RESOURCE_ATTRIBUTES" not in env_by_name(one(bare, "Deployment"))


KUBELET_PROBE = f"struct-otel-collector-test-kubelet-{RUN_ID}"


def kubelet_scrape_error(endpoint: str) -> str:
    """Run the kubernetes-node preset's kubelet receiver, exactly as the preset defines it,
    against `endpoint` where no kubelet answers, and return the error of its first scrape."""
    receiver = yaml.safe_load((PRESETS_DIR / "kubernetes-node.yaml").read_text())["receivers"]["kubelet_stats"]
    config = yaml.safe_dump({
        "receivers": {"kubelet_stats": receiver}, "exporters": {"debug": {}},
        "service": {"telemetry": {"metrics": {"level": "none"}},
                    "pipelines": {"metrics": {"receivers": ["kubelet_stats"], "exporters": ["debug"]}}},
    })
    env = {"STRUCT_TEST_CONFIG": config, "STRUCT_KUBELET_ENDPOINT": endpoint, "K8S_NODE_NAME": "node-1",
           "STRUCT_KUBERNETES_INTERVAL": "1s"}
    docker("rm", "-f", KUBELET_PROBE)
    try:
        docker("run", "-d", "--name", KUBELET_PROBE, *env_args(env), *kubernetes_args(), build_image(),
               "--config=env:STRUCT_TEST_CONFIG", check=True)

        def failed_scrape() -> str | None:
            logs = docker("logs", KUBELET_PROBE)
            for line in ((logs.stderr or "") + (logs.stdout or "")).splitlines():
                if "call to /stats/summary endpoint failed" in line:
                    return line
            return None

        return wait_for(failed_scrape, timeout=60, what=f"the kubelet receiver's first scrape of {endpoint}", interval=0.5)
    finally:
        docker("rm", "-f", KUBELET_PROBE)


def test_the_kubelet_is_reached_at_the_nodes_address_and_ipv6_can_be_bracketed() -> None:
    def kubelet(*extra: str) -> tuple[dict, dict[str, str]]:
        node = one(render(f"ingest.key={KEY}", *extra), "DaemonSet")
        names = [entry["name"] for entry in container(node)["env"]]
        assert names.index("K8S_NODE_IP") < names.index("STRUCT_KUBELET_ENDPOINT"), "$(K8S_NODE_IP) must be defined first"
        return node, env_by_name(node)

    node, env = kubelet()
    assert env["STRUCT_KUBELET_ENDPOINT"]["value"] == "https://$(K8S_NODE_IP):10250"
    assert literal_env(node)["STRUCT_KUBELET_ENDPOINT"] == "https://10.255.255.2:10250"

    # An IPv6 node address is only a valid URL host in brackets, and an IPv4 one only without,
    # so the form is the cluster's to choose.
    node, env = kubelet("node.kubeletEndpoint=https://[$(K8S_NODE_IP)]:10250")
    assert env["STRUCT_KUBELET_ENDPOINT"]["value"] == "https://[$(K8S_NODE_IP)]:10250"

    # `validate` accepts any string here, so the forms are checked where they fail: the
    # receiver's first scrape, with no kubelet to answer. Without brackets the URL cannot be
    # parsed; with them the request is made to that address.
    bare = kubelet_scrape_error("https://2001:db8::1:10250")
    assert "invalid port" in bare, bare
    bracketed = kubelet_scrape_error("https://[2001:db8::1]:10250")
    assert "invalid port" not in bracketed and "https://[2001:db8::1]:10250/stats/summary" in bracketed, bracketed

    error = render(f"ingest.key={KEY}", "node.kubeletEndpoint=", expect_failure=True)[0]["error"]
    assert "node.kubeletEndpoint is required" in error, error


def test_the_node_collector_reads_pod_logs_and_nothing_else_of_the_node() -> None:
    docs = render(f"ingest.key={KEY}")
    node = one(docs, "DaemonSet")
    host_paths = {volume["hostPath"]["path"] for volume in node["spec"]["template"]["spec"]["volumes"] if "hostPath" in volume}
    assert host_paths == {"/var/log/pods", "/var/lib/docker/containers", "/var/lib/struct-otel-collector"}, host_paths
    spec = node["spec"]["template"]["spec"]
    assert not spec.get("hostNetwork") and not spec.get("hostPID"), "the node collector stays in its own namespaces"
    assert container(node)["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["DAC_READ_SEARCH"]}

    # The hosts preset describes the machine the collector runs on. In a pod that is the pod.
    for half in ("node", "cluster"):
        error = render(f"ingest.key={KEY}", f'json:{half}.presets=["kubernetes-{half}","hosts"]', expect_failure=True)[0]["error"]
        assert "hosts preset is for the Linux install" in error, error


def test_rbac_is_read_only_and_each_collector_has_its_own_account() -> None:
    docs = render(f"ingest.key={KEY}")
    roles = [d for d in docs if d["kind"] == "ClusterRole"]
    for role in roles:
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= READ_ONLY_VERBS, f"{role['metadata']['name']}: {rule}"
            assert "secrets" not in rule["resources"], "the collector never reads Secrets"
    node_role = one(docs, "ClusterRole", "-node")
    assert {"apiGroups": [""], "resources": ["nodes/stats"], "verbs": ["get"]} in node_role["rules"]
    # Everything the node collector may read: its node's stats, and pods and namespaces.
    assert sorted(resource for rule in node_role["rules"] for resource in rule["resources"]) == [
        "namespaces", "nodes/stats", "pods"], node_role["rules"]
    cluster_role = one(docs, "ClusterRole", "-cluster")
    assert any("events.k8s.io" in rule["apiGroups"] for rule in cluster_role["rules"])

    for component, kind in (("node", "DaemonSet"), ("cluster", "Deployment")):
        account = one(docs, kind)["spec"]["template"]["spec"]["serviceAccountName"]
        assert account.endswith(f"-{component}")
        assert one(docs, "ServiceAccount", f"-{component}")["metadata"]["name"] == account
        binding = one(docs, "ClusterRoleBinding", f"-{component}")
        assert binding["roleRef"]["name"] == one(docs, "ClusterRole", f"-{component}")["metadata"]["name"]
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": account, "namespace": "struct"}]


def test_the_key_is_required_unless_a_secret_is_named() -> None:
    error = render(expect_failure=True)[0]["error"]
    assert "ingest.key" in error and "ingest.existingSecret" in error, error

    docs = render("ingest.existingSecret=my-secret", "ingest.existingSecretKey=key")
    assert not [d for d in docs if d["kind"] == "Secret"], "no Secret is created when one is named"
    for kind in ("DaemonSet", "Deployment"):
        reference = env_by_name(one(docs, kind))["STRUCT_INGEST_KEY"]["valueFrom"]["secretKeyRef"]
        assert reference == {"name": "my-secret", "key": "key"}


def test_each_half_can_be_switched_off() -> None:
    docs = render(f"ingest.key={KEY}", "node.enabled=false")
    assert not [d for d in docs if d["kind"] == "DaemonSet"]
    assert not [d for d in docs if d["metadata"]["name"].endswith("-node")], "node-only objects were still rendered"
    docs = render(f"ingest.key={KEY}", "cluster.enabled=false")
    assert not [d for d in docs if d["kind"] == "Deployment"]
    assert not [d for d in docs if d["metadata"]["name"].endswith("-cluster")]


def test_presets_settings_and_extra_config_reach_the_collector() -> None:
    docs = render(
        f"ingest.key={KEY}",
        'json:cluster.presets=["kubernetes-cluster","redis"]',
        'json:cluster.env=[{"name":"STRUCT_REDIS_ENDPOINT","value":"redis.default.svc:6379"}]',
        'json:cluster.extraConfig={"processors":{"memory_limiter":{"limit_mib":300}}}',
    )
    cluster = one(docs, "Deployment")
    assert container(cluster)["args"] == [
        "--config=/etc/struct-otel-collector/config.yaml",
        "--config=/etc/struct-otel-collector/presets/kubernetes-cluster.yaml",
        "--config=/etc/struct-otel-collector/presets/redis.yaml",
        "--config=/etc/struct-otel-collector-extra/extra.yaml",
    ]
    assert env_by_name(cluster)["STRUCT_REDIS_ENDPOINT"]["value"] == "redis.default.svc:6379"
    extra = one(docs, "ConfigMap", "-cluster-extra")
    assert yaml.safe_load(extra["data"]["extra.yaml"]) == {"processors": {"memory_limiter": {"limit_mib": 300}}}
    mounts = {m["mountPath"]: m["name"] for m in container(cluster)["volumeMounts"]}
    volumes = {v["name"]: v for v in cluster["spec"]["template"]["spec"]["volumes"]}
    assert volumes[mounts["/etc/struct-otel-collector-extra"]]["configMap"]["name"] == extra["metadata"]["name"]
    # The node collector asked for none, so it gets none.
    assert not [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-node-extra")]


def test_default_presets_exist_in_the_image() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    for half in ("node", "cluster"):
        for preset in values[half]["presets"]:
            assert (PRESETS_DIR / f"{preset}.yaml").is_file(), f"{half}.presets names {preset}, which the image lacks"


def test_what_the_chart_renders_validates_on_the_image() -> None:
    """The exact arguments and environment of each rendered workload, given to the collector."""
    docs = render(f"ingest.key={KEY}", "environment=production", "clusterName=prod-1")
    for kind in ("DaemonSet", "Deployment"):
        workload = one(docs, kind)
        # The host paths the pod mounts read-only exist in the pod; give the test container stand-ins.
        host_mounts = [m["mountPath"] for m in container(workload)["volumeMounts"] if m.get("readOnly")]
        result = validate(container(workload)["args"], literal_env(workload), kubernetes=True, empty_mounts=host_mounts)
        assert result.returncode == 0, f"{kind}:\n{result.stdout}\n{result.stderr}"


def load_script(name: str):
    """One of the package's scripts, as a module."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), PACKAGE_DIR / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_files() -> list[str]:
    """Every path in the image this checkout builds."""
    created = docker("create", build_image(), check=True).stdout.strip()
    try:
        export = subprocess.Popen([*docker_command(), "export", created], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=export.stdout, mode="r|") as archive:
            names = [member.name for member in archive]
        assert export.wait(timeout=300) == 0, "docker export failed"
        return names
    finally:
        docker("rm", "-f", created)


def chart_package() -> bytes:
    """The chart as `helm package` packs it: what a release pushes."""
    if shutil.which("helm") and not os.environ.get("STRUCT_TEST_HELM_IMAGE"):
        with tempfile.TemporaryDirectory() as directory:
            result = helm("package", "/chart", "--destination", directory)
            assert result.returncode == 0, f"helm package failed:\n{result.stdout}\n{result.stderr}"
            (packed,) = Path(directory).glob("*.tgz")
            return packed.read_bytes()
    result = docker(
        "run", "--rm", "-v", f"{CHART_DIR}:/chart:ro", "--entrypoint", "sh", HELM_IMAGE,
        "-c", "helm package /chart --destination /tmp >/dev/null && base64 /tmp/*.tgz",
    )
    assert result.returncode == 0, f"helm package failed:\n{result.stdout}\n{result.stderr}"
    return base64.b64decode(result.stdout)


def test_nothing_internal_is_in_the_image_the_chart_package_or_the_installer() -> None:
    """A release publishes these three and nothing else. None holds an AGENTS.md, a test or a tests directory."""
    release = load_script("release.py")
    files, chart, installer = image_files(), chart_package(), load_script("build-installer.py").build()
    counted = release.check_contents(files, chart, installer)
    # The checks looked at the real things: every config file is in the image and in the installer.
    configs = sorted(path.relative_to(CONFIG_DIR).as_posix() for path in CONFIG_DIR.rglob("*.yaml"))
    assert counted["installer config files"] == len(configs), counted
    for name in configs:
        assert f"etc/struct-otel-collector/{name}" in files, f"the image has no {name}"
    assert counted["chart files"] == sum(1 for path in CHART_DIR.rglob("*") if path.is_file()), counted
    # And the check does refuse: the same image with one test file, or an AGENTS.md, in it.
    for added in ("etc/struct-otel-collector/AGENTS.md", "etc/struct-otel-collector/tests/harness.py", "test_configs.py"):
        try:
            release.check_contents([*files, added], chart, installer)
        except release.ReleaseError as error:
            assert added in str(error), error
        else:
            raise AssertionError(f"an image with {added} was accepted")


TESTS = [
    test_chart_lints,
    test_default_render_has_a_collector_per_node_and_one_for_the_cluster,
    test_rbac_is_read_only_and_each_collector_has_its_own_account,
    test_the_key_is_required_unless_a_secret_is_named,
    test_each_half_can_be_switched_off,
    test_presets_settings_and_extra_config_reach_the_collector,
    test_identity_is_the_node_for_the_node_collector_and_the_cluster_for_the_cluster_collector,
    test_the_kubelet_is_reached_at_the_nodes_address_and_ipv6_can_be_bracketed,
    test_the_node_collector_reads_pod_logs_and_nothing_else_of_the_node,
    test_default_presets_exist_in_the_image,
    test_what_the_chart_renders_validates_on_the_image,
    test_nothing_internal_is_in_the_image_the_chart_package_or_the_installer,
]


def _cleanup() -> None:
    remove_image()
    remove_work_dir()


def teardown_module() -> None:  # pytest
    _cleanup()


if __name__ == "__main__":
    run_tests(TESTS, cleanup=_cleanup)
