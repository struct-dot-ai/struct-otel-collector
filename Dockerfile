# The Struct Collector image: the upstream OpenTelemetry Collector (contrib)
# release with Struct's config baked in. No code of our own. The tests check
# that this file, the install script and the chart name the same upstream release.
FROM otel/opentelemetry-collector-contrib:0.165.0

COPY config/ /etc/struct-otel-collector/
# The upstream image has no shell, so the state directory is created by copying
# one in, owned by the collector's user (10001).
COPY --chown=10001:10001 state/ /var/lib/struct-otel-collector/

EXPOSE 13133

# Presets are added with further --config arguments.
CMD ["--config=/etc/struct-otel-collector/config.yaml"]
