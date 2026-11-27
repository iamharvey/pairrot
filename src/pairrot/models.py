"""Findings, run reports, and the audit-template vocabulary."""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    business_intent = "business_intent"
    logic_error = "logic_error"
    data_consistency = "data_consistency"
    concurrency = "concurrency"
    contract_break = "contract_break"
    claim_mismatch = "claim_mismatch"
    error_handling = "error_handling"
    security = "security"
    performance = "performance"
    complexity = "complexity"


# The categories reviewed when the base has no REVIEW.toml.
CORRECTNESS_CATEGORIES = frozenset({Category.logic_error, Category.error_handling})

CATEGORY_FROM_LABEL: dict[str, Category] = {
    "业务意图不符": Category.business_intent,
    "逻辑错误": Category.logic_error,
    "数据一致性": Category.data_consistency,
    "并发与时序": Category.concurrency,
    "跨模块契约破坏": Category.contract_break,
    "声明与实现不一致": Category.claim_mismatch,
    "错误处理缺失": Category.error_handling,
    "安全与权限": Category.security,
    "性能后果": Category.performance,
    "不必要的复杂度": Category.complexity,
}

OUT_OF_WHITELIST_LABEL = "白名单外"

CONCLUSIONS_EFFECTIVE = ("有效-已修改", "有效-未修改")
CONCLUSION_EXCLUDED = "无法判断"
NOISE_CONCLUSIONS = (
    "正确但无价值",
    "风格偏好",
    "与 Sonar 重复",
    "误报-事实错误",
    "误报-缺上下文",
    "既有问题",
)
NOISE_TYPES = (
    "风格格式",
    "命名",
    "复杂度或重构建议",
    "加注释或加测试建议",
    "空值等防御性检查",
    "与 Sonar 重复",
    "事实错误",
    "缺上下文误判",
    "既有问题",
    "过度泛化",
    "其他",
)
DEFECT_SOURCES = ("hotfix", "revert", "事故单", "其他")
OLD_TOOL_FLAGS = ("是", "部分", "否")
CHANGE_TYPES = ("新功能", "缺陷修复", "重构", "配置变更", "依赖升级", "其他")
YES_NO_UNKNOWN = ("是", "否", "未知")

DATASET_C_FIELDS = ("repo", "base", "head", "note")
DATASET_C_ALIASES = {"仓库": "repo", "备注": "note", "base_sha": "base", "head_sha": "head"}


class Severity(str, Enum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


class Verdict(str, Enum):
    confirmed = "confirmed"
    rejected = "rejected"
    undecided = "undecided"


class IntroducedIn(str, Enum):
    this_change = "this_change"
    preexisting = "preexisting"
    unknown = "unknown"


class RunStatus(str, Enum):
    passed = "passed"
    partial = "partial"
    failed = "failed"


def norm_path(path: str | None) -> str:
    text = (path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") and not text.startswith("//"):
        text = text.lstrip("/")
    return text


def category_key(label: str | None) -> str | None:
    text = (label or "").strip()
    found = CATEGORY_FROM_LABEL.get(text)
    if found is None:
        return None
    return found.value


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    ref: str
    note: str = ""


class Location(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str
    start_line: int
    end_line: int


class Verification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Verdict
    checked: list[str] = Field(default_factory=list)
    reason: str = ""


class Finding(BaseModel):
    """One candidate after schema validation, with verification filled in later."""

    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    category: Category
    severity: Severity
    location: Location
    title: str = ""
    trigger: str
    consequence: str = ""
    suggestion: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    verification: Verification | None = None
    introduced_in: IntroducedIn = IntroducedIn.unknown
    snippet: str = ""
    symbol: str = "unknown"
    rule: str = ""
    level: str = ""
    replacement: str | None = None
    edits: list[str] = Field(default_factory=list)
    equivalence: str = ""
    performance_note: str = ""
    locked_by: list[str] = Field(default_factory=list)


class LineRange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: int
    end: int


class FileSpan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    status: str = ""
    added: int = 0
    deleted: int = 0
    ranges: list[LineRange] = Field(default_factory=list)
    reason: str = ""
    old_path: str | None = None

    @property
    def changed_lines(self) -> int:
        return self.added + self.deleted


class Budget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_tool_calls_per_perspective: int = 2
    wall_clock_seconds: int = 600
    max_files: int = 40
    max_changed_lines: int = 2000
    cost_cap: float | None = None
    cost_per_1k_tokens: float = 0.0


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total: int = 0


class Coverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ci: str
    ticket: str


class DroppedCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str
    category: str | None = None
    file: str | None = None
    trigger: str | None = None


class ReportSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    s1_undecided_pending_human: list[Finding] = Field(default_factory=list)
    note: str = ""


class ComplexityStats(BaseModel):
    """Counted from git without a model. Test files are left out."""

    model_config = ConfigDict(extra="ignore")

    net_added: int = 0
    new_files: list[str] = Field(default_factory=list)
    new_symbols: list[str] = Field(default_factory=list)
    single_use: list[str] = Field(default_factory=list)
    new_env_reads: int = 0
    exceeded: list[str] = Field(default_factory=list)
    complete: bool = True


class RunReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: RunStatus
    base_sha: str
    head_sha: str
    head_kind: str = "commit"
    prompt_version: str
    budget: Budget
    reuse_key: str = ""
    reviewer_model: str | None = None
    verifier_model: str | None = None
    reviewed: list[FileSpan] = Field(default_factory=list)
    unreviewed: list[FileSpan] = Field(default_factory=list)
    coverage: Coverage
    confirmed: list[Finding] = Field(default_factory=list)
    rejected: list[Finding] = Field(default_factory=list)
    undecided: list[Finding] = Field(default_factory=list)
    summary: ReportSummary = Field(default_factory=ReportSummary)
    elapsed_seconds: float = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost: float = 0
    budget_exhausted: bool = False
    partial_reasons: list[str] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    error: str | None = None
    reused: bool = False
    policy_source: str = "default"
    policy_note: str = ""
    fail_on_must_fix: bool = False
    fail_check: bool = False
    fail_reasons: list[str] = Field(default_factory=list)
    complexity: ComplexityStats | None = None


_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+([A-Za-z_$][\w$]*)\s*\("
    ),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)\b"),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    ),
    re.compile(r"^\s*func\s+(?:\([^)\n]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(
        r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:[\w.<>,\[\]?]+\s+){1,6}([A-Za-z_][A-Za-z0-9_]*)\s*\("
    ),
)


def normalize_snippet(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def nearest_symbol(source: str, start_line: int) -> str:
    """Regex approximation of the enclosing function or class. No tree-sitter."""
    lines = source.splitlines()
    if not lines:
        return "unknown"
    index = min(max(start_line, 1), len(lines)) - 1
    for cursor in range(index, -1, -1):
        line = lines[cursor]
        for pattern in _SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                return match.group(1)
    return "unknown"


def fingerprint(category: str, path: str, symbol: str, snippet: str) -> str:
    """Stable id. Line numbers are intentionally not part of the input."""
    symbol_text = (symbol or "").strip() or "unknown"
    payload = "\n".join(
        [category, norm_path(path), symbol_text, normalize_snippet(snippet)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_conclusion(value: str | None) -> str | None:
    """Map a 结论 cell onto effective / noise / excluded.

    Empty cells are unlabeled. 无法判断 is excluded from the denominator and
    is not dataset A. The two 有效-* values are effective. Every other
    non-empty conclusion is noise, including labels the template does not
    list yet, so a new dropdown value cannot silently disappear.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text in CONCLUSIONS_EFFECTIVE:
        return "effective"
    if text == CONCLUSION_EXCLUDED:
        return "excluded"
    return "noise"


def dump_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
