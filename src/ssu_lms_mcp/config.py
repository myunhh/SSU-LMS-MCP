"""독립 서버의 로컬 데이터 경로 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path.home() / ".ssu-lms-mcp"
    headless: bool = True
    session_refresh_seconds: int = 5400
    materials_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        root = Path(self.materials_dir).expanduser().resolve() if self.materials_dir is not None else self.data_dir / "materials"
        if root.exists() and not root.is_dir():
            raise ValueError("자료 저장 경로는 파일이 아닌 폴더여야 합니다.")
        object.__setattr__(self, "materials_dir", root)
        if self.session_refresh_seconds < 0:
            raise ValueError("세션 갱신 간격은 0 이상이어야 합니다.")

    @classmethod
    def from_env(
        cls, data_dir: str | Path | None = None, materials_dir: str | Path | None = None
    ) -> Settings:
        return cls(
            data_dir=Path(data_dir or os.getenv("SSU_LMS_DATA_DIR", Path.home() / ".ssu-lms-mcp")),
            headless=_bool(os.getenv("SSU_LMS_HEADLESS"), True),
            session_refresh_seconds=int(os.getenv("SSU_LMS_SESSION_REFRESH_SECONDS", "5400")),
            materials_dir=materials_dir or os.getenv("SSU_LMS_MATERIALS_DIR") or None,
        )

    @property
    def session_file(self) -> Path:
        return self.data_dir / "session_state.json"

    @property
    def manifest_file(self) -> Path:
        return self.data_dir / "material_manifest.json"
