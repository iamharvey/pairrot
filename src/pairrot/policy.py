"""Repository review policy. Read from the base commit only."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from pairrot.models import CORRECTNESS_CATEGORIES, Category, Finding, IntroducedIn, norm_path
from pairrot.rules import CATALOG, OFF, SEVERITY_RANK, table_of, valid_selector
from pairrot.snapshot import run_git

POLICY_FILE = "REVIEW.toml"
_LEVELS = {"must_fix", "recommend"}
_PREEXISTING = {"recommend", "keep", OFF}
_LEVEL_ORDER = [OFF, "recommend", "must_fix"]
_TABLES = ("correctness", "secrets", "performance", "security", "compatibility", "simplicity")
BUDGET_LIMITS = ("max_net_added", "max_new_files", "max_new_symbols", "max_single_use", "max_new_env_reads")


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    target = norm_path(path)
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns)


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Override:
    """Settings for findings whose file matches paths. Later overrides win."""

    paths: tuple[str, ...]
    off: tuple[str, ...] = ()
    level: str | None = None
    must_fix_min_severity: str | None = None

    def turns_off(self, table: str, rule: str) -> bool:
        return table in self.off or f"{table}.{rule}" in self.off

    def as_dict(self) -> dict:
        return {
            "paths": list(self.paths),
            "off": list(self.off),
            "level": self.level,
            "must_fix_min_severity": self.must_fix_min_severity,
        }


@dataclass(frozen=True)
class Policy:
    source: str = "default"
    fail_on_must_fix: bool = False
    correctness_enabled: bool = True
    correctness_level: str = "recommend"
    secrets_enabled: bool = False
    secrets_level: str = "must_fix"
    performance_enabled: bool = False
    performance_level: str = "recommend"
    security_enabled: bool = False
    security_level: str = "must_fix"
    compatibility_enabled: bool = False
    compatibility_level: str = "must_fix"
    stable: tuple[str, ...] = ()
    allow_break: tuple[str, ...] = ()
    simplicity_enabled: bool = False
    simplicity_level: str = "must_fix"
    extension_points: tuple[str, ...] = ()
    budget_limits: tuple[tuple[str, int], ...] = ()
    extras: tuple[str, ...] = ()
    rule_levels: tuple[tuple[str, str], ...] = ()
    must_fix_min_severity: str = "S3"
    overrides: tuple[Override, ...] = ()
    preexisting: str = "recommend"
    exclude: tuple[str, ...] = ()
    error: str | None = None

    @property
    def active(self) -> bool:
        return self.source in {"base", "local"} and not self.error

    def table_enabled(self, table: str) -> bool:
        return bool(getattr(self, f"{table}_enabled"))

    def rule_level(self, table: str, rule: str) -> str:
        """must_fix, recommend or off for one rule, before path overrides."""
        if not self.table_enabled(table):
            return OFF
        return dict(self.rule_levels).get(f"{table}.{rule}", getattr(self, f"{table}_level"))

    def live_rules(self, table: str) -> list[str]:
        return [rule for rule in CATALOG[table] if self.rule_level(table, rule) != OFF]

    def llm_categories(self) -> frozenset[Category]:
        if not self.active:
            return frozenset(CORRECTNESS_CATEGORIES)
        found: set[Category] = set()
        for category in CORRECTNESS_CATEGORIES:
            if self.rule_level("correctness", category.value) != OFF:
                found.add(category)
        for table, category in (
            ("performance", Category.performance),
            ("security", Category.security),
            ("compatibility", Category.contract_break),
            ("simplicity", Category.complexity),
        ):
            if self.live_rules(table):
                found.add(category)
        return frozenset(found)

    def scans_secrets(self) -> bool:
        return self.active and bool(self.live_rules("secrets"))

    def decide(self, finding: Finding, *, secret: bool = False, locked: bool = False) -> str:
        """must_fix, recommend or off for one finding, after rules, path overrides and the severity floor."""
        return self.grade(finding, secret=secret, locked=locked)[0]

    def grade(self, finding: Finding, *, secret: bool = False, locked: bool = False) -> tuple[str, str]:
        """The level, and why it is off. The reason is empty unless the level is off."""
        table = table_of(finding.category, secret=secret)
        if table is None:
            return "recommend", ""
        level = self.rule_level(table, finding.rule)
        reason = "rule off in REVIEW.toml" if level == OFF else ""
        floor = self.must_fix_min_severity
        for override in self.overrides:
            if not matches(finding.location.file, override.paths):
                continue
            if override.turns_off(table, finding.rule):
                level, reason = OFF, "path override in REVIEW.toml"
            elif override.level and level != OFF:
                level = override.level
            if override.must_fix_min_severity:
                floor = override.must_fix_min_severity
        if finding.introduced_in == IntroducedIn.preexisting and self.preexisting != "keep":
            if self.preexisting == OFF:
                if level != OFF:
                    reason = "preexisting off in REVIEW.toml"
                level = OFF
            else:
                level = min(level, "recommend", key=_LEVEL_ORDER.index)
        if level == OFF:
            return OFF, reason
        if table == "simplicity" and not locked:
            return "recommend", ""
        if level == "must_fix" and SEVERITY_RANK[finding.severity.value] > SEVERITY_RANK[floor]:
            return "recommend", ""
        return level, ""

    def design_categories(self) -> frozenset[Category]:
        return self.llm_categories() & {Category.contract_break, Category.complexity}

    def main_categories(self) -> frozenset[Category]:
        return self.llm_categories() - self.design_categories()

    def needs_model(self) -> bool:
        if not self.active:
            return True
        return bool(self.llm_categories())

    def is_stable(self, path: str) -> bool:
        """Breaking this path breaks outside users. allow_break lifts it for the change that the base allows."""
        return matches(path, self.stable) and not matches(path, self.allow_break)

    def note(self) -> str:
        if not self.active:
            return ""
        parts = [
            f"{table}={'开启' if self.table_enabled(table) else '关闭'}:{getattr(self, f'{table}_level')}"
            for table in _TABLES
        ]
        if self.rule_levels:
            parts.append("rules=" + ",".join(f"{name}:{level}" for name, level in self.rule_levels))
        if self.must_fix_min_severity != "S3":
            parts.append(f"must_fix_min_severity={self.must_fix_min_severity}")
        if self.overrides:
            parts.append(f"override={len(self.overrides)}")
        if self.preexisting != "recommend":
            parts.append(f"preexisting={self.preexisting}")
        if self.budget_limits:
            parts.append(f"complexity_budget={len(self.budget_limits)}项")
        if self.extras:
            parts.append(f"extra={len(self.extras)}")
        if self.exclude:
            parts.append(f"exclude={len(self.exclude)}")
        text = "、".join(parts)
        if self.source == "local":
            return "本地规则，CI 不会用这份。" + text
        return text

    def digest(self) -> str:
        if self.error:
            raw = "invalid:" + self.error
        elif not self.active:
            raw = "default"
        else:
            raw = json.dumps(
                {
                    "source": self.source,
                    "fail_on_must_fix": self.fail_on_must_fix,
                    "correctness_enabled": self.correctness_enabled,
                    "correctness_level": self.correctness_level,
                    "secrets_enabled": self.secrets_enabled,
                    "secrets_level": self.secrets_level,
                    "performance_enabled": self.performance_enabled,
                    "performance_level": self.performance_level,
                    "security_enabled": self.security_enabled,
                    "security_level": self.security_level,
                    "compatibility_enabled": self.compatibility_enabled,
                    "compatibility_level": self.compatibility_level,
                    "stable": list(self.stable),
                    "allow_break": list(self.allow_break),
                    "simplicity_enabled": self.simplicity_enabled,
                    "simplicity_level": self.simplicity_level,
                    "extension_points": list(self.extension_points),
                    "budget_limits": dict(self.budget_limits),
                    "extras": list(self.extras),
                    "rule_levels": dict(self.rule_levels),
                    "must_fix_min_severity": self.must_fix_min_severity,
                    "overrides": [item.as_dict() for item in self.overrides],
                    "preexisting": self.preexisting,
                    "exclude": list(self.exclude),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _level(value: object, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    if text not in _LEVELS:
        raise PolicyError("level 只能是 must_fix 或 recommend")
    return text


def _enabled(table: object, default: bool) -> bool:
    if not isinstance(table, dict):
        raise PolicyError("规则必须是表")
    value = table.get("enabled", default)
    if not isinstance(value, bool):
        raise PolicyError("enabled 必须是布尔值")
    return value


def _table(data: dict, name: str) -> dict:
    raw = data.get(name, {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PolicyError(f"{name} 必须是表")
    return raw


def _globs(table: dict, name: str) -> tuple[str, ...]:
    raw = table.get(name, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise PolicyError(f"{name} 必须是非空字符串数组")
    return tuple(norm_path(item.strip()) for item in raw)


def _exclude(data: dict) -> tuple[str, ...]:
    """Directory patterns ending in / cover that directory and everything under it."""
    found: list[str] = []
    for pattern in _globs(data, "exclude"):
        if pattern.endswith("/"):
            stem = pattern.rstrip("/")
            if not stem:
                raise PolicyError("exclude 不能是空路径")
            pattern = f"{stem}/**"
        elif not pattern:
            raise PolicyError("exclude 不能是空路径")
        found.append(pattern)
    return tuple(found)


def _limits(table: dict) -> tuple[tuple[str, int], ...]:
    unknown = sorted(set(table) - set(BUDGET_LIMITS))
    if unknown:
        raise PolicyError(f"complexity_budget 不认识的键: {'、'.join(unknown)}")
    found: list[tuple[str, int]] = []
    for name in BUDGET_LIMITS:
        if name not in table:
            continue
        value = table[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError(f"complexity_budget.{name} 必须是非负整数")
        found.append((name, value))
    return tuple(found)


def _severity(value: object, name: str) -> str:
    text = str(value).strip()
    if text not in SEVERITY_RANK:
        raise PolicyError(f"{name} 只能是 S1、S2 或 S3")
    return text


def _rules(table: dict, name: str) -> list[tuple[str, str]]:
    raw = table.get("rules", {})
    if not isinstance(raw, dict):
        raise PolicyError(f"{name}.rules 必须是表")
    found: list[tuple[str, str]] = []
    for rule, value in raw.items():
        if rule not in CATALOG[name]:
            raise PolicyError(f"{name}.rules 不认识 {rule}，可选：{'、'.join(CATALOG[name])}")
        level = str(value).strip()
        if level not in _LEVELS | {OFF}:
            raise PolicyError(f"{name}.rules.{rule} 只能是 must_fix、recommend 或 off")
        found.append((f"{name}.{rule}", level))
    return found


_OVERRIDE_KEYS = {"paths", "off", "level", "must_fix_min_severity"}


def _overrides(data: dict) -> tuple[Override, ...]:
    raw = data.get("override", [])
    if not isinstance(raw, list):
        raise PolicyError("override 必须写成 [[override]] 数组")
    found: list[Override] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PolicyError("override 的每一项必须是表")
        unknown = sorted(set(item) - _OVERRIDE_KEYS)
        if unknown:
            raise PolicyError(f"override 不认识的键: {'、'.join(unknown)}")
        paths = _globs(item, "paths")
        if not paths:
            raise PolicyError("override.paths 不能为空")
        off = item.get("off", [])
        if not isinstance(off, list) or not all(isinstance(entry, str) for entry in off):
            raise PolicyError("override.off 必须是字符串数组")
        for entry in off:
            if not valid_selector(entry):
                raise PolicyError(f"override.off 不认识 {entry}，写成 security 或 security.authz 这样的形式")
        level = item.get("level")
        floor = item.get("must_fix_min_severity")
        found.append(
            Override(
                paths=paths,
                off=tuple(off),
                level=_level(level, default="") if level is not None else None,
                must_fix_min_severity=_severity(floor, "override.must_fix_min_severity") if floor is not None else None,
            )
        )
    return tuple(found)


def policy_from_toml(text: str) -> Policy:
    try:
        data = tomllib.loads(text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"REVIEW.toml 无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("REVIEW.toml 无法解析")
    fail = data.get("fail_on_must_fix", False)
    if not isinstance(fail, bool):
        raise PolicyError("fail_on_must_fix 必须是布尔值")
    correctness = _table(data, "correctness")
    secrets = _table(data, "secrets")
    performance = _table(data, "performance")
    security = _table(data, "security")
    compatibility = _table(data, "compatibility")
    simplicity = _table(data, "simplicity")
    budget = _table(data, "complexity_budget")
    extras_raw = data.get("extra", [])
    if extras_raw is None:
        extras_raw = []
    if not isinstance(extras_raw, list):
        raise PolicyError("extra 必须是数组")
    extras: list[str] = []
    for item in extras_raw:
        if not isinstance(item, dict):
            raise PolicyError("extra 的每一项必须是表")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PolicyError("extra.enabled 必须是布尔值")
        instruction = str(item.get("instruction") or "").strip()
        if enabled and instruction:
            extras.append(instruction)
    tables = {
        "correctness": correctness,
        "secrets": secrets,
        "performance": performance,
        "security": security,
        "compatibility": compatibility,
        "simplicity": simplicity,
    }
    rule_levels = [pair for name, table in tables.items() for pair in _rules(table, name)]
    preexisting = str(data.get("preexisting", "recommend")).strip()
    if preexisting not in _PREEXISTING:
        raise PolicyError("preexisting 只能是 recommend、keep 或 off")
    return Policy(
        source="base",
        fail_on_must_fix=fail,
        correctness_enabled=_enabled(correctness, True),
        correctness_level=_level(correctness.get("level"), default="recommend"),
        secrets_enabled=_enabled(secrets, False),
        secrets_level=_level(secrets.get("level"), default="must_fix"),
        performance_enabled=_enabled(performance, False),
        performance_level=_level(performance.get("level"), default="recommend"),
        security_enabled=_enabled(security, False),
        security_level=_level(security.get("level"), default="must_fix"),
        compatibility_enabled=_enabled(compatibility, False),
        compatibility_level=_level(compatibility.get("level"), default="must_fix"),
        stable=_globs(compatibility, "stable"),
        allow_break=_globs(compatibility, "allow_break"),
        simplicity_enabled=_enabled(simplicity, False),
        simplicity_level=_level(simplicity.get("level"), default="must_fix"),
        extension_points=_globs(simplicity, "extension_points"),
        budget_limits=_limits(budget),
        extras=tuple(extras),
        rule_levels=tuple(rule_levels),
        must_fix_min_severity=_severity(data.get("must_fix_min_severity", "S3"), "must_fix_min_severity"),
        overrides=_overrides(data),
        preexisting=preexisting,
        exclude=_exclude(data),
    )


def misnamed_policy(repo: Path, rev: str) -> str | None:
    """A root file that only differs from REVIEW.toml by case. Windows hides the difference, git does not."""
    listed = run_git(repo, "ls-tree", "--name-only", rev)
    if listed.returncode != 0:
        return None
    for name in listed.stdout.splitlines():
        if name != POLICY_FILE and name.lower() == POLICY_FILE.lower():
            return name
    return None


def load_policy_file(path: Path | str) -> Policy:
    """A REVIEW.toml from disk. Active for this run only; CI keeps reading the base commit."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return Policy(source="local", error=f"读不到规则文件 {file}: {exc}")
    try:
        parsed = policy_from_toml(text)
    except PolicyError as exc:
        return Policy(source="local", error=str(exc))
    return replace(parsed, source="local")


def load_policy(repo: Path, base_sha: str) -> Policy:
    shown = run_git(repo, "show", f"{base_sha}:{POLICY_FILE}")
    if shown.returncode != 0:
        misnamed = misnamed_policy(repo, base_sha)
        if misnamed:
            return Policy(source="base", error=f"规则文件名必须是 {POLICY_FILE}，base 上是 {misnamed}")
        return Policy()
    try:
        return policy_from_toml(shown.stdout)
    except PolicyError as exc:
        return Policy(source="base", error=str(exc))
