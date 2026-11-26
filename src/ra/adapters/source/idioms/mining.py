"""挖掘机制（代码侧静态值）：方块硬度与工具挖掘速度。

这组数值不进 RA 的建图——获取路径只问「拿不拿得到」，不问「挖多久」。之所以仍然
落成事实，是因为它们在代码里是一手数据，采集成本低，且跨版本 diff 能直接看出
「这版把哪类方块调硬了」。账本、provenance、`ra export` 都可直接复用。

## 机制（两版都读自源码）

| 环节 | 位置 | 读法 |
|---|---|---|
| 方块硬度 | `BlockBehaviour.Properties.destroyTime` | `strength(a[, b])` / `destroyTime(a)` / `instabreak()`；缺省 0.0 |
| 挖不动的哨兵 | `BlockBehaviour.getDestroyProgress` | 硬度 -1.0 → 每 tick 进度 0 |
| 每 tick 进度 | 同上 | `player_speed / hardness / (正确工具 ? 30 : 100)` |
| 工具速度（26.3） | 物品组件 `DataComponents.TOOL` | 规则表按方块/标签给速度，未命中取 `default_mining_speed` |
| 工具速度（1.18.2） | `Tier.getSpeed()` + `DiggerItem` | 方块在工具标签内用 tier 速度，否则 1.0 |

**硬度只在两处产生**：注册点的 `Properties` 链，以及方块构造器对同一个
`Properties` 的就地改写（实测只有 `InfestedBlock`：宿主硬度的一半）。后者按
构造器源码求值，不猜。

## 注册点与展开

方块只在 `Blocks.java` 注册，工具只在 `Items.java` 声明，锚点取**调用形态**：

| 形态 | 版本 | 展开 |
|---|---|---|
| `Blocks.register(id[, factory], props)` | 两版 | 1 个方块 |
| `Blocks.registerStair/registerWall/registerSlab/registerLegacyStair` | 26.3 | 1 个方块，`Properties` 复制自底方块，`registerSlab` 可覆盖硬度 |
| `ColorCollection.registerBlocks` / `zipMap` | 26.3 | 16 色各 1 个 |
| `WeatheringCopperCollection.registerBlocks` | 26.3 | 4 态 × (未涂蜡 / 涂蜡) 共 8 个 |

`ColorCollection` / `WeatheringCopperCollection` 的注册名来自
`BlockItemIds` / `BlockIds` 的常量表，表里三种形态（标量、16 色、8 铜态）按其
工厂方法名识别，不按字段名拼。

复制语义只继承 `destroyTime` / `explosionResistance` / `requiresCorrectToolForDrops`
三项，其余键与挖掘无关。

## 对账

解析结果必须覆盖 `registry` 事实里 `minecraft:block` 的全集：
多出来的 id 不作为事实产出，缺的逐条登记到台账（`verify_mining_blocks`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ....facts import Fact, Prov, Status
from ..index import JavaIndex
from ..javatext import match_brace, match_paren, split_args, strip_comments
from .base import Idiom, IdiomResult

ID = "mining"

BLOCK_FILE = "Blocks"
ITEM_FILE = "Items"
BEHAVIOUR_FILE = "BlockBehaviour"
PLAYER_FILE = "Player"
ITEM_CLASS_FILE = "Item"
TAGS_FILE = "BlockTags"

# 26.3 的两种集合注册；1.18.2 没有。
COLLECTION_FILES = ("ColorCollection", "WeatheringCopperCollection")

# ------------------------------------------------------------------ 数据形状


@dataclass(frozen=True)
class _Props:
    """`BlockBehaviour.Properties` 链里与挖掘有关的三项。"""

    hardness: float = 0.0
    explosion: float = 0.0
    requires_tool: bool = False
    unresolved: str | None = None


@dataclass(frozen=True)
class _Block:
    block_id: str
    props: _Props


@dataclass(frozen=True)
class _Ids:
    """注册名常量表的一项。

    `kind` 三种：`scalar`（一个 id）、`color`（16 色）、`copper`（8 个铜态）。
    `items` 是 `(标签, id)`，标签用来在集合注册里对位。
    """

    kind: str
    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Material:
    name: str
    speed: float
    durability: int | None = None
    attack_damage_bonus: float | None = None
    enchantment_value: int | None = None
    incorrect_tag: str | None = None
    tier_level: int | None = None


@dataclass(frozen=True)
class _CtorMutation:
    """方块构造器对 `Properties` 的就地改写。"""

    params: tuple[str, ...]
    expr: str


@dataclass
class _Ctx:
    blocks: dict[str, _Block] = field(default_factory=dict)
    # 集合注册：字段名 → 标签（颜色名 / 铜态）→ 方块
    colors: dict[str, dict[str, _Block]] = field(default_factory=dict)
    coppers: dict[str, dict[str, _Block]] = field(default_factory=dict)
    ids: dict[str, dict[str, _Ids]] = field(default_factory=dict)
    helpers: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)
    mutations: dict[str, _CtorMutation] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    materials: dict[str, _Material] = field(default_factory=dict)
    item_ids: dict[str, str] = field(default_factory=dict)
    mineable_tags: dict[str, str] = field(default_factory=dict)
    # 已由字段级或 zipMap lambda 认领的注册调用数，用于对账「还剩几处没解释」
    claimed_calls: int = 0


# ------------------------------------------------------------------ 正则

FIELD_CALL = re.compile(
    r"(?<![\w.])(?:Blocks\.)?(register|registerStair|registerLegacyStair|registerSlab|registerWall)\s*\("
)
COLLECTION_CALL = re.compile(
    r"(ColorCollection|WeatheringCopperCollection)\.(registerBlocks|zipMap)\s*\("
)
BASE_RE = re.compile(r"BlockBehaviour\.Properties\.(of|ofLegacyCopy|ofFullCopy|copy)\s*\(")
CHAIN_RE = re.compile(
    r"\.(strength|destroyTime|instabreak|requiresCorrectToolForDrops)\s*\("
)
PICK_RE = re.compile(r"([A-Z][A-Z0-9_]*)(?:\.(weathering|waxed)\(\))?\.pick\s*\(")
NUM_RE = re.compile(r"^(?:\(float\)\s*)?\(?\s*(-?\d+(?:\.\d+)?)[fF]?\s*\)?$")

SCALAR_ID = re.compile(
    r"public static final (?:BlockItemId|ResourceKey<Block>)\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"(?:(?:BlockItemId|BlockIds)\.)?create\(\"([^\"]+)\"(?:\s*,\s*\"[^\"]+\")?\)"
)
COLOR_ID = re.compile(
    r"public static final ColorCollection<(?:BlockItemId|ResourceKey<Block>)>\s+"
    r"([A-Z][A-Z0-9_]*)\s*=\s*(?:(?:BlockItemIds|BlockIds)\.)?createSimpleColored\(\"([^\"]+)\"\)"
)
COPPER_SIMPLE_ID = re.compile(
    r"public static final WeatheringCopperCollection<BlockItemId>\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"(?:BlockItemIds\.)?createSimpleCopper\(\"([^\"]+)\"\)"
)
COPPER_SPECIAL_ID = re.compile(
    r"public static final WeatheringCopperCollection<BlockItemId>\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"WeatheringCopperCollection\.prefixWithState\(\s*"
    r"WeatheringCopperCollection\.same\(([A-Z][A-Z0-9_]*)\)\s*\)\s*\.map\(BlockItemId::create\)"
)
BY_STATE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*new WeatheringCopperCollection\.ByState<(?:String)?>\(([^)]*)\)")

MATERIAL_263 = re.compile(
    r"public static final ToolMaterial\s+([A-Z][A-Z0-9_]*)\s*=\s*new ToolMaterial\(\s*"
    r"BlockTags\.([A-Z][A-Z0-9_]*)\s*,\s*(\d+)\s*,\s*(-?\d+(?:\.\d+)?)[fF]\s*,\s*"
    r"(-?\d+(?:\.\d+)?)[fF]\s*,\s*(\d+)\s*,"
)
MATERIAL_1182 = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+(?:\.\d+)?)[fF]\s*,\s*"
    r"(-?\d+(?:\.\d+)?)[fF]\s*,\s*(\d+)\s*,",
    re.MULTILINE,
)

TAG_CONST = re.compile(
    r"public static final TagKey<Block>\s+([A-Z][A-Z0-9_]*)\s*=\s*(?:BlockTags\.)?create\(\"([^\"]+)\"\)"
)
ITEM_ID_263 = re.compile(
    r"public static final ResourceKey<Item>\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"(?:ItemIds\.)?create\(\"([^\"]+)\"(?:\s*,[^)]*)?\)"
)
REGISTER_ITEM = re.compile(r"(?<![\w.])(?:Items\.)?registerItem\s*\(")
MATERIAL_TOOL_263 = re.compile(
    r"\.(pickaxe|axe|hoe|shovel|sword)\s*\(\s*ToolMaterial\.([A-Z][A-Z0-9_]*)"
)
TOOL_METHOD_TAG = re.compile(
    r"public (?:Item\.)?Properties (pickaxe|axe|hoe|shovel)\((?:final )?ToolMaterial material,\s*"
    r"(?:final )?float attackDamageBaseline,\s*(?:final )?float attackSpeedBaseline\)\s*\{\s*"
    r"return this\.tool\(material,\s*BlockTags\.([A-Z][A-Z0-9_]*)",
)
# Text right before a match that marks a method definition, e.g. `static Block register(`.
METHOD_DEFINITION = re.compile(r"\bstatic\s+[\w.<>]+\s+$")
TERNARY_RETURN = re.compile(r"\breturn\s+([^;?]+?)\s*\?\s*([^;:]+?)\s*:\s*([^;]+?)\s*;")
RULE_CALL = re.compile(r"Tool\.Rule\.(minesAndDrops|deniesDrops|overrideSpeed)\s*\(")
SEL_DIRECT_BLOCK = re.compile(
    r"HolderSet\.direct\(\s*Blocks\.([A-Z][A-Z0-9_]*)\.builtInRegistryHolder\(\)\s*\)"
)
SEL_TAG = re.compile(r"getOrThrow\(\s*BlockTags\.([A-Z][A-Z0-9_]*)\s*\)")
IF_SEL_BLOCK = re.compile(r"blockState\.is\(\s*Blocks\.([A-Z][A-Z0-9_]*)\s*\)")
IF_SEL_TAG = re.compile(r"blockState\.is\(\s*BlockTags\.([A-Z][A-Z0-9_]*)\s*\)")
IF_SEL_MATERIAL = re.compile(r"material\s*==\s*Material\.([A-Z][A-Z0-9_]*)")
CONSTRUCTED = re.compile(r"new\s+([A-Z][A-Za-z0-9_]*)\s*\(")
METHOD_REF_NEW = re.compile(r"([A-Z][A-Za-z0-9_]*)::new")
SUPER_CALL = re.compile(r"super\s*\(")


# ------------------------------------------------------------------ 通用工具


def _text(index: JavaIndex, stem: str) -> tuple[str, str] | None:
    """按类名取一份去注释的源码，返回 `(相对路径, 正文)`。"""
    java = index.load(stem)
    if java is None:
        return None
    return java.path, strip_comments(java.text)


def _field_name(text: str, call_start: int) -> str | None:
    """`X = Blocks.register(...)` 里的字段名；方法体内的调用返回 None。"""
    head = text[max(0, call_start - 400) : call_start]
    found = re.search(r"([A-Z][A-Z0-9_]*)\s*=\s*$", head.rstrip() + " ")
    return found.group(1) if found else None


def _as_float(token: str) -> float | None:
    found = NUM_RE.match(token.strip())
    return float(found.group(1)) if found else None


def _call_args(text: str, match: re.Match[str]) -> tuple[list[str], int]:
    """取匹配到的调用的实参，以及右括号位置。"""
    open_index = text.index("(", match.end() - 1)
    close = match_paren(text, open_index)
    if close < 0:
        return [], -1
    return split_args(text[open_index + 1 : close]), close


def _lambda_body(expr: str) -> str:
    """`color -> ...` / `(a, b) -> { ... }` 取箭头右侧。"""
    arrow = expr.find("->")
    return expr[arrow + 2 :].strip() if arrow >= 0 else expr


# ------------------------------------------------------------------ 常量表


def _color_names(collection_text: str) -> tuple[str, ...]:
    found = re.search(r"VALUES\s*=\s*new ColorCollection<(?:DyeColor)?>\(([^)]*)\)", collection_text)
    if not found:
        return ()
    return tuple(
        value.split(".")[-1].strip().lower()
        for value in split_args(found.group(1))
        if value.strip()
    )


def _copper_prefixes(collection_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    found = re.search(
        r"PREFIXES\s*=\s*new WeatheringCopperCollection<(?:String)?>\s*\(", collection_text
    )
    if not found:
        return (), ()
    open_index = found.end() - 1
    close = match_paren(collection_text, open_index)
    if close < 0:
        return (), ()
    inner = collection_text[open_index + 1 : close]
    states: list[tuple[str, ...]] = []
    for start in re.finditer(r"new (?:WeatheringCopperCollection\.)?ByState<(?:String)?>\s*\(", inner):
        state_open = start.end() - 1
        state_close = match_paren(inner, state_open)
        if state_close < 0:
            continue
        states.append(
            tuple(
                item.strip().strip('"')
                for item in split_args(inner[state_open + 1 : state_close])
            )
        )
    if len(states) < 2:
        return (), ()
    return states[0], states[1]


def _id_tables(
    names: tuple[str, ...],
    prefixes: tuple[tuple[str, ...], tuple[str, ...]],
    text: str,
) -> dict[str, _Ids]:
    tables: dict[str, _Ids] = {}
    by_state: dict[str, tuple[str, ...]] = {
        m.group(1): tuple(item.strip().strip('"') for item in split_args(m.group(2)))
        for m in BY_STATE.finditer(text)
    }
    for m in SCALAR_ID.finditer(text):
        tables[m.group(1)] = _Ids("scalar", (("", m.group(2)),))
    for m in COLOR_ID.finditer(text):
        base = m.group(2)
        tables[m.group(1)] = _Ids(
            "color", tuple((color, f"{color}_{base}") for color in names)
        )
    for m in COPPER_SIMPLE_ID.finditer(text):
        base = m.group(2)
        weathering, waxed = prefixes
        items = [("weathering", f"{pre}{base}") for pre in weathering]
        items += [("waxed", f"{pre}{base}") for pre in waxed]
        tables[m.group(1)] = _Ids("copper", tuple(items))
    for m in COPPER_SPECIAL_ID.finditer(text):
        names4 = by_state.get(m.group(2))
        if names4 is None or len(names4) != 4:
            continue
        weathering, waxed = prefixes
        items = [("weathering", f"{pre}{name}") for pre, name in zip(weathering, names4)]
        items += [("waxed", f"{pre}{name}") for pre, name in zip(waxed, names4)]
        tables[m.group(1)] = _Ids("copper", tuple(items))
    return tables


def _load_ids(ctx: _Ctx, index: JavaIndex) -> None:
    collection = _text(index, "ColorCollection")
    colour_names = _color_names(collection[1]) if collection else ()
    copper = _text(index, "WeatheringCopperCollection")
    prefixes = _copper_prefixes(copper[1]) if copper else ((), ())
    for stem in ("BlockItemIds", "BlockIds"):
        java = index.identifier_text(stem)
        if java is None:
            loaded = _text(index, stem)
            java = loaded[1] if loaded is not None else None
        if java is None:
            continue
        ctx.ids[stem] = _id_tables(colour_names, prefixes, java)


def _resolve_ids(expr: str, ctx: _Ctx) -> _Ids | None:
    literal = re.fullmatch(r'"(?:minecraft:)?([a-z0-9_/]+)"', expr.strip())
    if literal:
        # 1.18.2 直接写字面量
        return _Ids("scalar", (("", literal.group(1)),))
    found = re.fullmatch(r"(BlockItemIds|BlockIds)\.([A-Z][A-Z0-9_]*)", expr.strip())
    if not found:
        return None
    return ctx.ids.get(found.group(1), {}).get(found.group(2))


# ------------------------------------------------------------------ 属性链


def _lookup_block(ref: str, ctx: _Ctx, label: str | None) -> _Props | None:
    """解析 `Properties` 复制的来源：字段、`X.pick(...)`、或同组任一。"""
    pick = PICK_RE.search(ref)
    if pick:
        field_name, group = pick.group(1), pick.group(2)
        if field_name in ctx.colors:
            by_label = ctx.colors[field_name]
            if label is not None and label in by_label:
                return by_label[label].props
            return None
        if field_name in ctx.coppers:
            by_label = ctx.coppers[field_name]
            chosen = [
                block.props
                for tag, block in by_label.items()
                if group is None or tag.split("#", 1)[0] == group
            ]
            if not chosen:
                return None
            first = chosen[0]
            if any((item.hardness, item.explosion) != (first.hardness, first.explosion) for item in chosen):
                return _Props(unresolved=f"{field_name} 组内硬度不一致，按组取首项会错")
            return first
        return None
    name = ref.strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) and name in ctx.blocks:
        return ctx.blocks[name].props
    return None


def _eval_helper(name: str, ctx: _Ctx, depth: int) -> _Props:
    params, body = ctx.helpers[name]
    returned = re.search(r"return\s+([A-Za-z_]\w*)\s*;", body)
    if returned:
        var = returned.group(1)
        assignments = re.findall(rf"\b{re.escape(var)}\s*=\s*([^;]+);", body)
        if assignments:
            later = [item for item in assignments[1:] if CHAIN_RE.search(item)]
            if later:
                return _Props(unresolved=f"{name} 对 {var} 有二次赋值，硬度可能被改写")
            return _eval_props(assignments[0], ctx, depth + 1, label=None)
    return _eval_props(body, ctx, depth + 1, label=None)


def _eval_props(expr: str, ctx: _Ctx, depth: int = 0, label: str | None = None) -> _Props:
    """求值一条 `Properties` 链里与挖掘有关的三项。"""
    if depth > 4:
        return _Props(unresolved="Properties 链过深")

    start = 0
    props = _Props()
    base = BASE_RE.search(expr)
    if base is not None:
        origin = base.group(1)
        args, close = _call_args(expr, base)
        if origin in ("ofLegacyCopy", "ofFullCopy", "copy"):
            if not args:
                return _Props(unresolved=f"{origin} 没有来源方块")
            inherited = _lookup_block(args[0], ctx, label)
            if inherited is None:
                return _Props(unresolved=f"复制来源不可解析：{args[0].strip()[:60]}")
            if inherited.unresolved:
                return inherited
            props = inherited
        start = close + 1 if close > 0 else base.end()
    else:
        helper = re.match(r"\s*(?:Blocks\.)?([A-Za-z_]\w*)\s*\(", expr)
        if helper is None or helper.group(1) not in ctx.helpers:
            return _Props(unresolved="属性来源不是 Properties.of* 也不是已知辅助方法")
        props = _eval_helper(helper.group(1), ctx, depth)
        if props.unresolved:
            return props
        close = match_paren(expr, helper.end() - 1)
        start = close + 1 if close > 0 else helper.end()

    hardness = props.hardness
    explosion = props.explosion
    requires = props.requires_tool
    for call in CHAIN_RE.finditer(expr, start):
        name = call.group(1)
        args, _ = _call_args(expr, call)
        if name == "requiresCorrectToolForDrops":
            requires = True
            continue
        if name == "instabreak":
            hardness, explosion = 0.0, 0.0
            continue
        value = _as_float(args[0]) if args else None
        if value is None:
            return _Props(unresolved=f"{name} 的实参不是字面量：{args[0].strip()[:40] if args else ''}")
        hardness = value
        if name == "strength":
            second = _as_float(args[1]) if len(args) > 1 else None
            if len(args) > 1 and second is None:
                return _Props(unresolved=f"strength 的爆炸抗性不是字面量：{args[1].strip()[:40]}")
            explosion = second if second is not None else value
    return _Props(hardness, explosion, requires)


# ------------------------------------------------------------------ 构造器改写


def _ctor_mutations(index: JavaIndex) -> dict[str, _CtorMutation]:
    """方块构造器对 `Properties` 的就地改写（实测只有侵染方块）。"""
    direct: dict[str, _CtorMutation] = {}
    extends: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
    java = index.load("InfestedBlock")
    candidates = [java] if java is not None else []
    for extra in ("InfestedRotatedPillarBlock",):
        found = index.load(extra)
        if found is not None:
            candidates.append(found)

    for item in candidates:
        text = strip_comments(item.text)
        cls = re.search(r"class\s+([A-Z]\w*)\s+extends\s+([A-Z]\w*)", text)
        if cls is None:
            continue
        name, parent = cls.group(1), cls.group(2)
        ctor = re.search(rf"public\s+{name}\s*\(([^)]*)\)\s*\{{", text)
        if ctor is None:
            continue
        params = tuple(part.split()[-1] for part in split_args(ctor.group(1)) if part.strip())
        body_start = ctor.end() - 1
        body_end = match_brace(text, body_start)
        body = text[body_start + 1 : body_end]
        sup = SUPER_CALL.search(body)
        if sup is None:
            continue
        args, _ = _call_args(body, sup)
        if any(CHAIN_RE.search(arg) for arg in args):
            direct[name] = _CtorMutation(params, args[0])
            continue
        extends[name] = (parent, params, tuple(arg.strip() for arg in args))

    for name, (parent, params, args) in extends.items():
        if parent not in direct:
            continue
        ancestor = direct[parent]
        mapping = {}
        for index_, arg in enumerate(args):
            if re.fullmatch(r"[A-Za-z_]\w*", arg) and index_ < len(ancestor.params):
                mapping[ancestor.params[index_]] = arg
        rewritten = ancestor.expr
        for source, target in mapping.items():
            rewritten = re.sub(rf"\b{re.escape(source)}\b", target, rewritten)
        direct[name] = _CtorMutation(params, rewritten)
    return direct


def _apply_ctor_mutation(
    props: _Props, mutation: _CtorMutation, call_args: Iterable[str], ctx: _Ctx
) -> _Props:
    """把构造器的 `destroyTime(host.defaultDestroyTime() / N)` 求成数值。"""
    args = list(call_args)
    expr = mutation.expr
    for position, param in enumerate(mutation.params):
        if ".defaultDestroyTime()" not in expr:
            break
        if re.search(rf"\b{re.escape(param)}\.defaultDestroyTime\(\)", expr) is None:
            continue
        if position >= len(args):
            return _Props(unresolved=f"构造器改写取不到 {param} 的实参")
        host = _lookup_block(args[position].strip(), ctx, None)
        if host is None or host.unresolved:
            return _Props(unresolved=f"构造器改写的宿主方块不可解析：{args[position].strip()[:40]}")
        expr = re.sub(
            rf"\b{re.escape(param)}\.defaultDestroyTime\(\)", repr(host.hardness), expr
        )

    hardness: float | None = None
    explosion: float | None = None
    for name, args_ in (
        (
            "destroyTime",
            _chain_args(expr, "destroyTime"),
        ),
        (
            "explosionResistance",
            re.findall(r"\.explosionResistance\(([^)]*)\)", expr),
        ),
    ):
        if not args_:
            continue
        value = _arithmetic(args_[0])
        if value is None:
            return _Props(unresolved=f"构造器改写的 {name} 不是可求值的表达式：{args_[0].strip()[:40]}")
        if name == "destroyTime":
            hardness = value
        else:
            explosion = value
    return _Props(
        hardness if hardness is not None else props.hardness,
        explosion if explosion is not None else props.explosion,
        props.requires_tool,
    )


def _chain_args(expr: str, name: str) -> list[str]:
    found = re.search(rf"\.{name}\s*\(", expr)
    if found is None:
        return []
    args, _ = _call_args(expr, found)
    return args


def _arithmetic(expr: str) -> float | None:
    """只认 `数值`、`数值 运算符 数值` 与括号，其余交回人工。"""
    text = expr.strip().replace(" ", "")
    value = _as_float(text)
    if value is not None:
        return value
    number = r"\(?(-?\d+(?:\.\d+)?)[fF]?\)?"
    found = re.fullmatch(rf"{number}([*/+-]){number}", text)
    if found is None:
        return None
    left, op, right = float(found.group(1)), found.group(2), float(found.group(3))
    if op == "*":
        return left * right
    if op == "/":
        return left / right if right else None
    return left + right if op == "+" else left - right


# ------------------------------------------------------------------ 方块注册


def _constructed(args: list[str]) -> tuple[str, list[str]] | None:
    """注册调用的工厂实参里，被构造的方块类与它的构造实参。"""
    for arg in args:
        for found in CONSTRUCTED.finditer(arg):
            _, close = _call_args(arg, found)
            if close < 0:
                continue
            return found.group(1), split_args(arg[found.end() : close])
        ref = METHOD_REF_NEW.search(arg)
        if ref:
            return ref.group(1), []
    return None


def _make_block(
    block_id: str,
    props: _Props,
    factory_args: list[str],
    ctx: _Ctx,
    notes: list[str],
) -> _Block:
    constructed = _constructed(factory_args)
    if constructed is not None:
        cls, ctor_args = constructed
        mutation = ctx.mutations.get(cls)
        if mutation is not None:
            props = _apply_ctor_mutation(props, mutation, ctor_args, ctx)
            if props.unresolved:
                notes.append(f"{block_id}: {props.unresolved}")
    return _Block(block_id, props)


def _scan_blocks(text: str, ctx: _Ctx, notes: list[str]) -> list[_Block]:
    produced: list[_Block] = []
    events: list[tuple[int, str, re.Match[str]]] = []
    for found in FIELD_CALL.finditer(text):
        events.append((found.start(), "block", found))
    for found in COLLECTION_CALL.finditer(text):
        events.append((found.start(), "collection", found))
    events.sort(key=lambda item: item[0])

    for _, kind, found in events:
        args, close = _call_args(text, found)
        if close < 0:
            notes.append(f"注册调用括号不配对，位置 {found.start()}")
            continue
        field_name = _field_name(text, found.start())
        if field_name is None:
            # 方法体内的调用是复制包装的定义，由 FIELD_CALL 的语义覆盖
            continue

        if kind == "block":
            name = found.group(1)
            if field_name is not None:
                ctx.claimed_calls += 1
            ids = _resolve_ids(args[0], ctx) if args else None
            if ids is None or ids.kind != "scalar":
                notes.append(f"{field_name}: 注册名实参不可解析（{args[0].strip()[:40] if args else '空'}）")
                continue
            block_id = ids.items[0][1]
            if name == "register":
                if len(args) < 2:
                    notes.append(f"{field_name}: register 实参不足")
                    continue
                props = _eval_props(args[-1], ctx)
                factory = [args[1]]
            else:
                if len(args) < 2:
                    notes.append(f"{field_name}: {name} 实参不足")
                    continue
                base = _lookup_block(args[1], ctx, None)
                if base is None:
                    notes.append(f"{field_name}: {name} 的底方块不可解析（{args[1].strip()[:40]}）")
                    continue
                props = base
                if name == "registerSlab" and len(args) >= 3:
                    value = _as_float(args[2])
                    if value is None:
                        notes.append(f"{field_name}: registerSlab 的硬度不是字面量")
                        continue
                    explosion = value
                    if len(args) >= 4:
                        second = _as_float(args[3])
                        if second is None:
                            notes.append(f"{field_name}: registerSlab 的爆炸抗性不是字面量")
                            continue
                        explosion = second
                    props = _Props(value, explosion, base.requires_tool)
                factory = []
            if props.unresolved:
                notes.append(f"{field_name}: {props.unresolved}")
                continue
            block = _make_block(block_id, props, factory, ctx, notes)
            ctx.blocks[field_name] = block
            produced.append(block)
            continue

        # 集合注册
        library, method = found.group(1), found.group(2)
        if library == "ColorCollection" and method == "registerBlocks":
            ids = _resolve_ids(args[0], ctx) if args else None
            if ids is None or ids.kind != "color":
                notes.append(f"{field_name}: 颜色集合的注册名表不可解析")
                continue
            props_expr = _lambda_body(args[-1]) if args else ""
            members: dict[str, _Block] = {}
            for label, block_id in ids.items:
                props = _eval_props(props_expr, ctx, label=label)
                if props.unresolved:
                    notes.append(f"{field_name}.{label}: {props.unresolved}")
                    continue
                members[label] = _Block(block_id, props)
            ctx.colors[field_name] = members
            produced.extend(members.values())
        elif library == "ColorCollection" and method == "zipMap":
            ids = _resolve_ids(args[1], ctx) if len(args) > 2 else None
            if ids is None or ids.kind != "color":
                notes.append(f"{field_name}: zipMap 的注册名表不可解析")
                continue
            body = _lambda_body(args[-1])
            inner = FIELD_CALL.search(body)
            if inner is None:
                notes.append(f"{field_name}: zipMap 的 lambda 里没有 Blocks.register* 调用")
                continue
            ctx.claimed_calls += len(FIELD_CALL.findall(body))
            inner_args, _ = _call_args(body, inner)
            base_expr = inner_args[1] if len(inner_args) > 1 else ""
            members = {}
            for label, block_id in ids.items:
                base = _lookup_block(base_expr, ctx, label)
                if base is None or base.unresolved:
                    notes.append(f"{field_name}.{label}: 底方块不可解析（{base_expr.strip()[:40]}）")
                    continue
                props = base
                if inner.group(1) == "registerSlab" and len(inner_args) >= 3:
                    value = _as_float(inner_args[2])
                    if value is None:
                        notes.append(f"{field_name}.{label}: registerSlab 的硬度不是字面量")
                        continue
                    props = _Props(value, base.explosion, base.requires_tool)
                members[label] = _Block(block_id, props)
            ctx.colors[field_name] = members
            produced.extend(members.values())
        else:
            ids = _resolve_ids(args[0], ctx) if args else None
            if ids is None or ids.kind != "copper":
                notes.append(f"{field_name}: 铜集合的注册名表不可解析")
                continue
            props_expr = _lambda_body(args[-1]) if args else ""
            members = {}
            for position, (label, block_id) in enumerate(ids.items):
                props = _eval_props(props_expr, ctx, label=label)
                if props.unresolved:
                    notes.append(f"{field_name}.{label}{position}: {props.unresolved}")
                    continue
                # 铜集合里 weathering / waxed 各 4 态，标签只有组名；补序号避免键碰撞
                members[f"{label}#{position}"] = _Block(block_id, props)
            ctx.coppers[field_name] = members
            produced.extend(members.values())

    return produced


# ------------------------------------------------------------------ 辅助方法


def _load_helpers(text: str, ctx: _Ctx) -> None:
    """收集「返回 Properties 或返回方块」的静态工厂，供属性求值时展开。

    1.18.2 的 `Blocks.log(...)` / `Blocks.leaves(...)` 直接返回构造好的方块，
    `Properties` 链在方法体里；只看返回类型会漏掉这整批。
    """
    for found in re.finditer(
        r"(?:private|protected|public)\s+static\s+[A-Za-z_][\w.<>]*\s+(\w+)\s*\(([^)]*)\)\s*\{",
        text,
    ):
        name = found.group(1)
        if name.startswith("register"):
            continue
        body_start = found.end() - 1
        body_end = match_brace(text, body_start)
        if body_end < 0:
            continue
        body = text[body_start + 1 : body_end]
        if "BlockBehaviour.Properties" not in body and "Properties.of" not in body:
            continue
        params = tuple(part.split()[-1] for part in split_args(found.group(2)) if part.strip())
        ctx.helpers[name] = (params, body)


# ------------------------------------------------------------------ 工具


def _tag_name(ctx: _Ctx, constant: str) -> str:
    return f"minecraft:{ctx.tags.get(constant, constant.lower())}"


def _rule_payload(selector: str, speed, correct) -> dict:
    return {"selector": selector, "speed": speed, "correct_for_drops": correct}


def _parse_tool_rules(expr: str, ctx: _Ctx, notes: list[str]) -> tuple[float | None, list[dict]]:
    """解析 `new Tool(List.of(rule...), default, ...)`。"""
    created = re.search(r"new\s+Tool\s*\(", expr)
    if created is None:
        notes.append("Tool 组件不是 new Tool(...)")
        return None, []
    args, _ = _call_args(expr, created)
    default = _as_float(args[1]) if len(args) > 1 else None
    rules: list[dict] = []
    list_call = re.search(r"List\.of\s*\(", args[0] if args else "")
    if list_call is None:
        return default, rules
    items, _ = _call_args(args[0], list_call)
    for item in items:
        call = RULE_CALL.search(item)
        if call is None:
            notes.append(f"未识别的 Tool 规则：{item.strip()[:60]}")
            continue
        rule_args, _ = _call_args(item, call)
        selector = _selector(rule_args[0] if rule_args else "", ctx, notes)
        if selector is None:
            continue
        method = call.group(1)
        if method == "deniesDrops":
            rules.append(_rule_payload(selector, None, False))
            continue
        speed: float | str | None = None
        if len(rule_args) > 1:
            parsed = _as_float(rule_args[1])
            speed = parsed if parsed is not None else rule_args[1].strip()
        rules.append(
            _rule_payload(selector, speed, True if method == "minesAndDrops" else None)
        )
    return default, rules


def _selector(expr: str, ctx: _Ctx, notes: list[str]) -> str | None:
    block = SEL_DIRECT_BLOCK.search(expr)
    if block is not None:
        known = ctx.blocks.get(block.group(1))
        if known is None:
            notes.append(f"工具规则引用了未解析的方块：Blocks.{block.group(1)}")
            return None
        return f"block:minecraft:{known.block_id}"
    tag = SEL_TAG.search(expr)
    if tag is not None:
        return f"tag:{_tag_name(ctx, tag.group(1))}"
    notes.append(f"工具规则的选择器不可解析：{expr.strip()[:60]}")
    return None


def _materials(ctx: _Ctx, index: JavaIndex, notes: list[str]) -> None:
    modern = _text(index, "ToolMaterial")
    if modern is not None:
        _, text = modern
        for found in MATERIAL_263.finditer(text):
            name = found.group(1).lower()
            ctx.materials[name] = _Material(
                name=name,
                speed=float(found.group(4)),
                durability=int(found.group(3)),
                attack_damage_bonus=float(found.group(5)),
                enchantment_value=int(found.group(6)),
                incorrect_tag=_tag_name(ctx, found.group(2)),
            )
        return
    legacy = _text(index, "Tiers")
    if legacy is None:
        notes.append("没有 ToolMaterial.java，也没有 Tiers.java")
        return
    _, text = legacy
    for found in MATERIAL_1182.finditer(text):
        name = found.group(1).lower()
        ctx.materials[name] = _Material(
            name=name,
            speed=float(found.group(4)),
            tier_level=int(found.group(2)),
            durability=int(found.group(3)),
            attack_damage_bonus=float(found.group(5)),
            enchantment_value=int(found.group(6)),
        )


def _mineable_tags(ctx: _Ctx, index: JavaIndex, notes: list[str]) -> None:
    item = _text(index, ITEM_CLASS_FILE)
    if item is None:
        return
    for found in TOOL_METHOD_TAG.finditer(item[1]):
        ctx.mineable_tags[found.group(1)] = found.group(2)


def _tool_facts_263(ctx: _Ctx, index: JavaIndex, notes: list[str]) -> list[tuple[dict, str, str]]:
    source = _text(index, ITEM_FILE)
    if source is None:
        return []
    path, text = source
    ids_text = index.identifier_text("ItemIds") or ""
    for found in ITEM_ID_263.finditer(ids_text):
        ctx.item_ids[found.group(1)] = found.group(2)

    sword_rules: tuple[float | None, list[dict]] | None = None
    material = _text(index, "ToolMaterial")
    if material is not None:
        method = re.search(r"applySwordProperties\s*\([^)]*\)\s*\{", material[1])
        if method is not None:
            body_start = method.end() - 1
            body = material[1][body_start + 1 : match_brace(material[1], body_start)]
            sword_rules = _parse_tool_rules(body, ctx, notes)

    facts: list[tuple[dict, str, str]] = []
    for found in REGISTER_ITEM.finditer(text):
        args, close = _call_args(text, found)
        if close < 0:
            continue
        field_name = _field_name(text, found.start())
        if field_name is None or len(args) < 2:
            continue
        props = args[-1]
        target = MATERIAL_TOOL_263.search(props)
        has_component = "DataComponents.TOOL" in props
        if target is None and not has_component:
            # 绝大多数物品没有工具语义，注册名也不必解析
            continue
        item_id = _item_id(args[0], ctx)
        if item_id is None:
            notes.append(f"{field_name}: 物品注册名不可解析")
            continue
        if target is not None:
            kind, material_name = target.group(1), target.group(2).lower()
            ctx_material = ctx.materials.get(material_name)
            if ctx_material is None:
                notes.append(f"{field_name}: 未知工具material {material_name}")
                continue
            if kind == "sword":
                if sword_rules is None:
                    notes.append(f"{field_name}: 剑的 Tool 规则不可解析")
                    continue
                default, rules = sword_rules
            else:
                tag_const = ctx.mineable_tags.get(kind)
                if tag_const is None:
                    notes.append(f"{field_name}: 找不到 {kind} 的可挖掘标签")
                    continue
                default, rules = 1.0, [
                    _rule_payload(
                        f"tag:{ctx_material.incorrect_tag}", None, False
                    ),
                    _rule_payload(
                        f"tag:{_tag_name(ctx, tag_const)}", ctx_material.speed, True
                    ),
                ]
            facts.append(
                (
                    {
                        "form": "tool",
                        "item": f"minecraft:{item_id}",
                        "tool_class": kind,
                        "material": material_name,
                        "default_speed": default,
                        "rules": rules,
                    },
                    path,
                    item_id,
                )
            )
            continue

        if "DataComponents.TOOL" not in props:
            continue
        expression = _component_tool_expression(props)
        if expression is None:
            notes.append(f"{field_name}: TOOL 组件的实参不可解析")
            continue
        default, rules = _parse_tool_rules(_resolve_tool_factory(expression, index, notes), ctx, notes)
        facts.append(
            (
                {
                    "form": "tool",
                    "item": f"minecraft:{item_id}",
                    "tool_class": _classify_component_tool(expression),
                    "material": None,
                    "default_speed": default,
                    "rules": rules,
                },
                path,
                item_id,
            )
        )
    return facts


def _item_id(expr: str, ctx: _Ctx) -> str | None:
    literal = re.fullmatch(r'"(?:minecraft:)?([a-z0-9_]+)"', expr.strip())
    if literal is not None:
        return literal.group(1)
    found = re.fullmatch(r"ItemIds\.([A-Z][A-Z0-9_]*)", expr.strip())
    if found is not None:
        return ctx.item_ids.get(found.group(1))
    return None


def _component_tool_expression(props: str) -> str | None:
    """`DataComponents.TOOL, <expr>` 里的 `<expr>`。"""
    marker = props.find("DataComponents.TOOL")
    if marker < 0:
        return None
    head = props[:marker]
    open_index = head.rfind("component")
    if open_index < 0:
        return None
    paren = props.find("(", open_index)
    close = match_paren(props, paren)
    if close < 0:
        return None
    args = split_args(props[paren + 1 : close])
    return args[1] if len(args) > 1 else None


def _resolve_tool_factory(expr: str, index: JavaIndex, notes: list[str]) -> str:
    """`X.createToolProperties()` → 展开成那个方法体里的 `new Tool(...)`。"""
    call = re.fullmatch(r"\s*([A-Z]\w*)\.createToolProperties\(\)\s*", expr)
    if call is None:
        return expr
    java = index.load(call.group(1))
    if java is None:
        notes.append(f"找不到工具类 {call.group(1)}")
        return expr
    text = strip_comments(java.text)
    found = re.search(r"createToolProperties\s*\([^)]*\)\s*\{", text)
    if found is None:
        notes.append(f"{call.group(1)} 没有 createToolProperties()")
        return expr
    start = found.end() - 1
    return text[start + 1 : match_brace(text, start)]


def _classify_component_tool(expr: str) -> str | None:
    found = re.match(r"\s*([A-Z]\w*)\.createToolProperties", expr)
    if found is None:
        return None
    return {
        "ShearsItem": "shears",
        "MaceItem": "mace",
        "TridentItem": "trident",
    }.get(found.group(1), found.group(1).lower())


def _tool_facts_1182(ctx: _Ctx, index: JavaIndex, notes: list[str]) -> list[tuple[dict, str, str]]:
    source = _text(index, ITEM_FILE)
    if source is None:
        return []
    path, text = source

    digger_tags: dict[str, str] = {}
    for name in ("PickaxeItem", "AxeItem", "ShovelItem", "HoeItem"):
        java = _text(index, name)
        if java is None:
            continue
        found = re.search(r"super\([^;]*BlockTags\.([A-Z][A-Z0-9_]*)", java[1])
        if found is not None:
            digger_tags[name] = found.group(1)

    gates: list[dict] = []
    digger = _text(index, "DiggerItem")
    if digger is not None:
        for found in re.finditer(
            # The last tier may be decompiled as a ternary return.
            r"(?:\bif\s*\(|\breturn)\s*\w+\s*<\s*(\d+)\s*&&\s*blockState\.is\(BlockTags\.([A-Z][A-Z0-9_]*)\)",
            digger[1],
        ):
            gates.append(
                {
                    "selector": f"tag:{_tag_name(ctx, found.group(2))}",
                    "min_level": int(found.group(1)),
                }
            )

    override_rules: dict[str, list[dict]] = {}
    for name, tool_class in (("SwordItem", "sword"), ("ShearsItem", "shears")):
        java = _text(index, name)
        if java is None:
            continue
        found = re.search(r"public float getDestroySpeed\([^)]*\)\s*\{", java[1])
        if found is None:
            continue
        start = found.end() - 1
        override_rules[tool_class] = _if_return_rules(
            java[1][start + 1 : match_brace(java[1], start)], ctx, notes
        )

    facts: list[tuple[dict, str, str]] = []
    for found in REGISTER_ITEM.finditer(text):
        args, close = _call_args(text, found)
        if close < 0:
            continue
        field_name = _field_name(text, found.start())
        if field_name is None or len(args) < 2:
            continue
        constructed = _constructed(args[1:])
        if constructed is None:
            continue
        cls, ctor_args = constructed
        if cls not in (*digger_tags, "SwordItem", "ShearsItem", "TridentItem"):
            continue
        item_id = _item_id(args[0], ctx)
        if item_id is None:
            notes.append(f"{field_name}: 物品注册名不可解析")
            continue
        payload: dict = {
            "form": "tool",
            "item": f"minecraft:{item_id}",
            "tool_class": None,
            "material": None,
            "default_speed": 1.0,
            "rules": [],
        }
        tier = next(
            (arg.split(".")[-1].strip().lower() for arg in ctor_args if arg.strip().startswith("Tiers.")),
            None,
        )
        if cls in digger_tags:
            if tier is None or tier not in ctx.materials:
                notes.append(f"{field_name}: 工具等级不可解析")
                continue
            material = ctx.materials[tier]
            payload.update(
                {
                    "tool_class": cls.removesuffix("Item").lower(),
                    "material": tier,
                    "tier_level": material.tier_level,
                    "rules": [
                        _rule_payload(
                            f"tag:{_tag_name(ctx, digger_tags[cls])}",
                            material.speed,
                            True,
                        )
                    ],
                    "drop_level_gates": gates,
                }
            )
        elif cls == "SwordItem":
            payload.update(
                {
                    "tool_class": "sword",
                    "material": tier,
                    "rules": override_rules.get("sword", []),
                }
            )
        elif cls == "ShearsItem":
            payload.update(
                {"tool_class": "shears", "rules": override_rules.get("shears", [])}
            )
        elif cls == "TridentItem":
            payload.update({"tool_class": "trident"})
        else:
            continue
        facts.append((payload, path, item_id))
    return facts


def _if_return_rules(body: str, ctx: _Ctx, notes: list[str]) -> list[dict]:
    """从 `if (条件) { return 数; }` 序列里取「条件 → 速度」。"""
    rules: list[dict] = []
    for found in re.finditer(r"\bif\s*\(", body):
        open_index = found.end() - 1
        close = match_paren(body, open_index)
        if close < 0:
            continue
        condition = body[open_index + 1 : close]
        brace = body.find("{", close)
        if brace < 0:
            continue
        end = match_brace(body, brace)
        if end < 0:
            continue
        returned = re.search(r"return\s+\(?(-?\d+(?:\.\d+)?)[fF]\)?\s*;", body[brace:end])
        if returned is None:
            continue
        speed = float(returned.group(1))
        selectors = [
            f"block:minecraft:{ctx.blocks[name].block_id}"
            for name in IF_SEL_BLOCK.findall(condition)
            if name in ctx.blocks
        ]
        selectors += [f"tag:{_tag_name(ctx, name)}" for name in IF_SEL_TAG.findall(condition)]
        selectors += [
            f"material:minecraft:{name.lower()}" for name in IF_SEL_MATERIAL.findall(condition)
        ]
        if not selectors:
            notes.append(f"条件里的选择器不可解析：{condition.strip()[:60]}")
            continue
        for selector in selectors:
            rules.append(_rule_payload(selector, speed, None))
    # A trailing `else if` may be decompiled as a ternary. Only one side may be a literal;
    # on the `:` side the condition must be all negated terms (De Morgan).
    for found in TERNARY_RETURN.finditer(body):
        condition, when_true, when_false = found.groups()
        true_speed, false_speed = _as_float(when_true), _as_float(when_false)
        if (true_speed is None) == (false_speed is None):
            continue
        terms = re.findall(r"(!?)\s*blockState\.is\(", condition)
        if not terms:
            continue
        negated = {flag == "!" for flag in terms}
        if false_speed is not None and negated == {True} and "||" not in condition:
            speed = false_speed
        elif true_speed is not None and negated == {False} and "&&" not in condition:
            speed = true_speed
        else:
            continue
        selectors = [
            f"block:minecraft:{ctx.blocks[name].block_id}"
            for name in IF_SEL_BLOCK.findall(condition)
            if name in ctx.blocks
        ]
        selectors += [f"tag:{_tag_name(ctx, name)}" for name in IF_SEL_TAG.findall(condition)]
        for selector in selectors:
            rules.append(_rule_payload(selector, speed, None))
    return rules


def _material_facts(ctx: _Ctx, path: str) -> list[tuple[dict, str, str]]:
    facts = []
    for material in ctx.materials.values():
        payload = {
            "form": "material",
            "material": material.name,
            "speed": material.speed,
        }
        if material.durability is not None:
            payload["durability"] = material.durability
        if material.attack_damage_bonus is not None:
            payload["attack_damage_bonus"] = material.attack_damage_bonus
        if material.enchantment_value is not None:
            payload["enchantment_value"] = material.enchantment_value
        if material.incorrect_tag is not None:
            payload["incorrect_tool_tag"] = material.incorrect_tag
        if material.tier_level is not None:
            payload["tier_level"] = material.tier_level
        facts.append((payload, path, material.name))
    return facts


# ------------------------------------------------------------------ 公式


def _formula(index: JavaIndex, notes: list[str]) -> tuple[dict, str, list[str]] | None:
    behaviour = _text(index, BEHAVIOUR_FILE)
    player = _text(index, PLAYER_FILE)
    if behaviour is None:
        notes.append("没有 BlockBehaviour.java，无法取证挖掘进度公式")
        return None
    path, text = behaviour
    method = re.search(r"float getDestroyProgress\([^)]*\)\s*\{", text)
    if method is None:
        notes.append("BlockBehaviour 里找不到 getDestroyProgress")
        return None
    start = method.end() - 1
    body = text[start + 1 : match_brace(text, start)]

    missing: list[str] = []
    unbreakable = re.search(r"==\s*(-?\d+(?:\.\d+)?)[fF]", body)
    divisors = re.search(r"\?\s*(\d+)\s*:\s*(\d+)", body)
    if unbreakable is None:
        missing.append("unbreakable_hardness")
    if divisors is None:
        missing.append("divisors")

    payload: dict = {
        "form": "formula",
        "hardness_field": "destroyTime",
        "default_hardness": 0.0,
        "progress_per_tick": "player_speed / hardness / divisor",
        "completion": "累计进度 >= 1.0",
    }
    if unbreakable is not None:
        payload["unbreakable_hardness"] = float(unbreakable.group(1))
    if divisors is not None:
        payload["divisors"] = {
            "correct_tool": int(divisors.group(1)),
            "wrong_tool": int(divisors.group(2)),
        }
    if payload.get("unbreakable_hardness") == -1.0:
        payload["unbreakable_rule"] = "硬度为 -1 时每 tick 进度恒为 0"

    if player is not None:
        payload["player_speed"], player_missing = _player_speed(player[1])
        missing.extend(player_missing)
        payload["player_speed_file"] = player[0]
    else:
        missing.append("player_speed")
    return payload, path, missing


def _player_speed(text: str) -> tuple[dict, list[str]]:
    method = re.search(r"public float getDestroySpeed\([^)]*\)\s*\{", text)
    if method is None:
        return {}, ["player_speed"]
    start = method.end() - 1
    body = text[start + 1 : match_brace(text, start)]
    out: dict = {}
    missing: list[str] = []

    threshold = re.search(r">\s*(-?\d+(?:\.\d+)?)[fF]", body)
    if threshold is not None:
        out["efficiency_min_speed"] = float(threshold.group(1))
    else:
        missing.append("efficiency_min_speed")

    if "MINING_EFFICIENCY" in body:
        out["efficiency"] = "attribute:minecraft:mining_efficiency"
    elif "getBlockEfficiency" in body:
        out["efficiency"] = "enchantment:block_efficiency（n*n+1）"
    else:
        missing.append("efficiency")

    haste = re.search(r"\+\s*1\)\s*\*\s*(-?\d+(?:\.\d+)?)[fF]", body)
    if haste is not None:
        out["haste_multiplier_per_level"] = float(haste.group(1))
    else:
        missing.append("haste_multiplier_per_level")

    if "MINING_FATIGUE" in body or "DIG_SLOWDOWN" in body:
        fatigue = re.search(r"Math\.pow\(\s*(-?\d+(?:\.\d+)?)", body)
        out["fatigue"] = (
            f"pow({fatigue.group(1)}f, amplifier+1)"
            if fatigue is not None
            else "按 amplifier 分档（见来源方法）"
        )
    else:
        missing.append("fatigue")

    if "SUBMERGED_MINING_SPEED" in body:
        out["submerged"] = "attribute:minecraft:submerged_mining_speed"
    else:
        water = re.search(
            r"isEyeInFluid\(FluidTags\.WATER\).*?/[=]?\s*(-?\d+(?:\.\d+)?)[fF]", body, re.S
        )
        if water is not None:
            out["submerged_divisor"] = float(water.group(1))
        else:
            missing.append("submerged")

    air = re.search(
        r"!\s*this\.onGround.*?/[=]?\s*(-?\d+(?:\.\d+)?)[fF]", body, re.S
    )
    if air is not None:
        out["airborne_divisor"] = float(air.group(1))
    else:
        missing.append("airborne_divisor")
    return out, missing


# ------------------------------------------------------------------ idiom


class MiningFacts(Idiom):
    """方块硬度、工具挖掘速度与挖掘进度公式。"""

    id = ID
    needs = (BLOCK_FILE, ITEM_FILE)
    min_hits = 100

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()
        notes: list[str] = []
        ctx = _Ctx()

        tags = _text(index, TAGS_FILE)
        if tags is not None:
            for found in TAG_CONST.finditer(tags[1]):
                ctx.tags[found.group(1)] = found.group(2)

        _load_ids(ctx, index)
        ctx.mutations = _ctor_mutations(index)
        _materials(ctx, index, notes)
        _mineable_tags(ctx, index, notes)

        blocks_source = _text(index, BLOCK_FILE)
        if blocks_source is None:
            result.note("输入包里没有 Blocks.java，方块硬度无法取证")
            return result
        scan_text = blocks_source[1]
        _load_helpers(scan_text, ctx)
        produced = _scan_blocks(scan_text, ctx, notes)
        for block in produced:
            result.add_fact(
                Fact(
                    kind="mining",
                    payload={
                        "form": "block",
                        "block": f"minecraft:{block.block_id}",
                        "hardness": block.props.hardness,
                        "explosion_resistance": block.props.explosion,
                        "requires_correct_tool": block.props.requires_tool,
                    },
                    prov=Prov(input="src", file=blocks_source[0], ptr=block.block_id),
                ),
                blocks_source[0],
            )

        # Unqualified calls also match method definitions; do not count those.
        calls = sum(
            1 for found in FIELD_CALL.finditer(scan_text)
            if not METHOD_DEFINITION.search(scan_text[max(0, found.start() - 120) : found.start()])
        )
        internal = calls - ctx.claimed_calls
        if internal:
            notes.append(f"Blocks.java 方法体内的注册调用 {internal} 处（复制包装定义），由包装语义覆盖")

        materials_source = _text(index, "ToolMaterial") or _text(index, "Tiers")
        if materials_source is not None:
            for payload, path, ptr in _material_facts(ctx, materials_source[0]):
                result.add_fact(
                    Fact(
                        kind="mining",
                        payload=payload,
                        prov=Prov(input="src", file=path, ptr=ptr),
                    ),
                    path,
                )
        else:
            notes.append("没有工具材料表（ToolMaterial / Tiers）")

        tool_facts = (
            _tool_facts_263(ctx, index, notes)
            if _text(index, "ToolMaterial") is not None
            else _tool_facts_1182(ctx, index, notes)
        )
        for payload, path, ptr in tool_facts:
            result.add_fact(
                Fact(kind="mining", payload=payload, prov=Prov(input="src", file=path, ptr=ptr)),
                path,
            )

        formula = _formula(index, notes)
        if formula is not None:
            payload, path, missing = formula
            result.add_fact(
                Fact(
                    kind="mining",
                    payload=payload,
                    prov=Prov(input="src", file=path, ptr="getDestroyProgress"),
                    status=Status.PARTIAL if missing else Status.PARSED,
                    missing=tuple(missing),
                ),
                path,
            )

        for note in notes:
            result.note(note)

        # 本 idiom 读过的机制文件逐一声明，让台账把它们记成「由 idiom 提取」。
        # 注册名常量表（BlockItemIds / BlockIds / ItemIds）不在此列：它们由
        # `index.identifiers` 单独分桶，再登记会变成两处交代同一件事。
        for stem in (
            BLOCK_FILE,
            ITEM_FILE,
            BEHAVIOUR_FILE,
            PLAYER_FILE,
            ITEM_CLASS_FILE,
            TAGS_FILE,
            "ToolMaterial",
            "Tiers",
            "DiggerItem",
            "SwordItem",
            "ShearsItem",
            "MaceItem",
            "TridentItem",
            *COLLECTION_FILES,
        ):
            java = index.load(stem)
            if java is not None and java.path not in result.files:
                result.files.append(java.path)
        return result


def exported() -> list[Idiom]:
    return [MiningFacts()]
