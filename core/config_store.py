from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import tomlkit

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from utils import CONFIG_PATH

log = logging.getLogger("court")


class ConfigStore:
    """Structured TOML read/modify/write with atomic replace and a process lock."""

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def read_dict(self) -> dict[str, Any]:
        with self._path.open("rb") as f:
            return tomllib.load(f)

    def update_sections(self, sections: dict[str, dict[str, Any]]) -> None:
        """Merge fields into TOML sections and atomically replace the file."""
        if not sections:
            return
        with self._lock:
            if self._path.exists():
                doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
            else:
                doc = tomlkit.document()

            for section, fields in sections.items():
                table = doc.get(section)
                if table is None:
                    doc[section] = tomlkit.table()
                    table = doc[section]
                if not isinstance(table, tomlkit.items.Table):
                    raise RuntimeError(f"config section [{section}] is not a table")
                for key, value in fields.items():
                    table[key] = value

            text = tomlkit.dumps(doc)
            tmp = self._path.with_suffix(".toml.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)

    def write_string_field(self, section: str, key: str, value: str) -> None:
        self.update_sections({section: {key: value}})

    def write_int_field(self, section: str, key: str, value: int) -> None:
        self.update_sections({section: {key: int(value)}})
