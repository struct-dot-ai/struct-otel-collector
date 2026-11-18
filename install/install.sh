#!/usr/bin/env bash
# Struct Collector installer for Linux hosts that run systemd.
#
# Installs the pinned upstream OpenTelemetry Collector (contrib) release for
# this machine's architecture, checks it against a pinned SHA-256, writes
# Struct's config and the presets you choose, and runs it as the
# struct-otel-collector systemd unit under its own unprivileged user.
#
#   STRUCT_INGEST_KEY=pk-... ./install.sh --presets hosts,redis --environment production
#
# Preset settings (see the header of each preset file) are read from this
# script's environment, from a --settings-file, or given with --set, and written
# with the ingest key to a file only root can read. `--dry-run` prints every
# step and changes nothing. `--uninstall` removes everything this script installs.
#
# Secrets (the ingest key and anything named *_PASSWORD, *_TOKEN, *_SECRET or
# *_KEY) never travel as a command-line argument, of this script or of anything
# it runs: other users can read arguments from the process list, and audit logs
# keep them. They come from the environment or from a file. Under sudo use a
# file, because `sudo NAME=value install.sh` puts the value in sudo's arguments.
#
# This file runs from a checkout with --config-dir. The single-file installer
# with the config embedded is built by scripts/build-installer.py.

set -euo pipefail

UPSTREAM_VERSION="0.165.0"
UPSTREAM_SHA256_AMD64="bd6e73d94bf3359ad6fac25529a8ee308adb9b0b8a0e8af034ed3891fbe5aa33"
UPSTREAM_SHA256_ARM64="376420729c862230807bc89811d07d10031eaeca3f6481fe371a2bdcefd2c8fa"
UPSTREAM_RELEASES="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download"

NAME="struct-otel-collector"
INSTALL_DIR="/opt/${NAME}"
BINARY="${INSTALL_DIR}/bin/otelcol-contrib"
CONFIG_DIR="/etc/${NAME}"
ENV_FILE="${CONFIG_DIR}/collector.env"
STATE_DIR="/var/lib/${NAME}"
UNIT_PATH="/etc/systemd/system/${NAME}.service"
SERVICE_USER="${NAME}"
HEALTH_ENDPOINT="127.0.0.1:13133"

# BEGIN EMBEDDED CONFIG
# scripts/build-installer.py replaces this block with a function that prints
# the config directory as a base64 tar.gz. From a checkout there is none.
embedded_config() { return 1; }
# END EMBEDDED CONFIG

usage() {
  cat <<EOF
Usage: STRUCT_INGEST_KEY=pk-... install.sh [options]

  --presets a,b,c         Integration presets to switch on (default: hosts)
  --environment NAME      Deployment environment to stamp on everything sent
  --ingest-url URL        Struct ingest URL (default https://ingest.struct.ai)
  --ingest-key-file PATH  Read the ingest key from a file instead of STRUCT_INGEST_KEY
  --settings-file PATH    Read settings from a file of NAME=VALUE lines (values are
                          taken literally). The place for passwords under sudo.
  --set NAME=VALUE        A setting that is not a secret; may be repeated.
                          Secrets are refused here: use the environment or a file.
  --extra-config PATH     A collector config file of your own, merged after the
                          presets; may be repeated. Keep secrets out of it: write
                          \${env:NAME} and give NAME as a setting.

Settings are also read from this script's environment. --set wins over
--settings-file, which wins over the environment. --ingest-url and
--environment are the same as setting STRUCT_INGEST_URL and the environment
attributes, and win over all three.
  --config-dir DIR        Take the config from DIR instead of the embedded copy
  --tarball PATH          Use a local copy of the upstream release (still checksum-verified)
  --no-start              Install and validate, but do not enable or start the unit
  --dry-run               Print what would be done and change nothing
  --uninstall             Remove the unit, binary, config, state and user
  --print-unit            Print the systemd unit and exit
  -h, --help              Show this help
EOF
}

log() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

PRESETS="hosts"
ENVIRONMENT=""
INGEST_URL=""
INGEST_KEY_FILE=""
SOURCE_CONFIG_DIR=""
TARBALL=""
START=1
DRY_RUN=0
ACTION="install"
SETTINGS_FILE=""
EXTRA_CONFIGS=()
declare -A SETTINGS=()
HEALTH_TIMEOUT_SECONDS=30

# The one rule for what a setting's name may be, wherever it is given: the
# shell's own `export` refuses anything else, and by then files are installed.
is_setting_name() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]*$ ]]
}

# The one rule for what is a secret. Everything that decides how a setting may
# be given or shown asks this.
is_secret() {
  case "$1" in
    STRUCT_INGEST_KEY|*_PASSWORD|*_TOKEN|*_SECRET|*_KEY) return 0 ;;
    *) return 1 ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --presets) PRESETS="${2:?--presets needs a value}"; shift 2 ;;
    --environment) ENVIRONMENT="${2:?--environment needs a value}"; shift 2 ;;
    --ingest-url) INGEST_URL="${2:?--ingest-url needs a value}"; shift 2 ;;
    --ingest-key-file) INGEST_KEY_FILE="${2:?--ingest-key-file needs a value}"; shift 2 ;;
    --settings-file) SETTINGS_FILE="${2:?--settings-file needs a value}"; shift 2 ;;
    --set)
      pair="${2:?--set needs NAME=VALUE}"
      case "$pair" in
        *=*) ;;
        *) fail "--set needs NAME=VALUE" ;;
      esac
      is_setting_name "${pair%%=*}" || fail "--set needs NAME=VALUE, where NAME is capitals, digits and underscores"
      # The message names the setting, never the value.
      if is_secret "${pair%%=*}"; then
        fail "--set cannot take ${pair%%=*}: an argument is visible to other users. Put it in the environment or in a --settings-file"
      fi
      SETTINGS["${pair%%=*}"]="${pair#*=}"
      shift 2 ;;
    --extra-config) EXTRA_CONFIGS+=("${2:?--extra-config needs a path}"); shift 2 ;;
    --config-dir) SOURCE_CONFIG_DIR="${2:?--config-dir needs a value}"; shift 2 ;;
    --tarball) TARBALL="${2:?--tarball needs a value}"; shift 2 ;;
    --no-start) START=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --print-unit) ACTION="print-unit"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown option: $1" ;;
  esac
done

# Run a command, or in a dry run print it.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

unit_file() {
  cat <<EOF
[Unit]
Description=Struct Collector (OpenTelemetry Collector with Struct's config)
After=network-online.target
Wants=network-online.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
# Root-only: holds the ingest key and any preset passwords. systemd reads it
# before it drops to the service user.
EnvironmentFile=${ENV_FILE}
ExecStart=${BINARY} \$STRUCT_COLLECTOR_ARGS
Restart=on-failure
RestartSec=5
StateDirectory=${NAME}
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
}

if [ "$ACTION" = "print-unit" ]; then
  unit_file
  exit 0
fi

require_root() {
  if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    fail "run as root (or with sudo), or add --dry-run to see what would be done"
  fi
}

uninstall() {
  require_root
  log "Removing the Struct Collector"
  if command -v systemctl >/dev/null 2>&1; then
    run systemctl disable --now "${NAME}.service" || true
  fi
  run rm -f "$UNIT_PATH"
  if command -v systemctl >/dev/null 2>&1; then
    run systemctl daemon-reload || true
  fi
  run rm -rf "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    run userdel "$SERVICE_USER" || true
  fi
  # userdel removes the group only where the machine creates one per user.
  if getent group "$SERVICE_USER" >/dev/null 2>&1; then
    run groupdel "$SERVICE_USER" || true
  fi
  log "Removed."
}

if [ "$ACTION" = "uninstall" ]; then
  uninstall
  exit 0
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

[ "$(uname -s)" = "Linux" ] || fail "this installer supports Linux only; on other systems run the container image"
case "$(uname -m)" in
  x86_64|amd64) ARCH="amd64"; EXPECTED_SHA256="$UPSTREAM_SHA256_AMD64" ;;
  aarch64|arm64) ARCH="arm64"; EXPECTED_SHA256="$UPSTREAM_SHA256_ARM64" ;;
  *) fail "unsupported architecture: $(uname -m) (amd64 and arm64 are supported)" ;;
esac
TARBALL_NAME="otelcol-contrib_${UPSTREAM_VERSION}_linux_${ARCH}.tar.gz"
TARBALL_URL="${UPSTREAM_RELEASES}/v${UPSTREAM_VERSION}/${TARBALL_NAME}"

require_root
if [ "$DRY_RUN" -eq 0 ]; then
  for tool in tar sha256sum useradd groupadd install; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing required tool: $tool"
  done
  [ -n "$TARBALL" ] || command -v curl >/dev/null 2>&1 || fail "missing required tool: curl (or pass --tarball)"
  if [ "$START" -eq 1 ]; then
    command -v systemctl >/dev/null 2>&1 || fail "systemd is required; without it, pass --no-start and run the binary yourself"
  fi
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The config to install: a directory given with --config-dir, or the embedded copy.
if [ -n "$SOURCE_CONFIG_DIR" ]; then
  [ -f "$SOURCE_CONFIG_DIR/config.yaml" ] || fail "--config-dir has no config.yaml: $SOURCE_CONFIG_DIR"
  mkdir -p "$WORK/config"
  cp -R "$SOURCE_CONFIG_DIR/." "$WORK/config/"
elif embedded_config >/dev/null 2>&1; then
  mkdir -p "$WORK/config"
  embedded_config | base64 -d | tar -xz -C "$WORK/config"
else
  fail "this copy of the installer has no embedded config; pass --config-dir with the checkout's config directory, the one next to install/"
fi

# Presets: each must exist, and each one's required settings must be given.
IFS=',' read -r -a PRESET_LIST <<<"$PRESETS"
CONFIG_ARGS="--config=${CONFIG_DIR}/config.yaml"
SETTING_FILES=("$WORK/config/config.yaml")
for preset in "${PRESET_LIST[@]}"; do
  [ -n "$preset" ] || continue
  case "$preset" in *[!a-z0-9-]*) fail "not a preset name: $preset" ;; esac
  if [ ! -f "$WORK/config/presets/${preset}.yaml" ]; then
    available="$(cd "$WORK/config/presets" && ls -- *.yaml | sed 's/\.yaml$//' | tr '\n' ' ')"
    fail "unknown preset: ${preset}. Available: ${available}"
  fi
  case "$preset" in
    kubernetes-*) fail "the ${preset} preset is for Kubernetes: install the Helm chart instead" ;;
  esac
  CONFIG_ARGS="${CONFIG_ARGS} --config=${CONFIG_DIR}/presets/${preset}.yaml"
  SETTING_FILES+=("$WORK/config/presets/${preset}.yaml")
done

# The customer's own config files go last, so what they say wins over a preset.
# Each is installed under a fixed name: a path is never part of an argument, and
# a list in it replaces a preset's list of the same key (config merging does
# that), so an addition needs its own named receiver and pipeline.
EXTRA_NUMBER=0
for extra in ${EXTRA_CONFIGS[@]+"${EXTRA_CONFIGS[@]}"}; do
  [ -f "$extra" ] && [ -r "$extra" ] || fail "cannot read --extra-config: $extra"
  EXTRA_NUMBER=$((EXTRA_NUMBER + 1))
  CONFIG_ARGS="${CONFIG_ARGS} --config=${CONFIG_DIR}/extra/extra-${EXTRA_NUMBER}.yaml"
  SETTING_FILES+=("$extra")
done

# The ingest key: from a file, or from the environment. Never from an argument,
# which other users could read from the process list.
if [ -n "$INGEST_KEY_FILE" ]; then
  [ -r "$INGEST_KEY_FILE" ] || fail "cannot read --ingest-key-file: $INGEST_KEY_FILE"
  SETTINGS[STRUCT_INGEST_KEY]="$(tr -d '\r\n' <"$INGEST_KEY_FILE")"
fi

# Settings from a file: NAME=VALUE lines, the value taken literally up to the end
# of the line. Blank lines and lines starting with # are skipped. --set wins.
if [ -n "$SETTINGS_FILE" ]; then
  [ -r "$SETTINGS_FILE" ] || fail "cannot read --settings-file: $SETTINGS_FILE"
  line_number=0
  while IFS= read -r line || [ -n "$line" ]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
      *=*) ;;
      *) fail "--settings-file line ${line_number} is not NAME=VALUE" ;;
    esac
    is_setting_name "${line%%=*}" || fail "--settings-file line ${line_number} is not NAME=VALUE, where NAME is capitals, digits and underscores"
    [ -n "${SETTINGS[${line%%=*}]+set}" ] || SETTINGS["${line%%=*}"]="${line#*=}"
  done <"$SETTINGS_FILE"
fi
if [ -z "${SETTINGS[STRUCT_INGEST_KEY]+set}" ] && [ -n "${STRUCT_INGEST_KEY:-}" ]; then
  SETTINGS[STRUCT_INGEST_KEY]="$STRUCT_INGEST_KEY"
fi
# --ingest-url is the explicit form of STRUCT_INGEST_URL and wins. Without it the
# setting resolves like any other below, and the config's own default applies.
if [ -n "$INGEST_URL" ]; then
  SETTINGS[STRUCT_INGEST_URL]="$INGEST_URL"
fi
# Fixed by the unit's StateDirectory, whatever the environment says.
SETTINGS[STRUCT_COLLECTOR_STATE_DIR]="$STATE_DIR"
if [ -n "$ENVIRONMENT" ]; then
  case "$ENVIRONMENT" in *[!A-Za-z0-9._-]*) fail "--environment may hold letters, digits, dot, dash and underscore only" ;; esac
fi

# Each config file's header documents its settings as `#   NAME   required|optional|default ...`.
# Take any of them set in this script's environment, then check the required ones.
MISSING=""
for file in "${SETTING_FILES[@]}"; do
  while read -r setting kind; do
    [ -n "$setting" ] || continue
    if [ -z "${SETTINGS[$setting]+set}" ] && [ -n "${!setting+set}" ]; then
      SETTINGS["$setting"]="${!setting}"
    fi
    if [ "$kind" = "required" ] && [ -z "${SETTINGS[$setting]:-}" ]; then
      MISSING="${MISSING} ${setting}"
    fi
  done < <(sed -n 's/^#   \([A-Z][A-Z0-9_]*\)  *\(required\|optional\|default\).*$/\1 \2/p' "$file")
done
[ -z "$MISSING" ] || fail "missing required settings:${MISSING}. Set them in the environment or in a --settings-file (or, when not a secret, with --set NAME=VALUE)"
# Defaults go in last, so a value from --set, the settings file or the
# environment is never replaced by one.
: "${SETTINGS[STRUCT_COLLECTOR_HEALTH_ENDPOINT]:=$HEALTH_ENDPOINT}"
: "${SETTINGS[STRUCT_INGEST_URL]:=https://ingest.struct.ai}"
# --environment goes first in the resource attributes and keeps any given another way.
if [ -n "$ENVIRONMENT" ]; then
  SETTINGS[OTEL_RESOURCE_ATTRIBUTES]="deployment.environment.name=${ENVIRONMENT},deployment.environment=${ENVIRONMENT}${SETTINGS[OTEL_RESOURCE_ATTRIBUTES]:+,${SETTINGS[OTEL_RESOURCE_ATTRIBUTES]}}"
fi

for setting in "${!SETTINGS[@]}"; do
  case "${SETTINGS[$setting]}" in *$'\n'*) fail "setting ${setting} contains a line break" ;; esac
done

log "Struct Collector: upstream otelcol-contrib ${UPSTREAM_VERSION} (linux/${ARCH}), presets: ${PRESETS}"
log "Sending to ${SETTINGS[STRUCT_INGEST_URL]}"

# 1. The upstream release, verified against the pinned checksum.
if [ -n "$TARBALL" ]; then
  log "Using local release archive ${TARBALL}"
  [ "$DRY_RUN" -eq 1 ] || cp "$TARBALL" "$WORK/$TARBALL_NAME"
else
  log "Downloading ${TARBALL_URL}"
  run curl --fail --silent --show-error --location --retry 3 --output "$WORK/$TARBALL_NAME" "$TARBALL_URL"
fi
log "Verifying SHA-256 ${EXPECTED_SHA256}"
if [ "$DRY_RUN" -eq 0 ]; then
  ACTUAL_SHA256="$(sha256sum "$WORK/$TARBALL_NAME" | cut -d' ' -f1)"
  [ "$ACTUAL_SHA256" = "$EXPECTED_SHA256" ] || fail "checksum mismatch for ${TARBALL_NAME}: expected ${EXPECTED_SHA256}, got ${ACTUAL_SHA256}. Nothing was installed."
  tar -xzf "$WORK/$TARBALL_NAME" -C "$WORK" otelcol-contrib
fi

# 2. The service user, the binary, the config and the state directory.
# The unit and the state directory name the group, so it is created here and not
# left to the machine's USERGROUPS_ENAB setting. A user that already exists
# without it keeps its own primary group: the unit's Group= is what the
# collector runs with.
if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
  run groupadd --system "$SERVICE_USER"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  NOLOGIN="$(command -v nologin || true)"
  run useradd --system --gid "$SERVICE_USER" --no-create-home --home-dir "$STATE_DIR" --shell "${NOLOGIN:-/usr/sbin/nologin}" "$SERVICE_USER"
fi
for preset in "${PRESET_LIST[@]}"; do
  if [ "$preset" = "docker" ] && getent group docker >/dev/null 2>&1; then
    # The docker preset reads the Docker socket.
    run usermod --append --groups docker "$SERVICE_USER"
  fi
done
run install -d -m 0755 "${INSTALL_DIR}/bin" "$CONFIG_DIR" "${CONFIG_DIR}/presets"
run install -m 0755 "$WORK/otelcol-contrib" "$BINARY"
run install -m 0644 "$WORK/config/config.yaml" "${CONFIG_DIR}/config.yaml"
for preset_file in "$WORK"/config/presets/*.yaml; do
  run install -m 0644 "$preset_file" "${CONFIG_DIR}/presets/$(basename "$preset_file")"
done
# Extra config files are the customer's: readable by root and the collector
# only, and the set from an earlier install is replaced, never added to.
run rm -rf "${CONFIG_DIR}/extra"
if [ "$EXTRA_NUMBER" -gt 0 ]; then
  run install -d -m 0750 -o root -g "$SERVICE_USER" "${CONFIG_DIR}/extra"
  EXTRA_NUMBER=0
  for extra in "${EXTRA_CONFIGS[@]}"; do
    EXTRA_NUMBER=$((EXTRA_NUMBER + 1))
    run install -m 0640 -o root -g "$SERVICE_USER" "$extra" "${CONFIG_DIR}/extra/extra-${EXTRA_NUMBER}.yaml"
  done
fi
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"

# 3. The settings file. Root-only: it holds the ingest key and preset passwords.
SETTINGS[STRUCT_COLLECTOR_ARGS]="$CONFIG_ARGS"
write_env_file() {
  local setting value
  umask 077
  : >"$1"
  for setting in $(printf '%s\n' "${!SETTINGS[@]}" | sort); do
    value="${SETTINGS[$setting]}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s="%s"\n' "$setting" "$value" >>"$1"
  done
}
if [ "$DRY_RUN" -eq 1 ]; then
  printf '+ write %s (mode 0600, root only) with: %s\n' "$ENV_FILE" "$(printf '%s\n' "${!SETTINGS[@]}" | sort | tr '\n' ' ')"
else
  write_env_file "$WORK/collector.env"
  install -m 0600 -o root -g root "$WORK/collector.env" "$ENV_FILE"
fi

# 4. The unit.
if [ "$DRY_RUN" -eq 1 ]; then
  printf '+ write %s\n' "$UNIT_PATH"
else
  unit_file >"$WORK/${NAME}.service"
  install -m 0644 "$WORK/${NAME}.service" "$UNIT_PATH"
fi

# 5. Ask the collector itself whether the config is valid, with the settings
#    it will run with, before anything is started.
log "Validating the config"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '+ %s validate %s\n' "$BINARY" "$CONFIG_ARGS"
else
  # The settings reach the collector through its environment, set with the
  # shell's own `export` in a subshell. Running it through env(1) would put
  # every value, the ingest key included, in that command's arguments.
  # shellcheck disable=SC2086 # CONFIG_ARGS is a list of --config arguments
  if ! (
    for setting in "${!SETTINGS[@]}"; do
      [ "$setting" = "STRUCT_COLLECTOR_ARGS" ] || export "${setting}=${SETTINGS[$setting]}"
    done
    exec "$BINARY" validate $CONFIG_ARGS
  ); then
    fail "the collector refused the config. The files are installed but the unit was not started; fix the settings and run this installer again."
  fi
fi

# 6. Start, or restart so a changed config takes effect, and wait for the
#    collector's own health check.
# One health request, allowed at most $1 seconds.
is_healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl --silent --fail --max-time "$1" "http://${SETTINGS[STRUCT_COLLECTOR_HEALTH_ENDPOINT]}/" >/dev/null 2>&1
  else
    systemctl is-active --quiet "${NAME}.service"
  fi
}
if [ "$START" -eq 1 ]; then
  run systemctl daemon-reload
  run systemctl enable "${NAME}.service"
  run systemctl restart "${NAME}.service"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ wait up to %s seconds for http://%s/\n' "$HEALTH_TIMEOUT_SECONDS" "${SETTINGS[STRUCT_COLLECTOR_HEALTH_ENDPOINT]}"
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    # A deadline, not a count of attempts: a listener that accepts and never
    # answers would otherwise stretch each attempt to its own timeout.
    healthy=0
    deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
      remaining=$((deadline - SECONDS))
      if is_healthy "$((remaining < 2 ? remaining : 2))"; then
        healthy=1
        break
      fi
      sleep 0.5
    done
    if [ "$healthy" -eq 1 ]; then
      log "The Struct Collector is running. Logs: journalctl -u ${NAME}"
    else
      fail "the unit was started but its health check did not answer within ${HEALTH_TIMEOUT_SECONDS} seconds. Check: journalctl -u ${NAME}"
    fi
  fi
else
  log "Installed. Start it with: systemctl enable --now ${NAME}"
fi
