"""Application configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    warehouse_id: str
    mappings_volume_path: str
    mapping_file: str
    ontology_file: str
    ontop_internal_port: int
    app_port: int
    work_dir: Path
    fm_model_name: str

    @property
    def warehouse_http_path(self) -> str:
        return f"/sql/1.0/warehouses/{self.warehouse_id}"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"],
            mappings_volume_path=os.environ["MAPPINGS_VOLUME_PATH"],
            mapping_file=os.environ.get("VKG_MAPPING_FILE", "mapping.ttl"),
            ontology_file=os.environ.get("VKG_ONTOLOGY_FILE", "ontology.ttl"),
            ontop_internal_port=int(os.environ.get("ONTOP_INTERNAL_PORT", "18080")),
            app_port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
            work_dir=Path("/tmp/ontop-vkg"),
            fm_model_name=os.environ["FM_MODEL_NAME"],
        )
