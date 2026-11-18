"""The base config and every preset load on the pinned collector, alone and together.

Two kinds of check:

1. Structure, read from the YAML. Collector config merging replaces lists, so a
   preset that redefined a shared list, or reused another preset's component or
   pipeline name, would silently switch something off when both are loaded.
2. The collector's own `validate`, run on the image this checkout builds, for
   the base config, each preset, and the combinations the chart and the install
   script produce.

Requires Docker.

Usage:
    uv run python tests/test_configs.py
    # Or: uv run pytest tests/test_configs.py -v
"""

from __future__ import annotations

import re

from harness import (
    BASE_CONFIG_ARG,
    CHART_DIR,
    CONFIG_DIR,
    INGEST_MAX_BODY_BYTES,
    INSTALL_SCRIPT,
    PACKAGE_DIR,
    PRESETS_DIR,
    clickhouse_keep_rules,
    clickhouse_kept,
    clickhouse_names,
    documented_settings,
    load_yaml,
    needs_kubernetes,
    preset_arg,
    preset_names,
    referenced_settings,
    remove_image,
    remove_work_dir,
    required_env,
    run_tests,
    validate,
)

# The launch set. A preset added or removed has to be added or removed here on
# purpose, with its docs.
EXPECTED_PRESETS = [
    "clickhouse",
    "docker",
    "hosts",
    "kafka",
    "kubernetes-cluster",
    "kubernetes-node",
    "mongodb",
    "mysql",
    "nginx",
    "otlp",
    "postgres",
    "prometheus",
    "redis",
    "redpanda",
    "temporal",
]

EXPORTER = "otlp_http/struct"
SHARED_PROCESSORS = {"memory_limiter", "resource_detection/struct"}


def base_config() -> dict:
    return load_yaml(CONFIG_DIR / "config.yaml")


def preset_config(name: str) -> dict:
    return load_yaml(PRESETS_DIR / f"{name}.yaml")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_the_launch_set_is_the_expected_presets() -> None:
    assert preset_names() == EXPECTED_PRESETS, preset_names()


def test_one_upstream_version_everywhere() -> None:
    """The Dockerfile, the install script and the chart name the same upstream release, and the
    chart's version is one release of it, <upstream>-<n>, which its appVersion repeats."""
    dockerfile = (PACKAGE_DIR / "Dockerfile").read_text()
    image_version = re.search(r"^FROM otel/opentelemetry-collector-contrib:(\S+)$", dockerfile, re.MULTILINE)
    assert image_version, "Dockerfile must pin otel/opentelemetry-collector-contrib:<version>"
    install_version = re.search(r'^UPSTREAM_VERSION="([^"]+)"$', INSTALL_SCRIPT.read_text(), re.MULTILINE)
    assert install_version, "install.sh must set UPSTREAM_VERSION"
    chart = load_yaml(CHART_DIR / "Chart.yaml")
    release = re.fullmatch(r"(\d+\.\d+\.\d+)-([1-9]\d*)", str(chart["version"]))
    assert release, f"Chart.yaml version must be <upstream>-<n>, got {chart['version']!r}"
    assert str(chart["appVersion"]) == str(chart["version"]), "the chart deploys the image of its own release"
    versions = {
        "Dockerfile": image_version.group(1),
        "install.sh": install_version.group(1),
        "Chart.yaml version": release.group(1),
    }
    assert len(set(versions.values())) == 1, versions


def test_base_exporter_fits_the_ingest_contract() -> None:
    """OTLP over HTTP with the ingest key header, and requests bounded by bytes under ingest's limit."""
    config = base_config()
    assert list(config["exporters"]) == [EXPORTER], "one exporter: everything goes to Struct ingest"
    exporter = config["exporters"][EXPORTER]
    assert exporter["endpoint"] == "${env:STRUCT_INGEST_URL:-https://ingest.struct.ai}"
    assert exporter["headers"] == {"x-struct-ingest-key": "${env:STRUCT_INGEST_KEY}"}
    # Ingest answers 415 to anything but gzip or no encoding.
    assert exporter["compression"] in ("gzip", "none")
    queue = exporter["sending_queue"]
    assert queue["enabled"] is True
    assert queue["storage"] == "file_storage/struct", "the queue must survive a restart"
    assert queue["block_on_overflow"] is False, "a full queue must not stall the receivers"
    # Bounded in bytes: what arrives is counted before it is split into requests, so a
    # count of requests would not bound what waits on disk.
    assert queue["sizer"] == "bytes" and 0 < queue["queue_size"] <= 256 * 1024 * 1024, queue
    batch = queue["batch"]
    assert batch["sizer"] == "bytes", "a record count cannot bound a request"
    # Leave room for what the size estimate does not count, such as resource
    # attributes repeated when a request is split.
    assert batch["min_size"] <= batch["max_size"] <= INGEST_MAX_BODY_BYTES * 3 // 4, batch
    retry = exporter["retry_on_failure"]
    # Sent again until taken: the queue's size alone decides how long an outage is survived.
    assert retry["enabled"] is True and retry["max_elapsed_time"] in (0, "0s"), retry
    assert config["service"]["extensions"] == ["health_check", "file_storage/struct"]
    assert SHARED_PROCESSORS <= set(config["processors"])


def test_base_pipelines_are_bounded() -> None:
    for name, pipeline in base_config()["service"]["pipelines"].items():
        assert pipeline["processors"][0] == "memory_limiter", name
        assert pipeline["exporters"] == [EXPORTER], name


def test_presets_only_add_their_own_components() -> None:
    """A preset adds receivers, processors and pipelines under names no other file uses, and nothing else."""
    base = base_config()
    seen: dict[str, str] = {}
    for kind in ("receivers", "processors"):
        for component in base.get(kind, {}):
            seen[f"{kind}:{component}"] = "config.yaml"
    for pipeline in base["service"]["pipelines"]:
        seen[f"pipeline:{pipeline}"] = "config.yaml"

    for name in preset_names():
        config = preset_config(name)
        assert set(config) <= {"receivers", "processors", "service"}, (
            f"{name}: a preset may define receivers, processors and service.pipelines only, got {sorted(config)}"
        )
        assert set(config["service"]) == {"pipelines"}, (
            f"{name}: service.extensions and service.telemetry are lists or settings the base config owns"
        )
        for kind in ("receivers", "processors"):
            for component in config.get(kind, {}):
                key = f"{kind}:{component}"
                assert key not in seen, f"{name}: {component} is already defined in {seen[key]}"
                seen[key] = f"{name}.yaml"
        own_receivers = set(config["receivers"])
        own_processors = set(config.get("processors", {}))
        used_receivers: set[str] = set()
        used_processors: set[str] = set()
        for pipeline_name, pipeline in config["service"]["pipelines"].items():
            key = f"pipeline:{pipeline_name}"
            assert key not in seen, f"{name}: pipeline {pipeline_name} is already defined in {seen[key]}"
            seen[key] = f"{name}.yaml"
            assert pipeline["exporters"] == [EXPORTER], f"{name}: {pipeline_name}"
            assert pipeline["processors"][0] == "memory_limiter", f"{name}: {pipeline_name}"
            assert "resource_detection/struct" in pipeline["processors"], f"{name}: {pipeline_name}"
            assert set(pipeline["receivers"]) <= own_receivers, f"{name}: {pipeline_name} uses another file's receiver"
            assert set(pipeline["processors"]) <= own_processors | SHARED_PROCESSORS, (
                f"{name}: {pipeline_name} uses a processor it does not define"
            )
            used_receivers |= set(pipeline["receivers"])
            used_processors |= set(pipeline["processors"])
        assert used_receivers == own_receivers, f"{name}: unused receivers {own_receivers - used_receivers}"
        assert own_processors <= used_processors, f"{name}: unused processors {own_processors - used_processors}"


def test_every_series_gets_a_service_name() -> None:
    """Struct's catalog groups by service: a pipeline either scrapes Prometheus (the job
    name becomes service.name) or sets service.name itself."""
    for name in preset_names():
        config = preset_config(name)
        processors = config.get("processors", {})
        for pipeline_name, pipeline in config["service"]["pipelines"].items():
            if all(receiver.startswith("prometheus/") for receiver in pipeline["receivers"]):
                continue
            sets_name = False
            for processor in pipeline["processors"]:
                body = processors.get(processor) or {}
                if processor.startswith("resource/"):
                    sets_name |= any(a["key"] == "service.name" and a["action"] == "insert" for a in body["attributes"])
                if processor.startswith("transform/"):
                    sets_name |= 'attributes["service.name"]' in str(body)
            assert sets_name, f"{name}: {pipeline_name} never sets service.name"


def test_the_otlp_preset_listens_on_this_machine_only_by_default() -> None:
    """Its receiver has no authentication: whoever reaches the port writes into the workspace."""
    protocols = preset_config("otlp")["receivers"]["otlp/apps"]["protocols"]
    assert protocols["grpc"]["endpoint"] == "${env:STRUCT_OTLP_GRPC_ENDPOINT:-127.0.0.1:4317}"
    assert protocols["http"]["endpoint"] == "${env:STRUCT_OTLP_HTTP_ENDPOINT:-127.0.0.1:4318}"
    assert "otlp" not in load_yaml(CHART_DIR / "values.yaml")["cluster"]["presets"]


def test_every_setting_is_documented_in_its_files_header() -> None:
    """Each file's header lists exactly the settings its YAML reads, and says `required`
    for the ones with no default. The install script refuses to start a preset whose
    required settings are missing, and it reads them from these headers: the collector
    itself treats an unset variable as an empty string and starts anyway."""
    files = {"config.yaml": CONFIG_DIR / "config.yaml"}
    files.update({f"presets/{name}.yaml": PRESETS_DIR / f"{name}.yaml" for name in preset_names()})
    for label, path in files.items():
        text = path.read_text()
        documented = documented_settings(text)
        referenced = referenced_settings(text)
        # Read by the `env` resource detector, not referenced in the YAML.
        documented.pop("OTEL_RESOURCE_ATTRIBUTES", None)
        assert set(documented) == set(referenced), (
            f"{label}: header and YAML disagree: only in header {sorted(set(documented) - set(referenced))}, "
            f"only in YAML {sorted(set(referenced) - set(documented))}"
        )
        for name, has_default in referenced.items():
            if has_default:
                assert documented[name] in ("default", "optional"), f"{label}: {name} has a default but is documented required"
            else:
                assert documented[name] == "required", f"{label}: {name} has no default, so the header must say required"


# ---------------------------------------------------------------------------
# The collector's own validation
# ---------------------------------------------------------------------------

def _validation_error(presets: list[str]) -> str:
    result = validate(
        [BASE_CONFIG_ARG, *[preset_arg(p) for p in presets]],
        required_env(presets),
        kubernetes=needs_kubernetes(presets),
    )
    if result.returncode == 0:
        return ""
    return f"presets {presets or '(none)'}:\n{result.stdout}\n{result.stderr}"


def _assert_valid(*combinations: list[str]) -> None:
    errors = [error for error in map(_validation_error, combinations) if error]
    assert not errors, "\n".join(errors)


def test_base_config_validates() -> None:
    _assert_valid([])


def test_each_preset_validates_alone() -> None:
    _assert_valid(*[[name] for name in preset_names()])


def test_all_presets_validate_together() -> None:
    _assert_valid(preset_names())


def test_the_charts_combinations_validate() -> None:
    """What the chart's DaemonSet and Deployment load by default."""
    _assert_valid(["kubernetes-node"], ["kubernetes-cluster"])


def test_a_config_of_the_customers_own_merges_into_a_preset() -> None:
    """What `--extra-config` and the chart's `extraConfig` rely on: a later config's maps merge
    into a preset's receiver, so a secured Kafka cluster needs only the keys it adds."""
    tls = "--config=yaml:receivers::kafka_metrics::tls::insecure_skip_verify: true"
    result = validate([BASE_CONFIG_ARG, preset_arg("kafka"), tls])
    assert result.returncode == 0, result.stdout + result.stderr
    # The check has teeth: a key the receiver does not have is refused, so the file is read as
    # part of the preset's receiver and not set aside.
    wrong = validate([BASE_CONFIG_ARG, preset_arg("kafka"), "--config=yaml:receivers::kafka_metrics::tls::not_a_key: true"])
    assert wrong.returncode != 0 and "not_a_key" in wrong.stdout + wrong.stderr, wrong.stdout + wrong.stderr


def test_presets_pin_the_upstream_defaults_that_changed_under_them() -> None:
    """An upstream release can change a receiver's default, and with it what a preset sends.
    Where a setting keeps what the preset sent before, the preset sets it: these are the pins
    made when the pinned release moved from 0.161.0 to 0.165.0. Each is a decision to take
    back on purpose, not by losing a line; `validate` checks the release still has the keys."""
    postgres = preset_config("postgres")["receivers"]["postgresql"]
    # 0.163.0 made a collection's deadline 60s; before, a collection had none.
    assert postgres["timeout"] == "0s", postgres
    # 0.162.0 began sending these two on every MySQL resource.
    assert preset_config("mysql")["receivers"]["mysql"]["resource_attributes"] == {
        "server.address": {"enabled": False}, "server.port": {"enabled": False}}
    # 0.163.0 began sending the port on every MongoDB resource.
    assert preset_config("mongodb")["receivers"]["mongodb"]["resource_attributes"] == {"server.port": {"enabled": False}}


def _alternatives(pattern: str) -> list[str]:
    """Every name a keep rule spells out: `A_(b|c(d|e))` gives A_b, A_cd, A_ce. A part that is
    not a literal (`.+`) stays as written, so the result is still an expression."""
    def expand(text: str, start: int) -> tuple[list[str], int]:
        done: list[str] = []
        current = [""]
        i = start
        while i < len(text):
            char = text[i]
            if char == "(":
                inner, i = expand(text, i + 1)
                current = [head + tail for head in current for tail in inner]
            elif char == ")":
                return done + current, i + 1
            elif char == "|":
                done += current
                current = [""]
                i += 1
            else:
                current = [head + char for head in current]
                i += 1
        return done + current, i
    return expand(pattern, 0)[0]


def test_the_clickhouse_preset_keeps_a_chosen_set_of_what_a_server_exposes() -> None:
    """Checked against every metric name one ClickHouse 25.8 server exposed with metrics, events,
    asynchronous metrics and errors on (tests/fixtures/clickhouse-25.8-metric-names.txt). What the
    collector itself does with these rules is in test_end_to_end.py."""
    names = clickhouse_names()
    assert len(names) > 2000, "the fixture is the whole list, not a sample"
    rules = clickhouse_keep_rules()
    marks = [rule for rule in rules if rule.get("target_label") == "__tmp_struct_keep"]
    # The mark is temporary: the last two rules keep what is marked and take the mark off again.
    assert rules[-2] == {"source_labels": ["__tmp_struct_keep"], "regex": "1", "action": "keep"}, rules[-2]
    assert rules[-1] == {"regex": "__tmp_struct_keep", "action": "labeldrop"}, rules[-1]
    assert marks == rules[:-2] and all(rule["source_labels"] == ["__name__"] and rule["replacement"] == "1" for rule in marks)
    chosen, setting = [rule["regex"] for rule in marks[:-1]], marks[-1]["regex"]
    assert setting == "${env:STRUCT_CLICKHOUSE_EXTRA_METRICS:-none}", setting

    default = clickhouse_kept()
    assert 110 <= len(default) <= 160, f"the default keeps {len(default)} of {len(names)}"
    for name in (
        "ClickHouseProfileEvents_Query", "ClickHouseProfileEvents_FailedQuery", "ClickHouseProfileEvents_InsertedRows",
        "ClickHouseProfileEvents_RejectedInserts", "ClickHouseMetrics_Query", "ClickHouseMetrics_Merge",
        "ClickHouseMetrics_PartsActive", "ClickHouseMetrics_MemoryTracking", "ClickHouseMetrics_ReadonlyReplica",
        "ClickHouseMetrics_ZooKeeperSession", "ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay",
        "ClickHouseAsyncMetrics_MaxPartCountForPartition", "ClickHouseAsyncMetrics_DiskAvailable_default",
        "ClickHouseAsyncMetrics_MemoryResident", "ClickHouseAsyncMetrics_Uptime", "ClickHouseErrorMetric_ALL",
    ):
        assert name in default, f"the default leaves out {name}"
    # What makes the full list large is left out: one counter for each error code, the host's
    # processor time repeated for each core, and one gauge triple for each thread pool.
    assert [name for name in default if name.startswith("ClickHouseErrorMetric_")] == ["ClickHouseErrorMetric_ALL"]
    assert not [name for name in default if re.search(r"CPU\d+$|_nvme|_eth\d", name)]
    assert sum(name.endswith(("ThreadsActive", "ThreadsScheduled")) for name in default) == 0
    # A name a rule spells out is a name a server has: a misspelt one would quietly keep nothing.
    for expression in chosen:
        for spelt in _alternatives(expression):
            assert any(re.fullmatch(spelt, name) for name in names), f"no metric is named {spelt}"
    # The setting adds to the default, and `.+` is everything.
    errors = clickhouse_kept("ClickHouseErrorMetric_.+")
    assert default < errors and len(errors) - len(default) > 600
    assert clickhouse_kept(".+") == set(names)


def test_an_unknown_component_is_refused() -> None:
    """The check has teeth: a config naming a component this release lacks fails."""
    result = validate([BASE_CONFIG_ARG, "--config=yaml:receivers::not_a_receiver::endpoint: localhost:1"])
    assert result.returncode != 0, "a config with an unknown receiver validated"
    assert 'unknown type: "not_a_receiver"' in result.stdout + result.stderr, result.stderr


TESTS = [
    test_the_launch_set_is_the_expected_presets,
    test_one_upstream_version_everywhere,
    test_base_exporter_fits_the_ingest_contract,
    test_base_pipelines_are_bounded,
    test_presets_only_add_their_own_components,
    test_every_series_gets_a_service_name,
    test_the_otlp_preset_listens_on_this_machine_only_by_default,
    test_every_setting_is_documented_in_its_files_header,
    test_base_config_validates,
    test_each_preset_validates_alone,
    test_all_presets_validate_together,
    test_the_charts_combinations_validate,
    test_a_config_of_the_customers_own_merges_into_a_preset,
    test_presets_pin_the_upstream_defaults_that_changed_under_them,
    test_the_clickhouse_preset_keeps_a_chosen_set_of_what_a_server_exposes,
    test_an_unknown_component_is_refused,
]


def _cleanup() -> None:
    remove_image()
    remove_work_dir()


def teardown_module() -> None:  # pytest
    _cleanup()


if __name__ == "__main__":
    run_tests(TESTS, cleanup=_cleanup)
