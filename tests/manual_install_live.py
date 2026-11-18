"""Manual suite: a real install with the single-file installer, on a machine that runs systemd.

Not one of the default test files: it downloads the upstream release (about 110 MB, kept
in ~/.cache/struct-otel-collector-tests/downloads), builds a Debian image with
systemd in it and runs that image privileged, so systemd is PID 1.

The machine is Debian 12 with `USERGROUPS_ENAB no`, where `useradd` does not
create a group for a new user. On it, as root, the checks:

1. give the installer a release archive with one byte added, and check it is
   refused on its checksum with nothing installed;
2. check that a secret given as an argument is refused, by name, with nothing
   installed;
3. install for real, with systemd starting the unit, under `strace`: the ingest
   key and a preset password come from a settings file, and neither may appear
   in the arguments of anything the installer runs;
4. check the files, their owners and modes, the binary's version, and that the
   service user cannot read the settings file;
5. check the collector systemd started: it runs as the service user and group,
   it answers on the health endpoint taken from the environment, and the
   password (which holds `$`, `"` and `\\`) reached it byte for byte;
6. install again over a health port that accepts connections and never answers,
   and check the installer gives up at its 30-second deadline;
7. uninstall and check nothing is left, the group included.

    uv run python tests/manual_install_live.py
"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from harness import (
    INSTALL_SCRIPT,
    PACKAGE_DIR,
    RUN_ID,
    docker,
    remove_work_dir,
    run_tests,
    work_dir,
)

IMAGE = f"struct-otel-collector-test-systemd:{RUN_ID}"
CONTAINER = f"struct-otel-collector-test-install-{RUN_ID}"
DOWNLOADS = Path.home() / ".cache" / "struct-otel-collector-tests" / "downloads"
KEY = "pk-live-install-test"
# What a customer might really have: characters a shell or systemd could expand or unquote.
PASSWORD = r'p@ss$MISSING_VAR "quoted" back\slash $(id) end'
EXPECTED_OK = 9

DOCKERFILE = """FROM debian:12-slim
RUN apt-get update -qq \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \\
      systemd systemd-sysv curl ca-certificates strace netcat-openbsd >/dev/null \\
 && rm -rf /var/lib/apt/lists/* \\
 && sed -i 's/^USERGROUPS_ENAB.*/USERGROUPS_ENAB no/' /etc/login.defs
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
"""

CHECKS = r"""#!/usr/bin/env bash
set -euo pipefail
NAME=struct-otel-collector
KEY="$(cat /work/key)"
PASSWORD="$(cat /work/password)"
ok() { echo "ok: $*"; }
nothing_installed() { test ! -e /opt/$NAME && test ! -e /etc/$NAME && ! id $NAME >/dev/null 2>&1; }

grep -q '^USERGROUPS_ENAB no' /etc/login.defs

# 1. A release archive that does not match the pinned checksum is refused.
cp /dl/release.tar.gz /tmp/tampered.tar.gz
printf 'x' >> /tmp/tampered.tar.gz
if STRUCT_INGEST_KEY="$KEY" bash /work/install.sh --tarball /tmp/tampered.tar.gz --no-start >/tmp/tampered.log 2>&1; then
  echo "FAIL: a tampered archive was installed"; exit 1
fi
grep -q "checksum mismatch" /tmp/tampered.log
nothing_installed
ok "tampered archive refused on its checksum, nothing installed"

# 2. A secret given as an argument is refused, and the refusal does not repeat it.
for pair in "STRUCT_INGEST_KEY=$KEY" "STRUCT_REDIS_PASSWORD=argument-secret"; do
  if bash /work/install.sh --tarball /dl/release.tar.gz --set "$pair" >/tmp/refused.log 2>&1; then
    echo "FAIL: --set accepted ${pair%%=*}"; exit 1
  fi
  grep -q -- "--set cannot take ${pair%%=*}" /tmp/refused.log
  if grep -q -F -- "${pair#*=}" /tmp/refused.log; then echo "FAIL: the refusal repeats the value"; exit 1; fi
done
nothing_installed
ok "secrets given with --set are refused by name, nothing installed"

# 3. The real install, traced. Secrets come from a file; the health endpoint from the environment.
umask 077
printf '# settings for the live test\nSTRUCT_INGEST_KEY=%s\nSTRUCT_REDIS_PASSWORD=%s\n' "$KEY" "$PASSWORD" >/root/collector-settings
umask 022
STRUCT_COLLECTOR_HEALTH_ENDPOINT=127.0.0.1:19999 \
  strace -f -qq -e trace=execve -s 16384 -o /tmp/execve.trace \
  bash /work/install.sh --tarball /dl/release.tar.gz --settings-file /root/collector-settings \
    --presets hosts,redis --environment staging --ingest-url http://127.0.0.1:9
test "$(grep -c 'execve(' /tmp/execve.trace)" -gt 20
grep -q 'otelcol-contrib", "validate"' /tmp/execve.trace
for secret in "$KEY" 'p@ss' 'MISSING_VAR'; do
  if grep -q -F -- "$secret" /tmp/execve.trace; then
    echo "FAIL: a secret is in the arguments of a command the installer ran:"
    grep -F -- "$secret" /tmp/execve.trace | cut -c1-160 | sed "s/$KEY/<key>/g"
    exit 1
  fi
done
ok "installer finished; no secret is in the arguments of any of the $(grep -c 'execve(' /tmp/execve.trace) commands it ran"

# 4. What it left behind.
test "$(stat -c '%a %U:%G' /etc/$NAME/collector.env)" = "600 root:root"
grep -q -F "STRUCT_INGEST_KEY=\"$KEY\"" /etc/$NAME/collector.env
grep -q '^OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=staging,deployment.environment=staging"$' /etc/$NAME/collector.env
grep -q '^STRUCT_COLLECTOR_HEALTH_ENDPOINT="127.0.0.1:19999"$' /etc/$NAME/collector.env
ok "settings file is root-only and holds the key, the environment and the health endpoint from the environment"
getent group $NAME >/dev/null
test "$(id -gn $NAME)" = "$NAME"
if runuser -u $NAME -- cat /etc/$NAME/collector.env >/dev/null 2>&1; then
  echo "FAIL: the service user can read the settings file"; exit 1
fi
test "$(stat -c '%U:%G' /var/lib/$NAME)" = "$NAME:$NAME"
test -f /etc/$NAME/config.yaml && test -f /etc/$NAME/presets/hosts.yaml
/opt/$NAME/bin/otelcol-contrib --version | grep -q "__VERSION__"
ok "user and group exist where useradd creates no group; binary is upstream __VERSION__; config and state are in place"

# 5. The collector systemd started.
test "$(systemctl is-active $NAME.service)" = "active"
test "$(systemctl is-enabled $NAME.service)" = "enabled"
pid="$(systemctl show --property MainPID --value $NAME.service)"
test "$pid" -gt 1
test "$(basename "$(readlink /proc/$pid/exe)")" = "otelcol-contrib"
uid="$(awk '/^Uid:/ {print $2}' /proc/$pid/status)"
gid="$(awk '/^Gid:/ {print $2}' /proc/$pid/status)"
test "$uid" = "$(id -u $NAME)" && test "$uid" != "0"
test "$gid" = "$(getent group $NAME | cut -d: -f3)"
curl --silent --fail --max-time 5 http://127.0.0.1:19999/ >/dev/null
ok "systemd runs the collector (pid $pid) as the service user and group, healthy on the endpoint from the environment"
tr '\0' '\n' </proc/$pid/environ >/tmp/collector-environ
if ! grep -q -x -F -- "STRUCT_REDIS_PASSWORD=$PASSWORD" /tmp/collector-environ; then
  echo "FAIL: the password did not reach the collector as written. Settings file line:"
  grep '^STRUCT_REDIS_PASSWORD=' /etc/$NAME/collector.env | cut -c1-200
  echo "What the collector got:"; grep '^STRUCT_REDIS_PASSWORD=' /tmp/collector-environ | cut -c1-200
  exit 1
fi
grep -q -x -F -- "STRUCT_INGEST_KEY=$KEY" /tmp/collector-environ
ok "a password with \$, quotes and a backslash reached the collector byte for byte through systemd"

# 6. A health port that accepts and never answers: the installer gives up at its deadline.
nc -lk 127.0.0.1 19998 >/dev/null 2>&1 &
holder=$!
started=$SECONDS
if STRUCT_COLLECTOR_HEALTH_ENDPOINT=127.0.0.1:19998 bash /work/install.sh --tarball /dl/release.tar.gz \
     --settings-file /root/collector-settings --presets hosts --ingest-url http://127.0.0.1:9 >/tmp/hang.log 2>&1; then
  echo "FAIL: the installer reported a healthy collector on a port that never answers"; exit 1
fi
elapsed=$((SECONDS - started))
kill "$holder" 2>/dev/null || true
grep -q "did not answer within 30 seconds" /tmp/hang.log
if [ "$elapsed" -gt 50 ]; then echo "FAIL: the installer waited $elapsed seconds, not about 30"; exit 1; fi
ok "over a silent health port the installer gave up after $elapsed seconds (deadline 30, plus the install itself)"

# 7. Uninstall.
bash /work/install.sh --uninstall >/dev/null
for path in /opt/$NAME /etc/$NAME /var/lib/$NAME /etc/systemd/system/$NAME.service; do
  test ! -e "$path"
done
if id $NAME >/dev/null 2>&1; then echo "FAIL: the service user is still there"; exit 1; fi
if getent group $NAME >/dev/null 2>&1; then echo "FAIL: the group is still there"; exit 1; fi
if systemctl is-active --quiet $NAME.service; then echo "FAIL: the unit is still active"; exit 1; fi
ok "uninstall removed the binary, config, state, unit, user and group"
"""


def pinned() -> tuple[str, str, str]:
    """Version, architecture and SHA-256 of the release for this machine, from the install script."""
    text = INSTALL_SCRIPT.read_text()
    version = re.search(r'^UPSTREAM_VERSION="([^"]+)"$', text, re.MULTILINE).group(1)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
    checksum = re.search(rf'^UPSTREAM_SHA256_{arch.upper()}="([0-9a-f]{{64}})"$', text, re.MULTILINE).group(1)
    return version, arch, checksum


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_archive() -> Path:
    version, arch, checksum = pinned()
    name = f"otelcol-contrib_{version}_linux_{arch}.tar.gz"
    path = DOWNLOADS / name
    if not (path.exists() and sha256(path) == checksum):
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v{version}/{name}"
        print(f"    downloading {url}")
        urllib.request.urlretrieve(url, path)
    assert sha256(path) == checksum, f"{name} does not match the checksum pinned in install.sh"
    return path


def wait_for_systemd() -> None:
    """Until systemd in the container has finished starting, or 60 seconds."""
    deadline = time.monotonic() + 60
    state = ""
    while time.monotonic() < deadline:
        state = docker("exec", CONTAINER, "systemctl", "is-system-running").stdout.strip()
        if state in ("running", "degraded"):
            return
        time.sleep(0.5)
    raise AssertionError(f"systemd in the container never finished starting (last state: {state!r})")


def test_install_run_and_uninstall_under_systemd() -> None:
    version, _, _ = pinned()
    archive = release_archive()
    work = work_dir()
    built = subprocess.run(
        ["python3", str(PACKAGE_DIR / "scripts" / "build-installer.py"), "--output", str(work / "install.sh")],
        check=False, capture_output=True, text=True,
    )
    assert built.returncode == 0, built.stderr
    (work / "checks.sh").write_text(CHECKS.replace("__VERSION__", version))
    (work / "key").write_text(KEY)
    (work / "password").write_text(PASSWORD)
    image_dir = work / "image"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text(DOCKERFILE)
    work.chmod(0o755)
    for path in work.iterdir():
        path.chmod(0o755)
    try:
        image = docker("build", "-q", "-t", IMAGE, str(image_dir), timeout=600)
        assert image.returncode == 0, image.stderr[-2000:]
        started = docker(
            "run", "-d", "--name", CONTAINER, "--privileged", "--cgroupns=host",
            "--tmpfs", "/run", "--tmpfs", "/run/lock",
            "--mount", "type=bind,src=/sys/fs/cgroup,dst=/sys/fs/cgroup",
            "--mount", f"type=bind,src={work},dst=/work,readonly",
            "--mount", f"type=bind,src={archive},dst=/dl/release.tar.gz,readonly",
            IMAGE,
        )
        assert started.returncode == 0, started.stderr[-2000:]
        wait_for_systemd()
        result = docker("exec", CONTAINER, "bash", "/work/checks.sh", timeout=600)
    finally:
        docker("rm", "-f", CONTAINER)
        docker("rmi", "-f", IMAGE)
    for line in result.stdout.splitlines():
        if line.startswith(("ok:", "FAIL")):
            print(f"    {line}")
    assert result.returncode == 0, f"checks failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    assert result.stdout.count("ok: ") == EXPECTED_OK, result.stdout


if __name__ == "__main__":
    run_tests([test_install_run_and_uninstall_under_systemd], cleanup=remove_work_dir)
