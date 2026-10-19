"""Workdir layout per DEVELOPMENT.md §7."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_WORKDIR_NAME = ".faillab"


def resolve_workdir(explicit: str | None, *, fallback: str = DEFAULT_WORKDIR_NAME) -> Path:
    root = Path(explicit) if explicit else Path.cwd() / fallback
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


@dataclass
class WorkLayout:
    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.root / "faillab.db"

    @property
    def logs_dir(self) -> Path:
        path = self.root / "logs"
        path.mkdir(exist_ok=True)
        return path

    def repro_log_path(self, public_id: str, stamp: str) -> Path:
        directory = self.logs_dir / public_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"repro-{stamp}.log"
