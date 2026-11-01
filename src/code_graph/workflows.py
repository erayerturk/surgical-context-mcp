"""Advanced cross-language workflows: i18n graph checker, `tests_for` finder, `get_diff_impact`, and token telemetry."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .context_builder import scan_coverage_warnings
from .db import CodeGraphDB
from .models import GraphNode


def _is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name.lower()
    if (
        name.endswith("_test.go")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.tsx")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name.endswith("_test.gd")
        or name.endswith("test.cs")
        or name.endswith("tests.cs")
        or name.endswith("test.java")
        or name.endswith("tests.java")
        or "/tests/" in f"/{rel_path.lower()}"
        or "/test/" in f"/{rel_path.lower()}"
        or "/__tests__/" in f"/{rel_path.lower()}"
    ):
        return True
    return False


def _is_test_node(node: GraphNode) -> bool:
    if _is_test_file(node.file_path):
        if node.kind in ("function", "method", "class"):
            return True
    if node.name.startswith(("Test", "test_")):
        return True
    sig_lower = (node.signature or "").lower()
    if any(tok in sig_lower for tok in ("#[test]", "#[tokio::test]", "[test]", "[fact]", "[theory]", "@test")):
        return True
    return False


# -------------------------------------------------------------------------
# 1. Translation / i18n Key Graph Validator (`check_translations`)
# -------------------------------------------------------------------------

def build_translation_report(
    db: CodeGraphDB,
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Analyze the translation (`i18n`) graph across code (`tr("...")`, `noticeT(..., "n.x")`)
    and locale files (`locales/*.json`, `*.po`):
      - `missing_keys`: used in code, but missing from locale files (flags suspicious non-key literals like `"aday"` / `"oy"`).
      - `placeholder_mismatches`: `%d`/`%s`/`{}` count mismatches between locales OR between a call site and locale definition.
      - `unused_keys`: defined in locale files, but never referenced in code.
    """
    def_rows = db.conn.execute(
        "SELECT target_name, receiver, file_path, line FROM edges WHERE edge_type = 'defines_i18n' ORDER BY target_name, file_path"
    ).fetchall()
    use_rows = db.conn.execute(
        "SELECT source_id, target_name, receiver, file_path, line FROM edges WHERE edge_type = 'uses_i18n' ORDER BY target_name, file_path, line"
    ).fetchall()

    # Map key -> list of {locale_file, line, placeholders}
    defined_keys: dict[str, list[dict[str, Any]]] = {}
    for r in def_rows:
        k = r["target_name"]
        recv = r["receiver"] or ""
        ph = int(recv.split(":")[1]) if recv.startswith("placeholders:") and recv.split(":")[1].isdigit() else 0
        defined_keys.setdefault(k, []).append({
            "file": r["file_path"],
            "line": int(r["line"]),
            "placeholders": ph,
        })

    # Map key -> list of {caller_id, file, line, args_passed}
    used_keys: dict[str, list[dict[str, Any]]] = {}
    for r in use_rows:
        k = r["target_name"]
        recv = r["receiver"] or ""
        args_cnt = int(recv.split(":")[1]) if recv.startswith("args:") and recv.split(":")[1].isdigit() else 0
        used_keys.setdefault(k, []).append({
            "caller_id": r["source_id"],
            "file": r["file_path"],
            "line": int(r["line"]),
            "args_passed": args_cnt,
        })

    missing_keys: list[dict[str, Any]] = []
    placeholder_mismatches: list[dict[str, Any]] = []
    unused_keys: list[dict[str, Any]] = []

    # 1. Check used keys for missing definitions or call-site placeholder mismatches
    for k, usages in sorted(used_keys.items()):
        looks_like_raw_word = "." not in k and "_" not in k and k.islower()
        if k not in defined_keys:
            missing_keys.append({
                "key": k,
                "suspicious_raw_literal": looks_like_raw_word,
                "usages": [f"{u['file']}:{u['line']}" for u in usages[:6]],
            })
            continue

        defs = defined_keys[k]
        expected_phs = {d["placeholders"] for d in defs}
        max_expected_ph = max(expected_phs) if expected_phs else 0

        if max_expected_ph > 0:
            for u in usages:
                # Report if call passes format args (>0) that don't match expected placeholder count,
                # or if a Go `noticeT`/`formatT` call passes 0 format args for a string requiring `%d`/`%s`
                is_go_call = u["file"].endswith(".go")
                if (u["args_passed"] > 0 and u["args_passed"] != max_expected_ph) or (is_go_call and u["args_passed"] != max_expected_ph):
                    placeholder_mismatches.append({
                        "key": k,
                        "kind": "call_site_mismatch",
                        "location": f"{u['file']}:{u['line']}",
                        "expected_placeholders": max_expected_ph,
                        "passed_args": u["args_passed"],
                        "detail": f"Call at `{u['file']}:{u['line']}` passes {u['args_passed']} format arg(s), but `{k}` expects {max_expected_ph} placeholder(s).",
                    })

    # 2. Check defined keys for cross-locale placeholder mismatches & unused keys
    for k, defs in sorted(defined_keys.items()):
        ph_set = {d["placeholders"] for d in defs}
        if len(ph_set) > 1:
            breakdown = ", ".join(f"{d['file']}:{d['line']} ({d['placeholders']} ph)" for d in defs)
            placeholder_mismatches.append({
                "key": k,
                "kind": "locale_mismatch",
                "location": f"{defs[0]['file']}:{defs[0]['line']}",
                "detail": f"Inconsistent placeholder count across locales for `{k}`: {breakdown}",
            })
        if k not in used_keys:
            unused_keys.append({
                "key": k,
                "defined_in": [f"{d['file']}:{d['line']}" for d in defs[:4]],
            })

    return {
        "refreshed_files": refreshed_files,
        "total_defined_keys": len(defined_keys),
        "total_used_keys": len(used_keys),
        "missing_keys": missing_keys,
        "placeholder_mismatches": placeholder_mismatches,
        "unused_keys": unused_keys,
    }


def format_translation_report_markdown(report: dict[str, Any]) -> str:
    refreshed = int(report.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"

    lines: list[str] = [
        fresh_header,
        "## Translation (`i18n`) Graph Report",
        f"- **Defined Keys in Locales:** {report.get('total_defined_keys', 0)} | **Keys Referenced in Code:** {report.get('total_used_keys', 0)}",
    ]

    missing = report.get("missing_keys", [])
    lines.append(f"\n### 1. Missing / Unresolved Keys Used in Code ({len(missing)})")
    if missing:
        for m in missing[:30]:
            flag = " **[SUSPICIOUS RAW WORD / NOT A KEY]**" if m.get("suspicious_raw_literal") else ""
            locs = ", ".join(f"`{loc}`" for loc in m["usages"])
            lines.append(f"- **`{m['key']}`**{flag} — used at {locs}")
        if len(missing) > 30:
            lines.append(f"- *(+{len(missing) - 30} more missing keys)*")
    else:
        lines.append("- None (all translation keys used in code exist in locale files).")

    mismatches = report.get("placeholder_mismatches", [])
    lines.append(f"\n### 2. Placeholder (`%d`/`%s`/`{{}}`) Count Mismatches ({len(mismatches)})")
    if mismatches:
        for mm in mismatches[:30]:
            lines.append(f"- **`{mm['key']}`** [{mm['kind']}] — {mm['detail']}")
    else:
        lines.append("- None.")

    unused = report.get("unused_keys", [])
    lines.append(f"\n### 3. Unused Locale Keys ({len(unused)})")
    if unused:
        for u in unused[:25]:
            locs = ", ".join(f"`{loc}`" for loc in u["defined_in"])
            lines.append(f"- `{u['key']}` (defined in {locs})")
        if len(unused) > 25:
            lines.append(f"- *(+{len(unused) - 25} more unused locale keys)*")
    else:
        lines.append("- None.")

    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# 2. Test Finder & Command Builder (`tests_for`)
# -------------------------------------------------------------------------

def _build_test_run_command(project_root: Path, test_file: str, test_names: list[str]) -> str:
    """Build the exact runnable terminal command for a test file and its test function names."""
    if test_file.endswith("_test.go"):
        # Find nearest parent directory with `go.mod`
        full_tf = project_root / test_file
        curr_dir = full_tf.parent
        go_mod_dir = project_root
        while curr_dir != project_root.parent:
            if (curr_dir / "go.mod").exists():
                go_mod_dir = curr_dir
                break
            if curr_dir == project_root:
                break
            curr_dir = curr_dir.parent

        try:
            rel_pkg = full_tf.parent.relative_to(go_mod_dir).as_posix()
        except ValueError:
            rel_pkg = "."
        pkg_arg = f"./{rel_pkg}" if rel_pkg not in ("", ".") else "./..."
        run_regex = "|".join(sorted(set(test_names)))
        run_flag = f" -run '^({run_regex})$'" if len(test_names) > 1 else (f" -run '^{test_names[0]}$'" if test_names else "")
        try:
            rel_mod = go_mod_dir.relative_to(project_root).as_posix()
        except ValueError:
            rel_mod = "."
        if rel_mod not in ("", "."):
            return f"cd {rel_mod} && go test {pkg_arg}{run_flag}"
        return f"go test {pkg_arg}{run_flag}"

    if test_file.endswith(".gd"):
        full_tf = project_root / test_file
        curr_dir = full_tf.parent
        godot_dir = project_root
        while curr_dir != project_root.parent:
            if (curr_dir / "project.godot").exists():
                godot_dir = curr_dir
                break
            if curr_dir == project_root:
                break
            curr_dir = curr_dir.parent
        try:
            rel_gd_dir = godot_dir.relative_to(project_root).as_posix()
            rel_script = full_tf.relative_to(godot_dir).as_posix()
        except ValueError:
            rel_gd_dir = "."
            rel_script = test_file
        if rel_gd_dir not in ("", "."):
            return f"godot --headless --path {rel_gd_dir} -s {rel_script}"
        return f"godot --headless -s {rel_script}"

    if test_file.endswith(".py"):
        k_expr = " or ".join(sorted(set(test_names)))
        k_flag = f' -k "{k_expr}"' if k_expr else ""
        return f"pytest {test_file}{k_flag}"

    if test_file.endswith((".ts", ".tsx", ".js", ".jsx")):
        t_expr = "|".join(sorted(set(test_names)))
        t_flag = f' -t "{t_expr}"' if t_expr else ""
        pkg_json = project_root / "package.json"
        if pkg_json.exists():
            try:
                pkg_txt = pkg_json.read_text(encoding="utf-8", errors="replace")
                if '"jest"' in pkg_txt and '"vitest"' not in pkg_txt:
                    return f"npx jest {test_file}{t_flag}"
            except OSError:
                pass
        return f"npx vitest run {test_file}{t_flag}"

    if test_file.endswith(".rs"):
        fn_arg = f" {test_names[0]}" if test_names else ""
        return f"cargo test{fn_arg}"

    if test_file.endswith(".cs"):
        if test_names:
            flt = "|".join(f"FullyQualifiedName~{n}" for n in sorted(set(test_names)))
            return f'dotnet test --filter "{flt}"'
        return f'dotnet test --filter "FullyQualifiedName~{Path(test_file).stem}"'

    if test_file.endswith(".java"):
        cls_stem = Path(test_file).stem
        if (project_root / "gradlew").exists() or (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
            if test_names:
                return f"./gradlew test --tests {cls_stem}.{test_names[0]}"
            return f"./gradlew test --tests {cls_stem}"
        if test_names:
            return f"mvn test -Dtest={cls_stem}#{test_names[0]}"
        return f"mvn test -Dtest={cls_stem}"

    return f"# run test file: {test_file}"


def build_tests_for_symbol(
    db: CodeGraphDB,
    symbol: str,
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Find all tests that exercise `symbol` (via 1..3 hop call/contract graph or naming/file conventions)
    and generate the exact CLI commands (`go test -run ...`, `godot -s ...`, `pytest ...`) to run them.
    """
    roots = db.find_symbol_nodes(symbol)
    short_name = symbol.split("::")[-1].split(".")[-1].strip()

    matched_test_nodes: dict[str, tuple[GraphNode, str]] = {}  # node_id -> (GraphNode, reason)

    # 1. Traverse upstream callers up to 3 hops from `roots` (plus cross-language contract peers)
    impact = db.get_symbol_impact(symbol, depth=3, max_lines=400, max_tokens=2000)
    for u in impact.get("upstream", []):
        from_id = u.get("from_id", "")
        if not from_id:
            continue
        row = db.conn.execute("SELECT * FROM nodes WHERE id = ?", (from_id,)).fetchone()
        if row:
            n = db._row_to_node(row)
            if _is_test_node(n):
                matched_test_nodes[n.id] = (n, f"calls `{u.get('to', symbol)}` (depth={u.get('depth', 1)}, {u.get('confidence', 'exact')})")

    for c_peer in impact.get("cross_language_contracts", []):
        for caller_str in c_peer.get("callers", []):
            c_name = caller_str.split(" (")[0]
            for cn in db.find_symbol_nodes(c_name):
                if _is_test_node(cn):
                    matched_test_nodes[cn.id] = (cn, f"exercises shared contract `{c_peer['contract']}` via `{c_peer['symbol']}`")

    # 2. Check unresolved/name_only edges or textual references in test files
    edge_rows = db.conn.execute(
        "SELECT source_id, file_path, line FROM edges WHERE target_name = ?",
        (short_name,),
    ).fetchall()
    for er in edge_rows:
        if _is_test_file(er["file_path"]):
            s_row = db.conn.execute("SELECT * FROM nodes WHERE id = ?", (er["source_id"],)).fetchone()
            if s_row:
                sn = db._row_to_node(s_row)
                if sn.id not in matched_test_nodes and sn.kind != "file":
                    matched_test_nodes[sn.id] = (sn, f"calls `{short_name}` at `{er['file_path']}:{er['line']}`")

    # 3. Naming convention match (`Test<Symbol>` or `test_<symbol>`)
    norm_sym = short_name.lstrip("_").lower().replace("_", "")
    if len(norm_sym) >= 3:
        cand_tests = db.conn.execute(
            "SELECT * FROM nodes WHERE (name LIKE 'Test%' OR name LIKE 'test_%') AND kind IN ('function', 'method')"
        ).fetchall()
        for ct in cand_tests:
            tn = db._row_to_node(ct)
            norm_t = tn.name.lower().replace("_", "")
            if norm_sym in norm_t and tn.id not in matched_test_nodes:
                matched_test_nodes[tn.id] = (tn, f"test name matches `{short_name}`")

    # 4. If a Godot / Python / Go test file mentions `short_name` in its body (e.g. script-level test `tests/test_*.gd`)
    if len(short_name) >= 3:
        test_file_rows = db.conn.execute("SELECT file_path FROM files").fetchall()
        for tfr in test_file_rows:
            tfp = tfr["file_path"]
            if not _is_test_file(tfp):
                continue
            full_p = db.project_root / tfp
            try:
                txt = full_p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(short_name)}(?![A-Za-z0-9_])", txt):
                f_nodes = db.conn.execute(
                    "SELECT * FROM nodes WHERE file_path = ? AND kind IN ('function', 'method')",
                    (tfp,),
                ).fetchall()
                added_any = False
                for fn_r in f_nodes:
                    fn_n = db._row_to_node(fn_r)
                    if fn_n.name.startswith(("Test", "test_", "_test", "run_")):
                        matched_test_nodes.setdefault(fn_n.id, (fn_n, f"references `{short_name}` in `{tfp}`"))
                        added_any = True
                if not added_any:
                    file_n_row = db.conn.execute(
                        "SELECT * FROM nodes WHERE file_path = ? AND kind = 'file'",
                        (tfp,),
                    ).fetchone()
                    if file_n_row:
                        fn_n = db._row_to_node(file_n_row)
                        matched_test_nodes.setdefault(fn_n.id, (fn_n, f"test script `{tfp}` references `{short_name}`"))

    # Group by test file and build runnable commands
    by_file: dict[str, list[dict[str, Any]]] = {}
    for _, (tnode, reason) in sorted(matched_test_nodes.items(), key=lambda x: (x[1][0].file_path, x[1][0].start_line)):
        by_file.setdefault(tnode.file_path, []).append({
            "test_symbol": tnode.qualified_name,
            "name": tnode.name if tnode.kind != "file" else "",
            "location": f"{tnode.file_path}:{tnode.start_line}-{tnode.end_line}",
            "signature": tnode.signature,
            "reason": reason,
        })

    commands: list[str] = []
    test_suites: list[dict[str, Any]] = []
    for tfp, items in by_file.items():
        fn_names = [it["name"] for it in items if it["name"]]
        cmd = _build_test_run_command(db.project_root, tfp, fn_names)
        commands.append(cmd)
        test_suites.append({
            "file": tfp,
            "command": cmd,
            "tests": items,
        })

    return {
        "symbol": symbol,
        "found": len(test_suites) > 0,
        "refreshed_files": refreshed_files,
        "commands": commands,
        "test_suites": test_suites,
    }


def format_tests_for_markdown(data: dict[str, Any]) -> str:
    refreshed = int(data.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"
    sym = data.get("symbol", "")

    lines: list[str] = [
        fresh_header,
        f"## Tests for Symbol (`tests_for`): `{sym}`",
    ]

    suites = data.get("test_suites", [])
    if not suites:
        lines.append(f"\n> No existing tests found that reference or call `{sym}`.")
        return "\n".join(lines) + "\n"

    lines.append("\n### Runnable Test Commands")
    lines.append("```bash")
    for cmd in data.get("commands", []):
        lines.append(cmd)
    lines.append("```")

    lines.append(f"\n### Matched Test Suites ({len(suites)} files)")
    for s in suites:
        lines.append(f"- **`{s['file']}`** -> `{s['command']}`")
        for t in s["tests"]:
            lines.append(f"  - `{t['test_symbol']}` (`{t['location']}`) — {t['reason']}")

    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# 3. Git Diff Impact Analyzer (`get_diff_impact`)
# -------------------------------------------------------------------------

def _collect_git_diffs(project_root: Path, base_ref: str = "HEAD") -> list[tuple[str, list[tuple[int, int]]]]:
    """
    Run `git diff --unified=0` in `project_root` (or immediate git subdirectories)
    and return `[(rel_file_path, [(start_line, end_line), ...]), ...]`.
    """
    git_roots: list[tuple[Path, str]] = []
    if (project_root / ".git").exists():
        git_roots.append((project_root, ""))
    else:
        try:
            for child in project_root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    git_roots.append((child, f"{child.name}/"))
        except OSError:
            pass
        if not git_roots:
            git_roots.append((project_root, ""))

    changed_files: dict[str, list[tuple[int, int]]] = {}
    for g_root, prefix in git_roots:
        cmd = ["git", "diff", "--unified=0"]
        if base_ref and base_ref not in ("working", "unstaged"):
            cmd.append(base_ref)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(g_root),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            diff_out = proc.stdout or ""
        except Exception:
            diff_out = ""

        curr_file: str | None = None
        for raw in diff_out.splitlines():
            if raw.startswith("+++ b/"):
                sub_rel = raw[len("+++ b/") :].strip()
                curr_file = f"{prefix}{sub_rel}"
                changed_files.setdefault(curr_file, [])
            elif raw.startswith("@@ ") and curr_file:
                # Parse `@@ -old_s,old_c +new_s,new_c @@`
                m_hunk = re.search(r"\+(\d+)(?:,(\d+))?", raw)
                if m_hunk:
                    start_l = int(m_hunk.group(1))
                    cnt = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
                    end_l = max(start_l, start_l + max(1, cnt) - 1)
                    changed_files[curr_file].append((start_l, end_l))

    return list(changed_files.items())


def build_diff_impact(
    db: CodeGraphDB,
    base_ref: str = "HEAD",
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Analyze `git diff` hunks and compute the blast radius of uncommitted / branch changes:
      1. Modified AST symbols (`nodes` overlapping diff line ranges)
      2. Direct upstream callers of modified symbols (`exact`/`scoped`)
      3. Cross-language contracts affected (`opcode::0x...`, `config::...`, `i18n::...`)
      4. Affected tests and exact commands to run
      5. Coverage warnings (`.tscn` / dynamic string references)
    """
    file_hunks = _collect_git_diffs(db.project_root, base_ref=base_ref)

    modified_symbols: list[dict[str, Any]] = []
    seen_mod_ids: set[str] = set()
    affected_callers: list[dict[str, Any]] = []
    seen_callers: set[tuple[str, str]] = set()
    affected_contracts: list[dict[str, Any]] = []
    seen_contracts: set[tuple[str, str]] = set()
    runnable_commands: list[str] = []
    matched_tests: list[dict[str, Any]] = []
    coverage_alerts: list[str] = []

    for rel_fp, ranges in file_hunks:
        node_rows = db.conn.execute(
            "SELECT * FROM nodes WHERE file_path = ? AND kind != 'file' ORDER BY start_line ASC",
            (rel_fp,),
        ).fetchall()
        for r in node_rows:
            n = db._row_to_node(r)
            overlaps = False
            if not ranges:
                overlaps = True
            else:
                for hs, he in ranges:
                    if n.start_line <= he and n.end_line >= hs:
                        overlaps = True
                        break
            if overlaps and n.id not in seen_mod_ids:
                seen_mod_ids.add(n.id)
                modified_symbols.append(n.to_dict())

                # Impact for this modified symbol
                imp = db.get_symbol_impact(f"{n.file_path}::{n.name}", depth=1, max_lines=200, max_tokens=800)
                for u in imp.get("upstream", []):
                    ckey = (u["from_id"], n.id)
                    if ckey not in seen_callers and u["from_id"] not in seen_mod_ids:
                        seen_callers.add(ckey)
                        affected_callers.append({
                            "caller": u["from"],
                            "caller_file": u["from_file"],
                            "confidence": u["confidence"],
                            "edge_type": u["edge_type"],
                            "modified_target": n.qualified_name,
                            "call_site": u["call_site"],
                        })

                for cc in imp.get("cross_language_contracts", []):
                    ct_key = (cc["contract"], cc["symbol"])
                    if ct_key not in seen_contracts:
                        seen_contracts.add(ct_key)
                        affected_contracts.append(cc)

                # Tests for this modified symbol
                t_info = build_tests_for_symbol(db, n.name)
                for cmd in t_info.get("commands", []):
                    if cmd not in runnable_commands:
                        runnable_commands.append(cmd)
                for suite in t_info.get("test_suites", []):
                    if suite["file"] not in {m["file"] for m in matched_tests}:
                        matched_tests.append(suite)

                # Scene / dynamic string warnings
                cov = imp.get("coverage", {})
                for sref in cov.get("scene_and_resource_refs", [])[:3]:
                    coverage_alerts.append(f"[scene] `{sref['location']}` references `{n.name}` — `{sref['snippet']}`")
                for dref in cov.get("dynamic_string_refs", [])[:3]:
                    coverage_alerts.append(f"[dynamic] `{dref['location']}` references `{n.name}` — `{dref['snippet']}`")

    return {
        "base_ref": base_ref,
        "refreshed_files": refreshed_files,
        "changed_files": [fp for fp, _ in file_hunks],
        "modified_symbols": modified_symbols,
        "affected_callers": affected_callers,
        "affected_contracts": affected_contracts,
        "runnable_test_commands": runnable_commands,
        "matched_tests": matched_tests,
        "coverage_alerts": list(dict.fromkeys(coverage_alerts)),
    }


def format_diff_impact_markdown(data: dict[str, Any]) -> str:
    refreshed = int(data.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"
    changed_files = data.get("changed_files", [])

    lines: list[str] = [
        fresh_header,
        f"## Git Diff Impact Report (`base_ref={data.get('base_ref', 'HEAD')}`)",
        f"- **Changed Files ({len(changed_files)}):** " + (", ".join(f"`{f}`" for f in changed_files[:12]) if changed_files else "None"),
    ]

    mods = data.get("modified_symbols", [])
    lines.append(f"\n### 1. Modified AST Symbols ({len(mods)})")
    if mods:
        for m in mods[:25]:
            lines.append(f"- **`{m['qualified_name']}`** ({m['kind']}) @ `{m['file']}:{m['lines']}` — `{m['signature']}`")
    else:
        lines.append("- *(No modified AST symbols detected in git diff)*")

    callers = data.get("affected_callers", [])
    lines.append(f"\n### 2. Direct Upstream Callers Affected ({len(callers)})")
    if callers:
        for c in callers[:25]:
            lines.append(
                f"- **`{c['caller']}`** [{c['confidence']}, {c['edge_type']}] calls modified `{c['modified_target']}` @ `{c['call_site']}` (`{c['caller_file']}`)"
            )
    else:
        lines.append("- None")

    contracts = data.get("affected_contracts", [])
    lines.append(f"\n### 3. Cross-Language Contracts Affected ({len(contracts)})")
    if contracts:
        for cc in contracts[:20]:
            callers_str = f" — callers: {', '.join(cc['callers'])}" if cc.get("callers") else ""
            lines.append(
                f"- **`{cc['contract']}`** -> `{cc['symbol']}` ({cc['language']}, `{cc['location']}`): `{cc['signature']}`{callers_str}"
            )
    else:
        lines.append("- None")

    cmds = data.get("runnable_test_commands", [])
    lines.append(f"\n### 4. Affected Tests & Runnable Commands ({len(cmds)})")
    if cmds:
        lines.append("```bash")
        for cmd in cmds:
            lines.append(cmd)
        lines.append("```")
    else:
        lines.append("- *(No automated tests directly linked to modified symbols)*")

    alerts = data.get("coverage_alerts", [])
    lines.append(f"\n### 5. Scene / Dynamic Coverage Alerts ({len(alerts)})")
    if alerts:
        for a in alerts[:15]:
            lines.append(f"- {a}")
    else:
        lines.append("- None")

    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# 4. Token Savings Telemetry Formatting (`get_token_savings`)
# -------------------------------------------------------------------------

def append_telemetry_line(
    db: CodeGraphDB,
    tool_name: str,
    target: str,
    markdown_text: str,
    candidate_files: set[str] | list[str],
) -> str:
    """Record telemetry in SQLite and append a 1-line token-savings badge to the output."""
    t = db.record_telemetry(tool_name, target, markdown_text, candidate_files)
    badge = (
        f"\n---\n"
        f"`telemetry: ~{t['returned_tokens']:,} tokens returned vs ~{t['raw_files_tokens']:,} tokens raw files "
        f"({t['files_touched']} files) — saved ~{t['saved_tokens']:,} tokens ({t['savings_percent']}%)`\n"
    )
    return markdown_text.rstrip() + badge


def format_token_savings_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = [
        "## Code Graph Direct Token Savings Telemetry",
        f"- **Total Recorded Queries:** {summary.get('total_calls', 0)}",
        f"- **Total Tokens Returned by `code-graph`:** ~{summary.get('total_returned_tokens', 0):,} tokens",
        f"- **Total Raw File Tokens Avoided:** ~{summary.get('total_raw_files_tokens', 0):,} tokens",
        f"- **Direct Net Tokens Saved:** **~{summary.get('total_saved_tokens', 0):,} tokens ({summary.get('overall_savings_percent', 0.0)}%)**",
    ]
    by_tool = summary.get("by_tool", [])
    if by_tool:
        lines.append("\n### Breakdown by Tool")
        for bt in by_tool:
            lines.append(
                f"- **`{bt['tool']}`** ({bt['calls']} calls): returned ~{bt['returned_tokens']:,} vs raw ~{bt['raw_files_tokens']:,} "
                f"-> **saved ~{bt['saved_tokens']:,} tokens ({bt['savings_percent']}%)**"
            )
    return "\n".join(lines) + "\n"
