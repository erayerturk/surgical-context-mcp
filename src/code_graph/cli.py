"""Command-line interface for `code-graph`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .context_builder import (
    build_architecture_map,
    build_edit_context,
    build_rename_or_delete_check,
    format_architecture_map_markdown,
    format_edit_context_markdown,
    format_rename_or_delete_markdown,
)
from .formatter import (
    format_diagnostics_markdown,
    format_flow_markdown,
    format_impact_compact,
    format_impact_markdown,
)
from .hooks import install_project_hooks_and_report, run_hook_guard
from .indexer import ProjectIndexer
from .mcp_server import run_server
from .workflows import (
    append_telemetry_line,
    build_diff_impact,
    build_tests_for_symbol,
    build_translation_report,
    format_diff_impact_markdown,
    format_tests_for_markdown,
    format_token_savings_markdown,
    format_translation_report_markdown,
)


def _cmd_index(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    if not target_path.exists():
        print(f"Error: Path does not exist: {target_path}", file=sys.stderr)
        return 1

    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=args.force)
        size_mb = round(stats.db_size_bytes / (1024 * 1024), 2)
        print("# Code Graph Index Summary")
        print(f"- **Project Root:** `{stats.project_root}`")
        print(f"- **Database:** `{stats.db_path}` ({size_mb} MB)")
        print(
            f"- **Files:** {stats.files_scanned} scanned | "
            f"{stats.files_indexed} parsed | "
            f"{stats.files_skipped} unchanged (cached) | "
            f"{stats.files_removed} removed"
        )
        print(f"- **Graph Size:** {stats.total_nodes} nodes, {stats.total_edges} edges")
        print(f"- **Time:** {stats.elapsed_ms} ms")
        if stats.warning:
            print(f"\n> ⚠️ **{stats.warning}**")
        return 0
    finally:
        indexer.close()


def _cmd_map(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        arch = build_architecture_map(indexer.db, top_n=args.top_n, refreshed_files=refreshed)
        md = format_architecture_map_markdown(arch)
        if args.write_report:
            report_file = target_path / "GRAPH_REPORT.md"
            report_file.write_text(md, encoding="utf-8")
        if args.json:
            print(json.dumps(arch, indent=2, ensure_ascii=False))
        else:
            print(md, end="")
            if args.write_report:
                print(f"\n> Saved `GRAPH_REPORT.md` to `{target_path / 'GRAPH_REPORT.md'}`")
        return 0
    finally:
        indexer.close()


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    if not target_path.exists():
        print(f"Error: Path does not exist: {target_path}", file=sys.stderr)
        return 1
    info = install_project_hooks_and_report(target_path, write_report=not args.no_report)
    print("# Code Graph Hooks & Architecture Report Installed")
    print(f"- **Project Root:** `{info['project_root']}`")
    print(f"- **Claude Hook Config:** `{info['settings_path']}`")
    print(f"- **Hook Command:** `{info['hook_command']}`")
    if info.get("report_path"):
        print(f"- **Architecture Report:** `{info['report_path']}` ({info['god_nodes_count']} god nodes, {info['clusters_count']} clusters)")
    return 0


def _cmd_hook_guard(_args: argparse.Namespace) -> int:
    return run_hook_guard()


def _cmd_impact(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        impact = indexer.db.get_symbol_impact(
            symbol=args.symbol,
            depth=args.depth,
            max_lines=args.max_lines,
            max_tokens=args.max_tokens,
            include_uncertain=args.include_uncertain,
            refreshed_files=refreshed,
        )
        if args.json:
            print(json.dumps(impact, indent=2, ensure_ascii=False))
        elif args.compact:
            print(format_impact_compact(impact), end="")
        else:
            print(format_impact_markdown(impact), end="")
        return 0 if impact.get("found") else 1
    finally:
        indexer.close()


def _cmd_edit_context(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        ctx = build_edit_context(
            db=indexer.db,
            symbol=args.symbol,
            budget_tokens=args.budget_tokens,
            refreshed_files=refreshed,
        )
        if args.json:
            print(json.dumps(ctx, indent=2, ensure_ascii=False))
        else:
            print(format_edit_context_markdown(ctx), end="")
        return 0 if ctx.get("found") else 1
    finally:
        indexer.close()


def _cmd_check(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        data = build_rename_or_delete_check(
            db=indexer.db,
            symbol=args.symbol,
            refreshed_files=refreshed,
        )
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(format_rename_or_delete_markdown(data), end="")
        return 0 if data.get("found") else 1
    finally:
        indexer.close()


def _cmd_flow(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        flow = indexer.db.find_flow(
            from_symbol=args.from_symbol,
            to_symbol=args.to_symbol,
            max_depth=args.max_depth,
            refreshed_files=refreshed,
        )
        if args.json:
            print(json.dumps(flow, indent=2, ensure_ascii=False))
        else:
            print(format_flow_markdown(flow), end="")
        return 0 if flow.get("found") else 1
    finally:
        indexer.close()


def _cmd_search(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        fresh = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"
        nodes = indexer.db.search_symbols(query=args.query, kind=args.kind, limit=args.limit)
        print(fresh)
        if not nodes:
            print(f"No symbols found matching '{args.query}'.")
            return 1
        print(f"## Symbols matching `{args.query or '*'}` ({len(nodes)})")
        for n in nodes:
            doc_str = f" — *{n.doc_comment[:100]}*" if getattr(n, "doc_comment", "") else ""
            print(
                f"- **`{n.qualified_name}`** ({n.kind}) — `{n.file_path}:{n.start_line}-{n.end_line}` ({n.line_count}L)\n"
                f"  `{n.signature}`{doc_str}"
            )
        return 0
    finally:
        indexer.close()


def _cmd_tests_for(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        data = build_tests_for_symbol(indexer.db, symbol=args.symbol, refreshed_files=refreshed)
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            out = format_tests_for_markdown(data)
            out = append_telemetry_line(
                indexer.db,
                "tests_for",
                args.symbol,
                out,
                data.get("touched_files", []),
            )
            print(out, end="")
        return 0 if data.get("found") else 1
    finally:
        indexer.close()


def _cmd_diff_impact(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        data = build_diff_impact(indexer.db, base_ref=args.base, refreshed_files=refreshed)
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            out = format_diff_impact_markdown(data)
            out = append_telemetry_line(
                indexer.db,
                "get_diff_impact",
                args.base,
                out,
                data.get("touched_files", []),
            )
            print(out, end="")
        return 0
    finally:
        indexer.close()


def _cmd_i18n(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        data = build_translation_report(indexer.db, refreshed_files=refreshed)
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            out = format_translation_report_markdown(data)
            out = append_telemetry_line(
                indexer.db,
                "check_translations",
                "*",
                out,
                data.get("touched_files", []),
            )
            print(out, end="")
        return 0
    finally:
        indexer.close()


def _cmd_savings(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        indexer.index(force=False)
        summary = indexer.db.get_telemetry_summary(reset=args.reset)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(format_token_savings_markdown(summary), end="")
        return 0
    finally:
        indexer.close()


def _cmd_stats(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    indexer = ProjectIndexer(target_path)
    try:
        stats = indexer.index(force=False)
        diag = indexer.db.get_diagnostics()
        if stats.warning:
            diag["warning"] = stats.warning
        if args.json:
            print(json.dumps(diag, indent=2, ensure_ascii=False))
        else:
            print(format_diagnostics_markdown(diag), end="")
            if stats.warning:
                print(f"\n> ⚠️ **{stats.warning}**")
        return 0
    finally:
        indexer.close()


def _cmd_watch(args: argparse.Namespace) -> int:
    target_path = Path(args.path).expanduser().resolve()
    print(f"Watching {target_path} every {args.interval}s (Ctrl+C to stop)...")
    try:
        while True:
            indexer = ProjectIndexer(target_path)
            try:
                stats = indexer.index(force=False)
                if stats.files_indexed > 0 or stats.files_removed > 0:
                    print(
                        f"[updated] +{stats.files_indexed} parsed, -{stats.files_removed} removed "
                        f"({stats.elapsed_ms} ms)"
                    )
            finally:
                indexer.close()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    run_server(default_root=args.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-graph",
        description="Local AST Code Knowledge Graph & MCP Server for minimal LLM token usage.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. code-graph index [path]
    p_index = subparsers.add_parser(
        "index",
        help="Scan directory and build/update .code_graph.sqlite incrementally.",
    )
    p_index.add_argument("path", nargs="?", default=".", help="Target project directory.")
    p_index.add_argument("--force", action="store_true", help="Force re-parsing all files.")
    p_index.set_defaults(func=_cmd_index)

    # 2. code-graph map / report [path]
    for cmd_alias in ("map", "report"):
        p_map = subparsers.add_parser(
            cmd_alias,
            help="Generate Architecture Map & God Nodes summary (and optionally write GRAPH_REPORT.md).",
        )
        p_map.add_argument("path", nargs="?", default=".", help="Target project directory.")
        p_map.add_argument("--top-n", type=int, default=15, help="Number of God Nodes to list (default: 15).")
        p_map.add_argument("--write-report", "-w", action="store_true", help="Write GRAPH_REPORT.md to project root.")
        p_map.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_map.set_defaults(func=_cmd_map)

    # 3. code-graph install-hooks [path]
    p_hooks = subparsers.add_parser(
        "install-hooks",
        help="Install Claude Code PreToolUse Read|Grep guard into .claude/settings.local.json and generate GRAPH_REPORT.md.",
    )
    p_hooks.add_argument("path", nargs="?", default=".", help="Target project directory.")
    p_hooks.add_argument("--no-report", action="store_true", help="Do not write GRAPH_REPORT.md.")
    p_hooks.set_defaults(func=_cmd_install_hooks)

    # 4. code-graph hook-guard (internal PreToolUse stdin handler)
    p_guard = subparsers.add_parser(
        "hook-guard",
        help="Internal Claude Code PreToolUse hook entrypoint (reads JSON from stdin).",
    )
    p_guard.set_defaults(func=_cmd_hook_guard)

    # 5. code-graph impact <symbol>
    p_impact = subparsers.add_parser(
        "impact",
        help="Print compact Markdown subgraph of callers, callees, events, and coverage warnings.",
    )
    p_impact.add_argument("symbol", help="Symbol (`name`, `Class.method`, or `file::symbol`).")
    p_impact.add_argument("--depth", "-d", type=int, default=2, help="Traversal depth (default: 2).")
    p_impact.add_argument("--max-lines", type=int, default=300, help="Max total lines in context locations (default: 300).")
    p_impact.add_argument("--max-tokens", type=int, default=800, help="Max estimated output tokens (default: 800).")
    p_impact.add_argument("--compact", "-c", action="store_true", help="Compact mode: only file:range — symbol.")
    p_impact.add_argument("--include-uncertain", action="store_true", help="Include name_only (uncertain) edges.")
    p_impact.add_argument("--path", "-p", default=".", help="Project root path.")
    p_impact.add_argument("--json", action="store_true", help="Output raw JSON.")
    p_impact.set_defaults(func=_cmd_impact)

    # 6. code-graph edit-context <symbol>
    for cmd_alias in ("edit-context", "get_edit_context"):
        p_edit = subparsers.add_parser(
            cmd_alias,
            help="Return skeleton, target body, used members/callees, caller ±5L windows, other signatures, and coverage warnings.",
        )
        p_edit.add_argument("symbol", help="Symbol to prepare edit context for.")
        p_edit.add_argument("--budget-tokens", "-b", type=int, default=4000, help="Token budget (default: 4000).")
        p_edit.add_argument("--path", "-p", default=".", help="Project root path.")
        p_edit.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_edit.set_defaults(func=_cmd_edit_context)

    # 7. code-graph check <symbol>
    for cmd_alias in ("check", "check_rename_or_delete"):
        p_check = subparsers.add_parser(
            cmd_alias,
            help="Pre-delete/rename safety report: call sites, text matches, overrides, .tscn & signal connections.",
        )
        p_check.add_argument("symbol", help="Symbol to verify before renaming or deleting.")
        p_check.add_argument("--path", "-p", default=".", help="Project root path.")
        p_check.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_check.set_defaults(func=_cmd_check)

    # 8. code-graph flow <from_symbol> <to_symbol>
    p_flow = subparsers.add_parser(
        "flow",
        help="Find the shortest call/event path between two symbols.",
    )
    p_flow.add_argument("from_symbol", help="Starting symbol.")
    p_flow.add_argument("to_symbol", help="Target symbol.")
    p_flow.add_argument("--max-depth", type=int, default=8, help="Max hops (default: 8).")
    p_flow.add_argument("--path", "-p", default=".", help="Project root path.")
    p_flow.add_argument("--json", action="store_true", help="Output raw JSON.")
    p_flow.set_defaults(func=_cmd_flow)

    # 9. code-graph search [query]
    p_search = subparsers.add_parser(
        "search",
        help="Search indexed symbols by name, concept/doc_comment, or kind.",
    )
    p_search.add_argument("query", nargs="?", default="", help="Substring or concept query.")
    p_search.add_argument(
        "--kind",
        "-k",
        choices=["class", "function", "method", "event", "constant", "field", "data", "i18n"],
        default=None,
        help="Filter by symbol kind.",
    )
    p_search.add_argument("--limit", type=int, default=30, help="Max results.")
    p_search.add_argument("--path", "-p", default=".", help="Project root path.")
    p_search.set_defaults(func=_cmd_search)

    # 9b. code-graph tests-for <symbol>
    for cmd_name in ("tests-for", "tests_for"):
        p_tests = subparsers.add_parser(
            cmd_name,
            help="Find tests exercising <symbol> and output exact runnable test commands.",
        )
        p_tests.add_argument("symbol", help="Target symbol (`name`, `Class.method`, or `file::symbol`).")
        p_tests.add_argument("--path", "-p", default=".", help="Project root path.")
        p_tests.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_tests.set_defaults(func=_cmd_tests_for)

    # 9c. code-graph diff-impact [--base HEAD]
    for cmd_name in ("diff-impact", "get_diff_impact"):
        p_diff = subparsers.add_parser(
            cmd_name,
            help="Analyze git diff hunks, modified symbols, callers, cross-language contracts, and runnable tests.",
        )
        p_diff.add_argument("--base", "-b", default="HEAD", help="Git ref to diff against (default: HEAD).")
        p_diff.add_argument("--path", "-p", default=".", help="Project root path.")
        p_diff.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_diff.set_defaults(func=_cmd_diff_impact)

    # 9d. code-graph i18n / translations
    for cmd_name in ("i18n", "translations", "check_translations"):
        p_i18n = subparsers.add_parser(
            cmd_name,
            help="Audit translation keys: missing keys, raw non-key literals, format placeholder mismatches, and unused keys.",
        )
        p_i18n.add_argument("--path", "-p", default=".", help="Project root path.")
        p_i18n.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_i18n.set_defaults(func=_cmd_i18n)

    # 9e. code-graph savings
    for cmd_name in ("savings", "get_token_savings"):
        p_sav = subparsers.add_parser(
            cmd_name,
            help="Show measured token savings telemetry (returned tokens vs raw touched file tokens).",
        )
        p_sav.add_argument("--reset", action="store_true", help="Reset telemetry after reading.")
        p_sav.add_argument("--path", "-p", default=".", help="Project root path.")
        p_sav.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_sav.set_defaults(func=_cmd_savings)

    # 10. code-graph stats / doctor [path]
    for cmd_name in ("stats", "doctor"):
        p_stats = subparsers.add_parser(
            cmd_name,
            help="Show language file counts, largest directories, DB size, and edge confidence breakdown.",
        )
        p_stats.add_argument("path", nargs="?", default=".", help="Project root path.")
        p_stats.add_argument("--json", action="store_true", help="Output raw JSON.")
        p_stats.set_defaults(func=_cmd_stats)

    # 11. code-graph watch [path]
    p_watch = subparsers.add_parser(
        "watch",
        help="Continuously watch directory and incrementally update .code_graph.sqlite.",
    )
    p_watch.add_argument("path", nargs="?", default=".", help="Project root path.")
    p_watch.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds (default: 2.0).")
    p_watch.set_defaults(func=_cmd_watch)

    # 12. code-graph serve [--path .]
    p_serve = subparsers.add_parser("serve", help="Run the MCP server over stdio.")
    p_serve.add_argument("--path", "-p", default=None, help="Optional default project root path.")
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
