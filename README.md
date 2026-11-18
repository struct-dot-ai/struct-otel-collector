# Struct Collector

The Struct Collector sends host, Kubernetes and service telemetry from your own
environment to [Struct](https://struct.ai). It is the upstream
[OpenTelemetry Collector (contrib)](https://github.com/open-telemetry/opentelemetry-collector-contrib)
with Struct's configuration: a base config that sends to Struct ingest, and presets
you add for what you run (hosts, Kubernetes, Docker, Postgres, MySQL, Redis, MongoDB,
Kafka, Redpanda, Temporal, NGINX, ClickHouse, any Prometheus endpoint, and OTLP from
your own applications).

Each release publishes three things under one version, `<upstream>-<n>`: the
upstream collector release it wraps, then Struct's revision of it.

| What | Where |
|---|---|
| Linux installer | `struct-otel-collector-install.sh` and its `.sha256`, on each [release](https://github.com/struct-dot-ai/struct-otel-collector/releases) |
| Container image | `docker.io/structai/struct-otel-collector:<version>`, for `linux/amd64` and `linux/arm64`. The same image is at `ghcr.io/struct-dot-ai/struct-otel-collector:<version>` |
| Helm chart | the chart repository `https://struct-dot-ai.github.io/struct-otel-collector`, chart `struct-otel-collector`, version `<version>` |

There is no `latest` image tag. Use the version of the
[latest release](https://github.com/struct-dot-ai/struct-otel-collector/releases/latest).

## Install

You need an ingest key from your Struct workspace. The key is never a command-line
argument: it travels in the environment, in a root-only file, or on standard input.

### Linux, with systemd

```bash
read -rs STRUCT_INGEST_KEY && export STRUCT_INGEST_KEY   # paste the key; nothing is shown
curl -fsSL https://github.com/struct-dot-ai/struct-otel-collector/releases/latest/download/struct-otel-collector-install.sh \
  | sudo --preserve-env=STRUCT_INGEST_KEY bash -s -- --presets hosts --environment production
```

Where `sudo` refuses `--preserve-env`, put the key in a root-only file first:

```bash
sudo sh -c 'umask 077 && cat > /root/struct-ingest-key'   # paste the key, then Ctrl-D
curl -fsSL https://github.com/struct-dot-ai/struct-otel-collector/releases/latest/download/struct-otel-collector-install.sh \
  | sudo bash -s -- --presets hosts --environment production --ingest-key-file /root/struct-ingest-key
```

To check the download before running it, fetch `struct-otel-collector-install.sh.sha256`
from the same release and compare it with `sha256sum`. The installer checks the
SHA-256 of the upstream collector it downloads. `--dry-run` shows what it would do,
`--uninstall` removes it, and `--help` lists every option and preset.

### Kubernetes

```bash
helm repo add struct https://struct-dot-ai.github.io/struct-otel-collector
helm repo update
printf '%s' "$STRUCT_INGEST_KEY" | helm install struct-otel-collector struct/struct-otel-collector \
  --version <version> --namespace struct --create-namespace --set-file ingest.key=/dev/stdin \
  --set environment=production --set clusterName=prod-1
```

Helm reads `<upstream>-<n>` as a pre-release, so `--version` is required. The chart
deploys the Docker Hub image; to pull the copy on GitHub's registry instead, add
`--set image.repository=ghcr.io/struct-dot-ai/struct-otel-collector`.

### Docker

```bash
docker run -e STRUCT_INGEST_KEY structai/struct-otel-collector:<version> \
  --config=/etc/struct-otel-collector/config.yaml \
  --config=/etc/struct-otel-collector/presets/hosts.yaml
```

Each preset is one more `--config` argument. If Docker Hub's limits on anonymous pulls
get in the way, use `ghcr.io/struct-dot-ai/struct-otel-collector:<version>`, the same
image.

## What is in this repository

| Path | What |
|---|---|
| `Dockerfile` | The image: the upstream collector with `config/` baked in |
| `config/config.yaml`, `config/presets/` | The base config and one file per preset |
| `chart/struct-otel-collector/` | The Helm chart |
| `install/install.sh` | The Linux installer; `scripts/build-installer.py` builds the single-file version a release publishes |
| `scripts/release.py`, `scripts/publish.sh` | The release rules and checks, and the release commands |
| `tests/` | The tests below |

## Development

The tests need Python 3.10 or later, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv run python tests/test_install.py      # the installer, no Docker
uv run python tests/test_configs.py      # every config and preset through the collector's own validate
uv run python tests/test_chart.py        # the Helm chart
uv run python tests/test_end_to_end.py   # the built image against a stand-in for Struct ingest
uv run --group dev python -m pytest -q tests/test_release.py   # the release rules
```

`tests/manual_presets_live.py` and `tests/manual_install_live.py` run presets against real
services and the installer under systemd, in containers. CI runs the default tests on every
pull request.

The upstream version is pinned in three places that change together: `FROM` in the
`Dockerfile`, `UPSTREAM_VERSION` and its checksums in `install/install.sh`, and the chart's
version.

## Releasing

A maintainer sets `version` and `appVersion` in `chart/struct-otel-collector/Chart.yaml` to
the next `<upstream>-<n>`, merges that to `main`, and pushes the tag `v<version>` on that
commit. The [release workflow](.github/workflows/release.yml) checks the tag, runs the tests,
and waits for a second maintainer to approve. It then pushes the image to Docker Hub and to
GitHub's registry, publishes the chart to the chart repository, and releases the installer
last.

## Help

Documentation is at [docs.struct.ai](https://docs.struct.ai).

## License

Apache License 2.0; see [LICENSE](LICENSE). The image contains the upstream
OpenTelemetry Collector, which is also licensed under Apache License 2.0.
