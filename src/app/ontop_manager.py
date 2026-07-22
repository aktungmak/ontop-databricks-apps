"""Ontop VKG subprocess lifecycle and JDBC configuration."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from config import Settings
from volume_files import bundle_remote_dir, download_volume_file

logger = logging.getLogger(__name__)

# Databricks JDBC uses Apache Arrow; Java 17+ requires module opens for direct buffers.
_ARROW_JAVA_OPENS = (
    "--add-opens=java.base/java.nio=org.apache.arrow.memory.core,ALL-UNNAMED"
)


class OntopProcessManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.work_dir = settings.work_dir
        self.ontop_dir = self.work_dir / "ontop"
        self.mappings_dir = self.work_dir / "mappings"
        self.properties_path = self.work_dir / "connection.properties"
        self._process: subprocess.Popen[bytes] | None = None
        self._ontop_binary: Path | None = None
        self._java_home: Path | None = None
        self._mapping_path: Path | None = None
        self._ontology_path: Path | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def prepare(self, client: WorkspaceClient) -> None:
        """Download artifacts and mappings from UC volume, extract Ontop bundle."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        volume = self.settings.mappings_volume_path.rstrip("/")
        artifacts_remote = bundle_remote_dir(volume, "artifacts")
        mappings_remote = bundle_remote_dir(volume, "mappings")

        artifacts_dir = self.work_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        protege_name, cli_name = self._find_ontop_artifact_names(client, artifacts_remote)
        extract_root = self.work_dir / "bundle-extract"
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True)

        if protege_name:
            protege_local = artifacts_dir / protege_name
            download_volume_file(
                client,
                f"{artifacts_remote}/{protege_name}",
                protege_local,
            )
            protege_extract = extract_root / "protege"
            self._extract_archive(protege_local, protege_extract)
            self._java_home = self._locate_java_home(protege_extract)
            if self._java_home is None:
                raise RuntimeError(
                    f"No embedded JRE found in {protege_name}. "
                    "Expected jre/bin/java or jdk/bin/java under the extracted bundle."
                )
            logger.info("Using bundled JRE at %s", self._java_home)

        if cli_name:
            cli_local = artifacts_dir / cli_name
            download_volume_file(
                client,
                f"{artifacts_remote}/{cli_name}",
                cli_local,
            )
            cli_extract = extract_root / "cli"
            self._extract_archive(cli_local, cli_extract)
            self._ontop_binary = self._locate_ontop_script(cli_extract)
        elif protege_name:
            protege_extract = extract_root / "protege"
            self._ontop_binary = self._locate_ontop_script(protege_extract)

        if self._ontop_binary is None:
            raise RuntimeError(
                "Could not find the ontop launcher script. "
                "Deploy the bundle to upload Ontop artifacts."
            )

        self._ontop_binary.chmod(self._ontop_binary.stat().st_mode | 0o111)
        if self._java_home is None:
            jre_name = self._find_jre_artifact_name(client, artifacts_remote)
            if jre_name:
                jre_local = artifacts_dir / jre_name
                download_volume_file(
                    client,
                    f"{artifacts_remote}/{jre_name}",
                    jre_local,
                )
                jre_extract = extract_root / "jre"
                self._extract_archive(jre_local, jre_extract)
                self._java_home = self._locate_java_home(jre_extract)
                if self._java_home is not None:
                    logger.info("Using bundled JRE at %s", self._java_home)

        if self._java_home is None:
            raise RuntimeError(
                "No embedded JRE found. Deploy the bundle to upload ontop-protege-bundle-linux-*.tar.gz "
                "or OpenJDK*jre*.tar.gz artifacts."
            )

        jdbc_local = artifacts_dir / "jdbc" / "DatabricksJDBC42.jar"
        download_volume_file(
            client,
            f"{artifacts_remote}/DatabricksJDBC42.jar",
            jdbc_local,
        )
        self._install_jdbc_driver(jdbc_local)

        self.mappings_dir.mkdir(parents=True, exist_ok=True)
        self._mapping_path = self.mappings_dir / self.settings.mapping_file
        download_volume_file(
            client,
            f"{mappings_remote}/{self.settings.mapping_file}",
            self._mapping_path,
        )

        ontology_remote = f"{mappings_remote}/{self.settings.ontology_file}"
        ontology_local = self.mappings_dir / self.settings.ontology_file
        try:
            download_volume_file(client, ontology_remote, ontology_local)
            self._ontology_path = ontology_local
        except Exception:
            logger.info("No ontology file at %s — continuing without --ontology", ontology_remote)
            self._ontology_path = None

        logger.info("Prepared Ontop launcher at %s", self._ontop_binary)

    def _find_ontop_artifact_names(
        self, client: WorkspaceClient, artifacts_remote: str
    ) -> tuple[str | None, str | None]:
        listing = client.files.list_directory_contents(artifacts_remote)
        protege_name: str | None = None
        cli_name: str | None = None
        for entry in listing:
            if entry.name.startswith("ontop-protege-bundle-linux") and entry.name.endswith(
                ".tar.gz"
            ):
                protege_name = entry.name
            elif entry.name.startswith("ontop-cli-") and entry.name.endswith(".zip"):
                cli_name = entry.name

        if protege_name or cli_name:
            return protege_name, cli_name

        raise FileNotFoundError(
            f"No ontop-protege-bundle-linux-*.tar.gz or ontop-cli-*.zip found in "
            f"{artifacts_remote}/. Deploy the bundle to upload artifacts."
        )

    def _find_jre_artifact_name(self, client: WorkspaceClient, artifacts_remote: str) -> str | None:
        listing = client.files.list_directory_contents(artifacts_remote)
        for entry in listing:
            if "jre" in entry.name.lower() and entry.name.endswith(".tar.gz"):
                return entry.name
        return None

    def _find_ontop_artifact_name(self, client: WorkspaceClient, volume: str) -> str:
        artifacts_remote = bundle_remote_dir(volume, "artifacts")
        protege_name, cli_name = self._find_ontop_artifact_names(client, artifacts_remote)
        return protege_name or cli_name or ""

    @staticmethod
    def _extract_archive(archive: Path, extract_to: Path) -> None:
        extract_to.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(path=extract_to)
            return

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=extract_to)

    @staticmethod
    def _locate_java_home(root: Path) -> Path | None:
        for java_bin in root.rglob("bin/java"):
            if not java_bin.is_file():
                continue
            java_home = java_bin.parent.parent
            if (java_home / "bin" / "java").is_file():
                return java_home
        return None

    def _locate_ontop_script(self, root: Path) -> Path | None:
        run_sh = next(root.rglob("run.sh"), None)
        if run_sh is not None and run_sh.is_file():
            run_sh.chmod(run_sh.stat().st_mode | 0o111)
            logger.info("Found run.sh wrapper at %s", run_sh)

        for candidate in root.rglob("ontop"):
            if candidate.is_file() and candidate.name == "ontop":
                if candidate.suffix:
                    continue
                return candidate
        return None

    def _install_jdbc_driver(self, jdbc_jar: Path) -> None:
        if self._ontop_binary is None:
            raise RuntimeError("Ontop binary not located before JDBC install")

        jdbc_dir = self._ontop_binary.parent / "jdbc"
        jdbc_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jdbc_jar, jdbc_dir / "DatabricksJDBC42.jar")

    def _ontop_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Ontop CLI reads MAPPING_FILE / ONTOLOGY_FILE (and ONTOP_* aliases) from the
        # environment. Strip them so only explicit --mapping / --ontology args apply.
        for key in (
            "MAPPING_FILE",
            "ONTOLOGY_FILE",
            "ONTOP_MAPPING_FILE",
            "ONTOP_ONTOLOGY_FILE",
        ):
            env.pop(key, None)
        if self._java_home is not None:
            java_home = str(self._java_home)
            env["JAVA_HOME"] = java_home
            env["PATH"] = f"{java_home}/bin:{env.get('PATH', '')}"

        # Ontop CLI honors ONTOP_JAVA_ARGS when launching the JVM (see ontop docker README).
        existing_java_args = env.get("ONTOP_JAVA_ARGS", "").strip()
        env["ONTOP_JAVA_ARGS"] = (
            f"{existing_java_args} {_ARROW_JAVA_OPENS}".strip()
            if existing_java_args
            else _ARROW_JAVA_OPENS
        )
        return env

    @staticmethod
    def _workspace_host() -> str:
        host = os.environ.get("DATABRICKS_HOST")
        if not host:
            cfg = Config()
            host = cfg.host or ""
        return host.replace("https://", "").replace("http://", "").rstrip("/")

    def write_jdbc_properties(self) -> None:
        client_id = os.environ.get("DATABRICKS_CLIENT_ID")
        client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET are required "
                "for JDBC M2M OAuth authentication"
            )

        host = self._workspace_host()
        jdbc_url = (
            f"jdbc:databricks://{host}:443;"
            f"HttpPath={self.settings.warehouse_http_path};"
            f"AuthMech=11;Auth_Flow=1;"
            f"OAuth2ClientId={client_id};OAuth2Secret={client_secret}"
        )
        content = (
            f"jdbc.url={jdbc_url}\n"
            f"jdbc.driver=com.databricks.client.jdbc.Driver\n"
            "ontop.reformulateToFullNativeQuery=true\n"
        )
        self.properties_path.write_text(content)
        logger.info("Wrote JDBC M2M OAuth properties for service principal %s", client_id)

    def start(self) -> None:
        if self._ontop_binary is None or self._mapping_path is None:
            raise RuntimeError("Call prepare() before start()")

        if self.is_running:
            return

        cmd = [
            str(self._ontop_binary),
            "endpoint",
            "--mapping",
            str(self._mapping_path),
            "--properties",
            str(self.properties_path),
            "--port",
            str(self.settings.ontop_internal_port),
            "--disable-portal-page",
            "--lazy",
            "--dev",  # exposes /ontop/reformulate
        ]
        if self._ontology_path and self._ontology_path.exists():
            cmd.extend(["--ontology", str(self._ontology_path)])

        env = self._ontop_env()
        logger.info(
            "Starting Ontop: %s (JAVA_HOME=%s)",
            " ".join(cmd),
            env.get("JAVA_HOME", "<unset>"),
        )
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(self._ontop_binary.parent),
            env=env,
        )
        self._wait_for_port(self.settings.ontop_internal_port, timeout=120)
        logger.info("Ontop endpoint started on port %s", self.settings.ontop_internal_port)

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        self._process = None
        self._wait_for_port_free(self.settings.ontop_internal_port, timeout=30)

    def _wait_for_port(self, port: int, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                output = ""
                if self._process.stdout:
                    output = self._process.stdout.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Ontop exited early:\n{output}")
            if self._port_open(port):
                return
            time.sleep(0.5)
        raise TimeoutError(f"Ontop did not open port {port} within {timeout}s")

    def _wait_for_port_free(self, port: int, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._port_open(port):
                return
            time.sleep(0.5)

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0
