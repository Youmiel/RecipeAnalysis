"""Java 侧标识符常量 → 注册名。

26.x 把注册名收进 `references/` 下的常量类：

```java
public static final BlockItemId STONE = BlockItemId.create("stone");
public static final BlockItemId REDSTONE_DUST = BlockItemId.create("redstone_wire", "redstone");
```

**双参数形式是关键**：第一个是方块名，第二个是物品名。`REDSTONE_DUST` 这种
常量自己就说明了「物品 `redstone` 放置成方块 `redstone_wire`」，不需要任何推断。
单参数形式表示两者同名。

常量名本身**不可靠**。实测三个反例：

| 常量名 | 方块名 | 物品名 | 差异 |
|---|---|---|---|
| `CUT_STANDSTONE_SLAB` | `cut_sandstone_slab` | 同 | Mojang 的字段名拼写错误（`standstone`） |
| `DRY_SHORT_GRASS` | `short_dry_grass` | 同 | 词序不同 |
| `DRY_TALL_GRASS` | `tall_dry_grass` | 同 | 词序不同 |

所以凡是要拿注册名的地方，都先查这张表。查不到才退回「常量名小写」，
且退回的情况由调用方对账。旧版本（1.18.2）没有这些常量类，只能全部退回。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .index import JavaIndex

# `FIELD = Cls.create("name")` 或 `FIELD = Cls.create("block", "item")`
CONSTANT_DECL = re.compile(
    r'([A-Z][A-Z0-9_]*)\s*=\s*(?:\w+\.)?create\(\s*"([a-z0-9_/]+)"(?:\s*,\s*"([a-z0-9_/]+)")?'
)

# 解析哪些类：类名 → 前缀
PREFIXES = {
    "BlockItemIds": "block_item",
    "ItemIds": "item",
}


@dataclass(frozen=True)
class Names:
    """常量给出的注册名。`single` 表示是单参数形式（方块名与物品名相同）。"""

    block: str
    item: str
    single: bool


def parse_constants(index: JavaIndex) -> dict[str, Names]:
    """扫 `references/` 下的常量类，返回 `{前缀:常量名 → Names}`。

    两类的常量名可能重名（`AIR` 在 `BlockItemIds` 与 `ItemIds` 里都有），
    所以 key 带前缀：`block_item:AIR` / `item:AIR`。
    """
    out: dict[str, Names] = {}
    for stem, prefix in PREFIXES.items():
        text = index.identifier_text(stem)
        if text is None:
            continue
        for field, first, second in CONSTANT_DECL.findall(text):
            if second:
                out[f"{prefix}:{field}"] = Names(block=first, item=second, single=False)
            else:
                out[f"{prefix}:{field}"] = Names(block=first, item=first, single=True)
    return out


def block_item_names(
    constants: dict[str, Names], constant: str, fallback_field: str
) -> tuple[str, str, bool]:
    """`BlockItemIds.X` → `(方块名, 物品名, 是否为推断)`。

    查不到时把字段名小写同时当作方块名与物品名，`inferred` 置真，由调用方对账。
    """
    found = constants.get(f"block_item:{constant}")
    if found is not None:
        return found.block, found.item, False
    guess = fallback_field.lower()
    return guess, guess, True


def item_name(
    constants: dict[str, Names], constant: str, fallback_field: str
) -> tuple[str, bool]:
    """`ItemIds.X` → `(物品名, 是否为推断)`。

    只认 `item:` 前缀：`BlockItemIds` 的条目同时描述方块，混用会把方块名当物品名。
    """
    found = constants.get(f"item:{constant}")
    if found is not None:
        return found.item, False
    return fallback_field.lower(), True
