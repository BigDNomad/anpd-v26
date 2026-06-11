"""
ANPD V25 Audit Checks — Drop-in check module registry.

Each check module in this package exposes:
    check_id:    str
    severity:    str  (CLASS_A, CLASS_B, or CLASS_C)
    description: str
    def run(manuscript, briefs) -> list[Finding]

The orchestrator (manuscript_auditor.py) discovers and runs all modules
registered in REGISTRY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable, Any


# ── Data structures shared by all check modules ────────────────────────────


@dataclass
class SceneText:
    """A single scene's prose + metadata."""
    scene_number: int
    text: str
    file_path: str
    word_count: int = 0

    def __post_init__(self):
        if not self.word_count:
            self.word_count = len(self.text.split())


@dataclass
class ManuscriptArtifact:
    """The loaded manuscript — a collection of scenes."""
    scenes: list[SceneText]
    manuscript_dir: str

    def full_text(self) -> str:
        return "\n\n".join(s.text for s in sorted(self.scenes, key=lambda s: s.scene_number))

    def scene_by_number(self, n: int) -> SceneText | None:
        for s in self.scenes:
            if s.scene_number == n:
                return s
        return None

    def total_words(self) -> int:
        return sum(s.word_count for s in self.scenes)


@dataclass
class BriefBundle:
    """All reference material available to check modules."""
    series_bible: dict = field(default_factory=dict)
    character_profiles: dict = field(default_factory=dict)
    book_config: dict = field(default_factory=dict)
    scene_map: dict = field(default_factory=dict)
    entity_ledger: dict = field(default_factory=dict)
    synopsis_text: str = ""
    synopsis_path: str | None = None
    synopsis_sha256: str | None = None


@dataclass
class Finding:
    """A single audit finding."""
    check_id: str
    severity: str          # CLASS_A, CLASS_B, CLASS_C
    scene_number: int | None
    description: str
    evidence: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    line_number: int | None = None
    scene_numbers: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "check_id": self.check_id,
            "severity": self.severity,
            "description": self.description,
            "evidence": self.evidence,
        }
        if self.scene_number is not None:
            d["scene_number"] = self.scene_number
        if self.scene_numbers:
            d["scene_numbers"] = self.scene_numbers
        if self.line_number is not None:
            d["line_number"] = self.line_number
        if self.suggested_fix:
            d["suggested_fix"] = self.suggested_fix
        return d


# ── Check module protocol ──────────────────────────────────────────────────


@runtime_checkable
class CheckModule(Protocol):
    check_id: str
    severity: str
    description: str

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        ...


# ── Registry ───────────────────────────────────────────────────────────────

REGISTRY: list[CheckModule] = []


def register(module: CheckModule) -> CheckModule:
    """Register a check module. Can be used as a decorator on the module class."""
    REGISTRY.append(module)
    return module


def discover_and_register():
    """Import all check modules in this package to trigger registration."""
    import importlib
    import pkgutil
    import os

    package_dir = os.path.dirname(__file__)
    for finder, name, ispkg in pkgutil.iter_modules([package_dir]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f".{name}", package=__package__)
        # Auto-register if module exposes a class with check_id attribute
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type) and
                hasattr(obj, "check_id") and
                hasattr(obj, "run") and
                obj.__module__ == mod.__name__):
                instance = obj()
                if instance not in REGISTRY:
                    register(instance)
