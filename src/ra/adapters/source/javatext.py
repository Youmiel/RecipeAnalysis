"""极简 Java 词法/结构工具。

目标不是「解析 Java」，而是可靠地做三件事：
1. 找到方法体（按花括号配对）
2. 切分调用参数（按逗号，跳过嵌套括号与字符串）
3. 取 `new X(...)` 的实参列表

刻意不做的事：不建 AST、不解析泛型、不处理 lambda 内部语义。
需要 AST 的时候说明该机制属于「过程式逻辑」，应进人工例外清单，
而不是把提取器写成半成品编译器。
"""

from __future__ import annotations

from dataclasses import dataclass


def strip_comments(text: str) -> str:
    """去掉行注释与块注释，保留字符串字面量内容。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def match_brace(text: str, open_index: int) -> int:
    """从 `{` 位置起找配对的 `}`，返回 `}` 的下标；找不到返回 -1。"""
    if open_index >= len(text) or text[open_index] != "{":
        return -1
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_method_body(text: str, signature_pattern: str) -> str | None:
    """找匹配签名的方法体内容（不含外层花括号）。"""
    import re

    match = re.search(signature_pattern, text)
    if match is None:
        return None
    brace = text.find("{", match.end())
    if brace < 0:
        return None
    close = match_brace(text, brace)
    if close < 0:
        return None
    return text[brace + 1 : close]


def split_args(text: str) -> list[str]:
    """按顶层逗号切分参数列表。跳过嵌套括号、字符串、泛型尖括号。"""
    parts: list[str] = []
    depth = 0
    # Generic brackets: `<` right after an identifier or `.` and before a type, `?` or `>`.
    angle = 0
    current: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "<" and i > 0 and (text[i - 1].isalnum() or text[i - 1] in "._") and (
            i + 1 < n and (text[i + 1].isupper() or text[i + 1] in "?>")
        ):
            angle += 1
        elif ch == ">" and angle:
            angle -= 1
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            current.append(text[i:j])
            i = j
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0 and angle == 0:
            parts.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


@dataclass(frozen=True)
class ConstructorCall:
    class_name: str
    args: tuple[str, ...]
    raw: str


def find_constructor_calls(text: str, class_name: str) -> list[ConstructorCall]:
    """找 `new ClassName(args)` 并按参数切分。"""
    import re

    pattern = re.compile(rf"new\s+{re.escape(class_name)}\s*\(")
    calls: list[ConstructorCall] = []
    for match in pattern.finditer(text):
        open_paren = text.index("(", match.start())
        close = match_paren(text, open_paren)
        if close < 0:
            continue
        inner = text[open_paren + 1 : close]
        calls.append(
            ConstructorCall(
                class_name=class_name,
                args=tuple(split_args(inner)),
                raw=text[match.start() : close + 1],
            )
        )
    return calls


def match_paren(text: str, open_index: int) -> int:
    """从 `(` 位置起找配对的 `)`，返回 `)` 的下标；找不到返回 -1。"""
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def java_ident(expr: str) -> str | None:
    """从 `Items.WHEAT` / `Blocks.OAK_PLANKS` / `Item.byBlock(Blocks.X)` 里取注册名。"""
    import re

    match = re.search(r"(?:Items|Blocks)\.([A-Z][A-Z0-9_]*)", expr)
    if match is None:
        return None
    return match.group(1).lower()
