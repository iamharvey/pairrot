"""Repository review policy. Read from the base commit only."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pairrot.models import CORRECTNESS_CATEGORIES, Category
from pairrot.snapshot import run_git

_LEVELS = {"must_fix", "recommend"}


class PolicyError(ValueError):
    pass


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
    extras: tuple[str, ...] = ()
    error: str | None = None

    @property
    def active(self) -> bool:
        return self.source == "base" and not self.error

    def llm_categories(self) -> frozenset[Category]:
        found: set[Category] = set()
        if not self.active or self.correctness_enabled:
            found.update(CORRECTNESS_CATEGORIES)
        if self.active and self.performance_enabled:
            found.add(Category.performance)
        if self.active and self.security_enabled:
            found.add(Category.security)
        return frozenset(found)

    def needs_model(self) -> bool:
        if not self.active:
            return True
        return bool(self.llm_categories())

    def level_for(self, category: Category, *, secret: bool = False) -> str:
        if secret:
            return self.secrets_level
        if category == Category.security:
            return self.security_level
        if category == Category.performance:
            return self.performance_level
        if category in CORRECTNESS_CATEGORIES:
            return self.correctness_level
        return "recommend"

    def note(self) -> str:
        if not self.active:
            return ""
        parts = [
            f"correctness={'开启' if self.correctness_enabled else '关闭'}:{self.correctness_level}",
            f"secrets={'开启' if self.secrets_enabled else '关闭'}:{self.secrets_level}",
            f"performance={'开启' if self.performance_enabled else '关闭'}:{self.performance_level}",
            f"security={'开启' if self.security_enabled else '关闭'}:{self.security_level}",
        ]
        if self.extras:
            parts.append(f"extra={len(self.extras)}")
        return "、".join(parts)

    def digest(self) -> str:
        if self.error:
            raw = "invalid:" + self.error
        elif not self.active:
            raw = "default"
        else:
            raw = json.dumps(
                {
                    "fail_on_must_fix": self.fail_on_must_fix,
                    "correctness_enabled": self.correctness_enabled,
                    "correctness_level": self.correctness_level,
                    "secrets_enabled": self.secrets_enabled,
                    "secrets_level": self.secrets_level,
                    "performance_enabled": self.performance_enabled,
                    "performance_level": self.performance_level,
                    "security_enabled": self.security_enabled,
                    "security_level": self.security_level,
                    "extras": list(self.extras),
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


def policy_from_toml(text: str) -> Policy:
    try:
        data = tomllib.loads(text)
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
        extras=tuple(extras),
    )


def load_policy(repo: Path, base_sha: str) -> Policy:
    shown = run_git(repo, "show", f"{base_sha}:REVIEW.toml")
    if shown.returncode != 0:
        return Policy()
    try:
        return policy_from_toml(shown.stdout)
    except PolicyError as exc:
        return Policy(source="base", error=str(exc))
