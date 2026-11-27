"""Rule catalog: the sub-rules a REVIEW.toml table can switch on, off, or grade one by one."""

from __future__ import annotations

from pairrot.models import Category

OFF = "off"
SEVERITY_RANK = {"S1": 1, "S2": 2, "S3": 3}

# table -> rule -> what the reviewer is told the rule covers. Order is the order shown in prompts.
CATALOG: dict[str, dict[str, str]] = {
    "correctness": {
        "logic_error": "逻辑错误：条件、边界、计算、状态转换或返回值错误",
        "error_handling": "错误处理缺失：吞掉异常、漏处理失败分支、资源或锁没有释放",
    },
    "secrets": {
        "private_key": "私钥",
        "token": "带固定前缀的令牌，例如 AKIA、ghp_、sk-、xoxb-、Bearer",
        "credential": "赋给 password、secret、api_key 等变量的明文值",
    },
    "prompt_injection": {
        "override": "要求忽略或覆盖已有指令、系统提示或规则",
        "exfiltration": "读取凭据并送出仓库",
        "reviewer_bypass": "要求评审工具不要报告或把问题标成安全",
        "hidden_text": "零宽字符、双向控制字符，或藏在注释里的指令",
        "tag_escape": "正文里出现数据标签，用来提前结束送给模型的数据区",
    },
    "risky_patterns": {
        "remote_script": "下载脚本后直接交给 shell 执行",
        "tls_off": "关掉 TLS 证书校验",
        "plaintext_http": "用明文 http 访问外部地址",
        "dangerous_call": "eval、exec、pickle.loads、不安全的 yaml.load、shell=True 或 os.system",
        "loose_perms": "把权限放宽到所有用户可写",
        "ci_workflow": "工作流检出 PR 代码、放开全部权限，或把密钥写进日志",
    },
    "security": {
        "injection": "注入：把外部输入拼进 SQL、命令、路径或模板",
        "authz": "越权：缺少登录、归属或权限校验",
        "sensitive_data": "敏感数据写进日志、错误信息或响应",
        "timing_compare": "用 == 比较令牌、签名或密码摘要",
        "other": "其他能说出利用路径的安全问题",
    },
    "performance": {
        "n_plus_one": "循环里逐条查询数据库或调用远程接口",
        "quadratic": "随输入规模平方增长的循环、列表成员判断或字符串拼接",
        "repeated_io": "循环里重复读同一个文件、重复解析同一份数据",
        "other": "其他能说出输入规模和后果的性能问题",
    },
    "compatibility": {
        "contract_break": "改了约定，但仓库内仍有按旧约定使用的地方，或破坏了稳定接口",
        "claim_mismatch": "函数名、文档、注释、帮助文本或 README 说的行为，和代码实际做的不一样",
    },
    "simplicity": {
        "stdlib": "标准库、语言内置或项目已有依赖能替代的手写代码",
        "dead_code": "不可达的分支、没人传的参数、没人用的别名或兼容代码",
        "needless_abstraction": "只有一个实现的抽象、工厂或策略，只转发的包装层",
        "duplicate": "重复实现仓库里已有的函数，或本次 diff 里的复制粘贴",
        "ceremony": "多余的重试、捕获后原样重抛、不会失败的兜底",
        "other": "其他能给出替换代码的不必要复杂度",
    },
}

_TABLE_OF = {
    Category.logic_error: "correctness",
    Category.error_handling: "correctness",
    Category.security: "security",
    Category.performance: "performance",
    Category.contract_break: "compatibility",
    Category.claim_mismatch: "compatibility",
    Category.complexity: "simplicity",
}
# Categories whose rule is the category itself, so the reviewer does not tag them.
_SELF_RULED = {Category.logic_error, Category.error_handling, Category.contract_break, Category.claim_mismatch}
# A deterministic finding's checked mark selects its table. The category alone would land on security.
CHECKED_TABLE = {
    "secret_pattern": "secrets",
    "injection_pattern": "prompt_injection",
    "policy_guard": "prompt_injection",
    "risky_pattern": "risky_patterns",
}
# Off unless a REVIEW.toml turns it on. The table level would otherwise enable it.
DEFAULT_RULE_LEVELS = {"compatibility.claim_mismatch": OFF}
# Instruction files. A REVIEW.toml paths list adds to this; it cannot remove an entry.
DEFAULT_INSTRUCTION_PATHS = (
    "SKILL.md",
    "*/SKILL.md",
    "AGENTS.md",
    "*/AGENTS.md",
    "CLAUDE.md",
    "*/CLAUDE.md",
    ".cursorrules",
    ".cursor/rules/**",
    "*.mdc",
    ".github/copilot-instructions.md",
    "skills/**",
    "*/skills/**",
)


def table_of(category: Category, *, table: str | None = None) -> str | None:
    return table if table is not None else _TABLE_OF.get(category)


def checked_table(checked: tuple[str, ...] | list[str]) -> str | None:
    """The policy table a deterministic finding belongs to, from its verification marks."""
    for mark in checked:
        found = CHECKED_TABLE.get(mark)
        if found:
            return found
    return None


def rule_for(category: Category, raw: object) -> str:
    """The rule a reviewer finding belongs to. Unknown or missing tags fall back to other when the table has one."""
    if category in _SELF_RULED:
        return category.value
    table = _TABLE_OF.get(category)
    rules = CATALOG.get(table or "", {})
    text = str(raw or "").strip()
    if text in rules:
        return text
    return "other" if "other" in rules else ""


def secret_rule(label: str) -> str:
    return label if label in {"private_key", "token"} else "credential"


def valid_selector(text: str) -> bool:
    table, _, rule = text.partition(".")
    if table not in CATALOG:
        return False
    return not rule or rule in CATALOG[table]
