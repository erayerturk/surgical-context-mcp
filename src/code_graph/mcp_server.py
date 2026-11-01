"""Model Context Protocol (MCP) stdio server for Code Knowledge Graph."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

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
from .indexer import ProjectIndexer

mcp = FastMCP(
    "code-graph",
    instructions=(
        "AST Code Graph. Call `get_architecture_map` first in a new session, `get_edit_context` before editing a symbol, "
        "`get_symbol_impact` / `find_flow` to explore, and `check_rename_or_delete` before renaming or deleting."
    ),
)


def _resolve_root(project_path: str | None = None) -> Path:
    if project_path:
        return Path(project_path).expanduser().resolve()
    env_root = os.environ.get("CODE_GRAPH_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


@mcp.tool()
def get_architecture_map(
    project_path: str | None = None,
    top_n: int = 15,
) -> str:
    """Call at session start to orient without Glob/Grep. Returns God Nodes (central hubs), subsystem clusters, Godot autoloads, and top event channels."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        arch = build_architecture_map(
            db=indexer.db,
            top_n=top_n,
            refreshed_files=refreshed,
        )
        return format_architecture_map_markdown(arch)
    finally:
        indexer.close()


@mcp.tool()
def get_edit_context(
    symbol: str,
    project_path: str | None = None,
    budget_tokens: int = 4000,
) -> str:
    """Call before modifying a symbol. Returns file skeleton, full target body, used members/callees, caller ±5L windows, other signatures, and coverage warnings within `budget_tokens`."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        ctx = build_edit_context(
            db=indexer.db,
            symbol=symbol,
            budget_tokens=budget_tokens,
            refreshed_files=refreshed,
        )
        return format_edit_context_markdown(ctx)
    finally:
        indexer.close()


@mcp.tool()
def check_rename_or_delete(
    symbol: str,
    project_path: str | None = None,
) -> str:
    """Call before deleting, renaming, or changing a signature. Returns all call sites, overrides, `.tscn` connections, signal emit/connect sites, and string/dynamic text references."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        data = build_rename_or_delete_check(
            db=indexer.db,
            symbol=symbol,
            refreshed_files=refreshed,
        )
        return format_rename_or_delete_markdown(data)
    finally:
        indexer.close()


@mcp.tool()
def get_symbol_impact(
    symbol: str,
    depth: int = 2,
    max_lines: int = 300,
    max_tokens: int = 800,
    output_format: str = "markdown",
    project_path: str | None = None,
) -> str:
    """Get callers, callees, events, coverage warnings, and exact file:line ranges for `symbol` (`name`, `Class.method`, or `file::symbol`)."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        impact = indexer.db.get_symbol_impact(
            symbol=symbol,
            depth=depth,
            max_lines=max_lines,
            max_tokens=max_tokens,
            refreshed_files=refreshed,
        )
        fmt = output_format.lower()
        if fmt == "json":
            return json.dumps(impact, indent=2, ensure_ascii=False)
        if fmt == "compact":
            return format_impact_compact(impact)
        return format_impact_markdown(impact)
    finally:
        indexer.close()


@mcp.tool()
def find_flow(
    from_symbol: str,
    to_symbol: str,
    max_depth: int = 8,
    output_format: str = "markdown",
    project_path: str | None = None,
) -> str:
    """Find shortest high-confidence call/signal/event path between `from_symbol` and `to_symbol`."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        flow = indexer.db.find_flow(
            from_symbol=from_symbol,
            to_symbol=to_symbol,
            max_depth=max_depth,
            refreshed_files=refreshed,
        )
        if output_format.lower() == "json":
            return json.dumps(flow, indent=2, ensure_ascii=False)
        return format_flow_markdown(flow)
    finally:
        indexer.close()


@mcp.tool()
def search_symbols(
    query: str = "",
    kind: str | None = None,
    limit: int = 25,
    project_path: str | None = None,
) -> str:
    """Search indexed symbols by substring and optional `kind` (`class`, `function`, `method`, `event`)."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=False)
        refreshed = stats.files_indexed + stats.files_removed
        fresh = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"
        nodes = indexer.db.search_symbols(query=query, kind=kind, limit=limit)
        if not nodes:
            return f"{fresh}\nNo symbols found matching query='{query}' kind='{kind}'."
        lines = [fresh, f"## Matched Symbols ({len(nodes)})"]
        for n in nodes:
            lines.append(
                f"- **`{n.qualified_name}`** ({n.kind}) — `{n.file_path}:{n.start_line}-{n.end_line}` ({n.line_count}L) | `{n.signature}`"
            )
        return "\n".join(lines)
    finally:
        indexer.close()


@mcp.tool()
def reindex_project(
    project_path: str | None = None,
    force: bool = False,
) -> str:
    """Incrementally update `.code_graph.sqlite` and return index diagnostics."""
    root = _resolve_root(project_path)
    indexer = ProjectIndexer(root)
    try:
        stats = indexer.index(force=force)
        diag = indexer.db.get_diagnostics()
        if stats.warning:
            diag["warning"] = stats.warning
        return format_diagnostics_markdown(diag)
    finally:
        indexer.close()


def run_server(default_root: str | None = None) -> None:
    """Start the MCP server over stdio."""
    if default_root:
        os.environ["CODE_GRAPH_ROOT"] = str(Path(default_root).expanduser().resolve())
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        os.environ["CODE_GRAPH_ROOT"] = str(Path(sys.argv[1]).expanduser().resolve())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
