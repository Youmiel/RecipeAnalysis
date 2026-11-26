"""idiom：村民交易表（1.18.x 形态，硬编码在 `VillagerTrades`）。

26.x 把交易搬进了数据包（`villager_trade/` + `trade_set/`），由 trades 适配器处理；
1.18.x 只能在代码里抽。这是一个典型的「机制搬家」案例。

**方向不可从类名推断**。实测教训：`EmeraldForItems` 是「用物品换绿宝石」，
名字读起来像反的。唯一可靠依据是 `MerchantOffer` 的**参数顺序**：

```
MerchantOffer(costA, result, maxUses, xp, priceMultiplier)          // 5 参
MerchantOffer(costA, costB, result, maxUses, xp, priceMultiplier)   // 6 参
```

所以每个交易类要回答一个问题：**它的构造实参里，第几个是产出物、第几个是支付物**。
这张表是人工知识（`knowledge/maps/` 下的机制表），每条带 `basis` 说明推导依据，未登记即硬失败。

覆盖策略：按实测占比排序，先覆盖 `ItemsForEmeralds`（63.1%）与
`EmeraldForItems`（21.2%），其余落台账要求补录。宁可少而准，不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ....facts import Fact
from ..index import JavaIndex
from ..javatext import java_ident, match_brace, split_args, strip_comments
from .base import Idiom, IdiomResult, Link

ID = "villager_trades"

PROFESSION_PUT = re.compile(r"hashMap\.put\(\s*VillagerProfession\.([A-Z][A-Z0-9_]*)\s*,")
# Nested classes may be qualified with the outer class name.
LEVEL_ARRAY = re.compile(r"(?<![\w.])(\d+)\s*,\s*new\s+(?:VillagerTrades\.)?ItemListing\[\]\s*\{")
OFFER_CONSTRUCT = re.compile(r"new\s+(?:VillagerTrades\.)?([A-Z]\w+)\s*\(")
TRADES_REGION_START = re.compile(r"Map<VillagerProfession[^;]*?=\s*Util\.make\(")

# 1.18.x 的流浪商人交易表不按职业注册，而是独立的一张
# `WANDERING_TRADER_TRADES = VillagerTrades.toIntMap(ImmutableMap.of(1, new ItemListing[]{…}, …))`。
# 它不在 `VillagerTrades` 的 `TRADES` 区域里，只扫 `hashMap.put(VillagerProfession.X, …)`
# 会整批漏掉——附魔书、藏宝图这类关键货物正在其中。
WANDERER_TABLE = re.compile(
    r"WANDERING_TRADER_TRADES\s*=\s*(?:VillagerTrades\.)?toIntMap\("
)
WANDERER_PROFESSION = "wandering_trader"

# 交易数组里出现的、**不是** ItemListing 实现的构造。
# 这些不该被当成「未登记的交易类」报出来，否则真正的缺口会被噪音埋掉。
# 每一条都对应一个具体用途，不是「看起来不像就跳过」。
NOT_A_LISTING = {
    "ItemStack": "匿名 ItemListing 的 getOffer 体里拼物品堆",
    "MerchantOffer": "匿名 ItemListing 的 getOffer 体里构造提议",
    "ArrayList": "getOffer 里拼染料/效果列表",
    "EnchantmentInstance": "getOffer 里造附魔实例",
    "TranslatableComponent": "getOffer 里设物品显示名",
}

# ---- 交易参数（经验值、使用次数、价格浮动）--------------------------------

# 反编译后的形参名是 `n` / `n2` / `n3`，**不能靠名字判断语义**。但构造器体里
# `this.villagerXp = n3;` 把「哪个形参给了哪个字段」写明了，按这条取。
FIELD_ASSIGN = re.compile(
    r"this\.(villagerXp|xp|maxUses|priceMultiplier)\s*=\s*([^;]+);"
)
# 字段名 → 事实里的键。与新版数据包侧的 `meta` 键对齐（`max_uses` / `xp`）。
FIELD_KEYS = {
    "villagerXp": "xp",
    "xp": "xp",
    "maxUses": "max_uses",
    "priceMultiplier": "price_multiplier",
}
CLASS_BODY = re.compile(r"\bclass\s+([A-Z][A-Za-z0-9_]*)[^{]*\{")
CTOR_HEAD = re.compile(r"public\s+([A-Z][A-Za-z0-9_]*)\s*\(")
THIS_DELEGATE = re.compile(r"this\s*\(([^;]*)\)\s*;")

# `(argc, 字段)` → `("arg", 形参下标)` 或 `("const", 数值)`
ParamSource = tuple[str, object]


@dataclass(frozen=True)
class Stack:
    """交易里的一项物品。

    四项来源各自有判据，不互相冒充：

    | 字段 | 来源 | 例子 |
    |---|---|---|
    | `arg` | 某个构造实参里的物品 | `Items.COD` |
    | `fixed` | 类内写死的常量物品 | `EnchantBookForEmeralds` 恒要一本书 |
    | `map_arg` | 实参是一组「类型 → 物品」的映射 | `EmeraldsForVillagerTypeItem` |
    | `count_arg` / `count_fixed` | 数量 | 同上，或固定 1 |

    `variant` 只进 `meta`：产出物带随机属性（附魔、药水、染料）时，物品 id 不变，
    IR 的节点也不该改；但读者必须知道「拿到的不是白板物品」。
    """

    arg: int | None = None
    fixed: str | None = None
    map_arg: int | None = None
    count_arg: int | None = None
    count_fixed: int | None = None
    variant: str = ""

    def _count(self, args: list[str]) -> int:
        if self.count_arg is not None and self.count_arg < len(args):
            raw = args[self.count_arg].strip()
            if raw.isdigit():
                return int(raw)
        return self.count_fixed if self.count_fixed is not None else 1

    def resolve(self, args: list[str]) -> tuple[str, int] | None:
        """单个物品堆。`map_arg` 形态返回第一个条目，见 `variants`。"""
        found = self.variants(args)
        return found[0] if found else None

    def variants(self, args: list[str]) -> list[tuple[str, int]]:
        """全部可能的物品堆。`map_arg` 会展开成多条——一笔交易按村民类型换不同的东西。"""
        if self.fixed is not None:
            return [(self.fixed, self._count(args))]
        if self.map_arg is not None:
            if self.map_arg >= len(args):
                return []
            # `ImmutableMap.builder().put(VillagerType.PLAINS, Items.OAK_BOAT)…`
            # 只要 value 侧：key 是村民类型，不是物品。
            out = []
            for item in re.findall(r",\s*(Items|Blocks)\.([A-Z][A-Z0-9_]*)", args[self.map_arg]):
                out.append((item[1].lower(), self._count(args)))
            return out
        if self.arg is not None and self.arg < len(args):
            ident = java_ident(args[self.arg])
            if ident is None:
                return []
            return [(ident, self._count(args))]
        return []


@dataclass(frozen=True)
class OfferShape:
    """一个交易类的实参 → 交易语义。

    一笔交易可以**要好几样东西**（附魔书要绿宝石 + 书，藏宝图要绿宝石 + 指南针，
    烤鱼要绿宝石 + 生鱼），所以支付侧是列表而不是单值。早期只支持一个支付物，
    结果是那几类交易整体无法表达。
    """

    give: tuple[Stack, ...]
    pay: tuple[Stack, ...]
    basis: str

    def resolve_give(self, args: list[str]) -> tuple[str, int] | None:
        for stack in self.give:
            found = stack.variants(args)
            if found:
                return found[0]
        return None

    def resolve_pay(self, args: list[str]) -> tuple[str, int] | None:
        for stack in self.pay:
            found = stack.variants(args)
            if found:
                return found[0]
        return None

    def pay_variants(self, args: list[str]) -> list[list[tuple[str, int]]]:
        """支付侧的每一「组」备选。按 Map 展开时会得到多组，每组一条规则。"""
        groups: list[list[tuple[str, int]]] = [[]]
        for stack in self.pay:
            found = stack.variants(args)
            if not found:
                continue
            if stack.map_arg is not None and len(found) > 1:
                groups = [group + [item] for group in groups for item in found]
            else:
                groups = [group + [found[0]] for group in groups]
        return [group for group in groups if group]

    @property
    def give_variant(self) -> str:
        return "、".join(s.variant for s in self.give if s.variant)

    @property
    def pay_variant(self) -> str:
        return "、".join(s.variant for s in self.pay if s.variant)


# 交易类 → 实参语义。全部依据 `getOffer` 里 `MerchantOffer` 的参数顺序推出。
#
# 写表时踩过的坑：把 `EmeraldForItems(Items.WHEAT, 20, 16, 2)` 当成「得到小麦」是**反的**。
# 它的 getOffer 是 `MerchantOffer(new ItemStack(wheat, cost), new ItemStack(EMERALD), ...)`，
# 即用 20 个小麦换 1 个绿宝石。产出恒为绿宝石，实参里只有支付物。
#
# 多支付物的三个类（附魔书、藏宝图、烤鱼）曾是「提取不到」的最大块：它们要
# MerchantOffer 的 costA + costB 两个支付位，单值模型表达不了。
OFFER_SHAPES: dict[str, OfferShape] = {
    "ItemsForEmeralds": OfferShape(
        give=(Stack(arg=0, count_arg=2),),
        pay=(Stack(fixed="emerald", count_arg=1),),
        basis="MerchantOffer(emeralds×arg1, item×arg2)：付绿宝石，得 arg0 共 arg2 个",
    ),
    "EmeraldForItems": OfferShape(
        give=(Stack(fixed="emerald", count_fixed=1),),
        pay=(Stack(arg=0, count_arg=1),),
        basis="MerchantOffer(item×arg1, emeralds, ...)：付 arg0 共 arg1 个，得 1 个绿宝石",
    ),
    "SuspiciousStewForEmerald": OfferShape(
        give=(Stack(fixed="suspicious_stew", count_fixed=1, variant="带随机效果"),),
        pay=(Stack(fixed="emerald", count_fixed=1),),
        basis="MerchantOffer(emerald, SUSPICIOUS_STEW, ...)：两个物品都是常量，实参只是效果",
    ),
    "ItemsAndEmeraldsToItems": OfferShape(
        # 6 参重载把 emeraldCost 折叠成 1，见 OFFER_SHAPES_BY_ARGC
        give=(Stack(arg=2, count_arg=3),),
        pay=(
            Stack(fixed="emerald", count_fixed=1),
            Stack(arg=0, count_arg=1),
        ),
        basis="MerchantOffer(emeralds, fromItem×arg1, toItem×arg3, ...)：付绿宝石 + 原料，得成品",
    ),
    "EnchantedItemForEmeralds": OfferShape(
        give=(Stack(arg=0, count_fixed=1, variant="随机附魔"),),
        pay=(Stack(fixed="emerald", count_arg=1),),
        basis="MerchantOffer(emeralds×base+rand, enchantItem(item), ...)：付绿宝石，得附魔后的 arg0",
    ),
    "EmeraldsForVillagerTypeItem": OfferShape(
        give=(Stack(fixed="emerald", count_fixed=1),),
        # 付什么取决于村民类型：`ImmutableMap.builder().put(VillagerType.X, Items.Y)`
        pay=(Stack(map_arg=3, count_arg=0),),
        basis="MerchantOffer(trades.get(type)×cost, emeralds)：支付物按村民类型取 map 的 value",
    ),
    "TippedArrowForItemsAndEmeralds": OfferShape(
        give=(Stack(arg=2, count_arg=3, variant="带随机药水"),),
        pay=(
            Stack(fixed="emerald", count_arg=4),
            Stack(arg=0, count_arg=1),
        ),
        basis="MerchantOffer(emeralds×arg4, fromItem×arg1, tippedResult, ...)：付绿宝石 + 箭，得药箭",
    ),
    "EnchantBookForEmeralds": OfferShape(
        give=(Stack(fixed="enchanted_book", count_fixed=1, variant="随机附魔"),),
        pay=(
            # 绿宝石数量随附魔等级浮动（`2 + rand(5+10n) + 3n`），只判「要绿宝石」
            Stack(fixed="emerald", count_fixed=1, variant="数量随附魔等级浮动"),
            Stack(fixed="book", count_fixed=1),
        ),
        basis="MerchantOffer(emeralds×随机, BOOK, enchantedBook, ...)：付绿宝石 + 书，得附魔书",
    ),
    "TreasureMapForEmeralds": OfferShape(
        give=(Stack(fixed="filled_map", count_fixed=1, variant="指向指定结构"),),
        pay=(
            Stack(fixed="emerald", count_arg=0),
            Stack(fixed="compass", count_fixed=1),
        ),
        basis="MerchantOffer(emeralds×arg0, COMPASS, map, ...)：付绿宝石 + 指南针，得地图",
    ),
    "DyedArmorForEmeralds": OfferShape(
        give=(Stack(arg=0, count_fixed=1, variant="随机染色"),),
        pay=(Stack(fixed="emerald", count_arg=1),),
        basis="MerchantOffer(emeralds×arg1, dyedArmor(arg0), ...)：付绿宝石，得染色后的 arg0",
    ),
}

# 同名类有多个重载、且实参下标不同时按实参数分派。
# 键是 `(类名, 实参个数)`，查不到再退回 `OFFER_SHAPES` 的默认语义。
OFFER_SHAPES_BY_ARGC: dict[tuple[str, int], OfferShape] = {
    # 7 参重载：`(from, fromCount, emeraldCost, to, toCount, maxUses, xp)`
    # 比 6 参多一个 emeraldCost，于是 to/toCount 整体后移一位。
    ("ItemsAndEmeraldsToItems", 7): OfferShape(
        give=(Stack(arg=3, count_arg=4),),
        pay=(
            Stack(fixed="emerald", count_arg=2),
            Stack(arg=0, count_arg=1),
        ),
        basis="MerchantOffer(emeralds×arg2, fromItem×arg1, toItem×arg4, ...)："
        "7 参重载把 emeraldCost 显式给出，to 与 toCount 因此后移一位",
    ),
}


def _offer_shape(class_name: str, argc: int | None = None) -> OfferShape | None:
    if argc is not None:
        exact = OFFER_SHAPES_BY_ARGC.get((class_name, argc))
        if exact is not None:
            return exact
    return OFFER_SHAPES.get(class_name)


# ---- 交易参数：按构造器体的字段赋值回取 -----------------------------------


def _split_params(text: str) -> list[str]:
    """按顶层逗号切形参列表，**含泛型尖括号**。

    `split_args` 不把 `<>` 计入嵌套，`Map<VillagerType, Item> map` 会被切成两段，
    形参个数于是多一，`(类名, 实参个数)` 键全部对不上——两版的这类交易
    会静默丢掉经验值。
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _param_names(params_text: str) -> list[str]:
    """`Item item, int n, Map<K, V> map` → `["item", "n", "map"]`。"""
    names: list[str] = []
    for segment in _split_params(params_text):
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", segment)
        if tokens:
            names.append(tokens[-1])
    return names


def _number(expr: str) -> int | float | None:
    """数字字面量。`0.05f` 这类浮点后缀要剥掉，否则整个字段会被判成不可解析。"""
    text = expr.strip().rstrip("fF")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return None


def _source_of(expr: str, params: list[str]) -> ParamSource | None:
    """表达式相对形参列表的来源：形参下标或常量。"""
    name = expr.strip()
    if name in params:
        return ("arg", params.index(name))
    number = _number(name)
    if number is not None:
        return ("const", number)
    return None


def _ctor_bodies(class_body: str, class_name: str) -> dict[int, tuple[list[str], str]]:
    """`{实参个数: (形参名, 构造器体)}`。同一类只保留一个同参数量的构造器。"""
    out: dict[int, tuple[list[str], str]] = {}
    for match in CTOR_HEAD.finditer(class_body):
        if match.group(1) != class_name:
            continue
        open_paren = class_body.index("(", match.end() - 1)
        close = _match_paren(class_body, open_paren)
        if close < 0:
            continue
        params = _param_names(class_body[open_paren + 1 : close])
        brace = class_body.find("{", close)
        if brace < 0:
            continue
        end = match_brace(class_body, brace)
        if end < 0:
            continue
        out[len(params)] = (params, class_body[brace + 1 : end])
    return out


def _direct_sources(params: list[str], body: str) -> dict[str, ParamSource]:
    sources: dict[str, ParamSource] = {}
    for match in FIELD_ASSIGN.finditer(body):
        source = _source_of(match.group(2), params)
        if source is not None:
            sources[FIELD_KEYS[match.group(1)]] = source
    return sources


def _delegated_sources(
    params: list[str],
    body: str,
    ctors: dict[int, tuple[list[str], str]],
    depth: int = 0,
) -> dict[str, ParamSource]:
    """构造器只做委托时（`this(new ItemStack(item), n, n2, 12, n3);`），按位置回溯。

    委托实参里既有当前形参，也有常量（`ItemsForEmeralds` 的 4 参重载把
    maxUses 固定成 12）。所以不能只按名字搬，要把两边都归到当前构造器的形参上。
    """
    if depth > 3:
        return {}
    match = THIS_DELEGATE.search(body)
    if match is None:
        return {}
    args = split_args(match.group(1))
    target = ctors.get(len(args))
    if target is None:
        return {}
    target_params, target_body = target
    sources = _direct_sources(target_params, target_body)
    if not sources:
        sources = _delegated_sources(target_params, target_body, ctors, depth + 1)
    out: dict[str, ParamSource] = {}
    for field, (kind, value) in sources.items():
        if kind == "const":
            out[field] = ("const", value)
            continue
        if not isinstance(value, int) or not 0 <= value < len(args):
            continue
        source = _source_of(args[value], params)
        if source is not None:
            out[field] = source
    return out


def offer_params(text: str) -> dict[tuple[str, int], dict[str, ParamSource]]:
    """`(类名, 实参个数) → {字段: 来源}`。

    反编译后的形参名是 `n` / `n2` / `n3`，靠名字判断语义会全错；构造器体里的
    `this.villagerXp = n3;` 是唯一可靠依据。字段名统一成数据包侧的键：
    `xp` / `max_uses` / `price_multiplier`。
    """
    out: dict[tuple[str, int], dict[str, ParamSource]] = {}
    for match in CLASS_BODY.finditer(text):
        class_name = match.group(1)
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            continue
        end = match_brace(text, brace)
        if end < 0:
            continue
        class_body = text[brace + 1 : end]
        ctors = _ctor_bodies(class_body, class_name)
        for argc, (params, body) in ctors.items():
            sources = _direct_sources(params, body)
            if not sources:
                sources = _delegated_sources(params, body, ctors)
            if sources:
                out[(class_name, argc)] = sources
    return out


def _param_meta(
    class_name: str,
    args: list[str],
    params: dict[tuple[str, int], dict[str, ParamSource]],
) -> dict:
    """把一条交易的实参按参数表换成 `xp` / `max_uses` / `price_multiplier`。"""
    spec = params.get((class_name, len(args)))
    if not spec:
        return {}
    out: dict = {}
    for field, (kind, value) in spec.items():
        if kind == "const":
            out[field] = value
            continue
        if not isinstance(value, int) or not 0 <= value < len(args):
            continue
        number = _number(args[value])
        if number is None:
            # 实参是变量或表达式：不猜值，留痕让下游知道这一项没取到
            out[f"unresolved_{field}"] = args[value].strip()
            continue
        out[field] = number
    return out


class VillagerTrades(Idiom):
    id = ID
    needs = ("trade_listing", "merchant_offer")
    min_hits = 1

    def run(self, index: JavaIndex) -> IdiomResult:
        result = IdiomResult()

        targets = index.find_path_contains("VillagerTrades.java")
        if not targets:
            result.note("未找到 VillagerTrades.java（26.x 起交易在数据包，属正常）")
            return result

        for java in targets:
            self._extract(java, result)

        return result

    def _extract(self, java, result: IdiomResult) -> None:
        text = strip_comments(java.text)
        region = self._trades_region(text)
        if region is None:
            result.note(f"{java.path}: 未找到交易表初始化区，无法提取")
            return
        params = offer_params(text)

        produced = 0
        unknown_classes: dict[str, int] = {}

        for match in PROFESSION_PUT.finditer(region):
            profession = match.group(1).lower()

            # 只取该 `hashMap.put(...)` 语句自身的范围。
            # 若从 match.end() 一路扫到 region 末尾，会把后面所有职业的等级数组
            # 也算进当前职业，产出成倍膨胀（实测会从 ~300 条虚增到 2500+）。
            open_paren = region.index("(", match.end() - 1)
            close_paren = _match_paren(region, open_paren)
            if close_paren < 0:
                result.note(f"{java.path}: {profession} 的 put 语句括号不配对")
                continue
            statement = region[open_paren + 1 : close_paren]
            produced += self._emit_levels(
                statement, profession, java, result, unknown_classes, params
            )

        # 流浪商人：独立的一张表，按等级 1/2 两个数组。
        wanderer = WANDERER_TABLE.search(text)
        if wanderer is None:
            # 只有存在 VillagerTrades 时才该有这张表；缺了必须留痕，
            # 否则「流浪商人卖什么」会静默变成「没有这种交易」。
            result.note(
                f"{java.path}: 未找到 WANDERING_TRADER_TRADES，"
                "流浪商人的交易未提取（不是「没有这种交易」）"
            )
        else:
            open_paren = text.index("(", wanderer.end() - 1)
            close_paren = _match_paren(text, open_paren)
            if close_paren < 0:
                result.note(f"{java.path}: WANDERING_TRADER_TRADES 括号不配对")
            else:
                produced += self._emit_levels(
                    text[open_paren + 1 : close_paren],
                    WANDERER_PROFESSION,
                    java,
                    result,
                    unknown_classes,
                    params,
                )

        for class_name, count in sorted(unknown_classes.items()):
            result.note(
                f"{java.path}: 交易类 {class_name} 未登记实参语义（{count} 处），"
                "该类的交易未提取；需在 OFFER_SHAPES 补一条并说明依据"
            )

        if produced == 0:
            result.note(f"{java.path}: 交易表里没有能解析的条目")

    def _emit_levels(
        self,
        statement: str,
        profession: str,
        java,
        result: IdiomResult,
        unknown_classes: dict[str, int],
        params: dict[tuple[str, int], dict[str, ParamSource]],
    ) -> int:
        """解析一段交易表文本里的全部 `等级 → ItemListing[]`，返回产出的链接数。"""
        produced = 0
        for level_match in LEVEL_ARRAY.finditer(statement):
            level = int(level_match.group(1))
            brace_index = statement.index("{", level_match.start())
            close = match_brace(statement, brace_index)
            if close < 0:
                result.note(f"{java.path}: {profession} 等级 {level} 的花括号不配对")
                continue
            body = statement[brace_index + 1 : close]

            for offer in self._offers_in(body):
                for link in offers_to_links(offer, profession, level, params):
                    result.add(
                        link,
                        ID,
                        java.path,
                        f"VillagerTrades/{profession}[{level}]",
                    )
                    produced += 1

            for class_name in OFFER_CONSTRUCT.findall(body):
                if _offer_shape(class_name) is not None:
                    continue
                if class_name in NOT_A_LISTING:
                    continue
                unknown_classes[class_name] = unknown_classes.get(class_name, 0) + 1
        return produced

    def _trades_region(self, text: str) -> str | None:
        start = TRADES_REGION_START.search(text)
        if start is None:
            return None
        return text[start.end() :]

    def _offers_in(self, body: str) -> list[tuple[str, list[str]]]:
        """从数组体里取出 `(类名, 实参)` 列表，只保留已登记的类。"""
        out: list[tuple[str, list[str]]] = []
        for match in OFFER_CONSTRUCT.finditer(body):
            class_name = match.group(1)
            if _offer_shape(class_name) is None:
                continue
            open_paren = body.index("(", match.start())
            close = _match_paren(body, open_paren)
            if close < 0:
                continue
            out.append((class_name, split_args(body[open_paren + 1 : close])))
        return out


def _match_paren(text: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def offers_to_links(
    offer: tuple[str, list[str]],
    profession: str,
    level: int,
    params: dict[tuple[str, int], dict[str, ParamSource]] | None = None,
) -> list[Link]:
    """把一条交易转成 IR 形状的链接；支付物按 Map 展开时会有多条。

    `head` 是所得物品，支付物品进 `inputs`（消耗品，必须先有）。

    ## 村民用假设节点，不用 `entity:villager`

    代码侧最早把村民当 `via`（中介者），求解器于是要求
    `entity:minecraft:villager` 可达。但它在两个版本都零规则、状态 FALSE
    （结构生成村民这条链没有提取），结果是**整批交易规则恒假**：1.18.2 的
    附魔书、藏宝图因此一律不可达，而数据包形态的 26.3 却正常——同一机制在
    两个版本判定相反。

    所以这里与数据包族对齐：前提就是 `assume:villager_trade` /
    `assume:wandering_trader`（按职业选，见 ADR 0006）。村民是
    交易得以发生的**前提**，不是要递归求可达的物品。

    产出物带随机属性（附魔、药水、染料）时，`item` 节点不变，属性写进
    `meta.give_variant`：求解器判的是「能不能拿到附魔书」，不是「拿到哪一条附魔」。
    """
    class_name, args = offer
    shape = _offer_shape(class_name, len(args))
    assert shape is not None

    give = shape.resolve_give(args)
    give_item, give_count = give if give is not None else ("unknown", 1)

    pay_groups = shape.pay_variants(args)
    if not pay_groups:
        pay_groups = [[]]

    links: list[Link] = []
    for group in pay_groups:
        meta: dict = {
            "profession": profession,
            "level": level,
            "give_count": give_count,
            "pay_counts": [count for _, count in group],
            "shape": class_name,
        }
        if params:
            # 经验值、使用次数、价格浮动：构造器体里的字段赋值就是判据，
            # 不是从实参位置猜的。取不到时留 `unresolved_*`，不猜值。
            meta.update(_param_meta(class_name, args, params))
        variant = shape.give_variant
        if variant:
            meta["give_variant"] = variant
        pay_variant = shape.pay_variant
        if pay_variant:
            meta["pay_variant"] = pay_variant
        if give_item == "unknown":
            meta["unresolved_give"] = True
        if not group:
            meta["unresolved_pay"] = True
        links.append(
            Link(
                head=f"item:minecraft:{give_item}",
                kind="yield",
                # 假设节点不是「生物」而是「这件事可发生」，所以不放 via：
                # 放进去它就要被求可达，而它的存在由假设清单直接给定。
                via=(),
                inputs=(
                    *_assumption_nodes(profession),
                    *[f"item:minecraft:{item}" for item, _ in group],
                ),
                meta=meta,
            )
        )
    return links


# 职业 → 前提节点。与 `ir/families/trades.py` 的判据必须一致：
# 流浪商人随机生成、不补货、不依赖村庄，单列一条假设（ADR 0006）；
# 其余职业共用村民交易（ADR 0006）。这里硬编码而不是读 knowledge，
# 因为 idiom 层不读文件，且「哪个职业是流浪商人」是数据形状，不是立场。
def _assumption_nodes(profession: str) -> tuple[str, ...]:
    if profession == WANDERER_PROFESSION:
        return ("assume:wandering_trader",)
    return ("assume:villager_trade",)


def offers_to_link(
    offer: tuple[str, list[str]],
    profession: str,
    level: int,
    params: dict[tuple[str, int], dict[str, ParamSource]] | None = None,
) -> Link:
    """单条形态。`EmeraldsForVillagerTypeItem` 会展开成多条，这里只取第一条。"""
    return offers_to_links(offer, profession, level, params)[0]


def exported() -> list[Idiom]:
    return [VillagerTrades()]
