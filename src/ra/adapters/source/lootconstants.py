"""解析 `BuiltInLootTables` 里的常量 → 表 ID 映射。

代码侧大量用 `BuiltInLootTables.XXX` 而不是字符串，所以任何基于它的 idiom
都必须先有这张映射，否则会像我第一版那样猜错 ID。

实测两种注册形态（26.3）：

```java
public static final ResourceKey<LootTable> SHEAR_SHEEP = register("shearing/sheep");
public static final ColorCollection<ResourceKey<LootTable>> SHEAR_DYED_SHEEP =
        ColorCollection.NAMES.map(color -> register("shearing/sheep/" + color));
```

第二种是**按颜色集合展开**：一个常量代表 16 张表。展开后的表 ID 由
`register("前缀/" + color)` 里的前缀与颜色名拼出。颜色名不写死，
从 `ColorCollection.NAMES` 的实际取值处读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..source.index import JavaIndex
from ..source.javatext import strip_comments

# 单个常量：`X = register("path")`
# 两版类型不同：26.x 用 `ResourceKey<LootTable>`，1.18.x 用 `ResourceLocation`。
# 类型符只允许这两种，避免把 `ColorCollection<ResourceKey<LootTable>>` 误吞。
SINGLE = re.compile(
    r"static\s+final\s+(?:ResourceKey<LootTable>|ResourceLocation)\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"(?:BuiltInLootTables\.)?register\(\s*\"([^\"]+)\"\s*\)"
)

# 颜色集合：`X = ColorCollection.NAMES.map(color -> register("prefix/" + color))`
COLOR_COLLECTION = re.compile(
    r"static\s+final\s+ColorCollection<ResourceKey<LootTable>>\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"ColorCollection\.\w+\.map\(\s*\w+\s*->\s*(?:BuiltInLootTables\.)?register\(\s*"
    r"\"([^\"]*)\"\s*\+\s*\w+\s*\)\s*\)"
)

# 颜色顺序的来源：`ColorCollection<DyeColor> VALUES = new ColorCollection<DyeColor>(DyeColor.WHITE, ...)`
COLOR_NAMES = re.compile(
    r"ColorCollection<DyeColor>\s+VALUES\s*=\s*new\s+ColorCollection<(?:DyeColor)?>\(([^;]+)\)"
)
DYE_LITERAL = re.compile(r"DyeColor\.([A-Z][A-Z0-9_]*)")


@dataclass
class LootConstants:
    single: dict[str, str] = field(default_factory=dict)
    """常量名 → 表路径，如 `SHEAR_SHEEP` → `shearing/sheep`。"""

    collections: dict[str, list[str]] = field(default_factory=dict)
    """常量名 → 展开后的表路径列表。"""

    colors: list[str] = field(default_factory=list)
    """颜色名（小写，DyeColor 字面量顺序）。"""

    def resolve(self, constant: str) -> str | None:
        """常量 → 单张表的完整 ID。"""
        path = self.single.get(constant)
        return f"minecraft:{path}" if path else None

    def expand(self, constant: str) -> list[str]:
        """常量 → 展开的表 ID 列表（单表返回 1 个）。"""
        if constant in self.single:
            return [f"minecraft:{self.single[constant]}"]
        return [f"minecraft:{p}" for p in self.collections.get(constant, [])]

    def as_json(self) -> dict:
        return {
            "single": len(self.single),
            "collections": {k: len(v) for k, v in self.collections.items()},
            "colors": len(self.colors),
        }


def _parse_colors_from_text(text: str) -> list[str]:
    """从含 `ColorCollection<DyeColor> VALUES = new ColorCollection<DyeColor>(...)` 的文本读颜色顺序。

    `DyeColor.getName()` 返回小写名，所以这里统一转小写。
    """
    match = COLOR_NAMES.search(text)
    if match is None:
        return []
    return [name.lower() for name in DYE_LITERAL.findall(match.group(1))]


def _parse_colors(index: JavaIndex) -> list[str]:
    """从 `ColorCollection` 的定义处读出颜色顺序。找不到就返回空，由调用方降级。"""
    candidates = index.find_path_contains("ColorCollection.java")
    found = index.load("ColorCollection")
    if found is not None:
        candidates = [*candidates, found]
    for java in candidates:
        colors = _parse_colors_from_text(strip_comments(java.text))
        if colors:
            return colors
    return []


def parse_builtin_loot_tables(index: JavaIndex) -> LootConstants:
    """解析 `BuiltInLootTables.java`。文件缺失返回空对象，调用方据此降级。

    常量表本身不含任何引用，按锚点分桶时进不了候选，所以不能只看
    `index.files`；否则 1.18.2 整张表都解不出，所有代码侧的常量都变空。
    """
    target = index.load("BuiltInLootTables")
    if target is None:
        return LootConstants()

    text = strip_comments(target.text)
    constants = LootConstants()
    constants.colors = _parse_colors(index)

    for name, path in SINGLE.findall(text):
        constants.single[name] = path

    for name, prefix in COLOR_COLLECTION.findall(text):
        if constants.colors:
            constants.collections[name] = [f"{prefix}{c}" for c in constants.colors]

    return constants
