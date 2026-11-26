"""`ra` 命令行入口。

当前实现 `inputs init` 与 `probe`。`ingest / run / why / diff` 随后接。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .adapters import (
    audit_tree,
    record,
    require_complete,
    run_adapters,
    skip_excluded_dirs,
    skip_inert_registries,
    sweep_assets,
)
from .errors import RaError
from .facts import FactStore, LedgerStatus
from .inputs import (
    ensure_data,
    ensure_datapack_merge,
    ensure_reports,
    ensure_source,
    ensure_structures,
    inspect_jar,
    load_manifest,
    reconcile,
)
from .inputs.datapacks import ACTION_FILTERED, ACTION_MERGED, ACTION_METADATA, ACTION_NON_DATA
from .inputs.datapacks import ACTION_INACTIVE_OVERLAY, ACTION_OVERRIDDEN, base_pack_version
from .inputs.manifest import check_pack_layout
from .ir.knowledge import load_exceptions
from .paths import (
    cache_root,
    input_pack,
    inputs_root,
    manifest_file,
    output_dir,
    output_export,
    output_facts,
    output_paths,
    output_profile,
    output_report,
    pack_data,
    pack_jar,
    repo_root,
)
from .probe import build_profile
from .probe.registries import Registries
from .report.drift import compare_ledger
from .report.paths.store import GRAPH_FILE
from .util.hashing import sha256_file
from .util.jsonio import write_json, write_jsonl


def _pack(args: argparse.Namespace):
    root = repo_root()
    pack_dir = input_pack(root, args.label)
    manifest = load_manifest(pack_dir)
    return root, pack_dir, manifest


def cmd_inputs_init(args: argparse.Namespace) -> int:
    """校验某版本的输入是否齐备，并把 `inputs/manifest.toml` 与目录对齐。"""
    root = repo_root()
    added, ignored, manifest_path = reconcile(root)

    if added:
        print(f"新增配置段：{', '.join(added)}", file=sys.stderr)
    if ignored:
        print(f"忽略无对应目录的配置段：{', '.join(ignored)}", file=sys.stderr)
    print(f"配置 {manifest_path}", file=sys.stderr)

    if args.label is None:
        packs = sorted(child.name for child in inputs_root(root).iterdir() if child.is_dir())
        print(f"输入包：{', '.join(packs) or '（无）'}")
        return 0

    pack_dir = input_pack(root, args.label)
    if not pack_dir.is_dir():
        raise RaError(f"输入包目录不存在：{pack_dir}")

    manifest = load_manifest(pack_dir)
    if not manifest.java_home:
        print(
            f'未配置 java_home。在 {manifest_path} 的 ["{manifest.label}"] 段填写。',
            file=sys.stderr,
        )

    if args.jar:
        src_jar = Path(args.jar)
        if not src_jar.is_absolute():
            src_jar = (root / args.jar).resolve()
        dest_jar = pack_jar(pack_dir)
        if dest_jar.exists() and sha256_file(dest_jar) == sha256_file(src_jar):
            print("jar 已在位（摘要一致），跳过复制", file=sys.stderr)
        else:
            shutil.copy2(src_jar, dest_jar)
            print(f"复制 jar → {dest_jar}  sha256={sha256_file(dest_jar)[:16]}…", file=sys.stderr)

    if not pack_jar(pack_dir).exists():
        print(f"尚缺：server.jar（放在 {pack_jar(pack_dir)} 或用 --jar 指定）", file=sys.stderr)
        return 1

    # 反编译源码二选一：从仓库导出，或用户已放好 java/。两种都不改用户的第一手内容。
    source = ensure_source(manifest, force=args.force)
    if source.commit:
        print(
            f"反编译源码 {'复用' if source.cache_hit else '已从仓库导出'}："
            f"{source.commit[:9]} 的 {source.source_path or '<根>'}"
            f"（{source.files} 个文件）→ {source.root}",
            file=sys.stderr,
        )
    else:
        print(
            f"反编译源码 复用现成副本（无 content_commit，provenance 记 null）："
            f"{source.files} 个文件 → {source.root}",
            file=sys.stderr,
        )

    check_pack_layout(pack_dir)
    jar = inspect_jar(pack_jar(pack_dir))
    print(
        "jar：bundler={b} main={m} java={j}（{c}）".format(
            b=jar.is_bundler, m=jar.main_class, j=jar.java_version, c=jar.java_component
        ),
        file=sys.stderr,
    )

    if not args.no_reports:
        reports, structures, data, merge = _prepare(manifest, root, force=args.force)
        print(f"reports {'复用缓存' if reports.cache_hit else '已生成'}：{reports.root}", file=sys.stderr)
        for note in reports.notes:
            print(f"  note: {note}", file=sys.stderr)
        print(
            f"数据包 {'复用缓存' if data.cache_hit else '已由 jar 派生'}："
            f"{data.files} 个文件 → {data.root}",
            file=sys.stderr,
        )
        _print_merge(merge)
        print(
            f"结构模板 {'复用缓存' if structures.cache_hit else '已生成'}："
            f"{structures.files} 个（{structures.rel_dir}）→ {structures.root}",
            file=sys.stderr,
        )

    print(f"输入包就绪：{pack_dir}")
    return 0


def _ingest_store(profile, pack_dir: Path, label: str, merge=None):
    """跑全部适配器，返回 `(store, unaccounted)`。

    `ingest` 与 `run` 共用这一段：`run` 需要同一份事实，
    重跑一遍适配器（约 20 秒）比读回 JSONL 再反序列化简单可靠。
    """
    store = FactStore(label=label)
    record_datapack_files(merge, pack_dir, store)
    skip_excluded_dirs(profile, pack_dir, store)
    skip_inert_registries(profile, pack_dir, store)
    asset_counts = sweep_assets(pack_dir, store)
    run_adapters(profile, pack_dir, store)
    store.extra["asset_counts"] = asset_counts
    if merge is not None:
        store.extra["datapack_merge"] = merge.as_json()
    unaccounted = audit_tree(profile, pack_dir, store)
    return store, unaccounted


def record_datapack_files(merge, pack_dir: Path, store: FactStore) -> None:
    """外来数据包是第一手输入，每个文件都要有交代（红线 3）。

    合并后的同名资源只留最终生效那份，被覆盖/被屏蔽的副本不是「不存在」：
    它们的去向逐条记在这里，否则「数据包里的配方为什么没生效」无从查起。
    """
    if merge is None or not merge.active:
        return
    rules = {
        ACTION_MERGED: "已并入覆盖结果，事实在 data/ 路径上记",
        ACTION_OVERRIDDEN: "被更高优先级的数据包或 overlay 覆盖",
        ACTION_METADATA: "数据包元数据，不进入合并结果",
        ACTION_FILTERED: "被更高优先级数据包的 filter 屏蔽",
        ACTION_NON_DATA: "非 data/ 内容，不参与数据侧合并",
        ACTION_INACTIVE_OVERLAY: "所在 overlay 不适用于当前格式版本",
    }
    for item in merge.dispositions:
        record(
            store,
            f"datapacks/{item.pack}/{item.file}",
            "datapack",
            status=LedgerStatus.CLASSIFIED_SKIP,
            rule=rules.get(item.action, item.action),
            note=f"数据包 {item.pack}",
        )


def _print_merge(merge) -> None:
    if merge is None or not merge.active:
        print("数据包覆盖   未启用（无 datapacks/ 输入）", file=sys.stderr)
        return
    names = ", ".join(pack.name for pack in merge.packs)
    print(
        f"数据包覆盖   {len(merge.packs)} 个包（{names}）→ {merge.root}",
        file=sys.stderr,
    )
    print(
        f"             覆盖 {merge.overrides} 处、标签合并 {merge.tag_merges} 处、"
        f"filter 屏蔽 {merge.filtered} 处、结果 {merge.files} 个文件",
        file=sys.stderr,
    )
    for note in merge.notes:
        print(f"  note: {note}", file=sys.stderr)


def _prepare(manifest, root: Path, *, force: bool = False):
    """确保 reports、数据包、覆盖合并、结构模板都已就位。

    四者都是派生品：reports 由 jar 跑 datagen 得到，数据包从内层 jar 解出，
    覆盖合并由两者加 `datapacks/` 算出，模板从 jar 内层转出（含数据包带入的那份）。
    顺序不能换：先解 bundler（reports 与数据包都要它），再合并，最后才能确定
    模板要转哪些 `.nbt`。
    """
    cache = cache_root(root)
    reports = ensure_reports(manifest, cache, force=force)
    data = ensure_data(manifest, cache, force=force)
    base_worldgen = None
    if reports.root is not None and not (pack_data(manifest.pack_dir) / "minecraft" / "worldgen").is_dir():
        candidate = Path(reports.root) / "worldgen" / "minecraft" / "worldgen"
        if candidate.is_dir():
            base_worldgen = candidate
    merge = ensure_datapack_merge(
        manifest,
        pack_data(manifest.pack_dir),
        f"{data.jar_sha256}|{data.files}",
        base_version=base_pack_version(manifest.pack_dir),
        base_worldgen=base_worldgen,
        force=force,
    )
    structures = ensure_structures(manifest, cache, force=force, overlay=merge)
    return reports, structures, data, merge


def cmd_ingest(args: argparse.Namespace) -> int:
    """把输入变成事实与台账。只做语法还原，不做语义。"""
    root, pack_dir, manifest = _pack(args)
    ensure_source(manifest)
    check_pack_layout(pack_dir)

    reports, structures, _, merge = _prepare(manifest, root, force=args.force_datagen)
    registries = Registries.load(reports.root / "registries.json")
    profile = build_profile(pack_dir, manifest, reports, registries, structures, merge=merge)

    write_json(
        output_profile(root, manifest.label),
        _profile_payload(profile, reports, manifest),
    )

    facts_dir = output_facts(root, manifest.label)
    store, unaccounted = _ingest_store(profile, pack_dir, manifest.label, merge)
    store.extra["ledger_drift"] = compare_ledger(facts_dir, store)
    store.write(facts_dir)

    print(f"事实种类     {store.kind_counts()}")
    print(f"事实状态     {store.status_counts()}")
    print(f"台账         {store.ledger.counts()}")
    skipped = store.ledger.skipped_rules()
    if skipped:
        print(f"整棵排除     {skipped}")
    for item in store.unavailable:
        print(f"族不可用     {item['family']}：{item['reason']}")
        if item["note"]:
            print(f"             {item['note']}")
    for item in store.extra.get("low_yield_idioms", []):
        print(
            f"低于下限     {item['idiom']}：命中 {item['hits']}，"
            f"参考下限 {item['reference_floor']}（事实仍可用）"
        )
    for cov in store.coverage():
        marker = "  <-- 有失败" if cov.failed else ""
        print(
            f"族 {cov.family:<16} {cov.files:>5} 文件  "
            f"parsed={cov.parsed:<5} partial={cov.partial:<4} "
            f"skipped={cov.skipped:<5} failed={cov.failed:<4}"
            f"{marker}"
        )
    if unaccounted:
        print(f"\n未交代文件 {len(unaccounted)} 个（前 10）：")
        for rel in unaccounted[:10]:
            print(f"  {rel}")
    drift = store.extra.get("ledger_drift") or {}
    if drift.get("baseline"):
        print("\n台账漂移   首次运行，已记为基线")
    elif drift.get("changed") or drift.get("appeared_count") or drift.get("vanished_count"):
        print(
            f"\n台账漂移   状态翻转 {drift['changed']}  "
            f"新增 {drift['appeared_count']}  消失 {drift['vanished_count']}"
        )
        for item in (drift.get("changes") or [])[:5]:
            print(f"  {item['file']}: {item['before']} -> {item['after']}")
    else:
        print("\n台账漂移   无（与上次运行一致）")
    print(f"\n写入 {facts_dir}")

    if not args.allow_unknown:
        try:
            require_complete(unaccounted)
        except RaError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            print("加 --allow-unknown 可先产出报告", file=sys.stderr)
            return 1
    return 0


def _profile_payload(profile, reports, manifest) -> dict:
    """Profile 的布局结论与 reports 的 provenance 合并成一节。

    两者都写 `reports`：前者的 `components_layout` / `datapack_capability` /
    `block_report` 是探测结论，后者是 jar 摘要与 Java 版本。合并而不是覆盖，
    否则探测结论会从产物里消失，下游读不到「这一版的报告布局是什么样」。
    `root` 两处同义，取 provenance 的那个。
    """
    payload = profile.as_json()
    layout = payload.get("reports") or {}
    payload["reports"] = {**layout, **reports.as_json()}
    payload["jar"] = {
        "path": pack_jar(manifest.pack_dir).name,
        "sha256": reports.jar_sha256,
    }
    return payload


def cmd_probe(args: argparse.Namespace) -> int:
    root, pack_dir, manifest = _pack(args)
    ensure_source(manifest)
    check_pack_layout(pack_dir)

    reports, structures, _, merge = _prepare(manifest, root, force=args.force_datagen)
    registries = Registries.load(reports.root / "registries.json")
    profile = build_profile(pack_dir, manifest, reports, registries, structures, merge=merge)

    out_path = output_profile(root, manifest.label)
    write_json(out_path, _profile_payload(profile, reports, manifest))

    print(f"label            {profile.label}")
    print(f"content_commit   {profile.content_commit or 'null'}")
    print(f"source_repo      {profile.source_repo or '（未配置 [source] repo）'}")
    print(
        "version          data_major={a} world_version={b} protocol={c}".format(
            a=profile.pack_data_major, b=profile.world_version, c=profile.protocol_version
        )
    )
    print(f"reports          cache_hit={reports.cache_hit} java={reports.java['version']}")
    print(f"components       {profile.components_layout}")
    print(f"datapack.json    {profile.datapack_capability}")
    _print_merge(merge)
    print(f"namespaces       {', '.join(profile.namespaces)}")
    for registry, claim in sorted(profile.dirs.items()):
        print(f"dir {registry:<12} {claim.path:<26} {claim.files:>5} files  {claim.coverage:.0%}")
    for registry, tag in sorted(profile.tag_dirs.items()):
        print(f"tag {registry:<26} {tag.path:<32} {tag.files:>4} files  {tag.coverage:.0%}")
    print(f"templates        {profile.template_dir}  source={profile.template_source}  files={profile.template_files}")
    print(f"worldgen         {profile.worldgen_source} {profile.worldgen_counts}")
    print(f"loot_dialect     {profile.loot_dialect} {profile.loot_dialect_evidence}")
    if profile.excluded_dirs:
        print(f"excluded         {', '.join(profile.excluded_dirs)}")
    for note in profile.notes:
        print(f"note: {note}")
    for item in profile.unresolved:
        print(f"UNRESOLVED: {item}")
    print(f"\n写入 {out_path}")

    if args.strict and profile.unresolved:
        return 2
    return 0


def _item_universe(store) -> list[str]:
    """物品全集：`registries.json` 的 `minecraft:item`。

    **必须用它而不是规则里出现的节点**：没有规则的物品在规则图上根本不存在，
    若拿规则节点当全集，这些物品会从报告里彻底消失——正是红线 3 禁止的静默跳过。
    运行期导出是唯一能给出注册表全集的途径（数据包里没有物品注册表）。
    """
    for fact in store.of_kind("registry"):
        if fact.payload.get("registry") == "minecraft:item":
            return sorted("item:" + name for name in fact.payload["entries"])
    return []


def cmd_run(args: argparse.Namespace) -> int:
    """事实 → 规则 → 不动点 → 报告。"""
    from .ir import IR_SCHEMA, KnowledgeError, compile_ir
    from .report import (
        build_coverage,
        build_reachability,
        build_run_manifest,
        gap_rows,
        write_json as write_report_json,
        write_reachability,
    )
    from .solve import solve

    root, pack_dir, manifest = _pack(args)
    ensure_source(manifest)
    check_pack_layout(pack_dir)

    reports, structures, _, merge = _prepare(manifest, root, force=args.force_datagen)
    registries = Registries.load(reports.root / "registries.json")
    profile = build_profile(pack_dir, manifest, reports, registries, structures, merge=merge)

    store, unaccounted = _ingest_store(profile, pack_dir, manifest.label, merge)

    # `run` 也落事实：报告里的每个判定都要能用 `output/<label>/facts/` 复盘，
    # 否则改了适配器之后只有报告变、事实不变，核对工具读到的是上一次的输入
    facts_dir = output_facts(root, manifest.label)
    store.write(facts_dir)

    try:
        rule_set, compile_report = compile_ir(store, root)
    except KnowledgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    compile_json = compile_report.as_json()
    if compile_json["unknown_conditions"] or compile_json["unknown_serializers"]:
        print("错误：编译期发现未登记的知识表项，不得静默当成默认值", file=sys.stderr)
        if compile_json["unknown_conditions"]:
            print(f"  未登记条件：{compile_json['unknown_conditions']}", file=sys.stderr)
        if compile_json["unknown_serializers"]:
            print(f"  未登记 serializer：{compile_json['unknown_serializers']}", file=sys.stderr)
        if not args.allow_unknown:
            print("加 --allow-unknown 可先产出报告并查看归因", file=sys.stderr)
            return 1

    result = solve(rule_set, compile_report.seeds)
    universe = _item_universe(store)
    reachability = build_reachability(
        rule_set,
        result,
        identities=universe,
        exceptions=load_exceptions(root, universe),
    )

    out_dir = output_report(root, manifest.label)
    write_reachability(out_dir / "reachability.jsonl", reachability)
    write_report_json(out_dir / "coverage.json", build_coverage(rule_set, result, compile_json))
    write_report_json(
        out_dir / "run.json",
        build_run_manifest(
            label=manifest.label,
            content_commit=manifest.content_commit,
            source_repo=str(manifest.source_repo) if manifest.source_repo else None,
            jar_sha256=reports.jar_sha256,
            ir_schema=IR_SCHEMA,
            adapter_versions={},
            facts_digest=dict(store.kind_counts()),
            compile_report=compile_json,
            reachability=reachability,
        ),
    )
    gaps = gap_rows(reachability)
    with (out_dir / "gaps.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in gaps:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # 工具要求明细：方块 → 最低等级 / 最弱合格工具。下游直接消费，不再自己
    # 读标签或从工具组件反推；`tools` 导出为物品 ID（去掉 `tool:` 节点前缀）。
    with (out_dir / "harvest.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for block in sorted(
            set(compile_report.harvest_levels) | set(compile_report.harvest_capabilities)
        ):
            tools = [
                value.split(":", 1)[1] if value.startswith("tool:") else value
                for value in compile_report.harvest_capabilities.get(block) or ()
            ]
            row = {
                "block": block,
                "tool_level": compile_report.harvest_levels.get(block),
                "tools": tools,
                "gap": block in set(compile_report.harvest_gaps),
            }
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    total = sum(reachability.counts.values())
    print(f"规则         {compile_report.rule_counts}")
    print(f"播种         {len(compile_report.seeds)} 个节点（assume + world）")
    print(f"节点状态     {result.counts()}")
    print(
        f"物品可达     {reachability.counts.get('true', 0)}/{total}"
        f" = {reachability.rate:.1%}"
    )
    reasons: dict[str, int] = {}
    for attribution in reachability.attributions.values():
        reasons[attribution.reason] = reasons.get(attribution.reason, 0) + 1
    if reasons:
        print(f"不可达归因   {reasons}")
    for note in compile_report.notes:
        print(f"编译期       {note}")
    if compile_report.missing_stations:
        print(f"缺设备物品   {compile_report.missing_stations[:8]}")
    if unaccounted:
        print(f"未交代文件   {len(unaccounted)}（详见 ingest）")
    print(f"\n写入 {out_dir}（reachability / coverage / run / gaps / harvest）")
    return 0


def _resolve_node(token: str) -> str:
    """把用户输入变成节点 id。

    接受三种写法：完整节点（`item:minecraft:cod_bucket`）、裸 id
    （`minecraft:cod_bucket`）、裸名（`cod_bucket`）。后两种只有在
    `items` 里能唯一命中才可用——其余排序（block/entity/table）不猜。
    """
    from .ir.nodes import SORTS

    head, sep, rest = token.partition(":")
    if sep and head in SORTS:
        return token
    if sep:
        return f"item:{token}"
    return f"item:minecraft:{token}"


def cmd_why(args: argparse.Namespace) -> int:
    """追踪获取途径。

    **不重跑适配器**：读 `output/<label>/facts/` 重建事实库，再编译规则、重算不动点。
    事实就是这一层的输入，输入没变结论必然一致；重跑 ingest 要扫七千个 Java 文件。
    """
    from .ir import KnowledgeError, compile_ir
    from .report import build_reachability
    from .solve import enumerate_proofs, solve

    root, _, manifest = _pack(args)
    facts_dir = output_facts(root, manifest.label)
    if not facts_dir.is_dir():
        print(f"错误：{facts_dir} 不存在，先跑 `ra run --label {manifest.label}`", file=sys.stderr)
        return 1

    store = FactStore.read(facts_dir, label=manifest.label)
    try:
        rule_set, report = compile_ir(store, root)
    except KnowledgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    result = solve(rule_set, report.seeds)
    exceptions = load_exceptions(root, _item_universe(store))
    reachability = build_reachability(rule_set, result, exceptions=exceptions)

    rules_by_head = {head: rule_set.of_head(head) for head in rule_set.heads()}
    exit_code = 0

    for token in args.nodes:
        value = _resolve_node(token)
        state = result.of(value)
        print(f"\n{'=' * 72}")
        print(f"{value}   {state.value}")
        print(f"{'=' * 72}")

        if state.name == "TRUE":
            proofs = enumerate_proofs(
                rules_by_head,
                result,
                value,
                max_depth=args.depth,
                # 顶层默认全列：用户问的就是「有几条途径」，截断会直接给错答案
                max_paths=args.paths if args.paths > 0 else len(rules_by_head.get(value, [])),
                branch_limit=args.paths if args.paths > 0 else 1,
            )
            total = len(rules_by_head.get(value, []))
            print(f"获取途径 {len(proofs)} 条（该节点共 {total} 条规则）\n")
            for index, step in enumerate(proofs, 1):
                print(f"--- 途径 {index} ---")
                _print_step(step, indent=0)
                print()
            continue

        attribution = reachability.attributions.get(value)
        if attribution is None:
            print("（该节点未在归因记录里；可能不是 item 节点）")
            exit_code = 1
            continue
        print(f"归因  {attribution.reason}")
        if attribution.note:
            print(f"说明  {attribution.note}")
        if attribution.missing:
            print(f"缺项  {attribution.missing}")
        if attribution.cycle:
            print(f"环    {attribution.cycle}")
        if attribution.rule is not None:
            print(f"规则  {attribution.rule.id}")
            if attribution.rule.prov:
                prov = attribution.rule.prov
                print(f"来源  {prov.file}" + (f"#{prov.ptr}" if prov.ptr else ""))
        if attribution.alternatives:
            print(f"其它走不通的规则  {attribution.alternatives}")
        exit_code = 1

    return exit_code


def _read_json_optional(path: Path) -> dict | None:
    """读 JSON，文件不存在时返回 None。产物缺失是常态，不是错误。"""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_graph(args: argparse.Namespace) -> int:
    """把全量关系图落成内部表示文件，供 `ra-paths` 取路线。

    与 `ra run` 共用同一套读入（事实 → 规则 → 不动点），但产物不同：
    `run` 出可达性报告，这里出**图本身**（规则 + 三值状态 + 种子）。

    为什么要分出来：建图与画图的变化节奏不同。内部表示只在事实或 IR 变化时变，
    画图参数每次都要调。拆开之后可视化不必重算，也不必管缓存失效——文件里带
    `facts_digest` 与 `ir_schema`，过期了一眼能看出来。
    """
    from .ir import KnowledgeError, compile_ir
    from .ir.compile import IR_SCHEMA
    from .report.paths.store import GRAPH_FILE, serialize_graph, write_graph
    from .solve import solve

    root, _, manifest = _pack(args)
    facts_dir = output_facts(root, manifest.label)
    if not facts_dir.is_dir():
        print(f"错误：{facts_dir} 不存在，先跑 `ra run --label {manifest.label}`", file=sys.stderr)
        return 1

    store = FactStore.read(facts_dir, label=manifest.label)
    try:
        rule_set, report = compile_ir(store, root)
    except KnowledgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    result = solve(rule_set, report.seeds)

    graph = serialize_graph(
        rule_set,
        result,
        report.seeds,
        label=manifest.label,
        ir_schema=IR_SCHEMA,
        facts_digest=dict(store.kind_counts()),
    )
    graph_dir = output_dir(root, manifest.label)
    path = graph_dir / GRAPH_FILE
    write_graph(graph, path)

    size = path.stat().st_size
    counts = result.counts()
    print(
        f"内部表示 {graph.header.rule_count} 条规则、{graph.header.node_count} 个节点"
        f"（true {counts.get('true', 0)}、false {counts.get('false', 0)}、"
        f"unknown {counts.get('unknown', 0)}）"
    )
    print(f"播种   {len(graph.seeds)} 个节点")
    print(f"体积   {size / 1024 / 1024:.2f} MB（graph_schema {graph.header.graph_schema}）")
    print(f" -> {path}")
    print(f"\n从这个文件出图：ra-paths --graph {path} <物品 id>")
    print(f"渲染件写入 {output_paths(root, manifest.label)}")
    return 0


def _read_json_optional(path: Path) -> dict | None:
    """读 JSON，文件不存在时返回 None。产物缺失是常态，不是错误。"""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_export(args: argparse.Namespace) -> int:
    """把事实导成可查表：结构布局四张 + 刷怪表一张 + 挖掘三张 + 公式一份。

    只读 `output/<label>/facts/`：这些表是事实的下游视图，不重跑探测与适配器。
    结构与模板之间的拼装关系、容器与方块白名单、刷怪权重、方块硬度与工具速度
    都在事实里，这一层不读 `inputs/`。挖掘进度公式（每 tick
    `玩家速度 / 硬度 / (正确工具 ? 30 : 100)`，累计 >= 1 破坏）落成
    `mining_formula.json`。
    """
    from .report.mining_tables import (
        FORMULA_FILE,
        TABLE_NAMES as MINING_TABLES,
        build_mining_tables,
        mining_formula,
    )
    from .report.spawn_tables import TABLE_SPAWNS, build_spawn_tables
    from .report.structure_tables import TABLE_NAMES, build_structure_tables

    root, _, manifest = _pack(args)
    facts_dir = output_facts(root, manifest.label)
    if not facts_dir.is_dir():
        print(
            f"错误：{facts_dir} 不存在，先跑 `ra ingest --label {manifest.label}`",
            file=sys.stderr,
        )
        return 1

    store = FactStore.read(facts_dir, label=manifest.label)
    tables = build_structure_tables(store)
    tables.update(build_spawn_tables(store))
    tables.update(build_mining_tables(store))
    export_dir = output_export(root, manifest.label)
    counts: dict[str, int] = {}
    for name in (*TABLE_NAMES, TABLE_SPAWNS, *MINING_TABLES):
        counts[name] = write_jsonl(export_dir / f"{name}.jsonl", tables[name])
        print(f"{name:<20} {counts[name]:>7} 行 -> {export_dir / f'{name}.jsonl'}")

    formula = mining_formula(store)
    if formula is not None:
        write_json(export_dir / FORMULA_FILE, formula)
        print(f"{FORMULA_FILE:<20} {'':>7}   -> {export_dir / FORMULA_FILE}")

    write_json(
        export_dir / "exports.json",
        {
            "label": manifest.label,
            "facts_digest": dict(store.kind_counts()),
            "tables": counts,
        },
    )
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    """把 `output/<label>/` 的探测与产物结论渲染成该版本的适配指南。

    指南回答「本次输入里哪些位置有数据、各自什么形状、哪些不能用」，
    而不是「这个版本有多少个文件」——规模随输入变化，判据在 `docs/shapes.md`。
    只读现有产物，不重跑探测与适配器。
    """
    from .report.guide import render_guide

    root, pack_dir, manifest = _pack(args)
    out_dir = output_dir(root, manifest.label)

    profile_path = output_profile(root, manifest.label)
    profile = _read_json_optional(profile_path)
    if profile is None:
        print(f"错误：{profile_path} 不存在，先跑 `ra probe --label {manifest.label}`",
              file=sys.stderr)
        return 1

    report_dir = output_report(root, manifest.label)
    text = render_guide(
        profile=profile,
        facts_summary=_read_json_optional(output_facts(root, manifest.label) / "summary.json"),
        coverage=_read_json_optional(report_dir / "coverage.json"),
        run=_read_json_optional(report_dir / "run.json"),
        root=str(root),
    )

    out_path = out_dir / "guide.md"
    out_path.write_text(text, encoding="utf-8")
    if args.stdout:
        print(text)
    else:
        print(f"写入 {out_path}")
    return 0


def _print_step(step, indent: int) -> None:
    """打印一步。假设与世界是起点，其余带规则 id 与来源。"""
    from .solve import Step  # noqa: F401

    pad = "  " * indent
    label = step.node
    if step.cut:
        print(f"{pad}{label}   ← （已展开过或超出深度，截断）")
        return
    if step.is_root:
        print(f"{pad}{label}   ← 起点")
        return
    if step.rule is not None:
        print(f"{pad}{label}")
        print(f"{pad}  ← {step.rule.kind} {step.rule.id}")
        prov = step.rule.prov
        if prov is not None:
            ptr = f"#{prov.ptr}" if prov.ptr else ""
            print(f"{pad}    {prov.input}: {prov.file}{ptr}")
        if step.rule.soft:
            print(f"{pad}    软约束 {list(step.rule.soft)}")
    else:
        print(f"{pad}{label}")
    for child in step.children:
        _print_step(child, indent + 1)
    for note in step.unexpanded:
        print(f"{pad}  ... {note}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ra", description="Minecraft 原版物品获取路径分析")
    sub = parser.add_subparsers(dest="command", required=True)

    inputs = sub.add_parser("inputs", help="输入包装配")
    inputs_sub = inputs.add_subparsers(dest="inputs_command", required=True)

    init = inputs_sub.add_parser("init", help="校验输入、对齐配置、跑 datagen")
    init.add_argument("--label", default=None, help="版本标签；不给则只对齐配置并列出输入包")
    init.add_argument("--jar", default=None, help="服务端 jar（相对仓库根或绝对路径）")
    init.add_argument("--no-reports", action="store_true", help="只校验，不跑 datagen")
    init.add_argument("--force", action="store_true", help="强制重跑 datagen")
    init.set_defaults(func=cmd_inputs_init)

    probe = sub.add_parser("probe", help="探测输入，输出 Profile")
    probe.add_argument("--label", required=True)
    probe.add_argument("--strict", action="store_true", help="存在 UNRESOLVED 时返回非零")
    probe.add_argument("--force-datagen", action="store_true")
    probe.set_defaults(func=cmd_probe)

    ingest = sub.add_parser("ingest", help="把输入变成事实与台账")
    ingest.add_argument("--label", required=True)
    ingest.add_argument("--allow-unknown", action="store_true",
                        help="有未交代文件时仍产出报告")
    ingest.add_argument("--force-datagen", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    run = sub.add_parser("run", help="事实 → 规则 → 不动点 → 可达性报告")
    run.add_argument("--label", required=True)
    run.add_argument("--allow-unknown", action="store_true",
                     help="知识表有未登记项时仍产出报告")
    run.add_argument("--force-datagen", action="store_true")
    run.set_defaults(func=cmd_run)

    why = sub.add_parser("why", help="追踪一个物品的获取途径，或说明为什么拿不到")
    why.add_argument("--label", required=True)
    why.add_argument("nodes", nargs="+", help="节点或物品 id，如 minecraft:cod_bucket")
    why.add_argument("--paths", type=int, default=0,
                     help="每个中间节点展开几条子路径；0 = 顶层全列、中间层各一条（默认）")
    why.add_argument("--depth", type=int, default=16, help="最大展开深度")
    why.set_defaults(func=cmd_why)

    guide = sub.add_parser("guide", help="把探测与产物结论渲染成该版本的适配指南")
    guide.add_argument("--label", required=True)
    guide.add_argument("--stdout", action="store_true", help="同时打印到终端")
    guide.set_defaults(func=cmd_guide)

    export = sub.add_parser("export", help="把事实导成可查表（结构布局、刷怪权重、挖掘数值与进度公式）")
    export.add_argument("--label", required=True)
    export.set_defaults(func=cmd_export)

    graph = sub.add_parser("graph", help="把全量关系图落成内部表示文件（供 ra-paths 出图）")
    graph.add_argument("--label", required=True)
    graph.set_defaults(func=cmd_graph)


    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RaError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
