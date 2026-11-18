"""A Struct Collector release: its rules, its checks and its workflow."""

import base64
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT
CHART_DIR = PACKAGE_DIR / "chart" / "struct-otel-collector"
WORKFLOW = ROOT / ".github/workflows/release.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PUBLISH_SCRIPT = PACKAGE_DIR / "scripts" / "publish.sh"
ENVIRONMENT = "struct-otel-collector-release"
GITHUB_TOKEN = "${{ github.token }}"
DOCKERHUB_TOKEN = "${{ secrets.STRUCT_OTEL_COLLECTOR_DOCKERHUB_TOKEN }}"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def load_script(name):
    """One of the package's scripts, as a module. They are the release's rules, so the tests read the real ones."""
    spec = importlib.util.spec_from_file_location(
        "struct_otel_collector_" + name.replace("-", "_").removesuffix(".py"), PACKAGE_DIR / "scripts" / name
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package = load_script("release.py")
installer = load_script("build-installer.py")


def load_workflow(path):
    # BaseLoader keeps `on` a key and every value a string, as GitHub reads them.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def copy_package(target):
    shutil.copytree(
        PACKAGE_DIR,
        target,
        ignore=shutil.ignore_patterns(".git", ".venv", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"),
    )


def set_chart_version(package_dir, version, app_version=None):
    path = package_dir / "chart" / "struct-otel-collector" / "Chart.yaml"
    text = re.sub(r"(?m)^version: .*$", f"version: {version}", path.read_text())
    text = re.sub(r'(?m)^appVersion: .*$', f'appVersion: "{app_version or version}"', text)
    path.write_text(text)


def run_git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_IDENTITY},
    ).stdout.decode().strip()


def tar_gz(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return data.getvalue()


def chart_package(extra=None):
    """The chart directory laid out as `helm package` packs it, with any extra files."""
    files = {
        f"struct-otel-collector/{path.relative_to(CHART_DIR).as_posix()}": path.read_bytes()
        for path in sorted(CHART_DIR.rglob("*"))
        if path.is_file()
    }
    return tar_gz({**files, **(extra or {})})


def installer_with(script, extra):
    """The built installer, with more files in the config archive it carries."""
    block = package.EMBEDDED_CONFIG.search(script).group(0)
    encoded = base64.encodebytes(tar_gz({**package.installer_members(script), **extra})).decode().strip()
    lines = [line for line in block.splitlines() if not re.fullmatch(r"[A-Za-z0-9+/=]+", line)]
    lines.insert(lines.index(installer.HEREDOC), encoded)
    return script.replace(block, "\n".join(lines))


class PackageVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.package = Path(temp.name) / "struct-otel-collector"
        copy_package(self.package)
        set_chart_version(self.package, "0.165.0-1")

    def test_the_committed_package_names_one_release_and_the_reader_agrees_with_yaml(self):
        found = package.read_package(PACKAGE_DIR)
        version, upstream = package.check_package(found)
        chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text())
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        self.assertEqual(found["Chart.yaml version"], str(chart["version"]))
        self.assertEqual(found["Chart.yaml appVersion"], str(chart["appVersion"]))
        self.assertEqual(found["Chart.yaml name"], chart["name"])
        self.assertEqual(found["values.yaml image.repository"], values["image"]["repository"])
        self.assertEqual(values["image"]["repository"], package.IMAGE)
        self.assertTrue(version.startswith(f"{upstream}-"))

    def test_every_place_that_names_the_version_must_agree(self):
        def edit(name, pattern, replacement):
            path = self.package / name
            original = path.read_text()
            path.write_text(re.sub(pattern, replacement, original, count=1, flags=re.MULTILINE))
            return lambda: path.write_text(original)

        self.assertEqual(package.check_package(package.read_package(self.package)), ("0.165.0-1", "0.165.0"))
        chart = "chart/struct-otel-collector/Chart.yaml"
        for name, pattern, replacement in (
            (chart, r"^appVersion: .*$", 'appVersion: "0.165.0"'),
            (chart, r"^appVersion: .*$", 'appVersion: "0.165.0-2"'),
            (chart, r"^version: .*$", "version: 0.165.0"),
            (chart, r"^version: .*$", "version: 0.165.0-0"),
            (chart, r"^version: .*$", "version: 0.165.0-01"),
            (chart, r"^version: .*$", "version: v0.165.0-1"),
            (chart, r"^version: .*$", "version: 0.165.0-1-rc1"),
            (chart, r"^version: .*$", "version: 0.165-1"),
            (chart, r"^version: .*$", "version: 00.165.0-1"),
            (chart, r"^name: .*$", "name: another-chart"),
            ("Dockerfile", r"^FROM .*$", "FROM otel/opentelemetry-collector-contrib:0.166.0"),
            ("install/install.sh", r'^UPSTREAM_VERSION=".*"$', 'UPSTREAM_VERSION="0.166.0"'),
            ("chart/struct-otel-collector/values.yaml", r"^  repository: .*$", "  repository: struct-otel-collector"),
        ):
            restore = edit(name, pattern, replacement)
            with self.subTest(file=name, change=replacement), self.assertRaises(package.ReleaseError):
                package.check_package(package.read_package(self.package))
            restore()
        # The next revision moves version and appVersion together.
        set_chart_version(self.package, "0.165.0-12")
        self.assertEqual(package.check_package(package.read_package(self.package)), ("0.165.0-12", "0.165.0"))

    def test_the_reader_handles_quotes_comments_and_nested_keys_like_yaml(self):
        text = (
            "# header\nname: demo  # trailing\nversion: '1.2.3-4'\nappVersion: \"1.2.3-4\"\n"
            "image:\n  # The image.\n  repository: ghcr.io/acme/collector  # where\n\n  tag: \"\"\n"
            "imagePullSecrets: []\nother:\n  repository: not-this-one\n"
        )
        parsed = yaml.safe_load(text)
        for key in ("name", "version", "appVersion"):
            self.assertEqual(package.yaml_scalar(text, key), parsed[key])
        self.assertEqual(package.yaml_scalar(text, "repository", parent="image"), parsed["image"]["repository"])
        self.assertEqual(package.yaml_scalar(text, "tag", parent="image"), "")
        self.assertIsNone(package.yaml_scalar(text, "repository"))
        self.assertIsNone(package.yaml_scalar(text, "repository", parent="missing"))


class PlanTests(unittest.TestCase):
    """The plan job: a tag v<version> for the committed version, on main, and only a new, newest version."""

    TAG = "v0.165.0-2"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "struct-otel-collector"
        copy_package(self.root)
        set_chart_version(self.root, "0.165.0-2")
        run_git(self.root, "init", "-q")
        run_git(self.root, "add", "-A")
        run_git(self.root, "commit", "-q", "-m", "package")
        self.sha = run_git(self.root, "rev-parse", "HEAD")
        run_git(self.root, "update-ref", "refs/remotes/origin/main", self.sha)
        self.env = {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REPOSITORY": package.REPOSITORY,
            "GITHUB_REF": f"refs/tags/{self.TAG}",
            "GITHUB_SHA": self.sha,
        }

    def plan(self, releases=(), **changes):
        return package.plan({**self.env, **changes}, self.root, self.root, releases=lambda: list(releases))

    def test_only_a_tag_for_the_committed_version_on_main_plans_a_release(self):
        self.assertEqual(package.REPOSITORY, "struct-dot-ai/struct-otel-collector")
        self.assertEqual(self.plan(), {"tag": self.TAG, "version": "0.165.0-2", "upstream": "0.165.0", "source_sha": self.sha})
        for changes in (
            {"GITHUB_EVENT_NAME": "workflow_dispatch"},
            {"GITHUB_EVENT_NAME": "release"},
            {"GITHUB_REPOSITORY": "someone/struct-otel-collector"},
            {"GITHUB_REF": "refs/heads/main"},
            {"GITHUB_REF": "refs/tags/v0.165.0-1"},
            {"GITHUB_REF": "refs/tags/v0.165.0-3"},
            {"GITHUB_REF": "refs/tags/v0.165.0"},
            {"GITHUB_REF": "refs/tags/0.165.0-2"},
            {"GITHUB_REF": "refs/tags/struct-otel-collector-v0.165.0-2"},
            {"GITHUB_REF": "refs/tags/v0.165.0-2\nversion=9.9.9-9"},
            {"GITHUB_SHA": "b" * 40},
        ):
            with self.subTest(changes=changes), self.assertRaises(package.ReleaseError):
                self.plan(**changes)
        # A commit that is not on main is refused.
        (self.root / "branch-only").write_text("x")
        run_git(self.root, "add", "branch-only")
        run_git(self.root, "commit", "-q", "-m", "off main")
        with self.assertRaisesRegex(package.ReleaseError, "on main"):
            self.plan(GITHUB_SHA=run_git(self.root, "rev-parse", "HEAD"))

    def test_a_released_version_is_refused_and_a_release_must_be_the_newest(self):
        with self.assertRaisesRegex(package.ReleaseError, "already released"):
            self.plan(["v0.165.0-1", "v0.165.0-2"])
        for newer in ("v0.165.0-3", "v0.166.0-1", "v1.0.0-1"):
            with self.subTest(newer=newer), self.assertRaisesRegex(package.ReleaseError, "must be newer"):
                self.plan(["v0.165.0-1", newer])
        # Older releases, and tags that are not releases of this scheme, are no obstacle.
        self.assertEqual(self.plan(["v0.164.0-9", "v0.165.0-1", "notes", "v2026-11-17.1", "0.165.0-2"])["version"], "0.165.0-2")
        # Revisions compare as numbers, not as text.
        set_chart_version(self.root, "0.165.0-10")
        self.assertEqual(self.plan(["v0.165.0-9"], GITHUB_REF="refs/tags/v0.165.0-10")["version"], "0.165.0-10")
        set_chart_version(self.root, "0.165.0-9")
        with self.assertRaisesRegex(package.ReleaseError, "must be newer"):
            self.plan(["v0.165.0-10"], GITHUB_REF="refs/tags/v0.165.0-9")

    def test_an_unreadable_release_list_refuses_to_guess(self):
        with patch.object(package.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 404")) as run:
            with self.assertRaisesRegex(package.ReleaseError, "refusing to guess"):
                package.plan(self.env, self.root, self.root, head=lambda root: self.sha, is_on_main=lambda root, sha: True)
        self.assertIn(f"repos/{package.REPOSITORY}/releases?per_page=100", run.call_args.args[0])


class PublicCheckTests(unittest.TestCase):
    """After pushing, the image is checked under each of its names as a customer pulls it: without signing in."""

    VERSION = "0.165.0-2"

    def setUp(self):
        self.calls = []
        self.index = {
            "manifests": [
                {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
                {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            ]
        }
        self.indexes = {}
        self.private = set()

    def fetch(self, url, headers):
        """Two registries that challenge every anonymous request, as Docker Hub and GHCR do."""
        self.calls.append((url, dict(headers)))
        for realm in ("https://auth.docker.io/token", "https://ghcr.io/token"):
            if url.startswith(realm + "?"):
                name = re.search(r"scope=repository%3A(.+)%3Apull", url).group(1).replace("%2F", "/")
                if "Authorization" in headers:
                    raise AssertionError("the token request must be anonymous")
                return (403, {}, b"") if name in self.private else (200, {}, b'{"token": "anonymous"}')
        host = url.split("/")[2]
        if headers.get("Authorization") != "Bearer anonymous":
            name = url.split("/v2/", 1)[1].rsplit("/", 2)[0]
            realm = "https://auth.docker.io/token" if host == "registry-1.docker.io" else "https://ghcr.io/token"
            challenge = f'Bearer realm="{realm}",service="{host}",scope="repository:{name}:pull"'
            return 401, {"www-authenticate": challenge}, b""
        for image in package.IMAGES:
            base, _ = package._endpoint(image, "https")
            if url == f"{base}/manifests/{self.VERSION}":
                return 200, {}, json.dumps(self.indexes.get(image, self.index)).encode()
        return 404, {}, b""

    def check(self):
        package.check_public(self.VERSION, get=lambda url, accept: package.anonymous_get(url, accept, fetch=self.fetch))

    def test_the_image_is_published_under_both_names_with_both_platforms(self):
        self.assertEqual(package.IMAGES, ("docker.io/structai/struct-otel-collector", "ghcr.io/struct-dot-ai/struct-otel-collector"))
        self.assertEqual(package.IMAGE, package.IMAGES[0])
        self.check()
        asked = {url for url, _ in self.calls if "/manifests/" in url}
        self.assertEqual(asked, {
            f"https://registry-1.docker.io/v2/structai/struct-otel-collector/manifests/{self.VERSION}",
            f"https://ghcr.io/v2/struct-dot-ai/struct-otel-collector/manifests/{self.VERSION}",
        })
        self.assertTrue(any(url.startswith("https://auth.docker.io/token?") for url, _ in self.calls))
        self.assertTrue(any(url.startswith("https://ghcr.io/token?") for url, _ in self.calls))

    def test_a_private_name_two_different_images_or_other_platforms_are_refused(self):
        for private in ("structai/struct-otel-collector", "struct-dot-ai/struct-otel-collector"):
            self.private = {private}
            with self.subTest(private=private), self.assertRaisesRegex(package.ReleaseError, "cannot be pulled without signing in.*makes the image public"):
                self.check()
        self.private = set()
        other = {"manifests": [dict(entry, digest="sha256:" + "9" * 64) for entry in self.index["manifests"]]}
        self.indexes = {package.IMAGES[1]: other}
        with self.assertRaisesRegex(package.ReleaseError, "do not hold the same image"):
            self.check()
        self.indexes = {}
        good = self.index
        for manifests in (
            good["manifests"][:1],
            good["manifests"] + [{"platform": {"os": "unknown", "architecture": "unknown"}}],
            good["manifests"] + [good["manifests"][1]],
            [{"digest": "sha256:" + "3" * 64}],
        ):
            self.index = {"manifests": manifests}
            with self.subTest(manifests=manifests), self.assertRaisesRegex(package.ReleaseError, "platforms"):
                self.check()

    def test_the_registry_token_is_never_sent_on_to_a_redirect(self):
        captured = []

        def urlopen(request, timeout):
            captured.append(request)
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, io.BytesIO())

        with patch.object(urllib.request, "urlopen", urlopen):
            self.assertEqual(package.http_get("https://ghcr.io/v2/x/blobs/y", {"Authorization": "Bearer t", "Accept": "*/*"})[0], 404)
        self.assertEqual(captured[0].unredirected_hdrs, {"Authorization": "Bearer t"})
        self.assertNotIn("Authorization", captured[0].headers)


class ChartRepositoryTests(unittest.TestCase):
    """The chart repository is the gh-pages branch, served by GitHub Pages; customers `helm repo add` it."""

    VERSION = "0.165.0-2"

    def package_for(self, version, repository=None):
        chart = (CHART_DIR / "Chart.yaml").read_text()
        chart = re.sub(r"(?m)^version: .*$", f"version: {version}", chart)
        chart = re.sub(r"(?m)^appVersion: .*$", f'appVersion: "{version}"', chart)
        values = (CHART_DIR / "values.yaml").read_text()
        if repository:
            values = re.sub(r"(?m)^  repository: .*$", f"  repository: {repository}", values)
        return tar_gz({"struct-otel-collector/Chart.yaml": chart.encode(), "struct-otel-collector/values.yaml": values.encode()})

    def pages(self, versions, **overrides):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        for version in versions:
            (directory / f"struct-otel-collector-{version}.tgz").write_bytes(overrides.get(version) or self.package_for(version))
        return directory

    def served(self, directory, index=None):
        files = {f"{package.CHART_REPOSITORY}/{path.name}": path.read_bytes() for path in directory.iterdir()}
        files[f"{package.CHART_REPOSITORY}/index.yaml"] = (index if index is not None else package.chart_index(directory)).encode()
        return lambda url, headers: (200, {}, files[url]) if url in files else (404, {}, b"")

    def test_the_index_lists_every_package_on_the_branch_newest_first_and_is_the_same_when_written_again(self):
        directory = self.pages(["0.161.0-1", self.VERSION, "0.165.0-1"])
        index = package.chart_index(directory)
        self.assertEqual(index, package.chart_index(directory))
        loaded = yaml.safe_load(index)  # Helm reads index.yaml as YAML; JSON is YAML.
        self.assertEqual(loaded["apiVersion"], "v1")
        entries = loaded["entries"]["struct-otel-collector"]
        self.assertEqual([entry["version"] for entry in entries], [self.VERSION, "0.165.0-1", "0.161.0-1"])
        first = entries[0]
        self.assertEqual(first["urls"], [f"{package.CHART_REPOSITORY}/struct-otel-collector-{self.VERSION}.tgz"])
        self.assertEqual(first["digest"], hashlib.sha256((directory / f"struct-otel-collector-{self.VERSION}.tgz").read_bytes()).hexdigest())
        self.assertEqual((first["name"], first["appVersion"], first["apiVersion"]), ("struct-otel-collector", self.VERSION, "v2"))

    def test_a_package_not_named_for_its_version_is_refused(self):
        directory = self.pages([self.VERSION], **{self.VERSION: self.package_for("0.165.0-3")})
        with self.assertRaisesRegex(package.ReleaseError, "named for its version"):
            package.chart_index(directory)

    def test_the_published_chart_is_listed_matches_its_digest_and_deploys_the_docker_hub_image(self):
        directory = self.pages(["0.165.0-1", self.VERSION])
        digest = package.check_chart(self.VERSION, fetch=self.served(directory), sleep=lambda seconds: None)
        self.assertEqual(digest, hashlib.sha256((directory / f"struct-otel-collector-{self.VERSION}.tgz").read_bytes()).hexdigest())

    def test_an_unlisted_tampered_or_wrong_chart_is_refused(self):
        waits = []
        directory = self.pages(["0.165.0-1"])
        with self.assertRaisesRegex(package.ReleaseError, "does not list"):
            package.check_chart(self.VERSION, fetch=self.served(directory), sleep=waits.append, attempts=3)
        self.assertEqual(waits, [30, 30])
        # A connection Pages refuses, then an answer: waited out, not a failed release.
        directory = self.pages([self.VERSION])
        served, tries = self.served(directory), []

        def flaky(url, headers):
            tries.append(url)
            if len(tries) == 1:
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            return served(url, headers)

        package.check_chart(self.VERSION, fetch=flaky, sleep=lambda seconds: None)
        with self.assertRaisesRegex(package.ReleaseError, "last answer URLError"):
            package.check_chart(self.VERSION, fetch=lambda url, headers: flaky.__call__(url, headers) if False else (_ for _ in ()).throw(urllib.error.URLError("down")), sleep=lambda seconds: None, attempts=2)
        directory = self.pages([self.VERSION])
        index = package.chart_index(directory)
        (directory / f"struct-otel-collector-{self.VERSION}.tgz").write_bytes(self.package_for(self.VERSION) + b"x")
        with self.assertRaisesRegex(package.ReleaseError, "do not match"):
            package.check_chart(self.VERSION, fetch=self.served(directory, index), sleep=lambda seconds: None)
        for repository in ("ghcr.io/struct-dot-ai/struct-otel-collector", "struct-otel-collector"):
            directory = self.pages([self.VERSION], **{self.VERSION: self.package_for(self.VERSION, repository)})
            with self.subTest(repository=repository), self.assertRaisesRegex(package.ReleaseError, "expected"):
                package.check_chart(self.VERSION, fetch=self.served(directory), sleep=lambda seconds: None)
        directory = self.pages([self.VERSION])
        moved = package.chart_index(directory).replace(package.CHART_REPOSITORY, "https://example.com/charts")
        with self.assertRaisesRegex(package.ReleaseError, "points"):
            package.check_chart(self.VERSION, fetch=self.served(directory, moved), sleep=lambda seconds: None)

    def test_the_committed_chart_deploys_the_docker_hub_image(self):
        self.assertEqual(package.yaml_scalar((CHART_DIR / "values.yaml").read_text(), "repository", parent="image"), package.IMAGE)


class ContentsTests(unittest.TestCase):
    """Before anything is pushed: no instruction file, no test and no tests directory is in the image, the chart or the installer."""

    INTERNAL = (
        "AGENTS.md",
        "CLAUDE.md",
        "etc/struct-otel-collector/AGENTS.md",
        "etc/struct-otel-collector/presets/CLAUDE.md",
        "tests/",
        "tests/harness.py",
        "./tests/test_configs.py",
        "etc/struct-otel-collector/tests/fixture.yaml",
        "etc/struct-otel-collector/test_configs.py",
        "etc/struct-otel-collector/manual_presets_live.py",
        "etc/struct-otel-collector/conftest.py",
        "etc/struct-otel-collector/release_test.py",
        "etc/struct-otel-collector/__pycache__/release.cpython-312.pyc",
    )

    def setUp(self):
        self.image_files = [
            "etc/",
            "etc/struct-otel-collector/",
            "etc/struct-otel-collector/config.yaml",
            "etc/struct-otel-collector/presets/hosts.yaml",
            "otelcol-contrib",
            "var/lib/struct-otel-collector/.keep",
        ]
        self.chart = chart_package()
        self.installer = installer.build()

    def test_the_committed_chart_and_installer_hold_nothing_internal(self):
        counted = package.check_contents(self.image_files, self.chart, self.installer)
        configs = [path for path in (PACKAGE_DIR / "config").rglob("*.yaml")]
        self.assertEqual(counted["installer config files"], len(configs))
        self.assertGreater(len(configs), 10)
        self.assertEqual(counted["chart files"], sum(1 for path in CHART_DIR.rglob("*") if path.is_file()))
        # Names that only look alike are not held back.
        self.assertEqual(
            package.internal_paths(["etc/latest/config.yaml", "etc/attests/x.yaml", "etc/contest.py", "AGENTS.md.txt", "testsuite/x", "etc/testing.yaml"]),
            [],
        )

    def test_an_instruction_file_or_a_test_is_refused_in_each_of_the_three(self):
        for path in self.INTERNAL:
            with self.subTest(where="image", path=path), self.assertRaisesRegex(package.ReleaseError, "the image has"):
                package.check_contents([*self.image_files, path], self.chart, self.installer)
            if path.endswith("/"):
                continue
            member = path.removeprefix("./")
            with self.subTest(where="chart", path=path), self.assertRaisesRegex(package.ReleaseError, "the chart has"):
                package.check_contents(self.image_files, chart_package({f"struct-otel-collector/{member}": b"x"}), self.installer)
            with self.subTest(where="installer", path=path), self.assertRaisesRegex(package.ReleaseError, "the installer has"):
                package.check_contents(self.image_files, self.chart, installer_with(self.installer, {member: b"x"}))

    def test_a_published_file_that_names_one_is_refused(self):
        for word in package.INTERNAL_WORDS:
            note = f"# see {word.upper()} for how this works\n".encode()
            with self.subTest(where="chart", word=word), self.assertRaisesRegex(package.ReleaseError, f"the chart: .* names {re.escape(word)}"):
                package.check_contents(self.image_files, chart_package({"struct-otel-collector/templates/note.yaml": note}), self.installer)
            with self.subTest(where="installer config", word=word), self.assertRaisesRegex(package.ReleaseError, f"the installer: .* names {re.escape(word)}"):
                package.check_contents(self.image_files, self.chart, installer_with(self.installer, {"presets/note.yaml": note}))
            with self.subTest(where="installer script", word=word), self.assertRaisesRegex(package.ReleaseError, f"the installer: its script names {re.escape(word)}"):
                package.check_contents(self.image_files, self.chart, self.installer + note.decode())

    def test_only_the_built_things_are_accepted(self):
        with self.assertRaisesRegex(package.ReleaseError, "not a listing of the built image"):
            package.check_contents(["otelcol-contrib"], self.chart, self.installer)
        # install/install.sh from a checkout carries no config: it is not what a release publishes.
        with self.assertRaisesRegex(package.ReleaseError, "embedded config is empty"):
            package.check_contents(self.image_files, self.chart, (PACKAGE_DIR / "install" / "install.sh").read_text())
        with self.assertRaisesRegex(package.ReleaseError, "no embedded config block"):
            package.check_contents(self.image_files, self.chart, "#!/usr/bin/env bash\n")


class InstallerCheckTests(unittest.TestCase):
    """After the release: the installer anyone downloads is the one this run built, with its checksum."""

    VERSION = "0.165.0-2"

    def setUp(self):
        self.built = b"#!/usr/bin/env bash\necho installer\n"
        self.digest = hashlib.sha256(self.built).hexdigest()
        base = f"https://github.com/struct-dot-ai/struct-otel-collector/releases/download/v{self.VERSION}"
        self.served = {
            f"{base}/struct-otel-collector-install.sh": (200, self.built),
            f"{base}/struct-otel-collector-install.sh.sha256": (200, f"{self.digest}  struct-otel-collector-install.sh\n".encode()),
        }
        self.calls = []

    def fetch(self, url, headers):
        self.calls.append((url, dict(headers)))
        status, body = self.served.get(url, (404, b""))
        return status, {}, body

    def test_the_released_installer_is_the_built_one_and_is_fetched_without_credentials(self):
        self.assertEqual(package.check_installer(self.VERSION, self.built, fetch=self.fetch), self.digest)
        self.assertEqual(self.calls, [(url, {}) for url in self.served])
        self.assertEqual(package.installer_url(self.VERSION), next(iter(self.served)))

    def test_another_installer_a_wrong_checksum_or_a_private_release_is_refused(self):
        script, checksum = self.served
        for url, answer, message in (
            (script, (200, self.built + b"x"), "not the installer this run built"),
            (checksum, (200, b"0" * 64 + b"  struct-otel-collector-install.sh\n"), "does not hold the installer's SHA-256"),
            (checksum, (200, b""), "does not hold the installer's SHA-256"),
            (script, (404, b""), "cannot be downloaded without signing in"),
            (checksum, (404, b""), "cannot be downloaded without signing in"),
        ):
            original = self.served[url]
            self.served[url] = answer
            with self.subTest(url=url, answer=answer[0]), self.assertRaisesRegex(package.ReleaseError, message):
                package.check_installer(self.VERSION, self.built, fetch=self.fetch)
            self.served[url] = original


class RepositoryTests(unittest.TestCase):
    """This repository is public: what it says, and that nothing in it is internal."""

    def tracked_files(self):
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        names = listed.stdout.split() if listed.returncode == 0 else []
        return [ROOT / name for name in names] or [path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]

    def test_no_file_is_an_instruction_file_or_names_one(self):
        for path in self.tracked_files():
            relative = path.relative_to(ROOT).as_posix()
            if relative == "tests/test_release.py" or relative.startswith("tests/test_chart.py") or relative == "scripts/release.py":
                continue  # the checks themselves name what they refuse
            with self.subTest(path=relative):
                self.assertNotIn(path.name, package.INTERNAL_NAMES)
                if path.is_file():
                    self.assertEqual(package.internal_words(relative, path.read_bytes()), [])

    def test_the_readme_names_what_a_release_publishes_and_the_licence_is_apache(self):
        readme = (ROOT / "README.md").read_text()
        for name in (*package.IMAGES, f"helm repo add struct {package.CHART_REPOSITORY}", "struct/struct-otel-collector", package.INSTALLER, f"https://github.com/{package.REPOSITORY}/releases"):
            self.assertIn(name, readme)
        licence = (ROOT / "LICENSE").read_text()
        self.assertIn("Apache License", licence)
        self.assertIn("Version 2.0, January 2004", licence)
        self.assertIn("Apache-2.0", PUBLISH_SCRIPT.read_text())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow(WORKFLOW)
        self.jobs = self.workflow["jobs"]
        self.publish = self.jobs["publish"]["steps"]
        self.runs = [step.get("run", "") for step in self.publish]

    def step(self, command):
        (index,) = [i for i, run in enumerate(self.runs) if command in run]
        return index

    def test_a_version_tag_is_the_only_trigger_and_only_the_approved_job_can_write(self):
        self.assertEqual(self.workflow["on"], {"push": {"tags": [f"{package.TAG_PREFIX}*"]}})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.jobs), {"plan", "test", "publish"})
        self.assertEqual({name: job.get("permissions") for name, job in self.jobs.items()},
                         {"plan": None, "test": None, "publish": {"contents": "write", "packages": "write"}})
        self.assertEqual({name: job.get("environment") for name, job in self.jobs.items()}, {"plan": None, "test": None, "publish": ENVIRONMENT})
        self.assertEqual(self.jobs["test"]["needs"], "plan")
        self.assertEqual(set(self.jobs["publish"]["needs"]), {"plan", "test"})
        plan = self.jobs["plan"]["steps"]
        self.assertEqual(plan[0]["with"], {"fetch-depth": "0", "persist-credentials": "false"})
        self.assertEqual(plan[-1]["run"], "python3 scripts/release.py plan")
        for job in self.jobs.values():
            self.assertTrue(job["steps"][0]["uses"].startswith("actions/checkout@"))
            self.assertEqual(job["steps"][0]["with"]["persist-credentials"], "false")

    def test_every_default_test_file_runs_in_ci_and_before_anything_is_published(self):
        default = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py") if path.name != "test_release.py")
        self.assertEqual(len(default), 4)
        for steps in (self.jobs["test"]["steps"], load_workflow(CI_WORKFLOW)["jobs"]["test"]["steps"]):
            ran = [m for step in steps for m in re.findall(r"^uv run python (tests/\S+)$", step.get("run", ""), re.M)]
            self.assertEqual(sorted(ran), default)
            self.assertTrue([step for step in steps if "tests/test_release.py" in step.get("run", "")])

    def test_the_docker_hub_token_is_the_one_secret_and_tokens_reach_only_the_steps_that_need_them(self):
        text = WORKFLOW.read_text()
        self.assertEqual(re.findall(r"secrets\.(\w+)", text), ["STRUCT_OTEL_COLLECTOR_DOCKERHUB_TOKEN"])
        holders = [step["name"] for step in self.publish if "secrets." in json.dumps(step) or "github.token" in json.dumps(step)]
        self.assertEqual(holders, [
            "Sign in to Docker Hub and GitHub's registry",
            "Publish the chart to the chart repository",
            "Release the installer, last",
        ])
        sign_in = self.publish[self.step("docker login docker.io")]
        self.assertEqual(sign_in["env"], {"DOCKERHUB_USER": "${{ vars.STRUCT_OTEL_COLLECTOR_DOCKERHUB_USER }}", "DOCKERHUB_TOKEN": DOCKERHUB_TOKEN, "GHCR_TOKEN": GITHUB_TOKEN})
        self.assertEqual(sign_in["run"].count("--password-stdin"), 2)
        for name in ("Publish the chart to the chart repository", "Release the installer, last"):
            (step,) = [step for step in self.publish if step.get("name") == name]
            self.assertEqual(step["env"], {"GH_TOKEN": GITHUB_TOKEN})
        # The commands themselves never sign in and never see a registry token.
        script = PUBLISH_SCRIPT.read_text()
        for word in ("login", "DOCKERHUB_TOKEN", "password", "secrets."):
            self.assertNotIn(word, script)

    def test_nothing_is_pushed_before_the_contents_check_and_the_release_comes_last(self):
        order = [
            self.step("scripts/publish.sh build"),
            self.step("scripts/release.py check-contents"),
            self.step("docker login docker.io"),
            self.step("scripts/publish.sh push-image"),
            self.step("docker logout ghcr.io"),
            self.step("scripts/release.py check-public"),
            self.step("scripts/publish.sh push-chart"),
            self.step("scripts/release.py check-chart"),
            self.step("scripts/publish.sh release"),
            self.step("scripts/release.py check-installer"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[-1], len(self.publish) - 1)
        self.assertFalse([step for step in self.publish[: order[1] + 1] if "secrets." in json.dumps(step) or "github.token" in json.dumps(step)])
        self.assertEqual(self.publish[order[4]]["if"], "always()")
        check = self.publish[order[1]]["run"]
        for argument in ("--image-files dist/image-files.txt", '--chart "dist/struct-otel-collector-$VERSION.tgz"', "--installer dist/struct-otel-collector-install.sh"):
            self.assertIn(argument, check)

    def test_the_commands_publish_what_the_rules_name(self):
        script = PUBLISH_SCRIPT.read_text()
        self.assertIn(f'IMAGES="${{IMAGES:-{" ".join(package.IMAGES)}}}"', script)
        self.assertIn(f'REPOSITORY="${{REPOSITORY:-{package.REPOSITORY}}}"', script)
        self.assertIn(f'CHART_REPOSITORY="${{CHART_REPOSITORY:-{package.CHART_REPOSITORY}}}"', script)
        self.assertEqual(package.CHART_REPOSITORY, "https://" + package.REPOSITORY.split("/")[0] + ".github.io/" + package.REPOSITORY.split("/")[1])
        self.assertIn(f'INSTALLER="{package.INSTALLER}"', script)
        self.assertIn("--platform linux/amd64,linux/arm64 --provenance=false --sbom=false", script)
        self.assertEqual(package.PLATFORMS, ("linux/amd64", "linux/arm64"))
        self.assertIn('--label "org.opencontainers.image.source=https://github.com/$REPOSITORY"', script)
        self.assertIn('--branch gh-pages "$PAGES_REMOTE"', script)
        self.assertIn('[[ -f "$pages/$package" ]] || cp', script)
        self.assertEqual(script.count("gh release create"), 1)
        self.assertIn('gh release create "v$VERSION" --repo "$REPOSITORY" --verify-tag --latest', script)
        self.assertEqual(subprocess.run(["bash", "-n", str(PUBLISH_SCRIPT)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
