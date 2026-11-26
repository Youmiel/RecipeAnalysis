"""反编译源码索引：把 Java 文件分桶，并给待解释部分精确留痕。

**为什么不是「每个文件一行」**：两版各有 7301 / 4236 个 Java 文件，
逐文件登记等于没有留痕（人看不到 4000 行）。分桶让「已知盲区」变成一个小整数：

| 桶 | 判定 | 留痕方式 |
|---|---|---|
| `excluded` | datagen 生成器、客户端与工具包 | 规则名 + 命中数 |
| `no_anchor` | 不含任何获取语义锚点 | 规则名 + 命中数 |
| `candidate` | 含锚点但无 idiom 提取 | 逐文件：锚点种类 + 原因 |
| `extracted` | idiom 命中并产出事实 | 事实自带 prov |

两条守卫：
1. 每个文件必须落入某个桶，否则硬失败（新结构不静默溜过）
2. 桶计数跨运行比对，突增即报警

**锚点必须精确到调用形态**。实测教训：裸 `LootTable` 命中 87 处，
其中绝大多数是 `import ...loot.LootTable` 这类类型引用，不是获取关系。
所以锚点一律写成「方法调用」或「常量引用」的形态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from ...paths import pack_java

JAVA_ROOT = Path("src/main/java")

# 注册名常量表：`references/BlockItemIds.java` / `references/ItemIds.java`。
# 它们不描述获取机制，但提供「常量名 → 注册名」的映射（两者不一定相同，
# 实测有 `CUT_STANDSTONE_SLAB` → `cut_sandstone_slab` 这种字段名拼写错误）。
# 单独一桶，不混进 no_anchor，避免看起来像是「扫描时漏掉了机制」。
IDENTIFIER_TABLES = frozenset({"BlockItemIds", "ItemIds"})

# L0：明确不参与获取路径推理。
# datagen 只生成数据文件；client/gui/render 是渲染与输入；datafixers 是存档升级。
EXCLUDE_RULES: tuple[tuple[str, str], ...] = (
    ("datagen", r"net/minecraft/data/"),
    ("client_ui", r"net/minecraft/(client|gui|render)/"),
    ("resources_assets", r"net/minecraft/(resources|assets)/"),
    ("datafixers", r"net/minecraft/(util/datafix|util/profiling)/"),
    ("network", r"net/minecraft/(network|advancements|commands|server/rcon|server/players)/"),
    ("experimental", r"net/minecraft/(test|experiments)/"),
)

# 获取语义锚点。名称 → (正则, 说明)。
# 每个都要求「调用」或「常量引用」形态，不做裸类名匹配。
ANCHORS: dict[str, tuple[str, str]] = {
    "loot_table_call": (
        r"dropFrom\w*LootTable\s*\(",
        "从数据包战利品表掉落（表本身在数据包事实里）",
    ),
    "loot_table_key": (
        r"BuiltInLootTables\.[A-Z][A-Z0-9_]*",
        "引用内置战利品表常量",
    ),
    "spawn_at_location": (
        r"\.spawnAtLocation\s*\(",
        "直接生成物品实体",
    ),
    "set_item_slot": (
        r"setItemSlot\s*\(",
        "实体装备槽（含初始化固定携带）",
    ),
    "drop_resources": (
        r"Block\.dropResources\s*\(",
        "方块破坏掉落",
    ),
    "pop_resource": (
        r"Block\.popResource\s*\(",
        "方块弹出物品",
    ),
    "merchant_offer": (
        r"new\s+MerchantOffer\s*\(",
        "交易提议构造",
    ),
    "trade_listing": (
        r"implements\s+ItemListing|ItemListing\[\]",
        "交易表条目",
    ),
    "registry_register": (
        r"Registry\.register\(\s*\w+\.(ITEM|BLOCK)\b",
        "注册物品或方块",
    ),
    "blocks_register": (
        r"Blocks\.register\s*\(",
        "Blocks.register（含借用战利品表等注册期声明）",
    ),
    "items_register_block": (
        r"Items\.(?:registerBlock|registerItem)\s*\(",
        "物品注册为方块物品（放置目标）",
    ),
    "container_loot": (
        r"setLootTable\s*\(",
        "给方块实体挂战利品表",
    ),
    "mob_bucket": (
        r"new\s+MobBucketItem\s*\(",
        "生物桶构造（桶物品与它承载的实体）",
    ),
    "data_marker": (
        r"handleDataMarker\s*\(",
        "结构模板标记回调（结构里塞物品/实体）",
    ),
    "structure_entity_reason": (
        r"EntitySpawnReason\.STRUCTURE",
        "以「结构生成」理由造实体（26.3 形态）",
    ),
    "structure_type_register": (
        r'(?:StructureType|StructureFeature)\.register\(\s*"',
        "结构类型注册（类型 id → 结构类）",
    ),
    "structure_piece_create": (
        # 1.18.2 没有 EntitySpawnReason，形态是 `create(某人.getLevel())`。
        # 写成这样仍然精确：它排除掉 spawn egg 那类 `create(level)` 的简写。
        r"EntityTypes?\.\w+\.create\s*\(\s*(?:\(Level\))?\w+\.getLevel\(\)",
        "结构拼装器里直接造实体（1.18.2 形态）",
    ),
    "shearing_table": (
        r"ITEM_BY_DYE|SHEAR_\w+",
        "剪毛产出的物品表或表常量",
    ),
    "block_transform_table": (
        r"(?:Map|BiMap)\s*<\s*Block\s*,",
        "静态方块变换表（方块→方块/方块状态）",
    ),
}

COMPILED_ANCHORS = {name: re.compile(pat) for name, (pat, _) in ANCHORS.items()}
# Static calls within the declaring class may omit the class name.
LOCAL_REGISTRATION = {
    "Items": ("items_register_block", re.compile(r"=\s*register(?:Block|Item)\s*\(")),
    "Blocks": ("blocks_register", re.compile(r"=\s*register\s*\(")),
    "StructureType": ("structure_type_register", re.compile(r'=\s*register\(\s*"')),
    "StructureFeature": ("structure_type_register", re.compile(r'=\s*register\(\s*"')),
}
COMPILED_EXCLUDE = [(name, re.compile(pat)) for name, pat in EXCLUDE_RULES]


@dataclass(frozen=True)
class JavaFile:
    path: str
    text: str
    anchors: tuple[str, ...]

    @property
    def stem(self) -> str:
        return self.path.rsplit("/", 1)[-1][: -len(".java")] if self.path.endswith(".java") else self.path


@dataclass
class JavaIndex:
    label: str
    root: Path
    files: dict[str, JavaFile] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    no_anchor: int = 0
    # `load()` 的缓存：无锚点文件不进 `files`，但 idiom 仍需读它们的正文。
    _loaded: dict[str, JavaFile | None] = field(default_factory=dict, repr=False)
    _paths: dict[str, Path] | None = field(default=None, repr=False)

    @property
    def candidates(self) -> list[JavaFile]:
        return [f for f in self.files.values() if f.anchors]

    def by_stem(self, stem: str) -> JavaFile | None:
        for java in self.files.values():
            if java.stem == stem:
                return java
        return None

    def load(self, stem: str) -> JavaFile | None:
        """按类名取文件，包含不在索引里的（无锚点只计数，正文不保留）。

        结构类大多没有任何获取语义锚点，却是指向拼装器唯一的数据来源；
        需要它们的 idiom 用这个入口读，不重复扫全量目录。
        """
        found = self.by_stem(stem)
        if found is not None:
            return found
        if stem in self._loaded:
            return self._loaded[stem]
        if self._paths is None:
            self._paths = {path.stem: path for path in self.root.rglob("*.java")}
        path = self._paths.get(stem)
        java = (
            JavaFile(
                path=path.relative_to(self.root).as_posix(),
                text=path.read_text(encoding="utf-8", errors="replace"),
                anchors=(),
            )
            if path is not None
            else None
        )
        self._loaded[stem] = java
        return java

    def find_path_contains(self, fragment: str) -> list[JavaFile]:
        return [f for f in self.files.values() if fragment in f.path]

    def identifier_text(self, stem: str) -> str | None:
        """`references/<stem>.java` 的内容。"""
        for path, text in self.identifiers.items():
            if path.rsplit("/", 1)[-1] == f"{stem}.java":
                return text
        return None

    def bucket_counts(self) -> dict[str, int]:
        out = dict(self.excluded)
        out["identifiers"] = len(self.identifiers)
        out["no_anchor"] = self.no_anchor
        out["candidate"] = len(self.files)
        return out

    def as_json(self, extracted: dict[str, int] | None = None) -> dict:
        extracted = extracted or {}
        total = (
            sum(self.excluded.values())
            + len(self.identifiers)
            + self.no_anchor
            + len(self.files)
        )
        return {
            "label": self.label,
            "total_files": total,
            "buckets": {
                "excluded": sum(self.excluded.values()),
                "identifiers": len(self.identifiers),
                "no_anchor": self.no_anchor,
                "candidate": len(self.files),
                "extracted": sum(extracted.values()),
            },
            "exclude_rules": self.excluded,
            "extracted_by_idiom": extracted,
            "anchors": {
                name: sum(1 for f in self.files.values() if name in f.anchors)
                for name in ANCHORS
            },
        }


def build_index(pack_dir: Path, label: str) -> JavaIndex:
    """扫描 `src/main/java`，把所有文件分到 excluded / no_anchor / candidate。

    不抛异常表示分桶完备；任何读不了的文件也归入 candidate 并带空锚点，
    由调用方在台账里记 failed。
    """
    java_root = pack_java(pack_dir)
    index = JavaIndex(label=label, root=java_root)

    if not java_root.is_dir():
        return index

    for path in sorted(java_root.rglob("*.java")):
        rel = path.relative_to(java_root).as_posix()

        rule = next((name for name, pat in COMPILED_EXCLUDE if pat.search(rel)), None)
        if rule is not None:
            index.excluded[rule] = index.excluded.get(rule, 0) + 1
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            index.files[rel] = JavaFile(path=rel, text="", anchors=())
            index.no_anchor += 1
            continue

        # 标识符常量表不进候选桶：它们不描述机制，只提供名字映射
        if rel.startswith("net/minecraft/references/") and path.stem in IDENTIFIER_TABLES:
            index.identifiers[rel] = text
            continue

        anchors = tuple(name for name, pat in COMPILED_ANCHORS.items() if pat.search(text))
        local = LOCAL_REGISTRATION.get(path.stem)
        if local and local[0] not in anchors and local[1].search(text):
            anchors += (local[0],)
        if not anchors:
            index.no_anchor += 1
            continue

        index.files[rel] = JavaFile(path=rel, text=text, anchors=anchors)

    return index


def anchor_reason(name: str) -> str:
    return ANCHORS.get(name, (r"", "未登记"))[1]
