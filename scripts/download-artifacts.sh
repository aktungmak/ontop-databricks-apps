#!/usr/bin/env bash
# Download Ontop, JRE, and JDBC files for bundle artifact upload.
#
# This fetches binaries from the public internet. Every source host can be
# overridden so the deploy can be pointed at an internal mirror (see README,
# "Build-time downloads"). Files already present in artifacts/ for the configured
# versions are kept, so an air-gapped deploy can pre-populate that directory
# instead; other versions are removed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/artifacts"

ONTOP_VERSION="${ONTOP_VERSION:-5.5.0}"
JDBC_VERSION="${JDBC_VERSION:-3.4.1}"
JRE_VERSION="${JRE_VERSION:-17.0.19_10}"

# Source hosts — override to use an internal mirror / proxy.
ONTOP_BASE_URL="${ONTOP_BASE_URL:-https://github.com/ontop/ontop/releases/download}"
JRE_BASE_URL="${JRE_BASE_URL:-https://github.com/adoptium/temurin17-binaries/releases/download}"
MAVEN_REPO_URL="${MAVEN_REPO_URL:-https://repo1.maven.org/maven2}"
# Optional fallback for the JDBC driver; set to an empty string to disable.
# Note ${VAR-default} (no colon) so that MAVEN_MIRROR_URL="" really means "none".
MAVEN_MIRROR_URL="${MAVEN_MIRROR_URL-https://maven.aliyun.com/repository/central}"

ONTOP_ZIP="ontop-cli-${ONTOP_VERSION}.zip"
JRE_TARBALL="OpenJDK17U-jre_x64_linux_hotspot_${JRE_VERSION}.tar.gz"
JDBC_JAR="databricks-jdbc-${JDBC_VERSION}.jar"

# Temurin release tags look like "jdk-17.0.19+10" while the tarball uses
# "17.0.19_10". Derive the tag from JRE_VERSION so the two cannot disagree
# ("+" must be percent-encoded in the URL path).
JRE_TAG="jdk-${JRE_VERSION%_*}+${JRE_VERSION##*_}"

ONTOP_URL="${ONTOP_BASE_URL}/ontop-${ONTOP_VERSION}/${ONTOP_ZIP}"
JRE_URL="${JRE_BASE_URL}/${JRE_TAG//+/%2B}/${JRE_TARBALL}"
JDBC_PATH="com/databricks/databricks-jdbc/${JDBC_VERSION}/databricks-jdbc-${JDBC_VERSION}.jar"

mkdir -p "${OUT}"

# fetch <file> <other-versions-glob> <url> [fallback-url]
# Removes every other version matching the glob, since the app takes the first
# match it finds. Keeps <file> if already present (so artifacts/ can be
# pre-populated); otherwise tries each URL in turn. Downloads go to <file>.part
# and are renamed only when complete, so an interrupted download is never reused.
fetch() {
  local file="$1" others="$2" old url
  shift 2
  for old in "${OUT}"/${others}; do
    [[ -e "${old}" && "${old}" != "${OUT}/${file}" ]] && rm -f "${old}"
  done
  [[ -f "${OUT}/${file}" ]] && return 0
  for url in "$@"; do
    if curl -fsSL -o "${OUT}/${file}.part" "${url}"; then
      mv "${OUT}/${file}.part" "${OUT}/${file}"
      return 0
    fi
    echo "Download failed: ${url}" >&2
  done
  rm -f "${OUT}/${file}.part"
  return 1
}

fetch "${ONTOP_ZIP}" 'ontop-cli-*.zip' "${ONTOP_URL}"
fetch "${JRE_TARBALL}" 'OpenJDK*jre*.tar.gz' "${JRE_URL}"
fetch "${JDBC_JAR}" 'databricks-jdbc-*.jar' "${MAVEN_REPO_URL}/${JDBC_PATH}" \
  ${MAVEN_MIRROR_URL:+"${MAVEN_MIRROR_URL}/${JDBC_PATH}"}

# Optional integrity check: if artifacts/SHA256SUMS exists, verify against it.
# Generate/refresh with:  cd artifacts && shasum -a 256 * > SHA256SUMS
if [[ -f "${OUT}/SHA256SUMS" ]]; then
  ( cd "${OUT}" && shasum -a 256 -c SHA256SUMS )
fi
