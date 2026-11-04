"""Loading company configuration (config/company.yaml + environment)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Employee:
    id: str
    name: str
    role: str
    title: str
    duties: str
    color: str = "#888888"

    @property
    def is_director(self) -> bool:
        return self.role == "director"


@dataclass
class Config:
    company: dict[str, Any]
    models: dict[str, Any]
    autonomy: dict[str, Any]
    limits: dict[str, Any]
    escalate_to_owner: list[str]
    employees: list[Employee]
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    workspace_dir: Path = field(default_factory=lambda: ROOT / "workspace")

    @property
    def director(self) -> Employee:
        return next(e for e in self.employees if e.is_director)

    @property
    def workers(self) -> list[Employee]:
        return [e for e in self.employees if not e.is_director]

    def employee(self, emp_id: str) -> Employee | None:
        return next((e for e in self.employees if e.id == emp_id), None)

    def limit(self, key: str, default: Any = None) -> Any:
        return self.limits.get(key, default)

    def auto(self, key: str, default: Any = None) -> Any:
        return self.autonomy.get(key, default)


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None, data_dir: Path | None = None,
                workspace_dir: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path or os.environ.get("OFFICE_CONFIG", ROOT / "config" / "company.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    employees = [Employee(**e) for e in raw["employees"]]
    if sum(e.is_director for e in employees) != 1:
        raise ValueError("В конфиге должен быть ровно один сотрудник с role: director")
    cfg = Config(
        company=raw.get("company", {}),
        models=raw.get("models", {}),
        autonomy=raw.get("autonomy", {}),
        limits=raw.get("limits", {}),
        escalate_to_owner=raw.get("escalate_to_owner", []),
        employees=employees,
    )
    if data_dir or os.environ.get("OFFICE_DATA_DIR"):
        cfg.data_dir = Path(data_dir or os.environ["OFFICE_DATA_DIR"])
    if workspace_dir or os.environ.get("OFFICE_WORKSPACE_DIR"):
        cfg.workspace_dir = Path(workspace_dir or os.environ["OFFICE_WORKSPACE_DIR"])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    return cfg
