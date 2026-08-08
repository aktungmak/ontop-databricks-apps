#!/usr/bin/env bash
# Download Ontop, JRE, and JDBC files for bundle artifact upload.
#
# This fetches binaries from the public internet. Every source host can be
# overridden so the deploy can be pointed at an internal mirror (see README,
# "Build-time downloads"). Files already present in artifacts/ are left alone,
# so an air-gapped deploy can pre-populate that directory instead.
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

if [[ ! -f "${OUT}/${ONTOP_ZIP}" ]]; then
  curl -fsSL -o "${OUT}/${ONTOP_ZIP}" "${ONTOP_URL}"
fi

if [[ ! -f "${OUT}/${JRE_TARBALL}" ]]; then
  curl -fsSL -o "${OUT}/${JRE_TARBALL}" "${JRE_URL}"
fi

if [[ ! -f "${OUT}/${JDBC_JAR}" ]]; then
  # Remove any older databricks-jdbc-*.jar so a version bump does not leave a
  # stale driver alongside the new one (the app discovers the jar by glob).
  rm -f "${OUT}"/databricks-jdbc-*.jar
  if ! curl -fsSL -o "${OUT}/${JDBC_JAR}" "${MAVEN_REPO_URL}/${JDBC_PATH}"; then
    if [[ -n "${MAVEN_MIRROR_URL}" ]]; then
      echo "Primary Maven repo failed; falling back to ${MAVEN_MIRROR_URL}" >&2
      curl -fsSL -o "${OUT}/${JDBC_JAR}" "${MAVEN_MIRROR_URL}/${JDBC_PATH}"
    else
      echo "Failed to download the Databricks JDBC driver from ${MAVEN_REPO_URL}" >&2
      exit 1
    fi
  fi
fi

# Optional integrity check: if artifacts/SHA256SUMS exists, verify against it.
# Generate/refresh with:  cd artifacts && shasum -a 256 * > SHA256SUMS
if [[ -f "${OUT}/SHA256SUMS" ]]; then
  ( cd "${OUT}" && shasum -a 256 -c SHA256SUMS )
fi
