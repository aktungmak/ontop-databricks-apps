#!/usr/bin/env bash
# Download Ontop, JRE, and JDBC files for bundle artifact upload.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/artifacts"

ONTOP_VERSION="${ONTOP_VERSION:-5.5.0}"
JDBC_VERSION="${JDBC_VERSION:-2.7.5}"
JRE_VERSION="${JRE_VERSION:-17.0.19_10}"

ONTOP_ZIP="ontop-cli-${ONTOP_VERSION}.zip"
JRE_TARBALL="OpenJDK17U-jre_x64_linux_hotspot_${JRE_VERSION}.tar.gz"
JRE_URL="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.19%2B10/${JRE_TARBALL}"
JDBC_URL_MAVEN="https://repo1.maven.org/maven2/com/databricks/databricks-jdbc/${JDBC_VERSION}/databricks-jdbc-${JDBC_VERSION}.jar"
JDBC_URL_MIRROR="https://maven.aliyun.com/repository/central/com/databricks/databricks-jdbc/${JDBC_VERSION}/databricks-jdbc-${JDBC_VERSION}.jar"

mkdir -p "${OUT}"

if [[ ! -f "${OUT}/${ONTOP_ZIP}" ]]; then
  curl -fsSL -o "${OUT}/${ONTOP_ZIP}" \
    "https://github.com/ontop/ontop/releases/download/ontop-${ONTOP_VERSION}/${ONTOP_ZIP}"
fi

if [[ ! -f "${OUT}/${JRE_TARBALL}" ]]; then
  curl -fsSL -o "${OUT}/${JRE_TARBALL}" "${JRE_URL}"
fi

if [[ ! -f "${OUT}/DatabricksJDBC42.jar" ]]; then
  curl -fsSL -o "${OUT}/DatabricksJDBC42.jar" "${JDBC_URL_MAVEN}" \
    || curl -fsSL -o "${OUT}/DatabricksJDBC42.jar" "${JDBC_URL_MIRROR}"
fi
