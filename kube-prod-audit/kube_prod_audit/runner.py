"""Result types for the audit."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    FAIL = "fail"        # blocks production-ready status
    WARN = "warn"        # concern, not a blocker
    PASS = "pass"        # compliant

    @property
    def color(self) -> str:
        return {"fail": "#f85149", "warn": "#d29922", "pass": "#3fb950"}[self.value]


class Category(str, Enum):
    WORKLOADS = "Workloads"
    SECURITY = "Security"
    RELIABILITY = "Reliability"
    OBSERVABILITY = "Observability"
    RESOURCES = "Resources"
    STORAGE = "Storage"
    NETWORK = "Network"


@dataclass
class CheckResult:
    """Outcome of a single check."""
    id: str                                    # e.g. "WL-001"
    title: str                                  # human title
    category: Category
    severity: Severity
    summary: str                                # one-line summary
    detail: str = ""                            # longer explanation
    affected: list[str] = field(default_factory=list)  # list of resource refs
    fix: str = ""                               # how to fix
    reference: str = ""                         # link into the K8s reference (Part X.Y)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "affected": self.affected,
            "fix": self.fix,
            "reference": self.reference,
        }


@dataclass
class AuditResult:
    """Aggregate result of a full audit run."""
    cluster: str
    mode: str  # "live" or "demo"
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def score(self) -> int:
        """0–100. weighted: pass=1, warn=0.5, fail=0."""
        if not self.checks:
            return 0
        total = sum(
            1 if c.severity == Severity.PASS
            else 0.5 if c.severity == Severity.WARN
            else 0
            for c in self.checks
        )
        return round(100 * total / len(self.checks))

    @property
    def counts(self) -> dict[str, int]:
        from collections import Counter
        c = Counter(c.severity.value for c in self.checks)
        return {"pass": c.get("pass", 0), "warn": c.get("warn", 0), "fail": c.get("fail", 0)}

    @property
    def by_category(self) -> dict[str, dict[str, int]]:
        from collections import Counter
        out: dict[str, Counter] = {}
        for c in self.checks:
            out.setdefault(c.category.value, Counter())[c.severity.value] += 1
        return {k: dict(v) for k, v in out.items()}

    def by_severity(self, sev: Severity) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == sev]
