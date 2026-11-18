"""The Linux install script: syntax, the systemd unit, the dry run and the single-file build.

Nothing here installs anything, needs root or Docker, or uses the network: the
script's --dry-run prints each step instead of taking it. The real install (a
download, the checksum, files, a running collector, uninstall) is the manual
suite in manual_install_live.py.

Usage:
    uv run python tests/test_install.py
    # Or: uv run pytest tests/test_install.py -v
"""

from __future__ import annotations

import base64
import io
import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from harness import (
    CONFIG_DIR,
    INSTALL_SCRIPT,
    PACKAGE_DIR,
    remove_work_dir,
    run_tests,
    work_dir,
)

BUILDER = PACKAGE_DIR / "scripts" / "build-installer.py"
KEY = "pk-install-test-do-not-print"
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "")


def install(*args: str, script: Path = INSTALL_SCRIPT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # A clean environment: the script reads settings from it, so the caller's must not leak in.
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    full_env.update(env or {})
    return subprocess.run(["bash", str(script), *args], check=False, capture_output=True, text=True, env=full_env, timeout=120)


def dry_run(*args: str, env: dict[str, str] | None = None, script: Path = INSTALL_SCRIPT) -> subprocess.CompletedProcess:
    extra = [] if script != INSTALL_SCRIPT else ["--config-dir", str(CONFIG_DIR)]
    return install("--dry-run", *extra, *args, script=script, env=env)


def build_installer(name: str) -> Path:
    output = work_dir() / name
    built = subprocess.run(["python3", str(BUILDER), "--output", str(output)], check=False, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    return output


def test_script_is_valid_bash() -> None:
    check = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], check=False, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    if shutil.which("shellcheck"):
        lint = subprocess.run(["shellcheck", "--severity=warning", str(INSTALL_SCRIPT)], check=False, capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout
    else:
        print("    (shellcheck not installed; only `bash -n` ran)")


def test_pinned_release_is_a_version_and_two_checksums() -> None:
    text = INSTALL_SCRIPT.read_text()
    assert re.search(r'^UPSTREAM_VERSION="\d+\.\d+\.\d+"$', text, re.MULTILINE)
    sums = re.findall(r'^UPSTREAM_SHA256_(AMD64|ARM64)="([0-9a-f]{64})"$', text, re.MULTILINE)
    assert [arch for arch, _ in sums] == ["AMD64", "ARM64"], sums
    assert sums[0][1] != sums[1][1]


# A diagnostic that starts with another unit file's path and line number.
_OTHER_UNIT = re.compile(r"^(/\S+\.(?:service|slice|socket|target|mount|automount|timer|path|scope|swap|device)):\d+: ")


def unit_problems(output: str, path: Path) -> list[str]:
    """What `systemd-analyze verify` says about the unit at ``path``. Left out: what it says about
    other units' files, and that the binary and the service user do not exist on this machine."""
    problems = []
    for line in output.splitlines():
        other = _OTHER_UNIT.match(line)
        if not line.strip() or (other and other.group(1) != str(path)):
            continue
        if any(expected in line for expected in ("is not executable", "otelcol-contrib", "Unknown user", "Failed to resolve user")):
            continue
        problems.append(line)
    return problems


def test_unit_runs_unprivileged_with_a_root_only_settings_file() -> None:
    unit = install("--print-unit")
    assert unit.returncode == 0, unit.stderr
    text = unit.stdout
    for line in (
        "User=struct-otel-collector",
        "EnvironmentFile=/etc/struct-otel-collector/collector.env",
        "ExecStart=/opt/struct-otel-collector/bin/otelcol-contrib $STRUCT_COLLECTOR_ARGS",
        "Restart=on-failure",
        "StateDirectory=struct-otel-collector",
        "NoNewPrivileges=true",
        "WantedBy=multi-user.target",
    ):
        assert line in text.splitlines(), f"unit lacks: {line}"
    if shutil.which("systemd-analyze"):
        path = work_dir() / "struct-otel-collector.service"
        path.write_text(text)
        verify = subprocess.run(["systemd-analyze", "verify", str(path)], check=False, capture_output=True, text=True)
        problems = unit_problems(verify.stdout + verify.stderr, path)
        assert not problems, problems
    # `verify` also loads the units this one depends on and reports what is wrong with them. Those
    # are the machine's, not ours (a CI runner's newer systemd warned about three of its own).
    theirs = "/usr/lib/systemd/system/walinuxagent.service:21: Support for option CPUAccounting= has been removed and it is ignored"
    ours = "/tmp/struct-otel-collector.service:9: Unknown key name 'NoNewPrivilege' in section 'Service', ignoring."
    assert unit_problems(f"{theirs}\n{ours}\n", Path("/tmp/struct-otel-collector.service")) == [ours]
    assert unit_problems("Unit struct-otel-collector.service has a bad unit file setting.\n", Path("/tmp/struct-otel-collector.service"))


def test_dry_run_prints_every_step_and_changes_nothing() -> None:
    assert ARCH, f"unsupported test machine: {platform.machine()}"
    installed_paths = ("/opt/struct-otel-collector", "/etc/struct-otel-collector", "/var/lib/struct-otel-collector",
                       "/etc/systemd/system/struct-otel-collector.service")
    before = {path: Path(path).exists() for path in installed_paths}
    result = dry_run("--presets", "hosts,redis", "--environment", "production", env={"STRUCT_INGEST_KEY": KEY})
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    out = result.stdout
    version = re.search(r'^UPSTREAM_VERSION="([^"]+)"$', INSTALL_SCRIPT.read_text(), re.MULTILINE).group(1)
    assert (f"https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
            f"v{version}/otelcol-contrib_{version}_linux_{ARCH}.tar.gz") in out
    assert re.search(r"Verifying SHA-256 [0-9a-f]{64}", out)
    for step in (
        "+ groupadd --system struct-otel-collector",
        "+ useradd --system --gid struct-otel-collector",
        "/opt/struct-otel-collector/bin/otelcol-contrib",
        "/etc/struct-otel-collector/presets/redis.yaml",
        "+ write /etc/struct-otel-collector/collector.env (mode 0600, root only)",
        "+ write /etc/systemd/system/struct-otel-collector.service",
        ("validate --config=/etc/struct-otel-collector/config.yaml "
         "--config=/etc/struct-otel-collector/presets/hosts.yaml --config=/etc/struct-otel-collector/presets/redis.yaml"),
        "+ systemctl daemon-reload",
        "+ systemctl enable struct-otel-collector.service",
        "+ systemctl restart struct-otel-collector.service",
        "+ wait up to 30 seconds for http://127.0.0.1:13133/",
    ):
        assert step in out, f"dry run lacks: {step}\n{out}"
    assert "OTEL_RESOURCE_ATTRIBUTES" in out and "STRUCT_INGEST_KEY" in out, "the settings file's names are listed"
    assert KEY not in out and KEY not in result.stderr, "the ingest key was printed"
    assert {path: Path(path).exists() for path in installed_paths} == before, "the dry run changed the machine"


def test_no_start_leaves_the_unit_alone() -> None:
    result = dry_run("--no-start", env={"STRUCT_INGEST_KEY": KEY})
    assert result.returncode == 0, result.stderr
    assert "systemctl" not in result.stdout.replace("systemctl enable --now struct-otel-collector", "")


def test_missing_settings_are_named_before_anything_is_done() -> None:
    no_key = dry_run()
    assert no_key.returncode != 0 and "missing required settings: STRUCT_INGEST_KEY" in no_key.stderr, no_key.stderr
    assert "+ " not in no_key.stdout, "steps were printed before the settings were checked"

    no_login = dry_run("--presets", "postgres", env={"STRUCT_INGEST_KEY": KEY})
    assert no_login.returncode != 0, no_login.stdout
    assert "STRUCT_POSTGRES_USERNAME" in no_login.stderr and "STRUCT_POSTGRES_PASSWORD" in no_login.stderr, no_login.stderr

    password = "pg-password-do-not-print"
    ok = dry_run("--presets", "postgres", "--set", "STRUCT_POSTGRES_USERNAME=monitor",
                 env={"STRUCT_INGEST_KEY": KEY, "STRUCT_POSTGRES_PASSWORD": password})
    assert ok.returncode == 0, ok.stderr
    assert "STRUCT_POSTGRES_USERNAME" in ok.stdout and "STRUCT_POSTGRES_PASSWORD" in ok.stdout
    assert password not in ok.stdout, "a preset password was printed"


def test_unknown_and_kubernetes_presets_are_refused() -> None:
    unknown = dry_run("--presets", "hosts,nope", env={"STRUCT_INGEST_KEY": KEY})
    assert unknown.returncode != 0 and "unknown preset: nope" in unknown.stderr, unknown.stderr
    assert "redis" in unknown.stderr, "the refusal lists what is available"
    kubernetes = dry_run("--presets", "kubernetes-node", env={"STRUCT_INGEST_KEY": KEY})
    assert kubernetes.returncode != 0 and "Helm chart" in kubernetes.stderr, kubernetes.stderr


def test_a_config_file_of_the_customers_own_is_installed_under_a_fixed_name_and_merged_last() -> None:
    first = work_dir() / "kafka tls.yaml"  # a space in the name: the path is never one of the collector's arguments
    first.write_text("receivers:\n  kafka_metrics:\n    tls:\n      insecure: false\n")
    second = work_dir() / "app-logs.yaml"
    second.write_text("# Settings:\n#   APP_LOG_PATH   required\nreceivers: {}\n")
    args = ("--presets", "hosts,kafka", "--extra-config", str(first), "--extra-config", str(second))

    ok = dry_run(*args, "--set", "APP_LOG_PATH=/var/log/app.log", env={"STRUCT_INGEST_KEY": KEY})
    assert ok.returncode == 0, ok.stderr
    out = ok.stdout
    # After every preset, so what the customer's file says wins, and in the order given.
    assert ("validate --config=/etc/struct-otel-collector/config.yaml "
            "--config=/etc/struct-otel-collector/presets/hosts.yaml --config=/etc/struct-otel-collector/presets/kafka.yaml "
            "--config=/etc/struct-otel-collector/extra/extra-1.yaml "
            "--config=/etc/struct-otel-collector/extra/extra-2.yaml") in out, out
    # Readable by root and the collector only, and an earlier install's set is replaced.
    assert "+ rm -rf /etc/struct-otel-collector/extra\n" in out
    assert "+ install -d -m 0750 -o root -g struct-otel-collector /etc/struct-otel-collector/extra" in out
    for number, source in ((1, first), (2, second)):
        assert (f"+ install -m 0640 -o root -g struct-otel-collector {source} "
                f"/etc/struct-otel-collector/extra/extra-{number}.yaml") in out, out
    assert "APP_LOG_PATH" in out, "a setting the file documents reaches the settings file"

    # A setting the customer's file documents as required is checked like a preset's.
    unset = dry_run(*args, env={"STRUCT_INGEST_KEY": KEY})
    assert unset.returncode != 0 and "missing required settings: APP_LOG_PATH" in unset.stderr, unset.stderr
    missing = dry_run("--extra-config", str(work_dir() / "nope.yaml"), env={"STRUCT_INGEST_KEY": KEY})
    assert missing.returncode != 0 and "cannot read --extra-config" in missing.stderr, missing.stderr
    assert "+ " not in missing.stdout, "steps were printed before the file was checked"

    # Without the flag there is no extra argument, and an earlier install's files still go.
    plain = dry_run(env={"STRUCT_INGEST_KEY": KEY})
    assert plain.returncode == 0, plain.stderr
    assert "/extra/extra-" not in plain.stdout and "+ rm -rf /etc/struct-otel-collector/extra\n" in plain.stdout


def test_ingest_key_can_come_from_a_file() -> None:
    key_file = work_dir() / "ingest-key"
    key_file.write_text(KEY + "\n")
    result = dry_run("--ingest-key-file", str(key_file))
    assert result.returncode == 0, result.stderr
    assert KEY not in result.stdout


def secret_rule(name: str) -> bool:
    """What the script's own is_secret says about a setting name."""
    function = re.search(r"^is_secret\(\) \{\n.*?^\}$", INSTALL_SCRIPT.read_text(), re.MULTILINE | re.DOTALL)
    assert function, "install.sh has no is_secret function"
    check = subprocess.run(["bash", "-c", f'{function.group(0)}\nis_secret "$1"', "bash", name], check=False)
    return check.returncode == 0


def test_secrets_are_refused_as_arguments_and_never_reach_another_commands_arguments() -> None:
    # Given by argument, a secret is refused by name, without repeating it, before anything is done.
    for pair in (f"STRUCT_INGEST_KEY={KEY}", "STRUCT_POSTGRES_PASSWORD=argument-secret-do-not-print"):
        name, value = pair.split("=", 1)
        refused = dry_run("--presets", "postgres", "--set", pair, env={"STRUCT_INGEST_KEY": KEY})
        assert refused.returncode != 0, refused.stdout
        assert f"--set cannot take {name}" in refused.stderr, refused.stderr
        assert value not in refused.stderr and value not in refused.stdout, "the refusal repeats the secret"
        assert "+ " not in refused.stdout, "steps were printed before the refusal"

    # Every documented setting that holds a credential is a secret by the script's one rule.
    documented = set()
    for path in [CONFIG_DIR / "config.yaml", *sorted((CONFIG_DIR / "presets").glob("*.yaml"))]:
        documented.update(re.findall(r"^#   ([A-Z][A-Z0-9_]*)  +(?:required|optional|default)", path.read_text(), re.MULTILINE))
    assert "STRUCT_INGEST_KEY" in documented and "STRUCT_POSTGRES_PASSWORD" in documented, sorted(documented)
    credentials = {name for name in documented if re.search(r"PASS|TOKEN|SECRET|KEY|CREDENTIAL", name)}
    assert credentials and all(secret_rule(name) for name in credentials), sorted(credentials)
    assert not secret_rule("STRUCT_POSTGRES_USERNAME") and not secret_rule("STRUCT_COLLECTOR_HEALTH_ENDPOINT")

    # Validation hands the settings to the collector with the shell's own export, not through
    # env(1), whose arguments would hold every value. manual_install_live.py traces the real run.
    text = INSTALL_SCRIPT.read_text()
    block = text[text.index("# 5. Ask the collector itself"):text.index("# 6. Start, or restart")]
    assert re.search(r'export "\$\{setting\}=\$\{SETTINGS\[\$setting\]\}"', block), block
    assert not re.search(r"^\s*(if ! )?env\b", text, re.MULTILINE), "install.sh runs a command through env(1)"


def test_settings_come_from_a_file_and_an_argument_wins_over_it_and_the_environment() -> None:
    settings = work_dir() / "settings"
    password = "file-password-$HOME-do-not-print"
    settings.write_text(f"# a comment\n\nSTRUCT_INGEST_KEY={KEY}\r\nSTRUCT_POSTGRES_USERNAME=from-file\n"
                        f"STRUCT_POSTGRES_PASSWORD={password}\nSTRUCT_COLLECTOR_HEALTH_ENDPOINT=127.0.0.1:18001\n")
    from_file = dry_run("--presets", "postgres", "--settings-file", str(settings),
                        env={"STRUCT_COLLECTOR_HEALTH_ENDPOINT": "127.0.0.1:18002"})
    assert from_file.returncode == 0, from_file.stderr
    assert "STRUCT_POSTGRES_PASSWORD" in from_file.stdout, "the settings file's names are listed"
    assert password not in from_file.stdout and KEY not in from_file.stdout
    assert "+ wait up to 30 seconds for http://127.0.0.1:18001/" in from_file.stdout, "the file wins over the environment"

    by_argument = dry_run("--presets", "postgres", "--settings-file", str(settings),
                          "--set", "STRUCT_COLLECTOR_HEALTH_ENDPOINT=127.0.0.1:18003")
    assert "+ wait up to 30 seconds for http://127.0.0.1:18003/" in by_argument.stdout, by_argument.stdout

    # With neither, the environment is used: a default never replaces a value that was given.
    from_environment = dry_run(env={"STRUCT_INGEST_KEY": KEY, "STRUCT_COLLECTOR_HEALTH_ENDPOINT": "127.0.0.1:18002"})
    assert "+ wait up to 30 seconds for http://127.0.0.1:18002/" in from_environment.stdout, from_environment.stdout

    malformed = work_dir() / "malformed-settings"
    malformed.write_text("not a setting line\n")
    refused = dry_run("--settings-file", str(malformed), env={"STRUCT_INGEST_KEY": KEY})
    assert refused.returncode != 0 and "line 1 is not NAME=VALUE" in refused.stderr, refused.stderr

    # A name the shell could not export is refused before any step, from an argument or a file.
    misnamed = work_dir() / "misnamed-settings"
    misnamed.write_text("STRUCT_REDIS-ENDPOINT=localhost:6379\n")
    for args in (("--set", "STRUCT_REDIS-ENDPOINT=localhost:6379"), ("--settings-file", str(misnamed))):
        refused = dry_run(*args, env={"STRUCT_INGEST_KEY": KEY})
        assert refused.returncode != 0 and "capitals, digits and underscores" in refused.stderr, refused.stderr
        assert "+ " not in refused.stdout, "steps were printed before the refusal"


def test_the_ingest_url_and_the_environment_resolve_like_every_other_setting() -> None:
    def sending_to(*args: str, env: dict[str, str] | None = None) -> str:
        result = dry_run(*args, env={"STRUCT_INGEST_KEY": KEY, **(env or {})})
        assert result.returncode == 0, result.stderr
        return re.search(r"^Sending to (\S+)$", result.stdout, re.MULTILINE).group(1)

    settings = work_dir() / "url-settings"
    settings.write_text("STRUCT_INGEST_URL=http://127.0.0.1:19876\n")
    from_environment = {"STRUCT_INGEST_URL": "http://from-environment.example"}
    assert sending_to() == "https://ingest.struct.ai", "the default, when nothing gives one"
    assert sending_to(env=from_environment) == "http://from-environment.example"
    assert sending_to("--settings-file", str(settings), env=from_environment) == "http://127.0.0.1:19876"
    assert sending_to("--settings-file", str(settings), "--set", "STRUCT_INGEST_URL=http://from-set.example") == (
        "http://from-set.example")
    assert sending_to("--settings-file", str(settings), "--set", "STRUCT_INGEST_URL=http://from-set.example",
                      "--ingest-url", "http://from-flag.example") == "http://from-flag.example"

    # --environment adds to resource attributes given another way; it does not replace them.
    script = INSTALL_SCRIPT.read_text()
    assert 'SETTINGS[OTEL_RESOURCE_ATTRIBUTES]="deployment.environment.name=${ENVIRONMENT},deployment.environment=${ENVIRONMENT}${SETTINGS[OTEL_RESOURCE_ATTRIBUTES]:+,${SETTINGS[OTEL_RESOURCE_ATTRIBUTES]}}"' in script


def test_checkout_script_needs_a_config_directory() -> None:
    result = install("--dry-run", env={"STRUCT_INGEST_KEY": KEY})
    assert result.returncode != 0 and "--config-dir" in result.stderr, result.stderr


def test_single_file_installer_carries_the_config() -> None:
    first, second = build_installer("install-a.sh"), build_installer("install-b.sh")
    assert first.read_bytes() == second.read_bytes(), "the same config must build the same installer"
    assert subprocess.run(["bash", "-n", str(first)], check=False, capture_output=True).returncode == 0

    text = first.read_text()
    payload = text.split("cat <<'__STRUCT_COLLECTOR_CONFIG__'\n", 1)[1].split("\n__STRUCT_COLLECTOR_CONFIG__", 1)[0]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload)), mode="r:gz") as archive:
        embedded = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    on_disk = {path.relative_to(CONFIG_DIR).as_posix(): path.read_bytes() for path in CONFIG_DIR.rglob("*.yaml")}
    assert embedded == on_disk, "the embedded config differs from config/"

    # Without --config-dir: the embedded copy is what gets installed.
    result = dry_run("--presets", "hosts,clickhouse", env={"STRUCT_INGEST_KEY": KEY}, script=first)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "/etc/struct-otel-collector/presets/clickhouse.yaml" in result.stdout


def test_uninstall_dry_run_names_everything_it_removes() -> None:
    result = install("--uninstall", "--dry-run")
    assert result.returncode == 0, result.stderr
    for step in ("rm -f /etc/systemd/system/struct-otel-collector.service",
                 "rm -rf /opt/struct-otel-collector /etc/struct-otel-collector /var/lib/struct-otel-collector"):
        assert step in result.stdout, result.stdout


TESTS = [
    test_script_is_valid_bash,
    test_pinned_release_is_a_version_and_two_checksums,
    test_unit_runs_unprivileged_with_a_root_only_settings_file,
    test_dry_run_prints_every_step_and_changes_nothing,
    test_no_start_leaves_the_unit_alone,
    test_missing_settings_are_named_before_anything_is_done,
    test_unknown_and_kubernetes_presets_are_refused,
    test_a_config_file_of_the_customers_own_is_installed_under_a_fixed_name_and_merged_last,
    test_ingest_key_can_come_from_a_file,
    test_secrets_are_refused_as_arguments_and_never_reach_another_commands_arguments,
    test_settings_come_from_a_file_and_an_argument_wins_over_it_and_the_environment,
    test_the_ingest_url_and_the_environment_resolve_like_every_other_setting,
    test_checkout_script_needs_a_config_directory,
    test_single_file_installer_carries_the_config,
    test_uninstall_dry_run_names_everything_it_removes,
]


def teardown_module() -> None:  # pytest
    remove_work_dir()


if __name__ == "__main__":
    run_tests(TESTS, cleanup=remove_work_dir)
