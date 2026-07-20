"""Download files from a Unity Catalog volume via the Databricks SDK."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def bundle_remote_dir(volume: str, prefix: str) -> str:
    """Path to DAB bundle-uploaded files under {volume}/{prefix}/.internal."""
    return f"{volume.rstrip('/')}/{prefix.strip('/')}/.internal"


def download_volume_file(client: WorkspaceClient, volume_path: str, dest: Path) -> None:
    """Download a single file from a UC volume to a local path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s -> %s", volume_path, dest)
    response = client.files.download(volume_path)
    with open(dest, "wb") as out:
        shutil.copyfileobj(response.contents, out)


def download_volume_directory(
    client: WorkspaceClient,
    volume_base: str,
    remote_prefix: str,
    local_dir: Path,
    *,
    required_files: list[str] | None = None,
) -> list[Path]:
    """Download files under volume_base/remote_prefix into local_dir."""
    local_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    if required_files:
        names = required_files
    else:
        remote_dir = f"{volume_base.rstrip('/')}/{remote_prefix.strip('/')}"
        listing = client.files.list_directory_contents(remote_dir)
        names = [entry.name for entry in listing if not entry.is_directory]

    for name in names:
        remote = f"{volume_base.rstrip('/')}/{remote_prefix.strip('/')}/{name}"
        dest = local_dir / name
        try:
            download_volume_file(client, remote, dest)
            downloaded.append(dest)
        except Exception:
            logger.warning("Could not download %s (may not exist)", remote)
            if required_files and name in required_files:
                raise

    return downloaded
