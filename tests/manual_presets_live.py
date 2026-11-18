"""Manual suite: more presets against the real thing, through the built image.

Not one of the default test files: it pulls service images and one case reads this
machine's Docker socket. Each case starts the real service in a container,
runs the collector with the preset, and waits for named metrics to reach the
ingest stand-in from test_end_to_end.py.

    uv run python tests/manual_presets_live.py              # every case
    uv run python tests/manual_presets_live.py postgres     # the named cases

Cases: postgres, redpanda (the redpanda and kafka presets against one Redpanda
broker), docker, clickhouse. Presets with no case here (mysql, mongodb, nginx,
temporal, the two kubernetes ones) are checked by `validate` only.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Callable

from harness import BASE_CONFIG_ARG, RUN_ID, clickhouse_keeps, docker, preset_arg, run_tests, wait_for, work_dir
from test_end_to_end import (
    COLLECTOR,
    INGEST_KEY,
    NETWORK,
    IngestStandIn,
    cleanup,
    collector_logs,
    has_string_attribute,
    network_gateway,
    start_collector,
    stop_collector,
)

SERVICE = f"struct-otel-collector-test-service-{RUN_ID}"
POSTGRES_IMAGE = "postgres:16-bookworm"
# The Redpanda release the redpanda and kafka presets were last run against.
REDPANDA_IMAGE = "docker.redpanda.com/redpandadata/redpanda:v26.2.5"
# The release tests/fixtures/clickhouse-25.8-metric-names.txt was read from.
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:25.8"
CLICKHOUSE_PROMETHEUS = """<clickhouse>
  <prometheus>
    <endpoint>/metrics</endpoint>
    <port>9363</port>
    <metrics>true</metrics>
    <events>true</events>
    <asynchronous_metrics>true</asynchronous_metrics>
    <errors>true</errors>
  </prometheus>
</clickhouse>
"""


def run_case(
    presets: list[str],
    env: dict[str, str],
    expected: set[bytes],
    services: dict[str, str],
    *extra_docker_args: str,
    timeout: float = 90,
) -> None:
    ingest = IngestStandIn(network_gateway())
    try:
        start_collector(
            [BASE_CONFIG_ARG, *[preset_arg(p) for p in presets]],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, **env},
            *extra_docker_args,
        )

        def seen() -> bytes:
            return b"".join(request.body for request in ingest.snapshot())

        try:
            wait_for(lambda: all(name in seen() for name in expected), timeout=timeout,
                     what=f"{sorted(n.decode() for n in expected)}", interval=1)
        except TimeoutError as error:
            missing = sorted(name.decode() for name in expected if name not in seen())
            raise AssertionError(f"{error}; missing {missing}\n{collector_logs()}") from None
        for key, value in services.items():
            assert has_string_attribute(seen(), key, value), f"no resource carries {key}={value}"
        print(f"    {presets}: {sorted(n.decode() for n in expected)} arrived")
    finally:
        ingest.close()
        stop_collector()


def test_postgres_preset_against_postgres() -> None:
    network_gateway()
    docker("run", "-d", "--name", SERVICE, "--network", NETWORK, "-e", "POSTGRES_PASSWORD=live-test",
           POSTGRES_IMAGE, check=True)
    try:
        wait_for(lambda: docker("exec", SERVICE, "pg_isready", "-U", "postgres").returncode == 0,
                 timeout=60, what="Postgres to accept connections")
        run_case(
            ["postgres"],
            {"STRUCT_POSTGRES_ENDPOINT": f"{SERVICE}:5432", "STRUCT_POSTGRES_USERNAME": "postgres",
             "STRUCT_POSTGRES_PASSWORD": "live-test", "STRUCT_POSTGRES_TLS_INSECURE": "true",
             "STRUCT_POSTGRES_INTERVAL": "2s"},
            {b"postgresql.backends", b"postgresql.commits", b"postgresql.db_size"},
            {"service.name": "postgresql"},
        )
    finally:
        docker("rm", "-f", SERVICE)


def test_redpanda_and_kafka_presets_against_redpanda() -> None:
    network_gateway()
    docker(
        "run", "-d", "--name", SERVICE, "--network", NETWORK, REDPANDA_IMAGE,
        "redpanda", "start", "--mode", "dev-container", "--smp", "1", "--memory", "512M", "--overprovisioned",
        "--kafka-addr", "PLAINTEXT://0.0.0.0:9092", "--advertise-kafka-addr", f"PLAINTEXT://{SERVICE}:9092",
        check=True, timeout=600,
    )
    try:
        wait_for(lambda: docker("exec", SERVICE, "rpk", "cluster", "health").returncode == 0,
                 timeout=90, what="Redpanda to be healthy")
        docker("exec", SERVICE, "rpk", "topic", "create", "live-test", check=True)
        run_case(
            ["redpanda", "kafka"],
            {"STRUCT_REDPANDA_ENDPOINT": f"{SERVICE}:9644", "STRUCT_REDPANDA_INTERVAL": "2s",
             "STRUCT_KAFKA_BROKER": f"{SERVICE}:9092", "STRUCT_KAFKA_INTERVAL": "2s"},
            {
                # redpanda preset: the broker's own /public_metrics
                b"redpanda_cluster_brokers",
                b"redpanda_storage_disk_free_bytes",
                b"redpanda_kafka_under_replicated_replicas",
                # kafka preset: read over the Kafka protocol
                b"kafka.brokers",
                b"kafka.topic.partitions",
            },
            {"service.name": "redpanda"},
        )
    finally:
        docker("rm", "-f", SERVICE)


def test_docker_preset_against_this_machines_docker() -> None:
    socket = "/var/run/docker.sock"
    info = os.stat(socket)
    assert stat.S_ISSOCK(info.st_mode), f"{socket} is not a socket"
    network_gateway()
    run_case(
        ["docker"],
        {"STRUCT_DOCKER_INTERVAL": "2s"},
        {b"container.cpu.usage.total", b"container.memory.usage.total"},
        {"service.name": "docker"},
        "-v", f"{socket}:{socket}:ro", "--group-add", str(info.st_gid),
    )


def test_clickhouse_preset_against_clickhouse() -> None:
    """A real server: the preset sends its chosen set and nothing else, and the setting adds to it."""
    network_gateway()
    config = work_dir() / "clickhouse-prometheus.xml"
    config.write_text(CLICKHOUSE_PROMETHEUS)
    config.chmod(0o644)
    docker("run", "-d", "--name", SERVICE, "--network", NETWORK, "-e", "CLICKHOUSE_SKIP_USER_SETUP=1",
           "-v", f"{config}:/etc/clickhouse-server/config.d/prometheus.xml:ro", CLICKHOUSE_IMAGE, check=True, timeout=900)

    def sent(env: dict[str, str]) -> set[str]:
        ingest = IngestStandIn(network_gateway())
        try:
            start_collector(
                [BASE_CONFIG_ARG, preset_arg("clickhouse")],
                {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY,
                 "STRUCT_CLICKHOUSE_ENDPOINT": f"{SERVICE}:9363", "STRUCT_CLICKHOUSE_INTERVAL": "2s", **env},
            )

            def scrapes() -> list[bytes]:
                return [request.body for request in ingest.snapshot() if b"ClickHouseMetrics_Query" in request.body]

            try:
                wait_for(lambda: len(scrapes()) >= 2 or None, timeout=120, what="two ClickHouse scrapes", interval=1)
            except TimeoutError as error:
                raise AssertionError(f"{error}\n{collector_logs()}") from None
            seen = b"".join(scrapes())
            assert has_string_attribute(seen, "service.name", "clickhouse"), "no resource carries service.name=clickhouse"
            assert b"__tmp_struct_keep" not in seen, "the keep rules' temporary label was sent"
            return {name.decode() for name in re.findall(rb"ClickHouse(?:Metrics|AsyncMetrics|ProfileEvents|ErrorMetric)_\w+", seen)}
        finally:
            ingest.close()
            stop_collector()

    try:
        wait_for(lambda: docker("exec", SERVICE, "clickhouse-client", "-q", "select 1").returncode == 0,
                 timeout=120, what="ClickHouse to answer")
        default = sent({})
        # Nothing the rules do not keep, and nearly all of the chosen set.
        outside = sorted(name for name in default if not clickhouse_keeps(name))
        assert not outside, outside[:5]
        assert len(default) >= 120, f"only {len(default)} of the chosen set arrived"
        for name in ("ClickHouseProfileEvents_Query", "ClickHouseMetrics_MemoryTracking", "ClickHouseAsyncMetrics_Uptime",
                     "ClickHouseErrorMetric_ALL"):
            assert name in default, f"{name} did not arrive"
        everything = sent({"STRUCT_CLICKHOUSE_EXTRA_METRICS": ".+"})
        assert default < everything and len(everything) > 1500, f"{len(everything)} names with everything kept"
        print(f"    [clickhouse]: {len(default)} names by default, {len(everything)} with STRUCT_CLICKHOUSE_EXTRA_METRICS=.+")
    finally:
        docker("rm", "-f", SERVICE)


CASES: dict[str, Callable[[], None]] = {
    "postgres": test_postgres_preset_against_postgres,
    "redpanda": test_redpanda_and_kafka_presets_against_redpanda,
    "docker": test_docker_preset_against_this_machines_docker,
    "clickhouse": test_clickhouse_preset_against_clickhouse,
}


def _cleanup() -> None:
    docker("rm", "-f", SERVICE, COLLECTOR)
    cleanup()


if __name__ == "__main__":
    chosen = sys.argv[1:] or list(CASES)
    unknown = [name for name in chosen if name not in CASES]
    if unknown:
        raise SystemExit(f"unknown case(s) {unknown}; choose from {sorted(CASES)}")
    run_tests([CASES[name] for name in chosen], cleanup=_cleanup)
