"""数据包覆盖：把外来数据包按游戏加载语义叠加到原版数据包之上。

**为什么不由原版生成器代劳**：`net.minecraft.data.Main` 的 providers 全部从
`VanillaRegistries`（代码内静态注册表）产出，`--input` 只喂给 `SnbtToNbt` /
`NbtToSnbt` 两个结构转换器。它不读 `pack.mcmeta`，也没有「多个包按顺序叠加」的
概念，因此合并只能在本模块实现（判据见 `docs/datagen.md`）。

合并语义照抄 26.3 反编译源码的 `MultiPackResourceManager`、`OverlayedPackResources`
与 `TagLoader`：

1. **包内叠加**：`pack.mcmeta` 的 `overlays.entries` 按当前格式版本筛出适用目录，
   每个 overlay 目录是包内的一个子目录（其下同样有 `data/`）。优先级是
   **包根 > 后列出的 overlay > 先列出的 overlay**（`OverlayedPackResources` 把
   overlay 逆序排在包根之后，查找时第一个命中者胜）。
2. **同路径资源整文件覆盖**，后加载者胜，不做深合并。
3. **标签文件**（`data/<ns>/tags/**/*.json`）把 `values` 追加到已有列表末尾；
   新文件 `replace: true` 时先清空。加载顺序即优先级顺序（低 → 高）。
4. **`pack.mcmeta` 的 `filter.block`** 屏蔽**比它低优先级**的包：`namespace` 与
   `path` 各一个正则，语义是 Java `Pattern.asPredicate()`（部分匹配，
   对应 Python 的 `re.search`）。

格式号是 `(major, minor)` 二元组：新式 `min_format`/`max_format` 可带 minor，
旧式 `pack_format`/`supported_formats` 只有 major（minor 视为 0）。两者都认，
按「新式优先」取一个范围。

规则之外的情形一律报错，不静默：缺 `pack.mcmeta`、格式号与原版不相容、
未知的 metadata 段、`filter` 正则无法编译。

## 目录分工

| 路径 | 性质 |
|---|---|
| `inputs/<label>/datapacks/` | 第一手：用户放置的数据包，每项是目录或 `.zip` |
| `inputs/<label>/datapack-merged/` | 派生：覆盖合并结果，可整目录删除后重建 |

合并结果的结构是对下游的契约：`data/` 即生效数据根（`profile.dirs` 相对它计算），
`merge-log.jsonl` 逐行记录覆盖与标签合并，`.datapack-merge.json` 是失效判据。
没有数据包时**不建目录**，生效数据根直接指向原版派生目录，`mode` 记 `identity`
——这样既有输入的产物逐字节不变。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError
from ..paths import pack_datapack_merged, pack_datapacks
from ..util.hashing import sha256_file
from ..util.jsonio import write_json
from .manifest import Manifest

MERGE_SCHEMA = 1
MARKER_NAME = ".datapack-merge.json"
LOG_NAME = "merge-log.jsonl"
DATA_DIRNAME = "data"
PACK_META = "pack.mcmeta"
TAGS_SEGMENT = "tags"

MODE_IDENTITY = "identity"
MODE_MERGED = "merged"

# 包内文件处置。`metadata` 是包根元数据，其余按路径判定。
ACTION_MERGED = "merged"
ACTION_OVERRIDDEN = "overridden"
ACTION_METADATA = "metadata"
ACTION_FILTERED = "filtered"
ACTION_NON_DATA = "non_data"
ACTION_INACTIVE_OVERLAY = "inactive_overlay"

PACK_SUFFIX = ".zip"
# 日志里给基底用的名字。基底不是外来包，但它的文件也可能被 filter 屏蔽。
BASE_PACK_NAME = "<vanilla>"

# 数据包格式号：`(major, minor)`。新式 metadata 带 minor，旧式只有 major。
PackVersion = tuple[int, int]
FormatRange = tuple[PackVersion, PackVersion]
MINOR_MAX = 2**31 - 1


@dataclass(frozen=True)
class FilterRule:
    """`pack.mcmeta` 的一条 `filter.block` 项。

    两个正则都可缺省（缺省即全匹配），语义与 `IdentifierPattern` 一致：
    `namespace` 匹配命名空间，`path` 匹配去掉 `data/<ns>/` 前缀后的资源路径。
    """

    namespace: str | None
    path: str | None
    _namespace: re.Pattern[str] | None = None
    _path: re.Pattern[str] | None = None

    def matches(self, namespace: str, path: str) -> bool:
        if self._namespace is not None and not self._namespace.search(namespace):
            return False
        if self._path is not None and not self._path.search(path):
            return False
        return True


@dataclass(frozen=True)
class OverlayDir:
    """`overlays.entries` 的一项：包内的附加目录及其适用格式范围。"""

    directory: str
    formats: FormatRange | None
    applicable: bool
    files: int

    def as_json(self) -> dict:
        return {
            "directory": self.directory,
            "formats": _range_json(self.formats),
            "applicable": self.applicable,
            "files": self.files,
        }


@dataclass(frozen=True)
class PackInfo:
    name: str
    kind: str
    digest: str
    files: int
    data_files: int
    formats: FormatRange | None
    filters: int
    overlays: tuple[OverlayDir, ...] = ()
    notes: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "digest": self.digest,
            "files": self.files,
            "data_files": self.data_files,
            "formats": _range_json(self.formats),
            "filters": self.filters,
            "overlays": [overlay.as_json() for overlay in self.overlays],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Disposition:
    """一个数据包内文件的最终处置，供台账逐文件登记。"""

    pack: str
    file: str
    action: str


@dataclass(frozen=True)
class DatapackMerge:
    mode: str
    # 生效数据根：`identity` 时指向原版派生目录，`merged` 时指向合并结果的 `data/`。
    data_root: Path
    root: Path | None
    packs: tuple[PackInfo, ...]
    files: int
    overrides: int
    tag_merges: int
    filtered: int
    dropped: int
    cache_hit: bool
    notes: tuple[str, ...]
    dispositions: tuple[Disposition, ...] = ()
    # 失效键：由基底摘要与各包摘要算出。结构模板转换要把数据包里的 .nbt
    # 并进同一批转换，因此需要同一个键来判自己的缓存。
    key: str = ""

    @property
    def active(self) -> bool:
        return self.mode == MODE_MERGED

    def as_json(self) -> dict:
        return {
            "mode": self.mode,
            "data_root": str(self.data_root),
            "root": str(self.root) if self.root else None,
            "packs": [pack.as_json() for pack in self.packs],
            "files": self.files,
            "overrides": self.overrides,
            "tag_merges": self.tag_merges,
            "filtered": self.filtered,
            "dropped": self.dropped,
            "cache_hit": self.cache_hit,
            "notes": list(self.notes),
        }


class _DirView:
    kind = "dir"

    def __init__(self, root: Path, prefix: str = "", skip: tuple[str, ...] = ()) -> None:
        self.root = root
        self.prefix = prefix
        base = root / prefix if prefix else root
        self.exists = base.is_dir()
        self._files = sorted(
            rel
            for rel in (
                path.relative_to(base).as_posix()
                for path in base.rglob("*")
                if path.is_file()
            )
            if not any(rel.startswith(item) for item in skip)
        ) if self.exists else []

    def files(self) -> list[str]:
        return self._files

    def read(self, rel: str) -> bytes:
        target = self.root / self.prefix / rel if self.prefix else self.root / rel
        return target.read_bytes()


class _ZipView:
    kind = "zip"

    def __init__(self, path: Path, prefix: str = "", skip: tuple[str, ...] = ()) -> None:
        self.path = path
        self.prefix = prefix
        with zipfile_ref(path) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
        if prefix:
            names = [name[len(prefix) :] for name in names if name.startswith(prefix)]
        if skip:
            names = [name for name in names if not any(name.startswith(item) for item in skip)]
        self._files = sorted(names)

    def files(self) -> list[str]:
        return self._files

    def read(self, rel: str) -> bytes:
        with zipfile_ref(self.path) as archive:
            return archive.read(self.prefix + rel)


def _range_json(formats: FormatRange | None) -> dict | None:
    if formats is None:
        return None
    return {"min": list(formats[0]), "max": list(formats[1])}


def _fmt_version(value: PackVersion) -> str:
    return f"{value[0]}.{value[1]}"


def _fmt_range(formats: FormatRange | None) -> str:
    if formats is None:
        return "未声明"
    return f"{_fmt_version(formats[0])}–{_fmt_version(formats[1])}"


def _in_range(formats: FormatRange | None, version: PackVersion) -> bool:
    if formats is None:
        return True
    return formats[0] <= version <= formats[1]


def zipfile_ref(path: Path):
    """延迟 import：只有用到压缩包的数据包才需要 `zipfile`。"""
    import zipfile

    return zipfile.ZipFile(path)


def _digest_files(view, files: list[str]) -> str:
    """内容摘要：按相对路径排序，逐个喂 路径与内容哈希。"""
    import hashlib

    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(view.read(rel)).digest())
    return digest.hexdigest()


def _compile(pattern: object, where: str) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise InputError(f"{where}：filter 的 namespace/path 必须是字符串")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise InputError(f"{where}：filter 正则可以编译失败：{pattern!r}（{exc}）") from exc


def _read_filters(doc: object, where: str) -> tuple[FilterRule, ...]:
    if doc is None:
        return ()
    if not isinstance(doc, dict):
        raise InputError(f"{where}：filter 段不是对象")
    unknown = set(doc) - {"block"}
    if unknown:
        raise InputError(f"{where}：filter 段有未知键 {sorted(unknown)}")
    blocks = doc.get("block")
    if blocks is None:
        return ()
    if not isinstance(blocks, list):
        raise InputError(f"{where}：filter.block 不是数组")
    rules: list[FilterRule] = []
    for index, item in enumerate(blocks):
        if not isinstance(item, dict):
            raise InputError(f"{where}：filter.block[{index}] 不是对象")
        extra = set(item) - {"namespace", "path"}
        if extra:
            raise InputError(f"{where}：filter.block[{index}] 有未知键 {sorted(extra)}")
        rules.append(
            FilterRule(
                namespace=item.get("namespace") if isinstance(item.get("namespace"), str) else None,
                path=item.get("path") if isinstance(item.get("path"), str) else None,
                _namespace=_compile(item.get("namespace"), where),
                _path=_compile(item.get("path"), where),
            )
        )
    return tuple(rules)


def _read_version(value: object, where: str, *, default_minor: int = 0) -> PackVersion:
    """格式号可写成整数或 `[major]` / `[major, minor]`。"""
    if isinstance(value, int):
        return (value, default_minor)
    if (
        isinstance(value, list)
        and 1 <= len(value) <= 2
        and all(isinstance(item, int) for item in value)
    ):
        return (value[0], value[1] if len(value) > 1 else default_minor)
    raise InputError(f"{where}：格式号形状无法识别：{value!r}（应为整数或 [major, minor]）")


def _read_legacy_range(value: object, where: str) -> FormatRange:
    """旧式 `supported_formats`：整数、`[min, max]` 或 `{min_inclusive, max_inclusive}`。"""
    if isinstance(value, int):
        return ((value, 0), (value, MINOR_MAX))
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value):
        return ((value[0], 0), (value[1], MINOR_MAX))
    if isinstance(value, dict):
        low = value.get("min_inclusive")
        high = value.get("max_inclusive")
        if isinstance(low, int) and isinstance(high, int):
            return ((low, 0), (high, MINOR_MAX))
    raise InputError(f"{where}：supported_formats 形状无法识别：{value!r}")


def _read_range(section: dict, where: str, *, overlay: bool) -> FormatRange | None:
    """读一个格式范围，新式优先。

    - 新式：`min_format` / `max_format`，minor 缺省分别为 0 与最大
    - 旧式：`supported_formats`，或单值的 `pack_format`
    - overlay 条目的旧式键叫 `formats`
    """
    has_new = "min_format" in section or "max_format" in section
    if has_new:
        low = _read_version(section["min_format"], where) if "min_format" in section else (0, 0)
        high = (
            _read_version(section["max_format"], where, default_minor=MINOR_MAX)
            if "max_format" in section
            else (MINOR_MAX, MINOR_MAX)
        )
        return (low, high)

    legacy_key = "formats" if overlay else "supported_formats"
    if legacy_key in section:
        return _read_legacy_range(section[legacy_key], f"{where} 的 {legacy_key}")
    if not overlay and "pack_format" in section:
        value = section["pack_format"]
        if not isinstance(value, int):
            raise InputError(f"{where}：pack_format 不是整数")
        return ((value, 0), (value, MINOR_MAX))
    return None


PACK_KEYS = frozenset(
    {"description", "version", "min_format", "max_format", "pack_format", "supported_formats"}
)
OVERLAY_META_KEYS = frozenset({"entries"})
OVERLAY_ENTRY_KEYS = frozenset({"directory", "min_format", "max_format", "formats"})


def _read_overlays(
    doc: object, where: str, base_version: PackVersion | None
) -> tuple[OverlayDir, ...]:
    """读 `overlays.entries`。顺序即优先级顺序（后列出者更优先）。"""
    if doc is None:
        return ()
    if not isinstance(doc, dict):
        raise InputError(f"{where}：overlays 段不是对象")
    extra = set(doc) - OVERLAY_META_KEYS
    if extra:
        raise InputError(f"{where}：overlays 段有未知键 {sorted(extra)}")
    entries = doc.get("entries")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise InputError(f"{where}：overlays.entries 不是数组")

    result: list[OverlayDir] = []
    for index, item in enumerate(entries):
        entry_where = f"{where} 的 overlays.entries[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{entry_where} 不是对象")
        unknown = set(item) - OVERLAY_ENTRY_KEYS
        if unknown:
            raise InputError(f"{entry_where} 有未知键 {sorted(unknown)}")
        directory = item.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            raise InputError(f"{entry_where} 缺 directory")
        formats = _read_range(item, entry_where, overlay=True)
        result.append(
            OverlayDir(
                directory=directory,
                formats=formats,
                applicable=base_version is None or _in_range(formats, base_version),
                files=0,  # 由调用方填
            )
        )
    return tuple(result)


def _read_meta(
    view, name: str, base_version: PackVersion | None
) -> tuple[FormatRange | None, tuple[FilterRule, ...], tuple[OverlayDir, ...], list[str]]:
    where = f"数据包 {name}"
    if PACK_META not in view.files():
        raise InputError(
            f"{where} 缺少 {PACK_META}（数据包根必须有它）。"
            f"目录形态要求 {PACK_META} 与 data/ 并列；压缩包形态要求 {PACK_META} 在包根"
        )
    try:
        doc = json.loads(view.read(PACK_META).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{where}：{PACK_META} 无法解析（{exc}）") from exc
    if not isinstance(doc, dict):
        raise InputError(f"{where}：{PACK_META} 顶层不是对象")

    notes: list[str] = []
    unknown = set(doc) - {"pack", "filter", "overlays"}
    if unknown:
        raise InputError(
            f"{where}：{PACK_META} 有本模块未实现的段 {sorted(unknown)}。"
            "静默忽略会得到与游戏不一致的合并结果"
        )

    pack_section = doc.get("pack")
    formats: FormatRange | None = None
    if isinstance(pack_section, dict):
        extra = set(pack_section) - PACK_KEYS
        if extra:
            raise InputError(f"{where}：pack 段有未知键 {sorted(extra)}")
        formats = _read_range(pack_section, f"{where} 的 pack 段", overlay=False)
    elif pack_section is not None:
        raise InputError(f"{where}：pack 段不是对象")
    if formats is None:
        notes.append("pack 段未声明格式号，无法判断该包与原版的格式是否相容")

    overlays = _read_overlays(doc.get("overlays"), where, base_version)
    return formats, _read_filters(doc.get("filter"), where), overlays, notes


def _split_data(rel: str) -> tuple[str, str] | None:
    """`data/<ns>/<rest>` → `(ns, rest)`；不是数据侧资源返回 None。"""
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != DATA_DIRNAME or not parts[1]:
        return None
    return parts[1], "/".join(parts[2:])


def _is_tag(rest: str) -> bool:
    return rest.split("/", 1)[0] == TAGS_SEGMENT


def _blocked(filters: list[FilterRule], namespace: str, path: str) -> bool:
    return any(rule.matches(namespace, path) for rule in filters)


def _discover(source_dir: Path) -> dict[str, tuple[Path, str]]:
    """扫 `datapacks/`：目录与 `.zip` 是数据包，其余一律报错。"""
    if not source_dir.is_dir():
        return {}
    found: dict[str, tuple[Path, str]] = {}
    strays: list[str] = []
    for entry in sorted(source_dir.iterdir()):
        if entry.is_dir():
            found[entry.name] = (entry, _DirView.kind)
        elif entry.suffix.lower() == PACK_SUFFIX:
            found[entry.name] = (entry, _ZipView.kind)
        elif entry.name.startswith("."):
            continue  # 工具自己写的标记文件不算输入
        else:
            strays.append(entry.name)
    if strays:
        raise InputError(
            f"{source_dir} 下有无法当作数据包的条目：{', '.join(strays)}。"
            "每一项必须是含 pack.mcmeta 的目录，或含 pack.mcmeta 的 .zip"
        )
    return found


def _order(names: dict[str, tuple[Path, str]], configured: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """决定加载顺序（低 → 高）。"""
    if not configured:
        ordered = sorted(names)
        notes = [
            "未在 manifest 配置 datapacks 列表，按目录名升序作为优先级（低 → 高）；"
            "顺序影响覆盖结果，需要固定时请在版本段写 datapacks = [...]"
        ]
        return ordered, notes

    missing = [name for name in configured if name not in names]
    if missing:
        raise InputError(
            f"manifest 的 datapacks 列表里有不存在的包：{', '.join(missing)}"
            f"（{names and '现有：' + ', '.join(sorted(names)) or '目录为空'}）"
        )
    extra = [name for name in sorted(names) if name not in configured]
    if extra:
        raise InputError(
            f"datapacks 目录里有未列入 manifest 的包：{', '.join(extra)}。"
            "未列出的包不会被加载，但静默跳过等于改动了输入，"
            "请把它们加入 datapacks 列表或移出该目录"
        )
    return list(configured), []


@dataclass(frozen=True)
class _Layer:
    """包内的一层资源：包根，或某个 overlay 目录。`label` 供台账溯源。"""

    label: str
    view: object


@dataclass(frozen=True)
class _Unit:
    """一个数据包在合并里的全部来源层。"""

    name: str
    filters: tuple[FilterRule, ...]
    # 高优先级在前：包根最前，其后是适用 overlay 的**逆序**（后列出者更优先）。
    layers: tuple[_Layer, ...]
    # 不适用于当前格式的 overlay：不参与合并，但文件仍要逐条留痕。
    inactive: tuple[_Layer, ...] = ()


def _build_unit(
    source: Path, name: str, base_version: PackVersion | None
) -> tuple[_Unit, PackInfo]:
    """读一个包的元数据，铺开成「包根 + 适用 overlay」的来源层。"""
    is_zip = source.is_file()
    # 先按全量读元数据（overlay 目录都在包内），再建一份跳过 overlay 的包根视图：
    # overlay 目录里的文件由各自的 layer 负责，包根不能重复登记（台账键必须唯一）。
    probe_view = _ZipView(source) if is_zip else _DirView(source)
    formats, filters, overlays, notes = _read_meta(probe_view, name, base_version)
    skip = tuple(overlay.directory + "/" for overlay in overlays)
    root_view = (
        _ZipView(source, skip=skip) if is_zip else _DirView(source, skip=skip)
    )

    active: list[_Layer] = []
    inactive: list[_Layer] = []
    resolved: list[OverlayDir] = []
    for overlay in overlays:
        prefix = overlay.directory + "/"
        overlay_view = _ZipView(source, prefix) if is_zip else _DirView(source, prefix)
        resolved.append(
            OverlayDir(
                directory=overlay.directory,
                formats=overlay.formats,
                applicable=overlay.applicable,
                files=len(overlay_view.files()),
            )
        )
        layer = _Layer(label=f"{name}/{overlay.directory}", view=overlay_view)
        (active if overlay.applicable else inactive).append(layer)
        if overlay.applicable and not overlay_view.files():
            notes.append(
                f"overlay 目录 {overlay.directory} 适用于当前格式但在包内不存在或为空"
            )

    # 包根优先，overlay 逆序跟随（`OverlayedPackResources` 的 stack 顺序）
    layers = (_Layer(label=name, view=root_view), *reversed(active))
    data_files = sum(
        1
        for layer in (*layers, *inactive)
        for rel in layer.view.files()
        if _split_data(rel) is not None
    )
    digest = sha256_file(source) if is_zip else _digest_files(probe_view, probe_view.files())
    info = PackInfo(
        name=name,
        kind=root_view.kind,
        digest=digest,
        files=len(probe_view.files()),
        data_files=data_files,
        formats=formats,
        filters=len(filters),
        overlays=tuple(resolved),
        notes=tuple(notes),
    )
    return _Unit(name=name, filters=filters, layers=layers, inactive=tuple(inactive)), info


def _plan(
    units: list[_Unit],
    base_rels: frozenset[str] = frozenset(),
) -> tuple[list[tuple[_Layer, str, str, str]], list[Disposition], int, int, int, int, int]:
    """逐文件判定处置。**不读文件内容**，只按路径与 filter 判。

    `base_rels` 是基底已经有资源的 `data/...` 路径集合，用于把「包覆盖基底」
    也计入覆盖数——基底在 `Disposition` 里没有条目，不能靠前驱记录发现。

    返回 `(待写入表, 处置清单, 覆盖数, 过滤数, 非数据数, 元数据数, 未生效 overlay 文件数)`。
    待写入表按「低优先级先写」排列：同一路径后写者覆盖先写者，与游戏查找顺序一致。
    """
    pending: list[tuple[_Layer, str, str, str]] = []
    dispositions: list[Disposition] = []
    final: dict[str, int] = {}
    overrides = 0
    filtered = 0
    non_data = 0
    metadata = 0
    inactive_files = 0

    for index, unit in enumerate(units):
        # 只被**更高优先级**的包屏蔽；自己的 filter 不屏蔽自己
        higher = [rule for later in units[index + 1 :] for rule in later.filters]
        for layer in reversed(unit.layers):
            for rel in layer.view.files():
                if rel == PACK_META:
                    dispositions.append(Disposition(pack=layer.label, file=rel, action=ACTION_METADATA))
                    metadata += 1
                    continue
                split = _split_data(rel)
                if split is None:
                    dispositions.append(Disposition(pack=layer.label, file=rel, action=ACTION_NON_DATA))
                    non_data += 1
                    continue
                namespace, path = split
                if _blocked(higher, namespace, path):
                    dispositions.append(Disposition(pack=layer.label, file=rel, action=ACTION_FILTERED))
                    filtered += 1
                    continue
                if rel in final:
                    previous = dispositions[final[rel]]
                    dispositions[final[rel]] = Disposition(
                        pack=previous.pack, file=previous.file, action=ACTION_OVERRIDDEN
                    )
                    if not _is_tag(path):
                        overrides += 1
                elif rel in base_rels and not _is_tag(path):
                    overrides += 1
                dispositions.append(Disposition(pack=layer.label, file=rel, action=ACTION_MERGED))
                final[rel] = len(dispositions) - 1
                pending.append((layer, rel, namespace, path))

        for layer in unit.inactive:
            for rel in layer.view.files():
                dispositions.append(
                    Disposition(pack=layer.label, file=rel, action=ACTION_INACTIVE_OVERLAY)
                )
                inactive_files += 1

    return pending, dispositions, overrides, filtered, non_data, metadata, inactive_files


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_payload(raw: bytes, where: str) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{where}：标签文件无法解析（{exc}）") from exc
    if not isinstance(doc, dict):
        raise InputError(f"{where}：标签文件顶层不是对象")
    unknown = set(doc) - {"values", "replace"}
    if unknown:
        raise InputError(f"{where}：标签文件有未知字段 {sorted(unknown)}")
    if not isinstance(doc.get("values", []), list):
        raise InputError(f"{where}：标签的 values 不是数组")
    return doc


def _dedupe(values: list) -> list:
    """按值去重并保持首次出现的顺序，对应 `TagLoader` 的 `LinkedHashSet`。"""
    seen: set[str] = set()
    out: list = []
    for item in values:
        key = _dumps(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _merge_tag(target: Path, payload: dict) -> str:
    """把一份标签文件并进目标。返回 `create` / `append` / `replace`。"""
    values = list(payload.get("values") or [])
    if target.is_file():
        current = _parse_payload(target.read_bytes(), str(target))
        if payload.get("replace", False):
            merged = values
            action = "replace"
        else:
            merged = list(current.get("values") or []) + values
            action = "append"
    else:
        merged = values
        action = "create"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((_dumps({"values": _dedupe(merged)}) + "\n").encode("utf-8"))
    return action


def _copy_base(base_data_root: Path, dest_data: Path) -> int:
    count = 0
    for path in sorted(p for p in base_data_root.rglob("*") if p.is_file()):
        rel = path.relative_to(base_data_root)
        target = dest_data / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def _base_rels(base_data_root: Path) -> frozenset[str]:
    """基底已有的资源路径，写成与包内一致的 `data/...` 形式。"""
    if not base_data_root.is_dir():
        return frozenset()
    return frozenset(
        f"{DATA_DIRNAME}/{path.relative_to(base_data_root).as_posix()}"
        for path in base_data_root.rglob("*")
        if path.is_file()
    )


def _plan_base(base_data_root: Path, filters: list[FilterRule]) -> list[str]:
    """基底里被任意外来包的 filter 命中的文件，返回相对数据根的路径。

    基底是最低优先级，任何包的 filter 都能屏蔽它；不这样做，「把旧配方清掉」
    这类包在分析结果里会留下已经被游戏移除的条目。
    """
    blocked: list[str] = []
    for path in sorted(p for p in base_data_root.rglob("*") if p.is_file()):
        parts = path.relative_to(base_data_root).as_posix().split("/")
        if len(parts) < 3:
            continue
        namespace, rest = parts[0], "/".join(parts[1:])
        if _blocked(filters, namespace, rest):
            blocked.append("/".join(parts))
    return blocked


def _check_format(pack: PackInfo, base_version: PackVersion, notes: list[str]) -> None:
    """格式范围不含原版格式号即报错：游戏自己会判该包不相容。"""
    if pack.formats is None:
        return
    if not _in_range(pack.formats, base_version):
        raise InputError(
            f"数据包 {pack.name} 声明的格式范围 {_fmt_range(pack.formats)} "
            f"与原版数据包格式 {_fmt_version(base_version)} 不相容。"
            "游戏会把它标为 incompatible；这类包不能用于分析，"
            "请换成该版本的包，或在包内补 min_format/max_format"
        )
    low, high = pack.formats
    if (low, high) != (base_version, base_version):
        notes.append(
            f"数据包 {pack.name} 声明格式范围 {_fmt_range(pack.formats)}，"
            f"原版为 {_fmt_version(base_version)}，在范围内"
        )


def ensure_datapack_merge(
    manifest: Manifest,
    base_data_root: Path,
    base_digest: str,
    *,
    base_version: PackVersion | None = None,
    base_worldgen: Path | None = None,
    force: bool = False,
) -> DatapackMerge:
    """确保覆盖合并结果就位。

    没有数据包时不建目录，返回 `mode=identity` 与传入的原版数据根——下游读到的
    仍是那份目录，既有输入的产物因此逐字节不变。

    `base_worldgen`: vanilla worldgen from the runtime reports, merged into the base
    when the jar data has none.
    """
    source_dir = pack_datapacks(manifest.pack_dir)
    names = _discover(source_dir)
    if not names:
        return DatapackMerge(
            mode=MODE_IDENTITY,
            data_root=base_data_root,
            root=None,
            packs=(),
            files=sum(1 for p in base_data_root.rglob("*") if p.is_file())
            if base_data_root.is_dir()
            else 0,
            overrides=0,
            tag_merges=0,
            filtered=0,
            dropped=0,
            cache_hit=True,
            notes=(),
        )

    ordered, notes = _order(names, manifest.datapacks)
    if base_version is None:
        notes.append(
            "未取到原版数据包格式号（version.json 与 pack.mcmeta 都没有），"
            "跳过格式相容性校验，overlay 也无法按版本筛选"
        )
    units: list[_Unit] = []
    packs: list[PackInfo] = []
    for name in ordered:
        source, _kind = names[name]
        unit, info = _build_unit(source, name, base_version)
        if base_version is not None:
            _check_format(info, base_version, notes)
        notes.extend(info.notes)
        units.append(unit)
        packs.append(info)
        active = [layer.label for layer in unit.layers[1:]]
        if info.overlays:
            notes.append(
                f"数据包 {name} 声明 {len(info.overlays)} 个 overlay，"
                f"适用于当前格式的 {len(active)} 个：{', '.join(active) or '（无）'}"
            )

    base_rels = _base_rels(base_data_root)
    worldgen_rels: frozenset[str] = frozenset()
    if base_worldgen is not None and base_worldgen.is_dir():
        worldgen_rels = frozenset(
            f"{DATA_DIRNAME}/minecraft/worldgen/{path.relative_to(base_worldgen).as_posix()}"
            for path in base_worldgen.rglob("*")
            if path.is_file()
        )
        notes.append(
            f"vanilla worldgen taken from runtime reports ({len(worldgen_rels)} files) "
            "and merged into the base before datapacks"
        )
    pending, dispositions, overrides, filtered, non_data, metadata, inactive_files = _plan(
        units, base_rels | worldgen_rels
    )
    if non_data:
        notes.append(
            f"数据包里有 {non_data} 个非 data/ 文件（如 assets/）未进入合并结果："
            "数据包合并只作用于 data/ 侧，资源侧不是本项目的分析对象"
        )
    if metadata:
        notes.append(f"{metadata} 个 {PACK_META} 只作元数据，不进入合并结果")
    if inactive_files:
        notes.append(
            f"{inactive_files} 个文件位于不适用于当前格式版本的 overlay 目录，"
            "未进入合并结果（台账里逐条标 inactive_overlay）"
        )

    root = pack_datapack_merged(manifest.pack_dir)
    dest_data = root / DATA_DIRNAME
    marker = root / MARKER_NAME
    key = {
        "merge_schema": MERGE_SCHEMA,
        "base": base_digest,
        "base_worldgen": len(worldgen_rels),
        "packs": [{"name": pack.name, "kind": pack.kind, "digest": pack.digest} for pack in packs],
    }
    key_digest = _dumps(key)

    recorded = _load_marker(marker)
    if not force and recorded.get("key") == key_digest and dest_data.is_dir():
        files = sum(1 for p in dest_data.rglob("*") if p.is_file())
        result = recorded.get("result") or {}
        if files == result.get("files"):
            return DatapackMerge(
                mode=MODE_MERGED,
                data_root=dest_data,
                root=root,
                packs=tuple(packs),
                files=files,
                overrides=int(result.get("overrides") or 0),
                tag_merges=int(result.get("tag_merges") or 0),
                filtered=int(result.get("filtered") or filtered),
                dropped=int(result.get("dropped") or non_data),
                cache_hit=True,
                notes=tuple(recorded.get("notes") or notes),
                dispositions=tuple(dispositions),
                key=key_digest,
            )

    if root.exists():
        shutil.rmtree(root)
    dest_data.mkdir(parents=True, exist_ok=True)
    files = _copy_base(base_data_root, dest_data)
    if worldgen_rels:
        files += _copy_base(base_worldgen, dest_data / "minecraft" / "worldgen")
    base_files = files
    print(f"合并数据包：基底 {files} 个文件 → {dest_data}", file=sys.stderr)

    tag_merges = 0
    log_path = root / LOG_NAME
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        # 基底被屏蔽的文件先删掉：它在合并结果里不该存在
        all_filters = [rule for unit in units for rule in unit.filters]
        blocked_base = _plan_base(base_data_root, all_filters)
        for rel in blocked_base:
            (dest_data / rel).unlink(missing_ok=True)
            log.write(
                json.dumps({"pack": BASE_PACK_NAME, "file": f"data/{rel}", "action": "filtered"},
                           ensure_ascii=False, sort_keys=True) + "\n"
            )
        filtered += len(blocked_base)

        for layer, rel, _namespace, path in pending:
            target = dest_data.parent / rel
            raw = layer.view.read(rel)
            if _is_tag(path):
                action = _merge_tag(target, _parse_payload(raw, f"{layer.label}:{rel}"))
                tag_merges += 1
                log.write(
                    json.dumps({"pack": layer.label, "file": rel, "action": "tag_" + action},
                               ensure_ascii=False, sort_keys=True) + "\n"
                )
                continue
            if target.is_file():
                log.write(
                    json.dumps({"pack": layer.label, "file": rel, "action": "override"},
                               ensure_ascii=False, sort_keys=True) + "\n"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

    files = sum(1 for p in dest_data.rglob("*") if p.is_file())
    write_json(
        marker,
        {
            "merge_schema": MERGE_SCHEMA,
            "mode": MODE_MERGED,
            "key": key_digest,
            "base": {"digest": base_digest, "files": base_files},
            "packs": [pack.as_json() for pack in packs],
            "result": {
                "files": files,
                "overrides": overrides,
                "tag_merges": tag_merges,
                "filtered": filtered,
                "dropped": non_data,
            },
            "contract": {"data_root": DATA_DIRNAME, "log": LOG_NAME},
            "notes": notes,
        },
    )
    print(
        f"合并完成：{len(packs)} 个数据包，覆盖 {overrides} 处，"
        f"标签合并 {tag_merges} 处，filter 屏蔽 {filtered} 处 → {root}",
        file=sys.stderr,
    )
    return DatapackMerge(
        mode=MODE_MERGED,
        data_root=dest_data,
        root=root,
        packs=tuple(packs),
        files=files,
        overrides=overrides,
        tag_merges=tag_merges,
        filtered=filtered,
        dropped=non_data,
        cache_hit=False,
        notes=tuple(notes),
        dispositions=tuple(dispositions),
        key=key_digest,
    )


def _load_marker(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _read_doc(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def base_pack_version(pack_dir: Path) -> PackVersion | None:
    """原版数据包自己的格式号，用于判外来包是否相容、overlay 是否适用。

    主来源是 `version.json` 的 `pack_version`：新式拆 `data_major` / `data_minor`，
    旧式只有 `data`（minor 视为 0）。该文件由 `ensure_data` 从内层 jar 派生。
    缺失时回退读原版 `pack.mcmeta` 的 `pack_format`；两处都没有返回 `None`，
    调用方跳过格式校验并把原因写进 notes——不猜一个数字。
    """
    version = _read_doc(pack_dir / "version.json")
    if version is not None:
        pack_version = version.get("pack_version")
        if isinstance(pack_version, dict):
            major = pack_version.get("data_major")
            if isinstance(major, int):
                minor = pack_version.get("data_minor")
                return (major, minor if isinstance(minor, int) else 0)
            legacy = pack_version.get("data")
            if isinstance(legacy, int):
                return (legacy, 0)
        elif isinstance(pack_version, int):
            return (pack_version, 0)

    meta = _read_doc(pack_dir / PACK_META)
    if meta is not None:
        pack_section = meta.get("pack")
        if isinstance(pack_section, dict):
            value = pack_section.get("pack_format")
            if isinstance(value, int):
                return (value, 0)
    return None


__all__ = [
    "DatapackMerge",
    "Disposition",
    "MODE_IDENTITY",
    "MODE_MERGED",
    "OverlayDir",
    "PackInfo",
    "base_pack_version",
    "ensure_datapack_merge",
]
