"""数据包覆盖：按游戏加载语义把外来数据包叠加到原版数据包之上。

判据来自 26.3 反编译源码，不来自习惯用法：

- `MultiPackResourceManager` / `FallbackResourceManager`：同路径资源整文件覆盖，
  `filter.block` 只屏蔽**更低优先级**的包；`namespace` 与 `path` 各一个正则，
  语义是 `Pattern.asPredicate()`（部分匹配，对应 `re.search`）
- `TagLoader.load` / `TagFile`：标签按 `values` 追加，`replace: true` 先清空，
  最终成员是 `LinkedHashSet`（去重且保序）

这些行为都是「静默错的来源」：覆盖顺序错了或标签当成整文件替换，产物看起来完整，
只是内容不对。因此逐条钉住。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ra.errors import InputError
from ra.inputs.datapacks import (
    ACTION_FILTERED,
    ACTION_INACTIVE_OVERLAY,
    ACTION_MERGED,
    ACTION_NON_DATA,
    MODE_IDENTITY,
    MODE_MERGED,
    ensure_datapack_merge,
)
from ra.inputs.manifest import Manifest


def _write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _meta(low: tuple[int, int] = (71, 0), high: tuple[int, int] = (71, 0), **extra: object) -> dict:
    """新式 metadata：`min_format` / `max_format` 带 minor。"""
    doc: dict = {
        "pack": {
            "description": "测试包",
            "min_format": list(low),
            "max_format": list(high),
        }
    }
    doc.update(extra)
    return doc


def _legacy_meta(pack_format: int = 71, **extra: object) -> dict:
    """旧式 metadata：单个 `pack_format` 整数。"""
    doc: dict = {"pack": {"pack_format": pack_format, "description": "测试包"}}
    doc.update(extra)
    return doc


class _MergeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pack_dir = self.root / "inputs" / "26.3"
        self.base = self.pack_dir / "data"
        # 基底：两条配方、一条标签
        _write_json(self.base / "minecraft" / "recipe" / "a.json", {"type": "crafting_shaped", "v": 1})
        _write_json(self.base / "minecraft" / "recipe" / "b.json", {"type": "crafting_shapeless"})
        _write_json(
            self.base / "minecraft" / "tags" / "item" / "planks.json",
            {"values": ["minecraft:oak_planks"]},
        )
        # 内置包目录不属于任意外来包；合并时要原样带过去
        _write_json(
            self.base / "minecraft" / "datapacks" / "experiment" / "data" / "minecraft" / "recipe" / "z.json",
            {"type": "crafting_shaped"},
        )
        (self.pack_dir / "pack.mcmeta").write_text(json.dumps(_meta()), encoding="utf-8")
        self.packs = self.pack_dir / "datapacks"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manifest(self, *names: str) -> Manifest:
        return Manifest(label="26.3", content_commit=None, pack_dir=self.pack_dir, datapacks=names)

    def merge(self, *names: str, force: bool = False, base_version: tuple[int, int] | None = (71, 0)):
        return ensure_datapack_merge(
            self.manifest(*names),
            self.base,
            "base-digest",
            base_version=base_version,
            force=force,
        )

    def add_pack(self, name: str, meta: dict | None = None) -> Path:
        root = self.packs / name
        root.mkdir(parents=True)
        (root / "pack.mcmeta").write_text(json.dumps(meta or _meta()), encoding="utf-8")
        return root


class IdentityTest(_MergeBase):
    def test_no_packs_keeps_vanilla_root(self) -> None:
        """没有数据包时不建合并目录：既有输入的产物必须逐字节不变。"""
        result = self.merge()
        self.assertEqual(result.mode, MODE_IDENTITY)
        self.assertEqual(result.data_root, self.base)
        self.assertIsNone(result.root)
        self.assertFalse((self.pack_dir / "datapack-merged").exists())

    def test_empty_directory_is_identity(self) -> None:
        self.packs.mkdir(parents=True)
        self.assertEqual(self.merge().mode, MODE_IDENTITY)


class OverrideTest(_MergeBase):
    def test_same_path_replaces_whole_file(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"type": "smelting", "v": 2})

        result = self.merge("tweak")
        self.assertEqual(result.mode, MODE_MERGED)
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"type": "smelting", "v": 2})
        # 没被覆盖的文件留原样
        self.assertTrue((result.data_root / "minecraft" / "recipe" / "b.json").is_file())
        self.assertEqual(result.overrides, 1)

    def test_later_pack_wins(self) -> None:
        low = self.add_pack("aa")
        high = self.add_pack("bb")
        _write_json(low / "data" / "minecraft" / "recipe" / "a.json", {"v": "low"})
        _write_json(high / "data" / "minecraft" / "recipe" / "a.json", {"v": "high"})

        result = self.merge("aa", "bb")
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": "high"})

        # 顺序反过来，结论必须跟着变——顺序就是语义
        result = self.merge("bb", "aa", force=True)
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": "low"})

    def test_inner_datapack_directory_is_carried_over(self) -> None:
        """原版内置包（data/<ns>/datapacks/）随基底一起进合并结果。"""
        pack = self.add_pack("tweak")
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": 2})
        result = self.merge("tweak")
        inner = result.data_root / "minecraft" / "datapacks" / "experiment" / "data" / "minecraft" / "recipe"
        self.assertTrue((inner / "z.json").is_file())


class ReportsWorldgenBaseTest(_MergeBase):
    """Vanilla worldgen from runtime reports is the base layer under datapacks."""

    def setUp(self) -> None:
        super().setUp()
        self.reports = self.root / "reports" / "worldgen" / "minecraft" / "worldgen"
        _write_json(self.reports / "biome" / "plains.json", {"features": []})
        _write_json(self.reports / "structure_set" / "villages.json", {"placement": {"spacing": 34}})

    def merge_with_reports(self, *names: str):
        return ensure_datapack_merge(
            self.manifest(*names),
            self.base,
            "base-digest",
            base_version=(71, 0),
            base_worldgen=self.reports,
        )

    def test_reports_worldgen_is_the_base_layer(self) -> None:
        pack = self.add_pack("bingo")
        _write_json(
            pack / "data" / "minecraft" / "worldgen" / "structure_set" / "villages.json",
            {"placement": {"spacing": 13}},
        )
        result = self.merge_with_reports("bingo")
        worldgen = result.data_root / "minecraft" / "worldgen"
        self.assertTrue((worldgen / "biome" / "plains.json").is_file(), "vanilla biome is missing")
        self.assertEqual(
            _read_json(worldgen / "structure_set" / "villages.json"), {"placement": {"spacing": 13}}
        )
        self.assertEqual(result.overrides, 1, "overriding a vanilla file counts as an override")

    def test_without_packs_nothing_is_merged(self) -> None:
        result = self.merge_with_reports()
        self.assertEqual(result.mode, MODE_IDENTITY)


class TagMergeTest(_MergeBase):
    def test_values_are_appended(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(
            pack / "data" / "minecraft" / "tags" / "item" / "planks.json",
            {"values": ["minecraft:birch_planks"]},
        )
        result = self.merge("tweak")
        doc = _read_json(result.data_root / "minecraft" / "tags" / "item" / "planks.json")
        self.assertEqual(
            doc["values"], ["minecraft:oak_planks", "minecraft:birch_planks"]
        )
        self.assertEqual(result.tag_merges, 1)

    def test_replace_clears_lower_priority(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(
            pack / "data" / "minecraft" / "tags" / "item" / "planks.json",
            {"replace": True, "values": ["minecraft:birch_planks"]},
        )
        result = self.merge("tweak")
        doc = _read_json(result.data_root / "minecraft" / "tags" / "item" / "planks.json")
        self.assertEqual(doc["values"], ["minecraft:birch_planks"])
        # 展开后的结果不再带 replace：语义已经消费掉了
        self.assertNotIn("replace", doc)

    def test_duplicates_collapse_in_first_seen_order(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(
            pack / "data" / "minecraft" / "tags" / "item" / "planks.json",
            {"values": ["minecraft:birch_planks", "minecraft:oak_planks"]},
        )
        result = self.merge("tweak")
        doc = _read_json(result.data_root / "minecraft" / "tags" / "item" / "planks.json")
        self.assertEqual(
            doc["values"], ["minecraft:oak_planks", "minecraft:birch_planks"]
        )

    def test_nested_tag_entries_survive(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(
            pack / "data" / "minecraft" / "tags" / "item" / "planks.json",
            {"values": [{"id": "#minecraft:wooden_slabs", "required": False}]},
        )
        result = self.merge("tweak")
        doc = _read_json(result.data_root / "minecraft" / "tags" / "item" / "planks.json")
        self.assertEqual(doc["values"][1], {"id": "#minecraft:wooden_slabs", "required": False})

    def test_unknown_tag_field_is_rejected(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(
            pack / "data" / "minecraft" / "tags" / "item" / "planks.json",
            {"values": [], "remove": ["x"]},
        )
        with self.assertRaises(InputError) as ctx:
            self.merge("tweak")
        self.assertIn("未知字段", str(ctx.exception))


class FilterTest(_MergeBase):
    def test_filter_blocks_lower_priority_only(self) -> None:
        """filter 屏蔽比它低的包；自己也命中时仍由自己提供资源。"""
        low = self.add_pack("aa")
        _write_json(low / "data" / "minecraft" / "recipe" / "a.json", {"v": "low"})
        high = self.add_pack(
            "zz",
            _meta(filter={"block": [{"namespace": "^minecraft$", "path": "recipe/a\\.json"}]}),
        )
        _write_json(high / "data" / "minecraft" / "recipe" / "a.json", {"v": "high"})

        result = self.merge("aa", "zz")
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": "high"})

        # 去掉高优先级包自己那份：低优先级与基底的 a.json 都被屏蔽
        (high / "data" / "minecraft" / "recipe" / "a.json").unlink()
        result = self.merge("aa", "zz", force=True)
        self.assertFalse((result.data_root / "minecraft" / "recipe" / "a.json").exists())
        self.assertEqual(result.filtered, 2)  # 基底一份 + 低优先级包一份

    def test_path_pattern_is_partial_match(self) -> None:
        """Java `Pattern.asPredicate()` 是 `find` 语义，不是整串匹配。"""
        pack = self.add_pack("tweak", _meta(filter={"block": [{"path": "recipe/"}]}))
        _write_json(pack / "data" / "minecraft" / "recipe" / "c.json", {"v": "new"})
        result = self.merge("tweak")
        self.assertFalse((result.data_root / "minecraft" / "recipe" / "a.json").exists())
        self.assertTrue((result.data_root / "minecraft" / "recipe" / "c.json").is_file())

    def test_bad_pattern_is_rejected(self) -> None:
        self.add_pack("tweak", _meta(filter={"block": [{"path": "recipe/(["}]}))
        with self.assertRaises(InputError) as ctx:
            self.merge("tweak")
        self.assertIn("filter", str(ctx.exception))


class OrderTest(_MergeBase):
    def test_unlisted_directory_is_rejected(self) -> None:
        self.add_pack("aa")
        self.add_pack("bb")
        with self.assertRaises(InputError) as ctx:
            self.merge("aa")
        self.assertIn("未列入 manifest", str(ctx.exception))

    def test_missing_pack_is_rejected(self) -> None:
        self.add_pack("aa")
        with self.assertRaises(InputError) as ctx:
            self.merge("aa", "ghost")
        self.assertIn("不存在的包", str(ctx.exception))

    def test_no_configured_order_falls_back_to_name_order(self) -> None:
        low = self.add_pack("aa")
        high = self.add_pack("bb")
        _write_json(low / "data" / "minecraft" / "recipe" / "a.json", {"v": "aa"})
        _write_json(high / "data" / "minecraft" / "recipe" / "a.json", {"v": "bb"})

        result = self.merge()
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": "bb"})
        # 顺序不能静默决定：必须留 note
        self.assertTrue(any("datapacks 列表" in note for note in result.notes))

    def test_stray_file_is_rejected(self) -> None:
        self.packs.mkdir(parents=True)
        (self.packs / "readme.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(InputError) as ctx:
            self.merge()
        self.assertIn("无法当作数据包", str(ctx.exception))


class MetaTest(_MergeBase):
    def test_missing_pack_mcmeta_is_rejected(self) -> None:
        root = self.packs / "tweak"
        (root / "data").mkdir(parents=True)
        with self.assertRaises(InputError) as ctx:
            self.merge("tweak")
        self.assertIn("pack.mcmeta", str(ctx.exception))

    def test_overlays_with_unknown_keys_are_rejected(self) -> None:
        """不认识的 overlay 字段要么支持要么报错，不能静默忽略。"""
        self.add_pack("tweak", _meta(overlays={"entries": [{"directory": "ov", "bogus": 1}]}))
        with self.assertRaises(InputError) as ctx:
            self.merge("tweak")
        self.assertIn("bogus", str(ctx.exception))

    def test_pack_format_mismatch_is_rejected(self) -> None:
        self.add_pack("tweak", _meta(low=(61, 0), high=(61, 0)))
        with self.assertRaises(InputError) as ctx:
            self.merge("tweak")
        self.assertIn("不相容", str(ctx.exception))

    def test_legacy_pack_format_is_accepted(self) -> None:
        """旧式只有 major：`pack_format: 71` 应视为 71.0–71.最大。"""
        self.add_pack("tweak", _legacy_meta(71))
        self.assertEqual(self.merge("tweak").mode, MODE_MERGED)

    def test_legacy_supported_formats_object_form(self) -> None:
        meta = {
            "pack": {
                "pack_format": 61,
                "supported_formats": {"min_inclusive": 61, "max_inclusive": 71},
                "description": "多版本",
            }
        }
        self.add_pack("tweak", meta)
        result = self.merge("tweak")
        self.assertEqual(result.mode, MODE_MERGED)
        self.assertTrue(any("格式范围" in note for note in result.notes))


class ZipPackTest(_MergeBase):
    def test_zip_pack_is_read_like_a_directory(self) -> None:
        self.packs.mkdir(parents=True)
        archive_path = self.packs / "zip-pack.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("pack.mcmeta", json.dumps(_meta()))
            archive.writestr("data/minecraft/recipe/a.json", json.dumps({"v": "zip"}))

        result = self.merge("zip-pack.zip")
        self.assertEqual(result.packs[0].kind, "zip")
        merged = _read_json(result.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": "zip"})


class MarkerTest(_MergeBase):
    def test_second_run_reuses_marker(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": 2})
        first = self.merge("tweak")
        self.assertFalse(first.cache_hit)

        second = self.merge("tweak")
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.files, first.files)
        self.assertEqual(second.overrides, first.overrides)

    def test_changing_pack_content_invalidates(self) -> None:
        pack = self.add_pack("tweak")
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": 2})
        self.merge("tweak")

        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": 3})
        again = self.merge("tweak")
        self.assertFalse(again.cache_hit)
        merged = _read_json(again.data_root / "minecraft" / "recipe" / "a.json")
        self.assertEqual(merged, {"v": 3})


class OverlayTest(_MergeBase):
    """overlays：包内按格式版本筛选的附加目录。

    判据是 `OverlayedPackResources` 的 stack 顺序——包根最前，overlay 逆序跟随，
    查找时第一个命中者胜。因此包根盖 overlay，后列出的 overlay 盖先列出的。
    """

    @staticmethod
    def entry(directory: str, low: tuple[int, int], high: tuple[int, int]) -> dict:
        return {
            "directory": directory,
            "min_format": list(low),
            "max_format": list(high),
        }

    def set_overlays(self, pack: Path, entries: list[dict], **meta_kwargs: object) -> None:
        _write_json(
            pack / "pack.mcmeta",
            _meta(overlays={"entries": entries}, **meta_kwargs),
        )

    def test_applicable_overlay_contributes_files(self) -> None:
        pack = self.add_pack("tweak")
        self.set_overlays(pack, [self.entry("ov", (71, 0), (71, 0))])
        _write_json(pack / "ov" / "data" / "minecraft" / "recipe" / "c.json", {"v": "ov"})

        result = self.merge("tweak")
        self.assertEqual(
            _read_json(result.data_root / "minecraft" / "recipe" / "c.json"), {"v": "ov"}
        )
        self.assertEqual(result.packs[0].overlays[0].applicable, True)

    def test_root_wins_over_overlay(self) -> None:
        pack = self.add_pack("tweak")
        self.set_overlays(pack, [self.entry("ov", (71, 0), (71, 0))])
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": "root"})
        _write_json(pack / "ov" / "data" / "minecraft" / "recipe" / "a.json", {"v": "ov"})

        result = self.merge("tweak")
        self.assertEqual(
            _read_json(result.data_root / "minecraft" / "recipe" / "a.json"), {"v": "root"}
        )

    def test_later_overlay_wins(self) -> None:
        pack = self.add_pack("tweak")
        self.set_overlays(
            pack,
            [self.entry("first", (71, 0), (71, 0)), self.entry("second", (71, 0), (71, 0))],
        )
        _write_json(pack / "first" / "data" / "minecraft" / "recipe" / "a.json", {"v": "first"})
        _write_json(pack / "second" / "data" / "minecraft" / "recipe" / "a.json", {"v": "second"})

        result = self.merge("tweak")
        self.assertEqual(
            _read_json(result.data_root / "minecraft" / "recipe" / "a.json"), {"v": "second"}
        )

    def test_inapplicable_overlay_is_skipped_and_recorded(self) -> None:
        pack = self.add_pack("tweak")
        self.set_overlays(pack, [self.entry("old", (60, 0), (60, 0))])
        _write_json(pack / "old" / "data" / "minecraft" / "recipe" / "x.json", {"v": "old"})

        result = self.merge("tweak")
        self.assertFalse((result.data_root / "minecraft" / "recipe" / "x.json").exists())
        actions = {(item.pack, item.file): item.action for item in result.dispositions}
        self.assertEqual(
            actions[("tweak/old", "data/minecraft/recipe/x.json")], ACTION_INACTIVE_OVERLAY
        )
        self.assertTrue(any("inactive_overlay" in note for note in result.notes))

    def test_overlay_range_uses_minor(self) -> None:
        pack = self.add_pack("tweak", _meta(low=(71, 0), high=(71, 9)))
        self.set_overlays(pack, [self.entry("ov", (71, 0), (71, 0))], low=(71, 0), high=(71, 9))
        _write_json(pack / "ov" / "data" / "minecraft" / "recipe" / "x.json", {"v": "ov"})

        inside = self.merge("tweak")
        self.assertTrue((inside.data_root / "minecraft" / "recipe" / "x.json").is_file())

        outside = self.merge("tweak", base_version=(71, 1), force=True)
        self.assertFalse((outside.data_root / "minecraft" / "recipe" / "x.json").exists())

    def test_missing_overlay_directory_is_noted(self) -> None:
        pack = self.add_pack("tweak")
        self.set_overlays(pack, [self.entry("ghost", (71, 0), (71, 0))])
        result = self.merge("tweak")
        self.assertTrue(any("不存在或为空" in note for note in result.notes))

    def test_zip_overlay(self) -> None:
        self.packs.mkdir(parents=True, exist_ok=True)
        meta = _meta(overlays={"entries": [self.entry("ov", (71, 0), (71, 0))]})
        with zipfile.ZipFile(self.packs / "z.zip", "w") as archive:
            archive.writestr("pack.mcmeta", json.dumps(meta))
            archive.writestr("ov/data/minecraft/recipe/x.json", json.dumps({"v": "ov"}))

        result = self.merge("z.zip")
        self.assertEqual(
            _read_json(result.data_root / "minecraft" / "recipe" / "x.json"), {"v": "ov"}
        )


class DispositionTest(_MergeBase):
    def test_every_pack_file_has_a_disposition(self) -> None:
        """红线 3：数据包是第一手输入，每个文件都要有去向。"""
        pack = self.add_pack("tweak")
        _write_json(pack / "data" / "minecraft" / "recipe" / "a.json", {"v": 2})
        (pack / "assets" / "minecraft").mkdir(parents=True)
        (pack / "assets" / "minecraft" / "note.txt").write_text("x", encoding="utf-8")

        result = self.merge("tweak")
        files = {(item.file, item.action) for item in result.dispositions}
        self.assertIn(("data/minecraft/recipe/a.json", ACTION_MERGED), files)
        self.assertIn(("assets/minecraft/note.txt", ACTION_NON_DATA), files)
        self.assertIn(("pack.mcmeta", "metadata"), files)
        # 包内每个文件都在清单里
        on_disk = {
            path.relative_to(pack).as_posix()
            for path in pack.rglob("*")
            if path.is_file()
        }
        self.assertEqual({item.file for item in result.dispositions}, on_disk)
        self.assertEqual(result.dropped, 1)

    def test_filtered_file_is_recorded(self) -> None:
        low = self.add_pack("aa")
        _write_json(low / "data" / "minecraft" / "recipe" / "a.json", {"v": "low"})
        self.add_pack("zz", _meta(filter={"block": [{"path": "recipe/a"}]}))
        result = self.merge("aa", "zz")
        actions = {(item.pack, item.file): item.action for item in result.dispositions}
        self.assertEqual(actions[("aa", "data/minecraft/recipe/a.json")], ACTION_FILTERED)


if __name__ == "__main__":
    unittest.main()
