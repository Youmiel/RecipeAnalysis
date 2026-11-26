"""Decompiled source idioms: unqualified and qualified calls, diamond generics,
upper-case float suffixes, nested class names and ternary returns."""

from __future__ import annotations

import unittest

from ra.adapters.source import identifiers
from ra.adapters.source.javatext import split_args
from ra.adapters.source.idioms import buckets, fluid_buckets, loot_alias, mining, place
from ra.adapters.source.idioms import structure_containers as sc
from ra.adapters.source.idioms import trades


class BlockRegistrationTest(unittest.TestCase):
    def test_bare_and_qualified_calls_both_match(self) -> None:
        for text in (
            "public static final Block STONE = register(BlockItemIds.STONE, p);",
            "public static final Block STONE = Blocks.register(BlockItemIds.STONE, p);",
            "public static final Block S = registerSlab(BlockItemIds.S, STONE);",
        ):
            self.assertIsNotNone(mining.FIELD_CALL.search(text), text)

    def test_other_receivers_are_not_block_registrations(self) -> None:
        for text in (
            "X = this.register(a, b);",
            "X = Items.register(a, b);",
            "X = registerBlock(a, b);",
        ):
            self.assertIsNone(mining.FIELD_CALL.search(text), text)

    def test_method_definitions_are_told_apart_from_calls(self) -> None:
        text = (
            "private static Block register(final BlockItemId id, final Properties p) {\n"
            "    return register(id, Block::new, p);\n"
            "}"
        )
        starts = [found.start() for found in mining.FIELD_CALL.finditer(text)]
        self.assertEqual(len(starts), 2)
        definitions = [
            start for start in starts if mining.METHOD_DEFINITION.search(text[max(0, start - 120) : start])
        ]
        self.assertEqual(definitions, starts[:1])

    def test_loot_alias_declaration(self) -> None:
        self.assertIsNotNone(loot_alias.REGISTER_DECL.search("STONE_WALL = register("))
        self.assertIsNotNone(loot_alias.REGISTER_DECL.search("STONE_WALL = Blocks.register("))


class IdentifierTest(unittest.TestCase):
    def test_scalar_id_without_owner(self) -> None:
        for text in (
            'public static final BlockItemId STONE = create("stone");',
            'public static final BlockItemId STONE = BlockItemId.create("stone");',
        ):
            found = mining.SCALAR_ID.search(text)
            self.assertIsNotNone(found, text)
            self.assertEqual(found.group(2), "stone")

    def test_constant_declaration_without_owner(self) -> None:
        for text in ('APPLE = create("apple")', 'APPLE = ItemIds.create("apple")'):
            found = identifiers.CONSTANT_DECL.search(text)
            self.assertIsNotNone(found, text)
            self.assertEqual(found.group(2), "apple")

    def test_item_id_without_owner(self) -> None:
        found = mining.ITEM_ID_263.search('public static final ResourceKey<Item> APPLE = create("apple");')
        self.assertIsNotNone(found)
        self.assertEqual(found.group(2), "apple")


class DiamondGenericTest(unittest.TestCase):
    def test_color_names_from_diamond_constructor(self) -> None:
        text = (
            "public static final ColorCollection<DyeColor> VALUES = new ColorCollection<>(\n"
            "    DyeColor.WHITE, DyeColor.ORANGE);"
        )
        self.assertEqual(mining._color_names(text)[:2], ("white", "orange"))

    def test_by_state_with_diamond(self) -> None:
        for text in (
            'NAMES = new WeatheringCopperCollection.ByState<>("a", "b")',
            'NAMES = new WeatheringCopperCollection.ByState<String>("a", "b")',
        ):
            self.assertIsNotNone(mining.BY_STATE.search(text), text)


class FloatSuffixTest(unittest.TestCase):
    def test_tool_material_accepts_upper_case_suffix(self) -> None:
        text = (
            "public static final ToolMaterial IRON = new ToolMaterial("
            "BlockTags.INCORRECT_FOR_IRON_TOOL, 250, 6.0F, 2.0F, 14,"
        )
        found = mining.MATERIAL_263.search(text)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(4), "6.0")

    def test_tool_method_with_final_parameters(self) -> None:
        text = (
            "public Item.Properties pickaxe(final ToolMaterial material, "
            "final float attackDamageBaseline, final float attackSpeedBaseline) {\n"
            "    return this.tool(material, BlockTags.MINEABLE_WITH_PICKAXE"
        )
        found = mining.TOOL_METHOD_TAG.search(text)
        self.assertIsNotNone(found)
        self.assertEqual(found.groups(), ("pickaxe", "MINEABLE_WITH_PICKAXE"))


class ItemRegistrationTest(unittest.TestCase):
    def test_owner_group_marks_qualified_calls(self) -> None:
        # Unqualified calls count only inside `Items.java`.
        for pattern in (buckets.REGISTER_DECL, fluid_buckets.REGISTER_DECL, place.REGISTER_DECL):
            bare = pattern.search("TADPOLE_BUCKET = registerItem(")
            qualified = pattern.search("TADPOLE_BUCKET = Items.registerItem(")
            self.assertIsNotNone(bare)
            self.assertIsNone(bare.group(2))
            self.assertEqual(qualified.group(2), "Items")


class StructureTypeRegistrationTest(unittest.TestCase):
    def test_bare_and_qualified_registrations(self) -> None:
        bare = sc.REGISTRATION.findall(
            'StructureType<BuriedTreasureStructure> BURIED_TREASURE = '
            'register("buried_treasure", BuriedTreasureStructure.CODEC);'
        )
        qualified = sc.REGISTRATION.findall(
            'StructureFeature.register("mineshaft", new MineshaftFeature(c));'
        )
        self.assertEqual(bare, [("", "buried_treasure", "BuriedTreasureStructure")])
        self.assertEqual(qualified, [("StructureFeature", "mineshaft", "MineshaftFeature")])

    def test_other_registries_are_not_structure_types(self) -> None:
        self.assertEqual(sc.REGISTRATION.findall('Registry.register("x", FooStructure.CODEC)'), [])


class QualifiedNestedClassTest(unittest.TestCase):
    """Nested classes with and without the outer class name."""

    def test_level_array(self) -> None:
        for text in ("1, new ItemListing[]{", "1,\n new VillagerTrades.ItemListing[]{"):
            found = trades.LEVEL_ARRAY.search(text)
            self.assertIsNotNone(found, text)
            self.assertEqual(found.group(1), "1")

    def test_offer_construct_takes_inner_name(self) -> None:
        for text in ("new EmeraldForItems(Items.WHEAT, 20, 16, 2)",
                     "new VillagerTrades.EmeraldForItems(Items.WHEAT, 20, 16, 2)"):
            self.assertEqual(trades.OFFER_CONSTRUCT.findall(text), ["EmeraldForItems"], text)

    def test_wanderer_table_with_bare_call(self) -> None:
        for text in ("WANDERING_TRADER_TRADES = toIntMap(", "WANDERING_TRADER_TRADES = VillagerTrades.toIntMap("):
            self.assertIsNotNone(trades.WANDERER_TABLE.search(text), text)


class GenericArgumentTest(unittest.TestCase):
    def test_explicit_type_arguments_do_not_split(self) -> None:
        args = split_args(
            "1, 12, ImmutableMap.<VillagerType, Item>builder().put(VillagerType.PLAINS, Items.OAK_BOAT).build()"
        )
        self.assertEqual(len(args), 3)
        self.assertTrue(args[2].startswith("ImmutableMap.<VillagerType, Item>builder()"))

    def test_generic_constructor_and_comparison(self) -> None:
        self.assertEqual(
            split_args("a < B, new HashMap<String, Integer>(), x<y"),
            ["a < B", "new HashMap<String, Integer>()", "x<y"],
        )


class TernarySpeedRuleTest(unittest.TestCase):
    """Speed rules written as a trailing ternary."""

    def _ctx(self) -> mining._Ctx:
        ctx = mining._Ctx()
        for field, block_id in (("VINE", "vine"), ("GLOW_LICHEN", "glow_lichen")):
            ctx.blocks[field] = mining._Block(block_id, mining._Props(0.2, 0.2, False))
        return ctx

    def test_negated_conjunction_with_literal_on_false_side(self) -> None:
        body = (
            "return !blockState.is(Blocks.VINE) && !blockState.is(Blocks.GLOW_LICHEN)"
            " ? super.getDestroySpeed(itemStack, blockState) : 2.0F;"
        )
        rules = mining._if_return_rules(body, self._ctx(), [])
        self.assertEqual(
            [(rule["selector"], rule["speed"]) for rule in rules],
            [("block:minecraft:vine", 2.0), ("block:minecraft:glow_lichen", 2.0)],
        )

    def test_both_sides_literal_is_ambiguous(self) -> None:
        body = "return !blockState.is(Blocks.VINE) ? 1.0F : 1.5F;"
        self.assertEqual(mining._if_return_rules(body, self._ctx(), []), [])


if __name__ == "__main__":
    unittest.main()
