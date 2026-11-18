# Struct Collector

The Struct Collector sends host, Kubernetes and service telemetry from your own
environment to [Struct](https://struct.ai). It is the upstream
[OpenTelemetry Collector (contrib)](https://github.com/open-telemetry/opentelemetry-collector-contrib)
with Struct's configuration: a base config that sends to Struct ingest, and presets
you add for what you run (hosts, Kubernetes, Docker, Postgres, MySQL, Redis, MongoDB,
Kafka, Redpanda, Temporal, NGINX, ClickHouse, any Prometheus endpoint, and OTLP from
your own applications).

This repository holds the releases and the Helm chart repository. Each release
publishes three things under one version, `<upstream>-<n>`: the upstream collector
release it wraps, then Struct's revision of it.

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

## Help

Documentation is at [docs.struct.ai](https://docs.struct.ai).

## License

Apache License 2.0; see [LICENSE](LICENSE). The image contains the upstream
OpenTelemetry Collector, which is also licensed under Apache License 2.0.
