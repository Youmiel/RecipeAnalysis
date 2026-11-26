"""方块借用另一个方块的战利品表。

`wall_torch` 挖掉掉火把、各种 `*_wall_sign` 掉对应的立牌——它们自己**没有**
战利品表，而是在注册时挂上另一个方块的表。数据包看不到这层关系（`loot_table`
里既没有 `wall_torch` 也没有别名声明），只能从代码取。

## 两版写法

```java
// 1.18.2：Properties 上链式调用 dropsLike
public static final Block WALL_TORCH = Blocks.register("wall_torch",
    new WallTorchBlock(BlockBehaviour.Properties.of(...).dropsLike(Blocks.TORCH)));

// 26.3：register 的第三个参数是 wallVariant
public static final Block WALL_TORCH = Blocks.register(BlockIds.WALL_TORCH,
    (BlockBehaviour.Properties p) -> new WallTorchBlock(ParticleTypes.FLAME, p),
    Blocks.wallVariant(Blocks.TORCH));
```

两者都表达了同一件事：**这个方块的掉落照抄另一个方块**。

## 形状

与剪毛在 26.3 的 `holds` 完全同构，因此复用 `code_link`：

```
table:minecraft:blocks/torch  ←holds←  block:minecraft:wall_torch
```

`head` 用 `table:` 前缀指被借用的表，`via` 是借用者（方块存在即可，不需消耗），
`inputs` 为空——挖方块不需要投入。

表 ID 由被借用方块的名字推出（`blocks/<name>`），这个推断是**可校验**的：
反编译源码适配器在收尾时把 `table:` 开头的 head 与 `loot_table` 事实比对，
不存在的记进台账，不静默留悬空边。
"""

from __future__ import annotations

import re

from ..index import JavaFile, JavaIndex
from .base import Idiom, IdiomResult, Link

ID = "loot_alias"

# `FIELD = Blocks.register(` —— 字段名即注册名（两版都成立）
REGISTER_DECL = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(?:Blocks\.)?register\s*\(")

# 借用声明：1.18.2 是 `dropsLike(Blocks.X)`，26.3 是 `wallVariant(X, true)`。
# 26.3 的 X 是裸标识符（`Blocks` 类内部指本类字段），且带第二个布尔参数。
ALIAS_CALL = re.compile(
    r"(?:dropsLike|wallVariant|grassVariant|dirtVariant)"
    r"\s*\(\s*(?:Blocks\.|BlockIds\.)?([A-Z][A-Z0-9_]*)\s*[,)]"
)


def _match_paren(text: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


class LootAliases(Idiom):
    id = ID
    needs = ("Blocks.register",)
    # 1.18.2 实测 43 处、26.3 47 处；低于 20 说明匹配退化。
    min_hits = 20

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        for java in index.files.values():
            if java.stem != "Blocks" and "Blocks.register" not in java.text:
                continue
            self._file(java, result)
        return result

    def _file(self, java: JavaFile, result: IdiomResult) -> None:
        text = java.text
        for match in REGISTER_DECL.finditer(text):
            if java.stem != "Blocks" and "Blocks.register" not in match.group():
                continue
            owner = match.group(1).lower()
            close = _match_paren(text, match.end() - 1)
            if close < 0:
                result.note(f"{java.path}#{match.group(1)}: register 调用括号不配对，跳过")
                continue
            call = text[match.end() : close]
            aliases = ALIAS_CALL.findall(call)
            if not aliases:
                continue
            if len(aliases) > 1:
                result.note(
                    f"{java.path}#{match.group(1)}: 注册时声明了 {len(aliases)} 个借用目标 "
                    f"{aliases}，语义不明，整条跳过"
                )
                continue
            borrowed = aliases[0].lower()
            if borrowed == owner:
                result.note(f"{java.path}#{match.group(1)}: 借用目标是自身，跳过")
                continue
            result.add(
                Link(
                    head=f"table:minecraft:blocks/{borrowed}",
                    kind="holds",
                    via=(f"block:minecraft:{owner}",),
                    meta={"alias_of": f"block:minecraft:{borrowed}", "table_guessed": True},
                ),
                ID,
                java.path,
                f"Blocks#{match.group(1)} -> dropsLike/wallVariant({borrowed})",
            )


def exported() -> list[Idiom]:
    return [LootAliases()]
