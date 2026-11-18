#!/usr/bin/env python3
"""The Struct Collector's release rules: one version names the image, the chart and the installer.

The version is `version` in chart/struct-otel-collector/Chart.yaml, written
<upstream>-<n>: the upstream OpenTelemetry Collector (contrib) release this
package wraps, then Struct's revision of it, counted from 1. The chart's
`appVersion` equals it, so the chart deploys the image of its own release.

A release is a tag v<version> on a commit of main in this repository,
struct-dot-ai/struct-otel-collector. Its workflow (.github/workflows/release.yml) publishes
from that commit and, last, creates the GitHub release for the tag. It runs this script:

    python3 scripts/release.py version          # print the version
    python3 scripts/release.py plan             # check the tag and its commit; refuse a version that is published, or older
    python3 scripts/release.py check-contents   # before pushing: nothing internal is in the image, the chart or the installer
    python3 scripts/release.py check-public     # after pushing: anyone can pull the image under both names, as published
    python3 scripts/release.py chart-index      # write the chart repository's index from the charts in it
    python3 scripts/release.py check-chart      # after publishing the chart: anyone can add the chart repository and get this chart
    python3 scripts/release.py check-installer  # after the release: anyone can download the installer, byte for byte

What is published:

    docker.io/structai/struct-otel-collector:<version>             the image, linux/amd64 and linux/arm64 (the name the chart deploys)
    ghcr.io/struct-dot-ai/struct-otel-collector:<version>         the same image, a copy for anyone Docker Hub limits
    https://struct-dot-ai.github.io/struct-otel-collector          the Helm chart repository (`helm repo add`), served by
                                                                   GitHub Pages from the gh-pages branch of
                                                                   struct-dot-ai/struct-otel-collector
    struct-dot-ai/struct-otel-collector, release v<version>       the single-file installer and its .sha256

The gh-pages branch holds nothing but the chart packages and their index; the releases hold the
installer.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
ROOT = PACKAGE_DIR
# Where a release starts: this repository, by a tag with this prefix.
TAG_PREFIX = "v"
# Where the installer is released. It holds no source.
REPOSITORY = "struct-dot-ai/struct-otel-collector"
INSTALLER = "struct-otel-collector-install.sh"
# The image is pushed under both names, one digest. The first is the one the chart deploys and the
# docs give; the second is a copy for anyone Docker Hub's limits on anonymous pulls get in the way of.
IMAGES = ("docker.io/structai/struct-otel-collector", "ghcr.io/struct-dot-ai/struct-otel-collector")
IMAGE = IMAGES[0]
CHART_NAME = "struct-otel-collector"
CHART_REPOSITORY = "https://struct-dot-ai.github.io/struct-otel-collector"
PLATFORMS = ("linux/amd64", "linux/arm64")

# <upstream>-<n>: three numbers without leading zeros, then a revision from 1.
VERSION = re.compile(
    r"(?P<upstream>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<revision>[1-9][0-9]*)"
)

# Never published, wherever it sits: instructions for working on the package, and its tests.
INTERNAL_NAMES = ("AGENTS.md", "CLAUDE.md")
INTERNAL_DIRECTORIES = ("tests", "test", "__pycache__")
INTERNAL_PATTERNS = tuple(re.compile(pattern) for pattern in (r"test_.*\.py", r".*_test\.py", r"conftest\.py", r"manual_.*\.py"))
# Never named in a published file either.
INTERNAL_WORDS = ("AGENTS.md", "CLAUDE.md")
EMBEDDED_CONFIG = re.compile(r"^# BEGIN EMBEDDED CONFIG$.*?^# END EMBEDDED CONFIG$", re.MULTILINE | re.DOTALL)

INDEX_TYPES = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"


class ReleaseError(Exception):
    pass


def yaml_scalar(text: str, key: str, parent: str | None = None) -> str | None:
    """A plain scalar from a simple YAML file: a top-level key, or a key one level under `parent`."""
    lines = text.splitlines()
    if parent is not None:
        try:
            start = next(i for i, line in enumerate(lines) if re.fullmatch(rf"{re.escape(parent)}:\s*(#.*)?", line))
        except StopIteration:
            return None
        block = []
        for line in lines[start + 1:]:
            if line.strip() and not line.lstrip().startswith("#") and not line[:1].isspace():
                break
            block.append(line)
        lines, pattern = block, rf"\s+{re.escape(key)}:\s*(.*?)\s*(#.*)?"
    else:
        pattern = rf"{re.escape(key)}:\s*(.*?)\s*(#.*)?"
    for line in lines:
        found = re.fullmatch(pattern, line)
        if found:
            value = found.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def _match(pattern: str, text: str) -> str | None:
    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1) if found else None


def read_package(package_dir: Path = PACKAGE_DIR) -> dict[str, str | None]:
    """Every place in the package that names the version, as written there."""
    chart_dir = package_dir / "chart" / "struct-otel-collector"
    chart = (chart_dir / "Chart.yaml").read_text()
    values = (chart_dir / "values.yaml").read_text()
    return {
        "Dockerfile FROM": _match(r"^FROM otel/opentelemetry-collector-contrib:(\S+)$", (package_dir / "Dockerfile").read_text()),
        "install.sh UPSTREAM_VERSION": _match(r'^UPSTREAM_VERSION="([^"]*)"$', (package_dir / "install" / "install.sh").read_text()),
        "Chart.yaml name": yaml_scalar(chart, "name"),
        "Chart.yaml version": yaml_scalar(chart, "version"),
        "Chart.yaml appVersion": yaml_scalar(chart, "appVersion"),
        "values.yaml image.repository": yaml_scalar(values, "repository", parent="image"),
    }


def check_package(found: dict[str, str | None]) -> tuple[str, str]:
    """(version, upstream), when the package describes exactly one release."""
    version = found["Chart.yaml version"] or ""
    parsed = VERSION.fullmatch(version)
    if not parsed:
        raise ReleaseError(f"Chart.yaml version {version!r} is not <upstream>-<n>, for example 0.165.0-1.")
    upstream = parsed["upstream"]
    expected = {
        "Chart.yaml name": "struct-otel-collector",
        "Chart.yaml appVersion": version,
        "Dockerfile FROM": upstream,
        "install.sh UPSTREAM_VERSION": upstream,
        "values.yaml image.repository": IMAGE,
    }
    wrong = [f"{where} is {found.get(where)!r}, expected {value!r}" for where, value in expected.items() if found.get(where) != value]
    if wrong:
        raise ReleaseError("The package does not describe one release: " + "; ".join(wrong) + ".")
    return version, upstream


def version_key(version: str) -> tuple[int, ...]:
    parsed = VERSION.fullmatch(version)
    if not parsed:
        raise ReleaseError(f"{version!r} is not <upstream>-<n>.")
    return (*(int(part) for part in parsed["upstream"].split(".")), int(parsed["revision"]))


def run(*command: str, cwd: Path = ROOT, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def release_tags() -> list[str]:
    """Tag names of the releases of the repository that holds the installer: v<version> for each released version."""
    result = run("gh", "api", "--paginate", f"repos/{REPOSITORY}/releases?per_page=100", "--jq", ".[].tag_name")
    if result.returncode:
        raise ReleaseError(
            f"Could not list the releases of {REPOSITORY}; refusing to guess whether this version exists. "
            "This repository must be public."
        )
    return [line for line in result.stdout.splitlines() if line]


def checked_out_commit(root: Path) -> str:
    result = run("git", "rev-parse", "HEAD", cwd=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def on_main(root: Path, sha: str) -> bool:
    return run("git", "merge-base", "--is-ancestor", sha, "origin/main", cwd=root).returncode == 0


def plan(
    env: dict[str, str], package_dir: Path = PACKAGE_DIR, root: Path = ROOT,
    releases=release_tags, head=checked_out_commit, is_on_main=on_main,
) -> dict[str, str]:
    """The release this tag asks for. Refuses a version that is published, or older than one that is.

    The installer's release is created last, after the image and the chart can be
    pulled, so it marks a version as published. A version without one was never
    finished: re-running the failed job of the same run finishes it.
    """
    ref = env.get("GITHUB_REF", "")
    if (
        env.get("GITHUB_EVENT_NAME") != "push"
        or env.get("GITHUB_REPOSITORY") != REPOSITORY
        or not ref.startswith(f"refs/tags/{TAG_PREFIX}")
    ):
        raise ReleaseError(f"A Struct Collector release is a tag {TAG_PREFIX}<version> pushed to {REPOSITORY}.")
    tag = ref.removeprefix("refs/tags/")
    version = tag.removeprefix(TAG_PREFIX)
    if not VERSION.fullmatch(version):
        raise ReleaseError(f"{version!r} is not <upstream>-<n>, for example 0.165.0-1.")
    expected, upstream = check_package(read_package(package_dir))
    if version != expected:
        raise ReleaseError(
            f"The tag says {version} but chart/struct-otel-collector/Chart.yaml at this commit says {expected}. "
            "Merge the version change first, then tag that commit."
        )
    sha = head(root)
    if not sha or sha != env.get("GITHUB_SHA"):
        raise ReleaseError("The checkout must match this workflow run's source commit.")
    if not is_on_main(root, sha):
        raise ReleaseError("The tagged commit must be on main; fetch full history before validation.")
    published = [other[1:] for other in releases() if other.startswith("v") and VERSION.fullmatch(other[1:])]
    if version in published:
        raise ReleaseError(f"{version} is already released, and a released version never changes. Release the next revision instead.")
    newer = [other for other in published if version_key(other) > version_key(version)]
    if newer:
        raise ReleaseError(
            f"{max(newer, key=version_key)} is already released. The install URL serves the latest release, so a release must be newer than every other."
        )
    return {"tag": tag, "version": version, "upstream": upstream, "source_sha": sha}


# ---------------------------------------------------------------------------
# Nothing internal leaves: checked on what is about to be pushed
# ---------------------------------------------------------------------------

def internal_paths(paths) -> list[str]:
    """The paths that must never be published: instruction files, tests, and anything under a tests directory."""
    found = []
    for raw in paths:
        path = raw.strip()
        parts = [part for part in path.split("/") if part not in ("", ".")]
        if not parts:
            continue
        name = parts[-1]
        if (
            name in INTERNAL_NAMES
            or name in INTERNAL_DIRECTORIES
            or any(part in INTERNAL_DIRECTORIES for part in parts[:-1])
            or any(pattern.fullmatch(name) for pattern in INTERNAL_PATTERNS)
        ):
            found.append(path)
    return found


def internal_words(name: str, data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace").lower()
    return [f"{name} names {word}" for word in INTERNAL_WORDS if word.lower() in text]


def archive_members(data: bytes) -> dict[str, bytes]:
    """Every regular file of a tar.gz, by path."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}


def installer_members(script: str) -> dict[str, bytes]:
    """The config files the single-file installer carries."""
    block = EMBEDDED_CONFIG.search(script)
    if not block:
        raise ReleaseError("The installer has no embedded config block.")
    encoded = "".join(line for line in block.group(0).splitlines() if re.fullmatch(r"[A-Za-z0-9+/=]+", line))
    if not encoded:
        raise ReleaseError("The installer's embedded config is empty: build it with scripts/build-installer.py.")
    return archive_members(base64.b64decode(encoded))


def check_contents(image_files: list[str], chart: bytes, installer: str) -> dict[str, int]:
    """Refuse an image, a chart or an installer that holds an instruction file or a test, or names one."""
    if not any(path.strip().lstrip("/") == "etc/struct-otel-collector/config.yaml" for path in image_files):
        raise ReleaseError("The image's file list has no etc/struct-otel-collector/config.yaml: it is not a listing of the built image.")
    problems = [f"the image has {path}" for path in internal_paths(image_files)]
    chart_files = archive_members(chart)
    embedded = installer_members(installer)
    for what, files in (("the chart", chart_files), ("the installer", embedded)):
        problems += [f"{what} has {path}" for path in internal_paths(files)]
        for path, data in files.items():
            problems += [f"{what}: {problem}" for problem in internal_words(path, data)]
    problems += [f"the installer: {problem}" for problem in internal_words("its script", EMBEDDED_CONFIG.sub("", installer).encode())]
    if problems:
        raise ReleaseError("Nothing internal may be published, but " + "; ".join(problems) + ".")
    return {"image files": len(image_files), "chart files": len(chart_files), "installer config files": len(embedded)}


# ---------------------------------------------------------------------------
# What a customer can pull, without signing in
# ---------------------------------------------------------------------------

def http_get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url)
    for name, value in headers.items():
        if name == "Authorization":
            # Registries answer blobs with a redirect to a storage host, which must not get the token.
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def anonymous_get(url: str, accept: str, fetch=http_get) -> bytes:
    """GET as someone who never signed in: answer the registry's Bearer challenge with an anonymous token."""
    status, headers, body = fetch(url, {"Accept": accept})
    if status == 401:
        challenge = headers.get("www-authenticate", "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer ") or "realm" not in params:
            raise ReleaseError(f"{url} asks for credentials this check cannot answer.")
        query = urllib.parse.urlencode({key: params[key] for key in ("service", "scope") if key in params})
        token_status, _, token_body = fetch(f"{params['realm']}?{query}", {})
        if token_status != 200:
            raise ReleaseError(
                f"{url} cannot be pulled without signing in (the registry refused an anonymous token with HTTP {token_status}). "
                "A package GitHub creates is private, and so is a Docker Hub repository whose organization makes new ones private: "
                "on the first release, an owner makes the image public there, then re-runs this job."
            )
        token = json.loads(token_body)
        status, headers, body = fetch(url, {"Accept": accept, "Authorization": f"Bearer {token.get('token') or token.get('access_token')}"})
    if status != 200:
        raise ReleaseError(f"GET {url} answered HTTP {status}.")
    return body


# Docker Hub's name is docker.io; its registry API answers at registry-1.docker.io.
REGISTRY_HOSTS = {"docker.io": "registry-1.docker.io"}


def _endpoint(reference: str, scheme: str) -> tuple[str, str]:
    host, _, name = reference.partition("/")
    return f"{scheme}://{REGISTRY_HOSTS.get(host, host)}/v2/{name}", name


def check_public(version: str, images: tuple[str, ...] = IMAGES, get=anonymous_get, scheme: str = "https") -> None:
    """Each published name of the image pulls without signing in, with exactly the two platforms, and
    all of them are the same image."""
    digests = {}
    for image in images:
        base, _ = _endpoint(image, scheme)
        body = get(f"{base}/manifests/{version}", INDEX_TYPES)
        index = json.loads(body)
        platforms = sorted(
            f"{entry.get('platform', {}).get('os')}/{entry.get('platform', {}).get('architecture')}"
            for entry in index.get("manifests", [])
        )
        if platforms != sorted(PLATFORMS):
            raise ReleaseError(f"{image}:{version} has platforms {platforms}, expected {sorted(PLATFORMS)}.")
        digests[image] = sorted(entry.get("digest") for entry in index.get("manifests", []))
    if len({json.dumps(found) for found in digests.values()}) != 1:
        raise ReleaseError(f"The image's names do not hold the same image: {digests}.")


def chart_archive_name(version: str) -> str:
    return f"{CHART_NAME}-{version}.tgz"


def chart_files(archive: bytes) -> tuple[str, str]:
    """A packaged chart's Chart.yaml and values.yaml."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        def read(name: str) -> str:
            member = tar.extractfile(f"{CHART_NAME}/{name}")
            if member is None:
                raise ReleaseError(f"The chart package has no {name}.")
            return member.read().decode()
        return read("Chart.yaml"), read("values.yaml")


def chart_index(pages: Path, base: str = CHART_REPOSITORY) -> str:
    """The chart repository's index.yaml, from every chart package in the gh-pages checkout `pages`.

    Written as JSON, which is YAML: Helm reads it as any index.yaml, and this needs no YAML library.
    Built from the packages themselves, so writing it again after a failed run gives the same file.
    """
    versions = []
    for archive_path in sorted(pages.glob(f"{CHART_NAME}-*.tgz")):
        archive = archive_path.read_bytes()
        chart_yaml, _ = chart_files(archive)
        entry = {
            "apiVersion": yaml_scalar(chart_yaml, "apiVersion"),
            "name": yaml_scalar(chart_yaml, "name"),
            "version": yaml_scalar(chart_yaml, "version"),
            "appVersion": yaml_scalar(chart_yaml, "appVersion"),
            "description": yaml_scalar(chart_yaml, "description"),
            "type": yaml_scalar(chart_yaml, "type") or "application",
            "digest": hashlib.sha256(archive).hexdigest(),
            "urls": [f"{base}/{archive_path.name}"],
        }
        if entry["name"] != CHART_NAME or archive_path.name != chart_archive_name(entry["version"] or ""):
            raise ReleaseError(f"{archive_path.name} is not a {CHART_NAME} chart package named for its version.")
        versions.append({key: value for key, value in entry.items() if value is not None})
    versions.sort(key=lambda entry: version_key(entry["version"]), reverse=True)
    return json.dumps({"apiVersion": "v1", "entries": {CHART_NAME: versions}}, indent=2) + "\n"


def check_chart(version: str, base: str = CHART_REPOSITORY, fetch=http_get, sleep=time.sleep, attempts: int = 20) -> str:
    """Anyone can add the chart repository and get this version's chart, and it deploys this
    release's image. GitHub Pages takes a minute or so to serve a push, so the index is read again
    until it lists the version."""
    for attempt in range(attempts):
        try:
            status, _, body = fetch(f"{base}/index.yaml", {})
            answer = f"HTTP {status}"
        except OSError as error:  # Pages not answering yet is waited out too
            status, body, answer = None, b"", f"{type(error).__name__}: {error}"
        if status == 200:
            entries = json.loads(body).get("entries", {}).get(CHART_NAME, [])
            matching = [entry for entry in entries if entry.get("version") == version]
            if matching:
                break
        if attempt == attempts - 1:
            raise ReleaseError(f"{base}/index.yaml does not list {CHART_NAME} {version} (last answer {answer}).")
        sleep(30)
    (entry,) = matching
    url = f"{base}/{chart_archive_name(version)}"
    if entry.get("urls") != [url]:
        raise ReleaseError(f"The index points {CHART_NAME} {version} at {entry.get('urls')}, expected [{url!r}].")
    status, _, archive = fetch(url, {})
    if status != 200:
        raise ReleaseError(f"GET {url} answered HTTP {status}.")
    if hashlib.sha256(archive).hexdigest() != entry.get("digest"):
        raise ReleaseError(f"{url}: the chart's bytes do not match the index's digest.")
    chart_yaml, values = chart_files(archive)
    found = {
        "version": yaml_scalar(chart_yaml, "version"),
        "appVersion": yaml_scalar(chart_yaml, "appVersion"),
        "image.repository": yaml_scalar(values, "repository", parent="image"),
    }
    expected = {"version": version, "appVersion": version, "image.repository": IMAGE}
    if found != expected:
        raise ReleaseError(f"{url} says {found}, expected {expected}.")
    return entry["digest"]


def installer_url(version: str, name: str = INSTALLER, base: str = f"https://github.com/{REPOSITORY}") -> str:
    return f"{base}/releases/download/v{version}/{name}"


def check_installer(version: str, built: bytes, base: str = f"https://github.com/{REPOSITORY}", fetch=http_get) -> str:
    """Anyone can download the released installer and its checksum, and both are what this run built."""
    digest = hashlib.sha256(built).hexdigest()
    for name in (INSTALLER, f"{INSTALLER}.sha256"):
        url = installer_url(version, name, base)
        status, _, body = fetch(url, {})
        if status != 200:
            raise ReleaseError(f"GET {url} answered HTTP {status}: the release's installer cannot be downloaded without signing in.")
        if name == INSTALLER and body != built:
            raise ReleaseError(f"{url} is not the installer this run built.")
        if name != INSTALLER and body.decode().split()[:1] != [digest]:
            raise ReleaseError(f"{url} does not hold the installer's SHA-256 {digest}.")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("version", "plan", "check-contents", "check-public", "chart-index", "check-chart", "check-installer"))
    parser.add_argument("--pages", type=Path, help="chart-index: the checkout of the gh-pages branch, which holds the chart packages")
    parser.add_argument("--base", default=CHART_REPOSITORY, help="chart-index and check-chart: the chart repository's address (a rehearsal's local server)")
    parser.add_argument("--image-files", type=Path, help="check-contents: a file listing every path in the built image, one a line")
    parser.add_argument("--chart", type=Path, help="check-contents: the packaged chart (.tgz)")
    parser.add_argument("--installer", type=Path, help="check-contents and check-installer: the built single-file installer")
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(check_package(read_package())[0])
        elif args.command == "plan":
            request = plan(dict(os.environ))
            print(json.dumps(request, indent=2))
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a") as file:
                    file.writelines(f"{key}={value}\n" for key, value in request.items())
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a") as file:
                    file.write(
                        f"## Struct Collector release\n\n{request['version']} (upstream {request['upstream']}) from "
                        f"`{request['source_sha']}`. After the tests, publishing waits for a reviewer.\n"
                    )
        elif args.command == "check-contents":
            if not (args.image_files and args.chart and args.installer):
                raise ReleaseError("check-contents needs --image-files, --chart and --installer.")
            counted = check_contents(args.image_files.read_text().splitlines(), args.chart.read_bytes(), args.installer.read_text())
            print("Nothing internal: " + ", ".join(f"{count} {what}" for what, count in counted.items()) + ".")
        elif args.command == "check-public":
            version = check_package(read_package())[0]
            check_public(version)
            print(f"Anyone can pull {' and '.join(f'{image}:{version}' for image in IMAGES)}, the same image, for {', '.join(PLATFORMS)}.")
        elif args.command == "chart-index":
            if not args.pages:
                raise ReleaseError("chart-index needs --pages.")
            (args.pages / "index.yaml").write_text(chart_index(args.pages, args.base))
            print(f"Wrote {args.pages / 'index.yaml'}.")
        elif args.command == "check-chart":
            version = check_package(read_package())[0]
            digest = check_chart(version, args.base)
            print(f"Anyone can `helm repo add` {args.base} and get {CHART_NAME} {version} (SHA-256 {digest}), which deploys {IMAGE}:{version}.")
        else:
            if not args.installer:
                raise ReleaseError("check-installer needs --installer.")
            version = check_package(read_package())[0]
            digest = check_installer(version, args.installer.read_bytes())
            print(f"Anyone can download {installer_url(version)} (SHA-256 {digest}).")
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, ReleaseError) else f"Release check failed ({type(error).__name__}: {error}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
