#!/usr/bin/env python3
"""Build the single-file Linux installer: install/install.sh with the config embedded.

install/install.sh needs the config files. From a checkout it takes them with
--config-dir. The one-command install has no checkout, so this writes a copy of
the script whose `embedded_config` function prints config/ as a base64 tar.gz.
The archive is deterministic (sorted, no timestamps, no owners), so the same
config always produces the same installer.

    python3 scripts/build-installer.py                  # writes dist/struct-otel-collector-install.sh
    python3 scripts/build-installer.py --output PATH

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import io
import tarfile
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_DIR / "config"
INSTALL_SCRIPT = PACKAGE_DIR / "install" / "install.sh"
DEFAULT_OUTPUT = PACKAGE_DIR / "dist" / "struct-otel-collector-install.sh"
BEGIN = "# BEGIN EMBEDDED CONFIG"
END = "# END EMBEDDED CONFIG"
HEREDOC = "__STRUCT_COLLECTOR_CONFIG__"


def config_archive() -> bytes:
    """config/ as a tar.gz with nothing in it that changes between builds."""
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(CONFIG_DIR.rglob("*.yaml")):
            info = tarfile.TarInfo(path.relative_to(CONFIG_DIR).as_posix())
            data = path.read_bytes()
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as stream:
        stream.write(tar_bytes.getvalue())
    return compressed.getvalue()


def build() -> str:
    source = INSTALL_SCRIPT.read_text()
    before, _, rest = source.partition(BEGIN)
    _, _, after = rest.partition(END)
    if not rest or not after:
        raise SystemExit(f"{INSTALL_SCRIPT} has no {BEGIN} ... {END} block")
    encoded = base64.encodebytes(config_archive()).decode().strip()
    block = "\n".join([
        BEGIN,
        "# Written by scripts/build-installer.py: config/ as a base64 tar.gz.",
        "embedded_config() {",
        f"  cat <<'{HEREDOC}'",
        encoded,
        HEREDOC,
        "}",
        END,
    ])
    return before + block + after


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build())
    args.output.chmod(0o755)
    print(args.output)


if __name__ == "__main__":
    main()
