"""Run the built image for real, against a stand-in for Struct ingest.

The stand-in is an HTTP server in this process that records what the collector
posts: path, headers, the size on the wire and the decompressed body. It is
bound to the gateway address of a Docker network made for this test process, so
only containers on that network (and this host) can reach it, and no port is
published for it.

What is checked:

1. The hosts preset, the redis preset (against a real Redis in a container), the
   prometheus preset (scraping a plain endpoint the stand-in serves) and the
   collector's own metrics all arrive, as OTLP protobuf on /v1/metrics, gzip
   encoded, with the ingest key in x-struct-ingest-key and the deployment
   environment on the resource. Ingest answering 503 first does not lose them.
   Nothing the collector logs calls the shipped config deprecated, and the two
   Kubernetes presets start without such a warning either.
2. A burst far larger than ingest's 2 MiB body limit is split into requests that
   each fit, and every record arrives exactly once.
3. Records waiting while ingest refuses everything are still sent, once each,
   after the collector is restarted.
4. While ingest refuses everything, the queue holds what its size in bytes
   allows and refuses the rest to the sender; everything it accepted arrives
   once ingest answers.
5. One record larger than a whole request is dropped and logged, never sent,
   and the records around it, in the same request and in later ones, arrive.
6. With the otlp preset, an application's traces, metrics and logs sent to the
   collector reach ingest with the ingest key, keeping the application's own
   service name, host and environment. Each resource is judged on its own
   entry: one request can also carry the collector's own metrics.

Requires Docker. Takes a couple of minutes: the collector scrapes its own
metrics once a minute.

Usage:
    uv run python tests/test_end_to_end.py
    # Or: uv run pytest tests/test_end_to_end.py -v
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import (
    BASE_CONFIG_ARG,
    INGEST_MAX_BODY_BYTES,
    RUN_ID,
    build_image,
    clickhouse_kept,
    clickhouse_names,
    docker,
    env_args,
    kubernetes_args,
    preset_arg,
    remove_image,
    remove_work_dir,
    required_env,
    run_tests,
    wait_for,
)

NETWORK = f"struct-otel-collector-test-{RUN_ID}"
COLLECTOR = f"struct-otel-collector-test-{RUN_ID}"
REDIS = f"struct-otel-collector-test-redis-{RUN_ID}"
# The official image from Amazon's public mirror, which has no anonymous pull limit.
REDIS_IMAGE = "public.ecr.aws/docker/library/redis:7.4-alpine"
INGEST_KEY = f"pk-test-{RUN_ID}"
ENVIRONMENT = f"e2e-{RUN_ID}"
SCRAPED_JOB = f"scraped-{RUN_ID}"
SCRAPED_METRIC = "struct_test_scraped_gauge"

# Loaded with --config=env:STRUCT_TEST_OVERLAY in the burst test only: a way to
# hand the collector more data at once than any preset produces in a test. It
# adds a receiver and a pipeline and changes nothing in the config under test.
BURST_OVERLAY = """
receivers:
  otlp/test:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
service:
  pipelines:
    logs/test:
      receivers: [otlp/test]
      processors: [memory_limiter]
      exporters: [otlp_http/struct]
"""

# Loaded after BURST_OVERLAY in the full-queue test only: the queue's own bound, made small
# enough that a test can fill it. The sizer, the batch sizes, the retry and what happens on
# overflow all stay the config under test.
SMALL_QUEUE_BYTES = 3_000_000
SMALL_QUEUE_OVERLAY = f"""
exporters:
  otlp_http/struct:
    sending_queue:
      queue_size: {SMALL_QUEUE_BYTES}
"""


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    wire_bytes: int
    body: bytes  # after gzip is undone


class IngestStandIn:
    """Records every POST. Answers 503 to the first `refuse_first` of them, then 200."""

    def __init__(self, host: str, refuse_first: int = 0) -> None:
        self.accepted: list[Received] = []
        self.refused: list[Received] = []
        # What GET /metrics answers; a test may put another exposition here.
        self.metrics_body = f"# TYPE {SCRAPED_METRIC} gauge\n{SCRAPED_METRIC} 42\n".encode()
        self._refuse = refuse_first
        self._lock = threading.Lock()
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = self.headers.get("Content-Length")
                if length is None:
                    self.send_error(411)
                    return
                raw = self.rfile.read(int(length))
                headers = {key.lower(): value for key, value in self.headers.items()}
                body = gzip.decompress(raw) if headers.get("content-encoding") == "gzip" else raw
                received = Received(self.path, headers, len(raw), body)
                with stand_in._lock:
                    refuse = stand_in._refuse > 0
                    if refuse:
                        stand_in._refuse -= 1
                        stand_in.refused.append(received)
                    else:
                        stand_in.accepted.append(received)
                if refuse:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # An empty body is a valid OTLP export response in protobuf.
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                # A plain Prometheus endpoint for the scrape preset to read. Not part of ingest.
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = stand_in.metrics_body
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self.address = f"{host}:{self._server.server_address[1]}"
        self.url = f"http://{self.address}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def snapshot(self) -> list[Received]:
        with self._lock:
            return list(self.accepted)

    def refused_snapshot(self) -> list[Received]:
        with self._lock:
            return list(self.refused)

    def accept_from_now(self) -> None:
        with self._lock:
            self._refuse = 0

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def has_string_attribute(body: bytes, key: str, value: str) -> bool:
    """Whether `body` holds an OTLP KeyValue with this key and this string value.

    The stand-in keeps bodies as bytes and has no protobuf library, so an exact
    attribute is found by its encoding: field 1 is the key, field 2 an AnyValue
    whose field 1 is the string. Protobuf allows either field first, and the
    collector writes the value first, so both orders are accepted. Lengths here
    are all under 128, one byte each.
    """
    encoded_key, encoded_value = key.encode(), value.encode()
    assert len(encoded_key) < 128 and len(encoded_value) < 126
    any_value = b"\x0a" + bytes([len(encoded_value)]) + encoded_value
    key_field = b"\x0a" + bytes([len(encoded_key)]) + encoded_key
    value_field = b"\x12" + bytes([len(any_value)]) + any_value
    return key_field + value_field in body or value_field + key_field in body


def resource_entries(body: bytes) -> list[bytes]:
    """The request's resource entries, each as its own bytes.

    An OTLP export request is one repeated field, number 1: a ResourceSpans, ResourceMetrics
    or ResourceLogs per resource. The collector sends its own metrics through the same
    exporter, so one request can carry an application's resource next to the collector's.
    What a resource says is therefore judged on its own entry, never on the whole body.
    """
    def varint(at: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = body[at]
            at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, at
            shift += 7

    entries, at = [], 0
    while at < len(body):
        tag, at = varint(at)
        assert tag & 7 == 2, f"an OTLP request holds only length-delimited fields, got wire type {tag & 7}"
        length, at = varint(at)
        if tag >> 3 == 1:
            entries.append(body[at:at + length])
        at += length
    return entries


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

_gateway: str | None = None


def network_gateway() -> str:
    """Create this process's Docker network once and return the host's address on it."""
    global _gateway
    if _gateway is None:
        docker("network", "create", NETWORK, check=True)
        _gateway = docker(
            "network", "inspect", "-f", "{{(index .IPAM.Config 0).Gateway}}", NETWORK, check=True
        ).stdout.strip()
    return _gateway


def start_collector(config_args: list[str], env: dict[str, str], *extra_docker_args: str) -> None:
    docker(
        "run", "-d", "--name", COLLECTOR, "--network", NETWORK, *extra_docker_args,
        *env_args(env), build_image(), *config_args,
        check=True,
    )


def collector_logs() -> str:
    result = docker("logs", COLLECTOR)
    return (result.stderr or result.stdout)[-3000:]


def stop_collector() -> None:
    docker("rm", "-f", COLLECTOR)


def deprecation_warnings(log: str) -> list[str]:
    """The collector's warnings that call something in its config deprecated or legacy."""
    return [line[:300] for line in log.splitlines() if "\twarn\t" in line and ("deprecated" in line or "legacy" in line)]


def cleanup() -> None:
    docker("rm", "-f", COLLECTOR, REDIS)
    docker("network", "rm", NETWORK)
    remove_image()
    remove_work_dir()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_presets_reach_ingest_with_the_key_and_survive_refusals() -> None:
    gateway = network_gateway()
    docker("run", "-d", "--name", REDIS, "--network", NETWORK, REDIS_IMAGE, check=True)
    ingest = IngestStandIn(gateway, refuse_first=2)
    expected = {
        # hosts preset
        b"system.cpu.time",
        b"system.memory.usage",
        b"system.cpu.load_average.1m",
        # redis preset, from the real Redis
        b"redis.memory.used",
        b"redis.clients.connected",
        b"redis.uptime",
        # the collector's own metrics: the liveness signal
        b"otelcol_process_uptime",
        # prometheus preset, from the stand-in's /metrics
        SCRAPED_METRIC.encode(),
    }
    try:
        start_collector(
            [BASE_CONFIG_ARG, preset_arg("hosts"), preset_arg("redis"), preset_arg("prometheus")],
            {
                "STRUCT_INGEST_URL": ingest.url,
                "STRUCT_INGEST_KEY": INGEST_KEY,
                "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment.name={ENVIRONMENT},deployment.environment={ENVIRONMENT}",
                "STRUCT_HOSTS_INTERVAL": "2s",
                "STRUCT_REDIS_INTERVAL": "2s",
                "STRUCT_REDIS_ENDPOINT": f"{REDIS}:6379",
                # The generic scrape preset, pointed at the stand-in's own /metrics.
                "STRUCT_PROMETHEUS_TARGET": ingest.address,
                "STRUCT_PROMETHEUS_JOB": SCRAPED_JOB,
                "STRUCT_PROMETHEUS_INTERVAL": "2s",
            },
        )

        def all_arrived() -> bool:
            seen = b"".join(request.body for request in ingest.snapshot())
            return all(name in seen for name in expected)

        try:
            # The self scrape runs once a minute from a random offset, then waits out a 10 s flush.
            wait_for(all_arrived, timeout=150, what="every expected metric to reach the stand-in", interval=1)
        except TimeoutError as error:
            seen = b"".join(request.body for request in ingest.snapshot())
            missing = sorted(name.decode() for name in expected if name not in seen)
            raise AssertionError(f"{error}; missing {missing}\n{collector_logs()}") from None

        assert len(ingest.refused) == 2, f"the stand-in refused {len(ingest.refused)} requests, expected its first 2"
        requests = ingest.snapshot() + ingest.refused
        for request in requests:
            assert request.path == "/v1/metrics", request.path
            assert request.headers.get("x-struct-ingest-key") == INGEST_KEY, "the ingest key header is missing or wrong"
            assert request.headers.get("content-type") == "application/x-protobuf", request.headers.get("content-type")
            assert request.headers.get("content-encoding") == "gzip", request.headers.get("content-encoding")
            assert request.wire_bytes <= INGEST_MAX_BODY_BYTES and len(request.body) <= INGEST_MAX_BODY_BYTES

        seen = b"".join(request.body for request in ingest.snapshot())
        # Each source is its own service, and everything carries the environment under both keys.
        for key, value in (
            ("service.name", "host"),
            ("service.name", "redis"),
            ("service.name", "struct-otel-collector"),
            # A scraped endpoint that does not name itself gets its job name as its service.
            ("service.name", SCRAPED_JOB),
            ("deployment.environment.name", ENVIRONMENT),
            ("deployment.environment", ENVIRONMENT),
        ):
            assert has_string_attribute(seen, key, value), f"no resource carries {key}={value}"
        assert b"host.name" in seen, "no resource carries host.name"
        # One CPU series per state, not per core.
        assert b"cpu0" not in seen, "per-core CPU series were sent"
        # The shipped config uses no form this release calls deprecated: a customer's first log
        # lines are not warnings about Struct's own files. (Found on a real machine: the
        # collector's own resource was written in a form 0.161.0 calls legacy.)
        whole_log = docker("logs", COLLECTOR)  # the warning is among the first lines, so not the tail
        stale = deprecation_warnings(whole_log.stderr + whole_log.stdout)
        assert not stale, stale
    finally:
        ingest.close()
        stop_collector()
        docker("rm", "-f", REDIS)


def test_the_kubernetes_presets_start_without_a_deprecation_warning() -> None:
    """The check above covers presets that run in a plain container. The two Kubernetes presets
    send nothing without a cluster, but what the collector says about the shipped config it
    says while it starts: here it starts with both, in a container that looks like a pod to it
    and has no cluster behind it, and stops when its receivers find none.

    (Found when the pinned release moved to upstream 0.165.0: the node preset asked the
    k8s_attributes processor for `container.image.tag`, a name it had stopped sending and, since
    0.162.0, warns about each time it starts.)
    """
    gateway = network_gateway()
    ingest = IngestStandIn(gateway)
    presets = ["kubernetes-node", "kubernetes-cluster"]
    try:
        start_collector(
            [BASE_CONFIG_ARG, *[preset_arg(preset) for preset in presets]],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, **required_env(presets)},
            *kubernetes_args(),
        )

        def log() -> str:
            result = docker("logs", COLLECTOR)
            return result.stderr + result.stdout

        # The node preset's processor is in two pipelines, pod logs and kubelet metrics. Each
        # instance logs this line once it has started, right after any warning about its config.
        try:
            wait_for(lambda: log().count("k8s filtering") >= 2 or None, timeout=90,
                     what="both instances of the node preset's k8s_attributes processor to start", interval=1)
        except TimeoutError as error:
            raise AssertionError(f"{error}\n{collector_logs()}") from None
        stale = deprecation_warnings(log())
        assert not stale, stale
    finally:
        ingest.close()
        stop_collector()


def clickhouse_exposition() -> bytes:
    """Every metric name one ClickHouse 25.8 server exposed, as a series of its family's type."""
    lines: list[str] = []
    for name in clickhouse_names():
        kind = "counter" if name.startswith(("ClickHouseProfileEvents_", "ClickHouseErrorMetric_")) else "gauge"
        lines += [f"# TYPE {name} {kind}", f"{name} 1"]
    return ("\n".join(lines) + "\n").encode()


def test_the_clickhouse_preset_sends_its_chosen_set_and_more_by_setting() -> None:
    """A ClickHouse server exposes a few thousand series. The preset sends a chosen set of them,
    and STRUCT_CLICKHOUSE_EXTRA_METRICS adds to it. The endpoint here is the stand-in, answering
    with every name a real server exposed; tests/manual_presets_live.py has the real server."""
    gateway = network_gateway()

    def sent(env: dict[str, str]) -> set[str]:
        ingest = IngestStandIn(gateway)
        ingest.metrics_body = clickhouse_exposition()
        try:
            start_collector(
                [BASE_CONFIG_ARG, preset_arg("clickhouse")],
                {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY,
                 "STRUCT_CLICKHOUSE_ENDPOINT": ingest.address, "STRUCT_CLICKHOUSE_INTERVAL": "2s", **env},
            )

            def scrapes() -> list[bytes]:
                return [request.body for request in ingest.snapshot() if b"ClickHouseMetrics_Query" in request.body]

            try:
                wait_for(lambda: len(scrapes()) >= 2 or None, timeout=90, what="two ClickHouse scrapes to reach the stand-in",
                         interval=0.5)
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
        default = sent({})
        # Exactly the names the rules keep, as test_configs.py reads them: no more, and none lost.
        assert default == clickhouse_kept(), (sorted(default - clickhouse_kept())[:5], sorted(clickhouse_kept() - default)[:5])
        assert len(default) < 200 < len(clickhouse_names())
        with_errors = sent({"STRUCT_CLICKHOUSE_EXTRA_METRICS": "ClickHouseErrorMetric_.+"})
        assert with_errors == clickhouse_kept("ClickHouseErrorMetric_.+"), len(with_errors)
        assert len(with_errors) - len(default) > 600
    finally:
        stop_collector()


def test_a_burst_is_split_into_requests_ingest_accepts() -> None:
    gateway = network_gateway()
    ingest = IngestStandIn(gateway)
    records = 4000
    try:
        start_collector(
            [BASE_CONFIG_ARG, "--config=env:STRUCT_TEST_OVERLAY"],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, "STRUCT_TEST_OVERLAY": BURST_OVERLAY},
            "-p", "127.0.0.1::4318",
        )
        port = int(docker("port", COLLECTOR, "4318/tcp", check=True).stdout.strip().rsplit(":", 1)[1])

        # About 4.8 MB of log records in one request, each body random enough that gzip
        # barely shrinks it, so the size on the wire is checked as well as the size before.
        payload = {"resourceLogs": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "burst"}}]},
            "scopeLogs": [{"logRecords": [
                {"timeUnixNano": "1700000000000000000",
                 "body": {"stringValue": f"rec-{index:05d}-" + base64.b64encode(os.urandom(900)).decode()}}
                for index in range(records)
            ]}],
        }]}
        body = json.dumps(payload).encode()
        assert len(body) > 2 * INGEST_MAX_BODY_BYTES, "the burst must be well over ingest's limit to prove anything"

        def post() -> bool:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/logs", data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status == 200
            except (urllib.error.URLError, ConnectionError, OSError):
                return False  # still starting

        wait_for(post, timeout=60, what="the collector to accept the burst", interval=0.5)

        marker = re.compile(rb"rec-(\d{5})-")

        def arrived() -> list[int]:
            return [int(m) for request in ingest.snapshot() for m in marker.findall(request.body)]

        try:
            wait_for(lambda: len(arrived()) >= records, timeout=90, what="every record of the burst", interval=0.5)
        except TimeoutError as error:
            raise AssertionError(f"{error}; {len(arrived())} of {records} arrived\n{collector_logs()}") from None

        log_requests = [request for request in ingest.snapshot() if request.path == "/v1/logs"]
        assert sorted(arrived()) == list(range(records)), "records were lost or sent twice"
        assert len(log_requests) >= 3, f"a {len(body)} byte burst arrived in only {len(log_requests)} requests"
        largest_wire = max(request.wire_bytes for request in log_requests)
        largest_body = max(len(request.body) for request in log_requests)
        assert largest_body <= INGEST_MAX_BODY_BYTES, f"a request was {largest_body} bytes before compression"
        assert largest_wire <= INGEST_MAX_BODY_BYTES, f"a request was {largest_wire} bytes on the wire"
        for request in log_requests:
            assert request.headers.get("x-struct-ingest-key") == INGEST_KEY
        print(f"    {len(body)} bytes in, {len(log_requests)} requests out, "
              f"largest {largest_body} bytes ({largest_wire} on the wire)")
    finally:
        ingest.close()
        stop_collector()


def test_waiting_requests_survive_a_restart() -> None:
    """Records accepted while ingest is refusing are still sent after the collector restarts."""
    gateway = network_gateway()
    ingest = IngestStandIn(gateway, refuse_first=10**9)
    records = 20
    marker = re.compile(rb"kept-(\d{2})-")
    try:
        start_collector(
            [BASE_CONFIG_ARG, "--config=env:STRUCT_TEST_OVERLAY"],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, "STRUCT_TEST_OVERLAY": BURST_OVERLAY},
            "-p", "127.0.0.1::4318",
        )
        port = int(docker("port", COLLECTOR, "4318/tcp", check=True).stdout.strip().rsplit(":", 1)[1])
        body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"timeUnixNano": "1700000000000000000", "body": {"stringValue": f"kept-{index:02d}-"}}
            for index in range(records)
        ]}]}]}).encode()

        def post() -> bool:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/logs", data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status == 200
            except (urllib.error.URLError, ConnectionError, OSError):
                return False

        wait_for(post, timeout=60, what="the collector to accept the records", interval=0.5)
        # The records were sent only once, before the restart. Wait until ingest has refused
        # them, so they are known to be waiting in the queue and not still in a batch.
        wait_for(
            lambda: any(marker.search(request.body) for request in ingest.refused_snapshot()),
            timeout=60, what="ingest to refuse the records at least once", interval=0.5,
        )
        assert not ingest.snapshot(), "ingest accepted something while it was refusing everything"

        docker("restart", COLLECTOR, check=True)
        ingest.accept_from_now()

        def arrived() -> list[int]:
            return [int(m) for request in ingest.snapshot() if request.path == "/v1/logs"
                    for m in marker.findall(request.body)]

        try:
            wait_for(lambda: len(arrived()) >= records, timeout=90, what="the waiting records after the restart", interval=0.5)
        except TimeoutError as error:
            raise AssertionError(f"{error}; {len(arrived())} of {records} arrived\n{collector_logs()}") from None
        assert sorted(arrived()) == list(range(records)), "records were lost or sent twice across the restart"
    finally:
        ingest.close()
        stop_collector()


def test_a_full_queue_refuses_new_data_and_everything_it_accepted_arrives() -> None:
    """While ingest is unreachable the queue holds what its size in bytes allows and no more.

    What arrives is counted in bytes before it is split into requests, so one large request
    cannot take many times the bound. Data that does not fit is refused to its sender, and
    everything that was accepted is delivered once ingest is back, however long that took.
    """
    gateway = network_gateway()
    ingest = IngestStandIn(gateway, refuse_first=10**9)
    per_request, requests = 1000, 14
    marker = re.compile(rb"q(\d{2})-(\d{4})-")
    try:
        start_collector(
            [BASE_CONFIG_ARG, "--config=env:STRUCT_TEST_OVERLAY", "--config=env:STRUCT_TEST_QUEUE_OVERLAY"],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, "STRUCT_TEST_OVERLAY": BURST_OVERLAY,
             "STRUCT_TEST_QUEUE_OVERLAY": SMALL_QUEUE_OVERLAY},
            "-p", "127.0.0.1::4318",
        )
        port = int(docker("port", COLLECTOR, "4318/tcp", check=True).stdout.strip().rsplit(":", 1)[1])

        def body(request: int) -> bytes:
            # About 1.2 MB of OTLP: 1,000 records of 900 random bytes each.
            return json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
                {"timeUnixNano": "1700000000000000000",
                 "body": {"stringValue": f"q{request:02d}-{index:04d}-" + base64.b64encode(os.urandom(900)).decode()}}
                for index in range(per_request)
            ]}]}]}).encode()

        def post(payload: bytes) -> int:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/logs", data=payload, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
            except (urllib.error.URLError, ConnectionError, OSError):
                return 0  # still starting

        first = body(0)
        wait_for(lambda: post(first) == 200, timeout=60, what="the collector to accept the first request", interval=0.5)
        accepted, refused = [0], []
        for request in range(1, requests):
            (accepted if post(body(request)) == 200 else refused).append(request)

        # 1.2 MB a request, measured as the collector measures it: the records' own bytes.
        request_bytes = per_request * 1215
        # What can be held: the queue, the two requests being sent (each up to the batch's
        # largest size), and one request's worth of slack for where a batch boundary falls.
        held_at_most = SMALL_QUEUE_BYTES + 2 * 1_500_000 + request_bytes
        assert refused, f"all {requests} requests ({requests * request_bytes} bytes) fit a {SMALL_QUEUE_BYTES} byte queue"
        assert len(accepted) * request_bytes <= held_at_most, (
            f"{len(accepted)} requests ({len(accepted) * request_bytes} bytes) were accepted; "
            f"the queue is {SMALL_QUEUE_BYTES} bytes")
        assert len(accepted) >= 2, f"only {len(accepted)} of {requests} requests were accepted"
        assert not ingest.snapshot(), "ingest accepted something while it was refusing everything"

        ingest.accept_from_now()

        def arrived() -> list[tuple[int, int]]:
            return [(int(request), int(index)) for received in ingest.snapshot() if received.path == "/v1/logs"
                    for request, index in marker.findall(received.body)]

        expected = sorted((request, index) for request in accepted for index in range(per_request))
        try:
            wait_for(lambda: len(arrived()) >= len(expected), timeout=120, what="every accepted record", interval=0.5)
        except TimeoutError as error:
            raise AssertionError(f"{error}; {len(arrived())} of {len(expected)} arrived\n{collector_logs()}") from None
        assert sorted(arrived()) == expected, "an accepted record was lost or sent twice, or a refused one arrived"
        print(f"    {len(accepted)} of {requests} requests accepted ({len(accepted) * request_bytes} bytes) "
              f"into a {SMALL_QUEUE_BYTES} byte queue; {len(refused)} refused; every accepted record arrived")
    finally:
        ingest.close()
        stop_collector()


def test_one_record_larger_than_a_request_is_dropped_and_logged() -> None:
    """A single record over the request cap cannot be split, and ingest could not take it
    either. It is dropped where requests are split, with a log line, and never retried.

    The records around it are sent, those after it in the same request included: since
    upstream 0.162.0 the splitter drops only the oversized log record. (On 0.161.0 it also
    dropped every record after it in the batch.) For metrics the splitter still drops the data
    points that follow an oversized one in the same batch (seen on 0.165.0); this test is about
    log records.
    """
    gateway = network_gateway()
    ingest = IngestStandIn(gateway)
    marker = f"oversized-{RUN_ID}"
    try:
        start_collector(
            [BASE_CONFIG_ARG, "--config=env:STRUCT_TEST_OVERLAY"],
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, "STRUCT_TEST_OVERLAY": BURST_OVERLAY},
            "-p", "127.0.0.1::4318",
        )
        port = int(docker("port", COLLECTOR, "4318/tcp", check=True).stdout.strip().rsplit(":", 1)[1])

        def post(*records: str) -> bool:
            body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
                {"timeUnixNano": "1700000000000000000", "body": {"stringValue": record}} for record in records
            ]}]}]}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/logs", data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status == 200
            except (urllib.error.URLError, ConnectionError, OSError):
                return False  # still starting

        # 1.6 MB in one record: over the 1,500,000-byte cap on a request.
        huge = f"huge-{marker}-" + "x" * 1_600_000
        wait_for(lambda: post(f"before-{marker}", huge, f"after-{marker}"), timeout=60,
                 what="the collector to accept the request", interval=0.5)
        wait_for(lambda: "single log record exceeds the max size limit, dropping items: 1" in docker("logs", COLLECTOR).stderr
                 or None, timeout=30, what="the collector to log the one dropped record", interval=0.5)
        assert post(f"later-{marker}"), "a request after the oversized one was refused"

        def bodies() -> bytes:
            return b"".join(request.body for request in ingest.snapshot() if request.path == "/v1/logs")

        kept = [f"{place}-{marker}".encode() for place in ("before", "after", "later")]
        try:
            wait_for(lambda: all(record in bodies() for record in kept) or None, timeout=60,
                     what="the records before and after the oversized one, and the later request's", interval=0.5)
        except TimeoutError as error:
            missing = [record.decode() for record in kept if record not in bodies()]
            raise AssertionError(f"{error}; missing {missing}\n{collector_logs()}") from None
        assert f"huge-{marker}".encode() not in bodies(), "a record over the request cap reached ingest"
        assert max(len(request.body) for request in ingest.snapshot()) <= INGEST_MAX_BODY_BYTES
    finally:
        ingest.close()
        stop_collector()


def test_applications_send_through_the_collector_with_the_otlp_preset() -> None:
    """Traces, metrics and logs an application posts to the collector reach ingest, under the
    application's own service, host and environment, with the ingest key the application never
    had. Only what a resource lacks is added."""
    gateway = network_gateway()
    ingest = IngestStandIn(gateway)
    marker = f"app-{RUN_ID}"
    now = "1700000000000000000"
    # The application names its own host and environment, and they differ from the collector's.
    app_host, app_environment = f"app-host-{RUN_ID}", f"app-env-{RUN_ID}"
    resource = {"attributes": [
        {"key": "service.name", "value": {"stringValue": "checkout"}},
        {"key": "host.name", "value": {"stringValue": app_host}},
        {"key": "deployment.environment.name", "value": {"stringValue": app_environment}},
    ]}
    payloads = {
        "/v1/traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [{
            "traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b174", "name": f"span-{marker}",
            "kind": 1, "startTimeUnixNano": now, "endTimeUnixNano": now}]}]}]},
        "/v1/metrics": {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"metrics": [{
            "name": f"gauge_{marker}".replace("-", "_"),
            "gauge": {"dataPoints": [{"asDouble": 1.5, "timeUnixNano": now}]}}]}]}]},
        "/v1/logs": {"resourceLogs": [
            {"resource": resource, "scopeLogs": [{"logRecords": [
                {"timeUnixNano": now, "body": {"stringValue": f"log-{marker}"}}]}]},
            # Nothing names this one's service.
            {"scopeLogs": [{"logRecords": [{"timeUnixNano": now, "body": {"stringValue": f"anonymous-{marker}"}}]}]},
        ]},
    }
    needles = {"/v1/traces": f"span-{marker}".encode(), "/v1/metrics": f"gauge_{marker}".replace("-", "_").encode(),
               "/v1/logs": f"log-{marker}".encode()}
    try:
        start_collector(
            [BASE_CONFIG_ARG, preset_arg("otlp")],
            # The preset listens on loopback by default; the test reaches it through a published port.
            {"STRUCT_INGEST_URL": ingest.url, "STRUCT_INGEST_KEY": INGEST_KEY, "STRUCT_OTLP_HTTP_ENDPOINT": "0.0.0.0:4318",
             "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment.name={ENVIRONMENT}"},
            "-p", "127.0.0.1::4318",
        )
        port = int(docker("port", COLLECTOR, "4318/tcp", check=True).stdout.strip().rsplit(":", 1)[1])

        def post(path: str) -> bool:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=json.dumps(payloads[path]).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status == 200
            except (urllib.error.URLError, ConnectionError, OSError):
                return False  # still starting

        for path in payloads:
            wait_for(lambda path=path: post(path), timeout=60, what=f"the collector to accept {path}", interval=0.5)

        def arrived(path: str, needle: bytes) -> list[tuple[Received, bytes]]:
            """Each request on this path with the resource entry in it that holds `needle`."""
            return [(request, entry) for request in ingest.snapshot() if request.path == path
                    for entry in resource_entries(request.body) if needle in entry]

        for path in payloads:
            try:
                wait_for(lambda path=path: arrived(path, needles[path]) or None, timeout=60,
                         what=f"the application's {path}", interval=0.5)
            except TimeoutError as error:
                raise AssertionError(f"{error}\n{collector_logs()}") from None
            for request, entry in arrived(path, needles[path]):
                assert request.headers.get("x-struct-ingest-key") == INGEST_KEY
                assert has_string_attribute(entry, "service.name", "checkout"), f"{path}: the application's service name was lost"
                assert has_string_attribute(entry, "host.name", app_host), f"{path}: the application's host name was replaced"
                assert has_string_attribute(entry, "deployment.environment.name", app_environment), (
                    f"{path}: the application's environment was replaced")
                # What the resource already says is kept, not added to. The same request may
                # carry the collector's own resource, which does name the collector's environment.
                assert not has_string_attribute(entry, "deployment.environment.name", ENVIRONMENT), (
                    f"{path}: the collector's environment was put on a resource that names its own")
        anonymous = [entry for _, entry in arrived("/v1/logs", f"anonymous-{marker}".encode())]
        assert anonymous and all(has_string_attribute(entry, "service.name", "unknown_service") for entry in anonymous), (
            "what arrives without a service name must get the default one")
        # The resource that names nothing gets the collector's identity instead.
        assert all(has_string_attribute(entry, "deployment.environment.name", ENVIRONMENT) for entry in anonymous), (
            "what arrives without an environment must get the collector's")
        assert not any(has_string_attribute(entry, "deployment.environment.name", app_environment) for entry in anonymous), (
            "the application's environment leaked onto another resource")
    finally:
        ingest.close()
        stop_collector()


TESTS = [
    test_presets_reach_ingest_with_the_key_and_survive_refusals,
    test_the_kubernetes_presets_start_without_a_deprecation_warning,
    test_the_clickhouse_preset_sends_its_chosen_set_and_more_by_setting,
    test_a_burst_is_split_into_requests_ingest_accepts,
    test_waiting_requests_survive_a_restart,
    test_a_full_queue_refuses_new_data_and_everything_it_accepted_arrives,
    test_one_record_larger_than_a_request_is_dropped_and_logged,
    test_applications_send_through_the_collector_with_the_otlp_preset,
]


def teardown_module() -> None:  # pytest
    cleanup()


if __name__ == "__main__":
    run_tests(TESTS, cleanup=cleanup)
