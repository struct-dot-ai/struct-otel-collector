"""Shared helpers for the Struct Collector tests.

Every test process gets its own image tag, container names, Docker network and
ports (RUN_ID), so separate test processes can run at the same time. Nothing
waits on a fixed sleep: readiness and arrival are bounded polls.

Docker: the tests call `docker`. Where the current user cannot reach the Docker
socket but passwordless sudo can, they fall back to `sudo -n docker`. Set
STRUCT_TEST_DOCKER to choose the command yourself (for example "podman").
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

try:
    import yaml
except ImportError:  # pragma: no cover - a setup error, not a test result
    raise SystemExit("PyYAML required: run the tests with `uv run`")

RUN_ID = uuid4().hex[:12]
PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_DIR / "config"
PRESETS_DIR = CONFIG_DIR / "presets"
CHART_DIR = PACKAGE_DIR / "chart" / "struct-otel-collector"
INSTALL_SCRIPT = PACKAGE_DIR / "install" / "install.sh"

# Where the image keeps what CONFIG_DIR holds.
IMAGE_CONFIG_DIR = "/etc/struct-otel-collector"
BASE_CONFIG_ARG = f"--config={IMAGE_CONFIG_DIR}/config.yaml"

IMAGE = f"struct-otel-collector-test:{RUN_ID}"

# Struct ingest's request limits, which the exporter has to fit.
INGEST_MAX_BODY_BYTES = 2 * 1024 * 1024
INGEST_MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024

# What each preset needs set before its config is complete. Presets not listed
# validate with their defaults alone.
PRESET_REQUIRED_ENV: dict[str, dict[str, str]] = {
    "postgres": {"STRUCT_POSTGRES_USERNAME": "monitor", "STRUCT_POSTGRES_PASSWORD": "secret"},
    "mysql": {"STRUCT_MYSQL_USERNAME": "monitor", "STRUCT_MYSQL_PASSWORD": "secret"},
    "prometheus": {"STRUCT_PROMETHEUS_TARGET": "localhost:9100"},
    "kubernetes-node": {
        "K8S_NODE_NAME": "node-1",
        "STRUCT_KUBELET_ENDPOINT": "https://10.255.255.2:10250",
        "K8S_POD_NAMESPACE": "struct",
        "K8S_POD_NAME": "struct-otel-collector-node-abcde",
    },
}

T = TypeVar("T")


def preset_names() -> list[str]:
    return sorted(path.stem for path in PRESETS_DIR.glob("*.yaml"))


def preset_arg(name: str) -> str:
    return f"--config={IMAGE_CONFIG_DIR}/presets/{name}.yaml"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# Every metric name one ClickHouse 25.8 server exposed with metrics, events, asynchronous metrics
# and errors switched on, one a line. The clickhouse preset's keep rules are checked against it.
CLICKHOUSE_NAMES = PACKAGE_DIR / "tests" / "fixtures" / "clickhouse-25.8-metric-names.txt"


def clickhouse_names() -> list[str]:
    return CLICKHOUSE_NAMES.read_text().split()


def clickhouse_keep_rules() -> list[dict]:
    scrape = load_yaml(PRESETS_DIR / "clickhouse.yaml")["receivers"]["prometheus/clickhouse"]["config"]["scrape_configs"][0]
    return scrape["metric_relabel_configs"]


def clickhouse_keeps(name: str, extra: str = "none") -> bool:
    """Whether the clickhouse preset keeps a metric of this name, with
    STRUCT_CLICKHOUSE_EXTRA_METRICS set to ``extra``. Prometheus anchors a relabel expression at
    both ends."""
    marks = [rule["regex"] for rule in clickhouse_keep_rules() if rule.get("target_label") == "__tmp_struct_keep"]
    return any(re.fullmatch(expression, name) for expression in [*marks[:-1], extra])


def clickhouse_kept(extra: str = "none") -> set[str]:
    """The names the clickhouse preset keeps of that 25.8 server's."""
    return {name for name in clickhouse_names() if clickhouse_keeps(name, extra)}


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

_docker_command: list[str] | None = None


def docker_command() -> list[str]:
    global _docker_command
    if _docker_command is None:
        override = os.environ.get("STRUCT_TEST_DOCKER", "").strip()
        candidates = [shlex.split(override)] if override else [["docker"], ["sudo", "-n", "docker"]]
        for candidate in candidates:
            try:
                probe = subprocess.run([*candidate, "info"], check=False, capture_output=True, text=True, timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                _docker_command = candidate
                break
        else:
            raise SystemExit("Docker is required: none of " + ", ".join(" ".join(c) for c in candidates) + " works")
    return _docker_command


def docker(*args: str, check: bool = False, timeout: float | None = 300, input: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run([*docker_command(), *args], check=False, capture_output=True, text=True, timeout=timeout, input=input)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


_image_built = False


def build_image() -> str:
    """Build the image this checkout produces, once per test process."""
    global _image_built
    if not _image_built:
        docker("build", "-t", IMAGE, str(PACKAGE_DIR), check=True, timeout=600)
        _image_built = True
    return IMAGE


def remove_image() -> None:
    global _image_built
    if _image_built:
        docker("rmi", "-f", IMAGE)
        _image_built = False


def env_args(env: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args


def work_dir() -> Path:
    """This process's scratch directory. Under $HOME, which Docker Desktop shares by default."""
    path = Path.home() / ".cache" / "struct-otel-collector-tests" / RUN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_work_dir() -> None:
    shutil.rmtree(Path.home() / ".cache" / "struct-otel-collector-tests" / RUN_ID, ignore_errors=True)


_service_account_dir: Path | None = None


def service_account_dir() -> Path:
    """A stand-in for the service account files Kubernetes mounts into a pod.

    `validate` builds the pipelines, and the kubelet receiver reads the cluster CA
    while it is built, so the Kubernetes presets only validate with one present.
    The certificate is made here, self-signed and thrown away with the directory.
    """
    global _service_account_dir
    if _service_account_dir is None:
        directory = work_dir() / "serviceaccount"
        directory.mkdir(exist_ok=True)
        key = directory / "key.pem"
        made = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=struct-otel-collector-test",
             "-days", "2", "-keyout", str(key), "-out", str(directory / "ca.crt")],
            check=False, capture_output=True, text=True,
        )
        if made.returncode != 0:
            raise RuntimeError(f"openssl is required to validate the Kubernetes presets:\n{made.stderr}")
        key.unlink()
        (directory / "token").write_text("not-a-real-token")
        (directory / "namespace").write_text("struct")
        directory.chmod(0o755)
        for path in directory.iterdir():
            path.chmod(0o644)  # the collector runs as another user
        _service_account_dir = directory
    return _service_account_dir


def kubernetes_args() -> list[str]:
    """docker run arguments that make a container look like a pod to the Kubernetes components."""
    return [
        "-e", "KUBERNETES_SERVICE_HOST=10.255.255.1",
        "-e", "KUBERNETES_SERVICE_PORT=443",
        "--mount", f"type=bind,src={service_account_dir()},dst=/var/run/secrets/kubernetes.io/serviceaccount,readonly",
    ]


def validate(
    config_args: Iterable[str],
    env: Mapping[str, str] | None = None,
    *,
    kubernetes: bool = False,
    empty_mounts: Iterable[str] = (),
) -> subprocess.CompletedProcess:
    """Run the collector's own `validate` on the built image with these --config arguments.

    `empty_mounts` are container paths that must exist for the config to load (a
    workload's volume mounts); each gets an empty read-only directory.
    """
    full_env = {"STRUCT_INGEST_KEY": "pk-validate"}
    full_env.update(env or {})
    extra = kubernetes_args() if kubernetes else []
    empty = work_dir() / "empty"
    empty.mkdir(exist_ok=True)
    empty.chmod(0o755)
    for path in empty_mounts:
        extra += ["--mount", f"type=bind,src={empty},dst={path},readonly"]
    return docker("run", "--rm", *env_args(full_env), *extra, build_image(), "validate", *config_args)


def required_env(presets: Iterable[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in presets:
        env.update(PRESET_REQUIRED_ENV.get(name, {}))
    return env


def needs_kubernetes(presets: Iterable[str]) -> bool:
    return any(name.startswith("kubernetes-") for name in presets)


# A preset's header documents each setting on a line `#   NAME   <default ...|required|optional>`.
# The install script reads the `required` ones from there.
_SETTING_LINE = re.compile(r"^#   ([A-Z][A-Z0-9_]+)\s+(required|optional|default)\b", re.MULTILINE)
_ENV_REFERENCE = re.compile(r"\$\{env:([A-Z][A-Z0-9_]+)(:-[^}]*)?\}")


def documented_settings(text: str) -> dict[str, str]:
    """Setting name to `required`, `optional` or `default`, from a config file's header."""
    return {name: kind for name, kind in _SETTING_LINE.findall(text)}


def referenced_settings(text: str) -> dict[str, bool]:
    """Setting name to whether every reference to it carries a default, from the YAML body."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    settings: dict[str, bool] = {}
    for name, default in _ENV_REFERENCE.findall(body):
        settings[name] = settings.get(name, True) and bool(default)
    return settings


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

def wait_for(check: Callable[[], T | None], *, timeout: float, what: str, interval: float = 0.25) -> T:
    """Poll `check` until it returns something truthy; fail with `what` at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def run_tests(tests: Iterable[Callable[[], None]], *, cleanup: Callable[[], None] = lambda: None) -> None:
    """The `python tests/test_x.py` runner: run each, print PASS/FAIL, exit non-zero on any failure."""
    failed = 0
    try:
        for test in tests:
            try:
                test()
            except Exception as error:  # noqa: BLE001 - report and keep going
                failed += 1
                print(f"  FAIL {test.__name__}: {error}")
            else:
                print(f"  PASS {test.__name__}")
    finally:
        cleanup()
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
