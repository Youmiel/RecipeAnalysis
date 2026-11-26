"""生物桶：`MobBucketItem` 注册的物品与它承载的实体。

把蝌蚪装进桶里的路径，数据包完全不含：`tadpole_bucket` 既没有配方（不是合成出来的），
也没有战利品表（不是掉落物），只能从代码的注册语句取。

## 判据

```java
// 26.3
Item TADPOLE_BUCKET = Items.registerItem(ItemIds.TADPOLE_BUCKET,
    (Item.Properties p) -> new MobBucketItem(EntityTypes.TADPOLE, Fluids.WATER, ...));

// 1.18.2（还只有水生的五种）
Item TADPOLE_BUCKET = Items.registerItem("tadpole_bucket",
    new MobBucketItem(EntityType.TADPOLE, Fluids.WATER, ...));
```

一条 `MobBucketItem(实体, 流体, ...)` 就够：实体是被装的东西，流体决定用哪个空桶。
两版构造器签名一致，只是 `EntityTypes` / `EntityType` 与 `Entities.X` / `EntityType.X`
两种前缀写法不同。

## 形状

```
item:minecraft:tadpole_bucket  ←yield←  entity:minecraft:tadpole (via) + item:minecraft:water_bucket (inputs)
```

- `head` 是桶本身（可获得的物品）
- `via` 是被装的实体：要它存在，但不消耗它，也不投入它
- `inputs` 是空桶：必须已有，且**会被消耗**（换成装满的桶）

这正是 `via` 与 `inputs` 必须分开的例子：蝌蚪不存在要去生成，空桶不存在路径就断。

## 物品名怎么来

优先级：注册调用里的**字符串字面量** > `ItemIds.X` 常量 > 字段名小写。

1.18.2 只有字面量（`registerItem("tadpole_bucket", ...)`），最准。
26.3 改成常量，`ItemIds.TADPOLE_BUCKET = ItemId.create("tadpole_bucket")` 同样准确。
两者都没有时才退回字段名，并由调用方对账。

## 流体 → 空桶

| 流体 | 空桶 |
|---|---|
| `Fluids.WATER` | `water_bucket` |
| `Fluids.LAVA` | `lava_bucket` |

`Fluids.EMPTY` 不产 `inputs`：26.3 的 `SULFUR_CUBE_BUCKET` 用它，倒出来不是流体。
这种情况不猜「大概也是水桶」，记入 `meta.fluid` 由下游判读。
"""

from __future__ import annotations

import re

from ..identifiers import item_name, parse_constants
from ..index import JavaFile, JavaIndex
from .base import Idiom, IdiomResult, Link

ID = "mob_buckets"

# `FIELD = Items.registerItem(
REGISTER_DECL = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(?:(Items)\.)?registerItem\s*\(")

# 26.3：`ItemIds.TADPOLE_BUCKET`；1.18.2：字符串字面量
ITEM_ID_CONST = re.compile(r"ItemIds\.([A-Z][A-Z0-9_]*)")
ITEM_ID_LITERAL = re.compile(r'registerItem\s*\(\s*"([a-z0-9_]+)"')

# `new MobBucketItem(EntityTypes.TADPOLE, Fluids.WATER, ...)`
#
# 26.3 在参数外带了泛型强制转换：`(EntityType<? extends Mob>)EntityTypes.TADPOLE`。
# 不吞掉这个 cast 会一条也匹配不到（实测：零命中）。
MOB_BUCKET = re.compile(
    r"new\s+MobBucketItem\s*\(\s*(?:\([^()]*(?:\([^()]*\))?[^()]*\)\s*)?"
    r"(?:EntityTypes|Entities|EntityType)\.([A-Z][A-Z0-9_]*)\s*,"
    r"\s*Fluids?\.([A-Z][A-Z0-9_]*)"
)

# 流体 → 空桶。只登记实测确认过的两种，其余留空让下游判读。
EMPTY_BUCKET = {
    "WATER": "water_bucket",
    "LAVA": "lava_bucket",
}


def _call_args(text: str, open_index: int) -> str:
    """`(` 起到配对 `)` 之间（含两端）。"""
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


def _registry_name(constant: str) -> str:
    """注册常量名 → 注册名。

    两种形态：
    - 全大写常量（`EntityTypes.TADPOLE`、`PUFFERFISH`）→ 直接小写
    - 驼峰类名（`EntityType.Pig`）→ 驼峰转下划线

    实测教训：不分情况一律做驼峰转换，会把 `PUFFERFISH` 拆成
    `p_u_f_f_e_r_f_i_s_h`——每个大写字母前都插了下划线。
    """
    if constant.isupper():
        return constant.lower()
    out: list[str] = []
    for index, ch in enumerate(constant):
        if ch.isupper() and index:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


class MobBuckets(Idiom):
    id = ID
    needs = ("MobBucketItem",)
    # 26.3 实测 7 个（含硫磺方块桶）、1.18.2 5 个；低于 4 说明匹配退化。
    min_hits = 4

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        constants = parse_constants(index)
        for java in index.files.values():
            if "MobBucketItem" not in java.text:
                continue
            self._file(java, constants, result)
        return result

    def _file(
        self, java: JavaFile, constants: dict[str, object], result: IdiomResult
    ) -> None:
        text = java.text
        produced = 0

        # 先建「字段名 → (注册调用结束位置, 实参文本)」，避免每个桶都重新扫全文
        registers: dict[str, tuple[int, str]] = {}
        for match in REGISTER_DECL.finditer(text):
            if match.group(2) is None and java.stem != "Items":
                continue
            registers[match.group(1)] = (match.start(), _call_args(text, match.end() - 1))

        for match in MOB_BUCKET.finditer(text):
            entity = _registry_name(match.group(1))
            fluid = match.group(2)
            owner = self._owner_before(registers, match.start())
            if owner is None:
                result.note(
                    f"{java.path}: {entity} 的 MobBucketItem 找不到所属注册字段，跳过"
                )
                continue

            bucket, inferred = self._bucket_name(registers[owner][1], owner, constants)
            inputs: tuple[str, ...] = ()
            empty = EMPTY_BUCKET.get(fluid)
            if empty is not None:
                inputs = (f"item:minecraft:{empty}",)
            else:
                result.note(
                    f"{java.path}#{owner}: 流体 {fluid} 未登记对应的空桶，"
                    "不产 inputs（不猜）"
                )

            produced += 1
            result.add(
                Link(
                    head=f"item:minecraft:{bucket}",
                    kind="yield",
                    via=(f"entity:minecraft:{entity}",),
                    inputs=inputs,
                    meta={
                        "fluid": fluid,
                        "bucket_name_inferred": inferred,
                    },
                ),
                ID,
                java.path,
                f"Items#{owner} -> MobBucketItem({entity}, {fluid})",
            )

        if produced == 0:
            result.note(f"{java.path}: 有 MobBucketItem 但没有解析出可用的注册")

    @staticmethod
    def _owner_before(registers: dict[str, tuple[int, str]], index: int) -> str | None:
        """构造调用属哪个字段：声明位置在它之前且最靠后的那个。"""
        best: str | None = None
        best_pos = -1
        for name, (pos, _) in registers.items():
            if pos < index and pos > best_pos:
                best_pos = pos
                best = name
        return best

    @staticmethod
    def _bucket_name(
        args: str, owner: str, constants: dict[str, object]
    ) -> tuple[str, bool]:
        """注册名优先级：字面量 > ItemIds 常量 > 字段名小写。"""
        # `args` 含外层括号，所以字面量在 `(` 之后
        literal = re.search(r'\(\s*"([a-z0-9_]+)"', args)
        if literal is not None:
            return literal.group(1), False

        const = ITEM_ID_CONST.search(args)
        if const is not None:
            return item_name(constants, const.group(1), owner)  # type: ignore[arg-type]

        return owner.lower(), True


def exported() -> list[Idiom]:
    return [MobBuckets()]
