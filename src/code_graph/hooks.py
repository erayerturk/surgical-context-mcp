"""Claude Code PreToolUse hook guard and `install-hooks` / `GRAPH_REPORT.md` installer."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .context_builder import build_architecture_map, format_architecture_map_markdown
from .db import DB_FILENAME, CodeGraphDB
from .indexer import ProjectIndexer
from .parser import EXTENSION_TO_LANGUAGE

MAX_UNBOUNDED_READ_LINES = 300
MAX_WINDOW_READ_LINES = 280


def _find_project_root_with_db(start_path: Path) -> Path | None:
    """Walk upward from `start_path` to find a directory containing `.code_graph.sqlite`."""
    curr = start_path.resolve()
    if curr.is_file():
        curr = curr.parent
    for parent in [curr, *curr.parents]:
        if (parent / DB_FILENAME).exists():
            return parent
    return None


def run_hook_guard() -> int:
    """
    Claude Code `PreToolUse` hook entrypoint (reads JSON from stdin).
    - Blocks unbounded `Read` calls on large source files (>300 lines) and returns the file's
      function index (`Lstart-end: signature`) so Claude uses `get_edit_context` or `Read(offset, limit)`.
    - Enriches `Grep` calls on symbol names with instant AST definition locations from `.code_graph.sqlite`.
    """
    try:
        raw_stdin = sys.stdin.read().strip()
        if not raw_stdin:
            return 0
        payload = json.loads(raw_stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    cwd_str = payload.get("cwd") or "."

    # 1. Guard unbounded `Read` on large source code files (>300 lines)
    if tool_name in ("Read", "read_file"):
        fp_str = tool_input.get("file_path") or tool_input.get("Path") or ""
        if not fp_str:
            return 0

        full_path = Path(fp_str).expanduser()
        if not full_path.is_Absolute() if hasattr(full_path, "is_Absolute") else not full_path.is_absolute():
            full_path = (Path(cwd_str) / full_path).resolve()

        ext = full_path.suffix.lower()
        if ext not in EXTENSION_TO_LANGUAGE or ext == ".tscn" or not full_path.is_file():
            return 0

        offset = tool_input.get("offset") or tool_input.get("StartLine")
        limit = tool_input.get("limit")
        end_line = tool_input.get("EndLine")

        # If a bounded line window (<= 280 lines) was requested, allow it!
        if offset is not None and (
            (limit is not None and int(limit) <= MAX_WINDOW_READ_LINES)
            or (end_line is not None and (int(end_line) - int(offset)) <= MAX_WINDOW_READ_LINES)
        ):
            return 0

        try:
            line_count = full_path.read_bytes().count(b"\n") + 1
        except OSError:
            return 0

        if line_count <= MAX_UNBOUNDED_READ_LINES:
            return 0

        # File is >300 lines and Read is unbounded: look up its function list in .code_graph.sqlite
        proj_root = _find_project_root_with_db(full_path) or _find_project_root_with_db(Path(cwd_str))
        fn_list_str = ""
        rel_display = full_path.name

        if proj_root:
            try:
                rel_display = full_path.relative_to(proj_root).as_posix()
                db = CodeGraphDB(proj_root)
                rows = db.conn.execute(
                    """
                    SELECT name, start_line, end_line, signature
                    FROM nodes
                    WHERE file_path = ? AND kind IN ('method', 'function', 'class')
                    ORDER BY start_line ASC
                    LIMIT 35
                    """,
                    (rel_display,),
                ).fetchall()
                if rows:
                    fn_lines = [
                        f"  L{r['start_line']}-{r['end_line']}: {r['signature']}"
                        for r in rows
                        if not str(r["signature"]).startswith("script ")
                    ]
                    if fn_lines:
                        fn_list_str = "\nFunctions in this file:\n" + "\n".join(fn_lines[:25])
                        if len(fn_lines) > 25:
                            fn_list_str += f"\n  ... (+{len(fn_lines) - 25} more)"
                db.close()
            except Exception:
                pass

        reason = (
            f"[code-graph guard] '{rel_display}' has {line_count} lines (> {MAX_UNBOUNDED_READ_LINES}L limit for full-file Read). "
            f"To save tokens: (1) Call MCP tool `get_edit_context(symbol)` or `get_symbol_impact(symbol)`, "
            f"OR (2) Call `Read` with `offset` and `limit` (<= {MAX_WINDOW_READ_LINES} lines) for the specific function you need."
            f"{fn_list_str}"
        )

        response = {
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
        print(json.dumps(response, ensure_ascii=False))
        return 0

    # 2. Enrich `Grep` on symbol names with instant AST locations
    if tool_name == "Grep":
        pattern = (tool_input.get("pattern") or "").strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.]{2,60}$", pattern):
            proj_root = _find_project_root_with_db(Path(cwd_str))
            if proj_root:
                try:
                    db = CodeGraphDB(proj_root)
                    matches = db.find_symbol_nodes(pattern)
                    db.close()
                    if matches:
                        locs = ", ".join(
                            f"{m.qualified_name} @ {m.file_path}:{m.start_line}-{m.end_line}"
                            for m in matches[:5]
                        )
                        hint = (
                            f"[code-graph hint] Symbol '{pattern}' is indexed in .code_graph.sqlite: {locs}. "
                            f"Prefer `get_symbol_impact('{pattern}')` or `get_edit_context('{pattern}')` over broad Grep."
                        )
                        print(
                            json.dumps(
                                {
                                    "hookSpecificOutput": {
                                        "hookEventName": "PreToolUse",
                                        "additionalContext": hint,
                                    }
                                },
                                ensure_ascii=False,
                            )
                        )
                except Exception:
                    pass
        return 0

    return 0


def install_project_hooks_and_report(
    project_root: Path,
    write_report: bool = True,
) -> dict[str, Any]:
    """
    1. Index `project_root` into `.code_graph.sqlite`.
    2. Generate `GRAPH_REPORT.md` (with God Nodes, Clusters, Autoloads, Event Hubs).
    3. Configure `.claude/settings.local.json` with the `PreToolUse` hook for `Read|Grep`.
    """
    project_root = project_root.expanduser().resolve()
    indexer = ProjectIndexer(project_root)
    try:
        stats = indexer.index(force=False)
        arch = build_architecture_map(indexer.db, top_n=15, refreshed_files=stats.files_indexed)
        report_md = format_architecture_map_markdown(arch)
    finally:
        indexer.close()

    report_path = project_root / "GRAPH_REPORT.md"
    if write_report:
        report_path.write_text(report_md, encoding="utf-8")

    # Resolve path to `code-graph` CLI binary in the same venv as sys.executable
    venv_bin_cli = Path(sys.executable).parent / "code-graph"
    if venv_bin_cli.exists():
        hook_cmd = f"{venv_bin_cli} hook-guard"
    else:
        hook_cmd = f"{sys.executable} -m code_graph.cli hook-guard"

    claude_dir = project_root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.local.json"

    settings_data: dict[str, Any] = {}
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings_data = {}

    hooks_section = settings_data.setdefault("hooks", {})
    pre_tool_list: list[dict[str, Any]] = hooks_section.setdefault("PreToolUse", [])

    # Remove any previous code-graph hook entry before adding fresh one
    pre_tool_list[:] = [
        entry for entry in pre_tool_list
        if "hook-guard" not in json.dumps(entry)
    ]
    pre_tool_list.append({
        "matcher": "Read|Grep",
        "hooks": [
            {
                "type": "command",
                "command": hook_cmd,
            }
        ],
    })

    settings_path.write_text(json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "project_root": str(project_root),
        "report_path": str(report_path) if write_report else None,
        "settings_path": str(settings_path),
        "hook_command": hook_cmd,
        "god_nodes_count": len(arch.get("god_nodes", [])),
        "clusters_count": len(arch.get("clusters", [])),
    }
