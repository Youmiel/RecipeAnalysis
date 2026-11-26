"""物品放置成方块：物品 ↔ 方块的注册期对应。

「拿着 X 右键，地上出现方块 Y」是方块物品的通用路径，也是判断**哪些物品是方块物品**
的唯一权威来源：jar 报告的 `blocks.json` 只有方块状态（`properties` / `states`），
不含对应物品；默认组件里也没有 `minecraft:block` 组件。

这层对应不能靠名字推断——`stone` 物品对应 `stone` 方块，但 `redstone` 物品对应
`redstone_wire` 方块，`stick` 物品根本没有方块。按名字匹配会把 `stick` 判成可放置，
把 `redstone` 判成不可放置。

## 两版写法

```java
// 26.3
Item STONE = Items.registerBlock(BlockItemIds.STONE, Blocks.STONE);
Item REDSTONE = Items.registerBlock(BlockItemIds.REDSTONE_DUST, Blocks.REDSTONE_WIRE, ...);
Item RESIN_CLUMP = Items.registerItem(BlockItemIds.RESIN_CLUMP,
                                      Items.createBlockItemWithCustomItemName(Blocks.RESIN_CLUMP));

// 1.18.2
Item STONE = Items.registerBlock(Blocks.STONE, CreativeModeTab.TAB_BUILDING_BLOCKS);
Item ANCIENT_DEBRIS = Items.registerBlock(new BlockItem(Blocks.ANCIENT_DEBRIS, ...));
```

两种写法都满足：**取 `registerBlock(...)` / `createBlockItemWithCustomItemName(...)`
参数里的第一个 `Blocks.X`**。物品名取字段名（`Items.<FIELD>` 的 `FIELD`），
26.3 的 `BlockItemIds.X` 不是注册名（`REDSTONE_DUST` → 物品 `redstone`），不能用。

## 形状

```
block:minecraft:redstone_wire  ←place←  item:minecraft:redstone
```

`via` 为空（放置不需要中介者），`inputs` 为空（放置不消耗额外的物品——
被放置的那件物品本身由 IR 的目标 `head` 表达，不重复计入前置）。

物品名靠字段名小写推出，属**推断**，因此反编译源码适配器收尾时会与 `registry` 事实
（物品全集）对账，推错即 `failed`。
"""

from __future__ import annotations

import re

from ..identifiers import block_item_names, parse_constants
from ..index import JavaFile, JavaIndex
from .base import Idiom, IdiomResult, Link

ID = "place_source"

# `FIELD = Items.registerBlock(` / `FIELD = Items.registerItem(`
REGISTER_DECL = re.compile(
    r"([A-Z][A-Z0-9_]*)\s*=\s*(?:(Items)\.)?(?:registerBlock|registerItem)\s*\("
)

# 放置目标的方块：`Blocks.X`。`createBlockItemWithCustomItemName(Blocks.X)` 也含这个形态。
BLOCK_ARG = re.compile(r"Blocks\.([A-Z][A-Z0-9_]*)")

# 26.3 的注册名常量：`Items.registerBlock(BlockItemIds.STONE, ...)`
ITEM_ID_ARG = re.compile(r"BlockItemIds\.([A-Z][A-Z0-9_]*)")


def _call_args(text: str, open_index: int) -> str:
    """`(` 起到配对 `)` 之间的实参文本（含两端括号）。"""
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_index : i + 1]
        i += 1
    return text[open_index:]


def _first_block_arg(text: str, open_index: int) -> str | None:
    """`registerBlock(` 参数里的**第一个** `Blocks.X`。

    取第一个而不是任一个：26.3 的 `TORCH` 在 lambda 里还引用了 `WALL_TORCH`,
    那是附带的墙面版本，不是放置目标。
    """
    match = BLOCK_ARG.search(_call_args(text, open_index))
    if match is None:
        return None
    # 在 `new BlockItem(...)` 与普通参数两种形态下都成立
    return match.group(1)


class PlaceSource(Idiom):
    id = ID
    needs = ("Items.registerBlock",)
    # 26.3 实测 826 条、1.18.2 758 条；低于 600 说明匹配退化。
    min_hits = 600

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        constants = parse_constants(index)
        if not constants:
            result.note(
                "没有解析到 references/ 下的注册名常量（旧版本无此结构），"
                "物品名全部退回字段名小写，需由对账确认"
            )
        for java in index.files.values():
            if java.stem != "Items" and "Items.registerBlock" not in java.text and "Items.registerItem" not in java.text:
                continue
            self._file(java, constants, result)
        return result

    def _file(
        self, java: JavaFile, constants: dict[str, str], result: IdiomResult
    ) -> None:
        text = java.text
        produced = 0
        non_block: list[str] = []
        for match in REGISTER_DECL.finditer(text):
            if match.group(2) is None and java.stem != "Items":
                continue
            field = match.group(1)
            args = _call_args(text, match.end() - 1)
            block = _first_block_arg(text, match.end() - 1)
            if block is None:
                # 不是方块物品（AIR、SADDLE、MINECART 这类）。26.3 实测 365 条，
                # 逐条写台账会让台账按注册条目爆炸；聚合为一条汇总。
                non_block.append(field)
                continue

            id_match = ITEM_ID_ARG.search(args)
            inferred = True
            if id_match is not None:
                const_block, item, inferred = block_item_names(constants, id_match.group(1), field)
                # 常量若同时给了方块名，以常量为准（它比 `Blocks.X` 的字段名更权威）
                block = const_block
            else:
                item = field.lower()

            produced += 1
            result.add(
                Link(
                    head=f"block:minecraft:{block.lower()}",
                    kind="places",
                    meta={
                        "item": f"item:minecraft:{item}",
                        "same_name": item == block.lower(),
                        "item_name_inferred": inferred,
                    },
                ),
                ID,
                java.path,
                f"Items#{field} -> Blocks.{block}",
            )

        if non_block:
            sample = "、".join(non_block[:6])
            more = "等" if len(non_block) > 6 else ""
            result.note(
                f"{java.path}: {len(non_block)} 个注册不是方块物品（{sample}{more}），"
                "参数里无 Blocks.X，不产出 places 链接"
            )
        if produced == 0 and not non_block:
            result.note(f"{java.path}: 有注册调用但没有解析出任何放置目标")


def exported() -> list[Idiom]:
    return [PlaceSource()]
