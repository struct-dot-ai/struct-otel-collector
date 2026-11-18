#!/usr/bin/env bash
# The commands of a Struct Collector release, one step each, as
# .github/workflows/release.yml runs them. scripts/release.py holds the rules and
# the checks; this only builds and pushes.
#
#   scripts/publish.sh build        the image for this machine and its file list, the chart
#                                   package, the installer and its checksum, into $DIST
#   scripts/publish.sh push-image   build for linux/amd64 and linux/arm64, push it as
#                                   <name>:$VERSION for every name in $IMAGES (one digest),
#                                   and validate the pushed digest
#   scripts/publish.sh push-chart   add the chart package to the gh-pages branch of
#                                   $REPOSITORY, write the index, and push (needs GH_TOKEN)
#   scripts/publish.sh release      create release v$VERSION in $REPOSITORY with the installer
#                                   and its checksum (needs GH_TOKEN)
#
# VERSION and UPSTREAM come from `scripts/release.py plan`. This script signs in
# nowhere: the workflow does; git and gh take GH_TOKEN from the environment. LOCAL_REGISTRY=1 is for a rehearsal against a
# registry on this machine that speaks plain HTTP; PAGES_REMOTE, a git remote to
# use in place of the public repository's.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${VERSION:?set VERSION, as scripts/release.py plan prints it}"
: "${UPSTREAM:?set UPSTREAM, as scripts/release.py plan prints it}"
IMAGES="${IMAGES:-docker.io/structai/struct-otel-collector ghcr.io/struct-dot-ai/struct-otel-collector}"
REPOSITORY="${REPOSITORY:-struct-dot-ai/struct-otel-collector}"
CHART_REPOSITORY="${CHART_REPOSITORY:-https://struct-dot-ai.github.io/struct-otel-collector}"
PAGES_REMOTE="${PAGES_REMOTE:-https://github.com/$REPOSITORY.git}"
# The upstream image's own copy on GHCR: the same digest as the Docker Hub name
# the Dockerfile uses, so nothing here pulls from Docker Hub.
UPSTREAM_IMAGE="${UPSTREAM_IMAGE:-ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib}"
DIST="${DIST:-dist}"
INSTALLER="struct-otel-collector-install.sh"
LOCAL_TAG="struct-otel-collector:release-check"
read -r -a DOCKER <<< "${DOCKER:-docker}"
read -r -a HELM <<< "${HELM:-helm}"

build_args=(
  --build-context "otel/opentelemetry-collector-contrib:$UPSTREAM=docker-image://$UPSTREAM_IMAGE:$UPSTREAM"
  --label "org.opencontainers.image.source=https://github.com/$REPOSITORY"
  --label "org.opencontainers.image.version=$VERSION"
  --label "org.opencontainers.image.licenses=Apache-2.0"
)

validate() {
  "${DOCKER[@]}" run --rm -e STRUCT_INGEST_KEY=pk-validate "$1" validate \
    --config=/etc/struct-otel-collector/config.yaml \
    --config=/etc/struct-otel-collector/presets/hosts.yaml
}

case "${1:-}" in
  build)
    mkdir -p "$DIST"
    "${DOCKER[@]}" buildx build --load --tag "$LOCAL_TAG" "${build_args[@]}" .
    validate "$LOCAL_TAG"
    container="$("${DOCKER[@]}" create "$LOCAL_TAG")"
    "${DOCKER[@]}" export "$container" | tar -t > "$DIST/image-files.txt"
    "${DOCKER[@]}" rm "$container" > /dev/null
    "${HELM[@]}" package chart/struct-otel-collector --destination "$DIST"
    python3 scripts/build-installer.py --output "$DIST/$INSTALLER"
    (cd "$DIST" && sha256sum "$INSTALLER" > "$INSTALLER.sha256")
    ;;
  push-image)
    read -r -a names <<< "$IMAGES"
    tags="$(printf '%s:'"$VERSION"',' "${names[@]}")"
    output="type=image,\"name=${tags%,}\",push=true"
    [[ -z "${LOCAL_REGISTRY:-}" ]] || output+=",registry.insecure=true"
    # No buildx provenance or SBOM entry: each would add an unknown/unknown
    # platform to the index, and scripts/release.py check-public wants exactly two.
    "${DOCKER[@]}" buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false \
      "${build_args[@]}" --output "$output" --metadata-file "$DIST/image.json" .
    digest="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$DIST/image.json")"
    [[ "$digest" == sha256:* ]] || { echo "::error::the image push reported no digest" >&2; exit 1; }
    for name in "${names[@]}"; do
      validate "$name@$digest"
      echo "Pushed $name:$VERSION ($digest)"
    done
    ;;
  push-chart)
    # A chart repository is the files on the gh-pages branch, which GitHub Pages serves at
    # $CHART_REPOSITORY. A package already there for this version is kept, so a re-run after a
    # later step failed changes nothing; the index is written from the packages present.
    package="struct-otel-collector-$VERSION.tgz"
    pages="$DIST/pages"
    rm -rf "$pages"
    git -c credential.helper= -c credential.helper='!gh auth git-credential' \
      clone --quiet --depth 1 --branch gh-pages "$PAGES_REMOTE" "$pages"
    [[ -f "$pages/$package" ]] || cp "$DIST/$package" "$pages/$package"
    python3 scripts/release.py chart-index --pages "$pages" --base "$CHART_REPOSITORY"
    git -C "$pages" add "$package" index.yaml
    if git -C "$pages" diff --cached --quiet; then
      echo "$package and its index are already published"
    else
      git -C "$pages" -c user.name="Struct Collector release" -c user.email="noreply@struct.ai" \
        commit --quiet -m "Struct Collector $VERSION"
      git -C "$pages" -c credential.helper= -c credential.helper='!gh auth git-credential' push --quiet origin gh-pages
      echo "Published $package to $CHART_REPOSITORY"
    fi
    ;;
  release)
    # --latest makes /releases/latest/download/ serve this installer. --verify-tag
    # uses the tag this run was started by, and never creates one.
    gh release create "v$VERSION" --repo "$REPOSITORY" --verify-tag --latest --title "Struct Collector $VERSION" --notes-file - \
      "$DIST/$INSTALLER" "$DIST/$INSTALLER.sha256" <<EOF
The upstream OpenTelemetry Collector (contrib) $UPSTREAM with Struct's config.

- Image: $(for name in $IMAGES; do printf '`%s:%s` ' "$name" "$VERSION"; done)(the same image), for linux/amd64 and linux/arm64
- Helm chart: \`helm repo add struct $CHART_REPOSITORY\`, then \`struct/struct-otel-collector\`, version \`$VERSION\`
- Linux installer: \`$INSTALLER\`, with its SHA-256 in \`$INSTALLER.sha256\`
EOF
    ;;
  *)
    echo "usage: scripts/publish.sh build | push-image | push-chart | release" >&2
    exit 2
    ;;
esac
