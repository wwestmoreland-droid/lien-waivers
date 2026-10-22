"""Template registry keyed by (state, waiver type). See templates/pdf/registry.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import BASE_DIR

TEMPLATE_DIR = BASE_DIR / "templates" / "pdf"


@dataclass
class Template:
    key: str
    state: str
    type: str
    file: Path
    fields: dict[str, dict]
    notary_required: bool = True


@dataclass
class Registry:
    statutory_states: set[str]
    templates: dict[str, Template] = field(default_factory=dict)

    def lookup(self, state: str | None, waiver_type: str) -> Template | None:
        """Exact state first; default only when the state doesn't require a statutory form."""
        st = (state or "").upper()
        for t in self.templates.values():
            if t.state == st and t.type == waiver_type:
                return t
        if st in self.statutory_states:
            return None
        for t in self.templates.values():
            if t.state == "default" and t.type == waiver_type:
                return t
        return None

    def requires_statutory(self, state: str | None) -> bool:
        return (state or "").upper() in self.statutory_states


def load_registry(path: Path | None = None) -> Registry:
    path = path or TEMPLATE_DIR / "registry.yaml"
    data = yaml.safe_load(path.read_text())
    reg = Registry(statutory_states={s.upper() for s in data.get("statutory_states", [])})
    default_notary = bool(data.get("notary_required", True))
    for key, spec in (data.get("templates") or {}).items():
        file = path.parent / spec["file"]
        if not file.exists():
            raise FileNotFoundError(f"Template {key}: {file} not found")
        reg.templates[key] = Template(
            key=key, state=str(spec["state"]).upper() if spec["state"] != "default" else "default",
            type=spec["type"], file=file, fields=spec.get("fields") or {},
            notary_required=bool(spec.get("notary_required", default_notary)),
        )
    return reg
