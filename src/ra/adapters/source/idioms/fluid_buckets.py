"""流体桶：空桶装液体或成品方块，得到满桶。

`mob_buckets` 覆盖的是 `MobBucketItem(实体, 流体)`——装的是生物，`via` 是那个实体。
本 idiom 覆盖**不装生物**的两类：

```java
// 26.3 Items.java
Item LAVA_BUCKET = Items.registerItem(ItemIds.LAVA_BUCKET,
    (Item.Properties p) -> new BucketItem(Fluids.LAVA, (Item.Properties)p), ...);

Item POWDER_SNOW_BUCKET = Items.registerItem(BlockItemIds.POWDER_SNOW,
    (Item.Properties p) -> new SolidBucketItem(Blocks.POWDER_SNOW, SoundEvents.BUCKET_EMPTY_POWDER_SNOW, ...));
```

构造语义是「空桶 + 流体/方块 → 装好的桶」，所以 `via` 是那个被装进去的
`block:` 节点：熔岩桶要 `block:minecraft:lava`，细雪桶要 `block:minecraft:powder_snow`。
与 `mob_buckets` 的 `entity:` 同构：都是「容器里装了什么东西」。

## 为什么不是 `mob_buckets` 的一支

`mob_buckets` 的每个条目都必须有实体（它的 `head` 是装生物的桶，`via` 是生物），
那正是它存在的理由。混进来会让「这个桶装什么」的字段在两种语义间摇摆。
分成两个 idiom，台账也能分开计数。

## 判据

1. 文件里有 `Items.registerItem(`（注册点）
2. 构造调用是 `new BucketItem(Fluids.X, ...)` 或 `new SolidBucketItem(Blocks.Y, ...)`

物品名取法沿用 `mob_buckets` 的三级优先：字面量 > `ItemIds.X` / `BlockItemIds.Y` 常量 > 字段名小写。
内容节点按常量名小写取——`Fluids.WATER` → `block:minecraft:water`。
常量名与注册名不一一对应的情况由 `verify_fluid_bucket_blocks` 对账，
不在注册表里的把 `via` 揟下并留台账，不让桶静默变不可达。
"""

from __future__ import annotations

import re

from ..identifiers import item_name, parse_constants
from ..index import JavaFile, JavaIndex
from .base import Idiom, IdiomResult, Link

ID = "fluid_buckets"

# 两种构造器。分组分别是流体名 / 方块名。
FLUID_BUCKET = re.compile(r"new\s+BucketItem\s*\(\s*Fluids?\.([A-Z][A-Z0-9_]*)")
SOLID_BUCKET = re.compile(r"new\s+SolidBucketItem\s*\(\s*Blocks\.([A-Z][A-Z0-9_]*)")

REGISTER_DECL = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(?:(Items)\.)?registerItem\s*\(")
ITEM_ID_CONST = re.compile(r"ItemIds\.([A-Z][A-Z0-9_]*)")
BLOCK_ITEM_ID_CONST = re.compile(r"BlockItemIds\.([A-Z][A-Z0-9_]*)")

# 空桶：所有流体桶的直接原料，不枚举也无法从代码取（注册名就叫 bucket）
EMPTY_BUCKET = "item:minecraft:bucket"


def _call_args(text: str, open_index: int) -> str:
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


class FluidBuckets(Idiom):
    id = ID
    needs = ("BucketItem", "SolidBucketItem")
    # 26.3 实测 2 条（熔岩桶 + 细雪桶）；低于 1 说明匹配退化。
    min_hits = 1

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        constants = parse_constants(index)
        for java in index.files.values():
            if "BucketItem" not in java.text or "registerItem" not in java.text:
                continue
            self._file(java, constants, result)
        return result

    def _file(self, java: JavaFile, constants: dict[str, object], result: IdiomResult) -> None:
        text = java.text
        registers: dict[str, tuple[int, str]] = {}
        for match in REGISTER_DECL.finditer(text):
            if match.group(2) is None and java.stem != "Items":
                continue
            registers[match.group(1)] = (match.start(), _call_args(text, match.end() - 1))
        if not registers:
            return

        produced = 0
        for pattern, kind in ((FLUID_BUCKET, "fluid"), (SOLID_BUCKET, "block")):
            for match in pattern.finditer(text):
                content = match.group(1)
                if kind == "fluid" and content == "EMPTY":
                    # `new BucketItem(Fluids.EMPTY)` 就是空桶本身；
                    # 给它挂 "空桶←空桶" 只会造出一条永远不成立的规则
                    result.note(f"{java.path}: Fluids.EMPTY 是空桶自身，不产规则")
                    continue

                owner = self._owner_before(registers, match.start())
                if owner is None:
                    result.note(
                        f"{java.path}: {content} 的桶构造找不到所属注册字段，跳过"
                    )
                    continue
                name, inferred = self._bucket_name(registers[owner][1], owner, constants)
                produced += 1
                result.add(
                    Link(
                        head=f"item:minecraft:{name}",
                        kind="yield",
                        # 空桶是消耗品：装完之后它不在手里了
                        inputs=(EMPTY_BUCKET,),
                        # 被装进去的东西：流体常量与方块常量都按名字取下写
                        via=(f"block:minecraft:{content.lower()}",),
                        meta={
                            "content_kind": kind,
                            "content": content,
                            "bucket_name_inferred": inferred,
                        },
                    ),
                    ID,
                    java.path,
                    f"Items#{owner} -> {kind}({content})",
                )

        if produced == 0:
            result.note(f"{java.path}: 有 BucketItem 但没有解析出可用的注册")

    @staticmethod
    def _owner_before(registers: dict[str, tuple[int, str]], index: int) -> str | None:
        best: str | None = None
        best_pos = -1
        for name, (pos, _) in registers.items():
            if pos < index and pos > best_pos:
                best_pos = pos
                best = name
        return best

    @staticmethod
    def _bucket_name(args: str, owner: str, constants: dict[str, object]) -> tuple[str, bool]:
        literal = re.search(r'\(\s*"([a-z0-9_]+)"', args)
        if literal is not None:
            return literal.group(1), False

        const = ITEM_ID_CONST.search(args) or BLOCK_ITEM_ID_CONST.search(args)
        if const is not None:
            # `item_name` 自己返回 `(名字, 是否推断)`，不要再包一层
            return item_name(constants, const.group(1), owner)  # type: ignore[arg-type]

        return owner.lower(), True


def exported() -> list[Idiom]:
    return [FluidBuckets()]
