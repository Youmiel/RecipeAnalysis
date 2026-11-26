"""结构拼装器代码里挂上的战利品表。

结构 JSON 只描述「生成什么」，不描述「箱子里放什么」。有些结构的箱子与表都在
生成代码里，模板里一个字节都没有：下界要塞、要塞、废弃矿井根本没有模板；船骸、
末地城、庄园的模板有箱子，但没有 `LootTable`。

**没有模板不等于有容器**：海底神殿也没有模板，但本版数据包里没有 `chests/monument`
这张表，`OceanMonumentPieces` 也不放箱子——不产行是正确结果。

## 三跳全部取自代码

不靠类名约定（`OceanMonumentPieces` 对应的是 `monument`，`WoodlandMansionPieces`
对应 `mansion`）：

1. `StructureType.register("<id>", <类>.CODEC)`（26.x）或
   `StructureFeature.register("<id>", new <类>(...))`（1.18.2）给出 id → 结构类
2. 结构类正文里引用到的 `*Pieces` / `*Piece` 类名，且该名字在索引里有文件
3. 拼装器类正文里的 `BuiltInLootTables.<常量>`，按所在语句的调用名分类

## 容器形态（按语句里的调用名判）

| 语句里出现 | `container` |
|---|---|
| `createChest` | `chest` |
| `createDispenser` | `dispenser` |
| `Map.of("标记", 常量, ...)` | `marker`，另给 `marker` 字段 |
| `archyRuleProcessor` / `AppendLoot` | `processor` |
| `setLootTable` / `setBlockEntityLootTable` | 语句含 `Chest` 时 `chest`，否则 `block_entity` |
| 其它 | `unknown` |

`sites` 是同一绑定在该文件里出现的次数，**不是每个结构实例的箱子数**：拼装器
按房间反复调用，实际数量取决于拼装结果。判据与 `docs/shapes.md` 的「容器」一节一致。

## 不产 `code_link`

这些绑定不描述「物品怎么获得」，只描述「哪个结构里有带表的容器」。走 `code_link`
会被编译层当成可达性规则（`holds` 甚至会被当成表别名），污染求解。因此单独一种
事实，只给导出表用。
"""

from __future__ import annotations

import re

from ....facts import Fact, Prov
from ..index import JavaFile, JavaIndex
from ..javatext import strip_comments
from ..lootconstants import LootConstants, parse_builtin_loot_tables
from .base import Idiom, IdiomResult

ID = "structure_containers"

KIND = "structure_container"

# `StructureType.register("mineshaft", MineshaftStructure.CODEC)`（26.x）
# `StructureFeature.register("mineshaft", new MineshaftFeature(...))`（1.18.2）
REGISTRATION = re.compile(
    r'(?<![\w.])(?:(StructureType|StructureFeature)\.)?register\(\s*"([a-z_]+)"\s*,\s*(?:new\s+)?([A-Z]\w*)\b'
)

# 结构类里引用到的拼装器：`OceanMonumentPieces.MonumentBuilding`。
PIECE_REF = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Pieces|Piece))\b")

LOOT_CONSTANT = re.compile(r"BuiltInLootTables\.([A-Z][A-Z0-9_]*)")

# `Map.of("supply_chest", BuiltInLootTables.SHIPWRECK_SUPPLY, ...)`
MARKER_PAIR = re.compile(r'"([a-z_]+)"\s*,\s*BuiltInLootTables\.([A-Z][A-Z0-9_]*)')

STRUCTURE_CLASS_SUFFIXES = ("Structure", "Feature")


def _statement_bounds(text: str, position: int) -> tuple[int, int]:
    """`position` 所在语句的 `[起, 止)`：向前到 `;` `{` `}`，向后到 `;`。"""
    start = position
    while start > 0 and text[start - 1] not in ";{}":
        start -= 1
    end = text.find(";", position)
    return start, len(text) if end < 0 else end + 1


def _classify(statement: str) -> tuple[str, str | None]:
    """语句 → `(容器形态, 标记名)`。常量文本先去掉，避免 `IGLOO_CHEST` 反推形态。"""
    body = LOOT_CONSTANT.sub("", statement)
    if "createDispenser" in body:
        return "dispenser", None
    if "createChest" in body:
        return "chest", None
    if "archyRuleProcessor" in body or "AppendLoot" in body:
        return "processor", None
    if "Map.of" in body:
        return "marker", None
    if "setLootTable" in body or "setBlockEntityLootTable" in body:
        return ("chest", None) if "chest" in body.lower() else ("block_entity", None)
    return "unknown", None


def _piece_classes(index: JavaIndex, structure_class: str) -> list[JavaFile]:
    """结构类自己 + 它引用到的拼装器类（必须是索引里真实存在的文件）。

    结构类通常没有任何获取语义锚点，不在 `index.files` 里，所以走 `load()`；
    拼装器带 `BuiltInLootTables` 引用，本身就在索引里。
    """
    owner = index.load(structure_class)
    if owner is None:
        return []
    found = [owner]
    for name in sorted(set(PIECE_REF.findall(owner.text))):
        piece = index.load(name)
        if piece is not None and piece.path != owner.path:
            found.append(piece)
    return found


class StructureContainers(Idiom):
    id = ID
    needs = ("structure_type_register", "container_loot")
    # 两版实测各 13–15 条绑定；低于 6 说明注册形态或调用形态变了。
    min_hits = 6

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        constants = parse_builtin_loot_tables(index)
        if not constants.single:
            result.note("找不到 `BuiltInLootTables.java`，结构容器的表无法解析")
            return result

        registrations: dict[str, str] = {}
        for java in index.files.values():
            if "structure_type_register" not in java.anchors:
                continue
            for owner, type_id, klass in REGISTRATION.findall(strip_comments(java.text)):
                if not owner and java.stem not in ("StructureType", "StructureFeature"):
                    continue
                if not klass.endswith(STRUCTURE_CLASS_SUFFIXES):
                    continue
                registrations[type_id] = klass

        if not registrations:
            result.note("没有解析到结构类型注册（`StructureType` / `StructureFeature`）")

        for type_id, structure_class in sorted(registrations.items()):
            files = _piece_classes(index, structure_class)
            if not files:
                result.note(f"{structure_class}: 结构类不在索引里，跳过 `{type_id}`")
                continue
            produced = 0
            for java in files:
                produced += self._file(java, type_id, structure_class, constants, result)
            if produced == 0:
                result.note(
                    f"{structure_class}: 生成代码里没有挂载战利品表（模板或处理器负责）"
                )

        return result

    def _file(
        self,
        java: JavaFile,
        type_id: str,
        structure_class: str,
        constants: LootConstants,
        result: IdiomResult,
    ) -> int:
        text = strip_comments(java.text)
        text = re.sub(r"\s+", " ", text)

        bindings: dict[tuple[str, str, str | None], int] = {}
        covered = 0
        for match in LOOT_CONSTANT.finditer(text):
            if match.start() < covered:
                continue
            start, end = _statement_bounds(text, match.start())
            covered = end
            statement = text[start:end]
            container, _ = _classify(statement)

            pairs = MARKER_PAIR.findall(statement) if container == "marker" else []
            if pairs:
                for marker, constant in pairs:
                    bindings[(container, constant, marker)] = (
                        bindings.get((container, constant, marker), 0) + 1
                    )
                continue

            for constant in dict.fromkeys(LOOT_CONSTANT.findall(statement)):
                bindings[(container, constant, None)] = (
                    bindings.get((container, constant, None), 0) + 1
                )

        produced = 0
        for (container, constant, marker), sites in sorted(
            bindings.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
        ):
            table = constants.resolve(constant)
            if table is None:
                result.note(f"{java.path}: `BuiltInLootTables.{constant}` 解析不出表 ID")
                continue
            payload = {
                "structure_type": f"minecraft:{type_id}",
                "structure_class": structure_class,
                "source_class": java.stem,
                "container": container,
                "loot_table": table,
                "sites": sites,
            }
            if marker:
                payload["marker"] = marker
            result.add_fact(
                Fact(
                    kind=KIND,
                    payload=payload,
                    prov=Prov(input="src", file=java.path, ptr=java.stem),
                ),
                java.path,
            )
            produced += 1
        return produced


def exported() -> list[Idiom]:
    return [StructureContainers()]
