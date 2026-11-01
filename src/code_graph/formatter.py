"""Compact, token-optimized Markdown formatters for CLI and MCP responses."""

from __future__ import annotations

import datetime
from typing import Any

from .context_builder import format_coverage_warnings_section


def _freshness_line(data: dict[str, Any]) -> str:
    refreshed = int(data.get("refreshed_files", 0))
    return "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"


def format_impact_compact(data: dict[str, Any]) -> str:
    """Output only `file:start-end — symbol` lines without prose (compact mode)."""
    fresh = _freshness_line(data)
    if not data.get("found"):
        return f"{fresh}\n# Not found: {data.get('symbol', '')}\n"
    lines: list[str] = [fresh]
    for f in data.get("files_to_read", []):
        conf = f.get("confidence", "exact")
        lines.append(f"{f['location']} [{conf}] — {f['symbol']}")
    omitted = data.get("omitted_locations", 0)
    if omitted > 0:
        lines.append(f"# +{omitted} more locations omitted")
    return "\n".join(lines) + "\n"


def format_impact_markdown(data: dict[str, Any]) -> str:
    """Format `get_symbol_impact` output into a dense, token-saving Markdown summary (< 3 KB)."""
    fresh = _freshness_line(data)
    symbol = data.get("symbol", "")
    if not data.get("found"):
        cov_lines = format_coverage_warnings_section(data.get("coverage", {}), upstream_empty=True)
        return (
            f"{fresh}\n### Symbol Impact: `{symbol}`\n\n"
            f"> {data.get('message', 'Symbol not found.')}\n"
            + "\n".join(cov_lines)
            + "\n"
        )

    max_tokens = int(data.get("max_tokens_budget", 800))
    max_chars = max(900, max_tokens * 4)

    lines: list[str] = [
        fresh,
        f"## Symbol Impact: `{symbol}` (depth={data.get('depth', 2)})",
    ]

    # Disambiguation candidates if multiple definitions exist
    disamb = data.get("disambiguation", [])
    if len(disamb) > 1:
        lines.append(f"\n> **Multiple definitions ({len(disamb)})** — specify `file::symbol` or `Class.symbol` to narrow:")
        for c in disamb[:6]:
            lines.append(f"> - `{c['selector']}` ({c['language']}) @ `{c['location']}`")
        if len(disamb) > 6:
            lines.append(f"> - *(+{len(disamb) - 6} more candidates)*")

    # 1. Definitions
    defs = data.get("definitions", [])
    if defs:
        lines.append("\n### Definitions")
        for d in defs:
            lcnt = d.get("line_count", 1)
            lines.append(
                f"- **`{d['qualified_name']}`** ({d['kind']}, exact) — `{d['file']}:{d['lines']}` ({lcnt} lines)\n"
                f"  `{d['signature']}`"
            )

    for hint in data.get("small_file_hints", []):
        lines.append(f"> 💡 **Tip:** {hint}")

    # 2. Minimal Context Locations
    files_to_read = data.get("files_to_read", [])
    if files_to_read:
        total_lines = data.get("total_context_lines", 0)
        lines.append(f"\n### Minimal Context Locations ({total_lines} lines total, sorted by relevance)")
        for f in files_to_read:
            note = f" [{f['window_note']}]" if f.get("window_note") else ""
            d_tag = "def" if f.get("depth", 0) == 0 else f"d={f['depth']}"
            conf = f.get("confidence", "exact")
            lines.append(
                f"- `{f['location']}` ({f.get('line_count', 1)} lines, {d_tag}, {conf}) — `{f['symbol']}` ({f['kind']}){note}"
            )
        omitted = data.get("omitted_locations", 0)
        if omitted > 0:
            lines.append(f"- *(+{omitted} more locations omitted due to budget)*")

    # 3. Upstream (Callers / Triggers)
    upstream = data.get("upstream", [])
    cov = data.get("coverage", {})
    if upstream:
        lines.append("\n### Upstream (Callers / Event Triggers)")
        shown_up = 0
        for u in upstream:
            entry = (
                f"- [d={u['depth']},{u.get('confidence', 'exact')}] `{u['from']}` "
                f"--(**{u['edge_type']}** @ `{u['call_site']}`)--> `{u['to']}` "
                f"(def: `{u['from_file']}`, {u.get('from_lines', 1)}L)"
            )
            if sum(len(x) for x in lines) + len(entry) > max_chars - 350 and shown_up >= 3:
                rem = len(upstream) - shown_up
                lines.append(f"- *(+{rem} more callers omitted due to token budget)*")
                break
            lines.append(entry)
            shown_up += 1
    elif cov.get("is_engine_lifecycle"):
        lines.append(
            "\n### Upstream\n- Engine lifecycle callback (invoked automatically by the engine runtime)."
        )
    else:
        total_txt = int(cov.get("total_text_matches", 0))
        lines.append(
            f"\n### Upstream\n- Grafta çağıran bulunamadı; metin taramasında **{total_txt}** eşleşme bulundu."
        )

    # 3b. Cross-Language Contracts (Opcodes `0x...`, Config `json:"..."`, Schema Keys)
    contracts = data.get("cross_language_contracts", [])
    if contracts:
        lines.append("\n### Cross-Language Contracts (Opcode / Config / JSON Schema)")
        for cc in contracts[:12]:
            callers_note = f" — used by: {', '.join(cc['callers'])}" if cc.get("callers") else ""
            lines.append(
                f"- **`{cc['contract']}`** <-> **`{cc['symbol']}`** ({cc['language']}, `{cc['location']}`) — `{cc['signature']}`{callers_note}"
            )

    # 4. Downstream (Callees / Emitted Events / Listeners)
    downstream = data.get("downstream", [])
    if downstream:
        lines.append("\n### Downstream (Callees / Events / Listeners)")
        shown_down = 0
        for d in downstream:
            entry = (
                f"- [d={d['depth']},{d.get('confidence', 'exact')}] `{d['from']}` "
                f"--(**{d['edge_type']}** @ `{d['call_site']}`)--> `{d['to']}` "
                f"(def: `{d['to_file']}`, {d.get('to_lines', 1)}L)"
            )
            if sum(len(x) for x in lines) + len(entry) > max_chars - 240 and shown_down >= 3:
                rem = len(downstream) - shown_down
                lines.append(f"- *(+{rem} more callees omitted due to token budget)*")
                break
            lines.append(entry)
            shown_down += 1
    else:
        lines.append("\n### Downstream\n- *(No outgoing calls or emitted events)*")

    # 5. Events Involved
    events = data.get("events", [])
    if events:
        evt_names = ", ".join(f"`{e['name']}`" for e in events[:12])
        lines.append(f"\n### Event / Signal Channels\n- {evt_names}")

    # 6. Uncertain (name_only) summary (max 5)
    uncertain = data.get("uncertain_name_only", [])[:5]
    if uncertain:
        summary_str = ", ".join(
            f"`{u['symbol']}` ({u['candidate_definitions']} candidates)" for u in uncertain
        )
        lines.append(f"\n### Uncertain (`name_only`, max 5)\n- {summary_str}")

    # 7. Coverage Warnings (always included)
    lines.extend(format_coverage_warnings_section(cov, upstream_empty=(len(upstream) == 0)))

    return "\n".join(lines) + "\n"


def format_flow_markdown(data: dict[str, Any]) -> str:
    """Format `find_flow` shortest path result into compact Markdown."""
    fresh = _freshness_line(data)
    src = data.get("from", "")
    dst = data.get("to", "")
    cov = data.get("coverage", {})

    if not data.get("found"):
        cov_lines = format_coverage_warnings_section(cov, upstream_empty=True)
        return (
            f"{fresh}\n### Flow: `{src}` -> `{dst}`\n\n"
            f"> {data.get('message', 'No path found.')}\n"
            + "\n".join(cov_lines)
            + "\n"
        )

    steps = data.get("steps", [])
    mode = data.get("mode", "directed")
    hops = data.get("hop_count", 0)

    lines: list[str] = [
        fresh,
        f"## Shortest Flow: `{src}` -> `{dst}` ({hops} hop{'s' if hops != 1 else ''}, {mode})",
        "",
    ]

    for idx, step in enumerate(steps):
        if idx == 0:
            lines.append(
                f"1. **START** `{step['symbol']}` ({step['kind']}) @ `{step['location']}`\n"
                f"   `{step['signature']}`"
            )
        else:
            edge = step.get("via_edge") or "calls"
            site = step.get("call_site") or ""
            lines.append(f"   │  *--[{edge} @ `{site}`]-->*")
            lines.append(
                f"{idx + 1}. **`{step['symbol']}`** ({step['kind']}) @ `{step['location']}`\n"
                f"   `{step['signature']}`"
            )

    lines.extend(format_coverage_warnings_section(cov, upstream_empty=False))
    return "\n".join(lines) + "\n"


def format_diagnostics_markdown(diag: dict[str, Any]) -> str:
    """Format `stats` / `doctor` output into clean Markdown."""
    ts = diag.get("last_indexed_at")
    ts_str = (
        datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        if ts
        else "Never"
    )
    lines: list[str] = [
        "# Code Graph Diagnostics (`stats` / `doctor`)",
        f"- **Project Root:** `{diag['project_root']}`",
        f"- **Database:** `{diag['db_path']}` ({diag['db_size_mb']} MB / {diag['db_size_bytes']:,} bytes)",
        f"- **Last Indexed:** {ts_str}",
        f"- **Totals:** {diag['total_files']} files | {diag['total_nodes']} nodes | {diag['total_edges']} edges",
    ]

    langs = diag.get("files_by_language", {})
    if langs:
        lang_str = ", ".join(f"`{k}`: {v}" for k, v in langs.items())
        lines.append(f"- **Files by Language:** {lang_str}")

    confs = diag.get("edges_by_confidence", {})
    if confs:
        conf_str = ", ".join(f"`{k}`: {v}" for k, v in confs.items())
        lines.append(f"- **Edges by Confidence:** {conf_str}")

    top_dirs = diag.get("top_directories", {})
    if top_dirs:
        dir_str = ", ".join(f"`{k}` ({v})" for k, v in top_dirs.items())
        lines.append(f"- **Top Directories:** {dir_str}")

    autoloads = diag.get("godot_autoloads", {})
    if autoloads:
        auto_str = ", ".join(f"`{k}` -> `{v}`" for k, v in autoloads.items())
        lines.append(f"- **Godot Autoloads:** {auto_str}")

    telem = diag.get("telemetry", {})
    if telem and telem.get("total_calls", 0) > 0:
        lines.append(
            f"- **Measured Token Savings ({telem['total_calls']} queries):** "
            f"returned ~{telem['total_returned_tokens']:,} vs raw files ~{telem['total_raw_files_tokens']:,} "
            f"-> **saved ~{telem['total_saved_tokens']:,} tokens ({telem['overall_savings_percent']}%)**"
        )

    return "\n".join(lines) + "\n"
