"""SQLite embedded graph database (.code_graph.sqlite) and scoped BFS query engine."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from .models import GraphNode, ParseResult
from .parser import LANGUAGE_FAMILY

DB_FILENAME = ".code_graph.sqlite"
LARGE_FUNCTION_THRESHOLD = 80
CALL_WINDOW_RADIUS = 5
SMALL_FILE_THRESHOLD = 200


class CodeGraphDB:
    """Manages the local SQLite database for the Code Knowledge Graph."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = self.project_root / DB_FILENAME
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA temp_store=MEMORY;")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS files (
                    file_path TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL,
                    mtime REAL NOT NULL DEFAULT 0,
                    language TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    language TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    receiver TEXT NOT NULL DEFAULT '',
                    target_id TEXT,
                    edge_type TEXT NOT NULL,
                    confidence TEXT NOT NULL DEFAULT 'exact',
                    file_path TEXT NOT NULL,
                    line INTEGER NOT NULL
                );
                """
            )
            # Migrate existing tables if older schema lacks new columns
            f_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)").fetchall()}
            if "mtime" not in f_cols:
                self.conn.execute("ALTER TABLE files ADD COLUMN mtime REAL NOT NULL DEFAULT 0;")

            e_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(edges)").fetchall()}
            if "receiver" not in e_cols:
                self.conn.execute("ALTER TABLE edges ADD COLUMN receiver TEXT NOT NULL DEFAULT '';")
            if "confidence" not in e_cols:
                self.conn.execute("ALTER TABLE edges ADD COLUMN confidence TEXT NOT NULL DEFAULT 'exact';")

            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
                CREATE INDEX IF NOT EXISTS idx_nodes_qual ON nodes(qualified_name);
                CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
                CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target_id ON edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target_name ON edges(target_name);
                CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
                CREATE INDEX IF NOT EXISTS idx_edges_conf ON edges(confidence);
                """
            )

    # -------------------------------------------------------------------------
    # Metadata & Incremental File Tracking
    # -------------------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        raw = json.dumps(value) if not isinstance(value, str) else value
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, raw),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def get_file_records(self) -> dict[str, tuple[str, float]]:
        """Return mapping of `{file_path: (file_hash, mtime)}` currently stored in DB."""
        rows = self.conn.execute("SELECT file_path, file_hash, mtime FROM files").fetchall()
        return {r["file_path"]: (r["file_hash"], float(r["mtime"])) for r in rows}

    def get_file_hashes(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT file_path, file_hash FROM files").fetchall()
        return {r["file_path"]: r["file_hash"] for r in rows}

    def remove_files(self, file_paths: list[str], vacuum_threshold: int = 10) -> None:
        """Delete records for removed files and VACUUM if many files were purged."""
        if not file_paths:
            return
        with self.conn:
            # Batch in chunks of 500 to avoid SQLite parameter limits
            for i in range(0, len(file_paths), 500):
                chunk = [(fp,) for fp in file_paths[i : i + 500]]
                self.conn.executemany("DELETE FROM files WHERE file_path = ?", chunk)
                self.conn.executemany("DELETE FROM nodes WHERE file_path = ? AND kind != 'event'", chunk)
                self.conn.executemany("DELETE FROM edges WHERE file_path = ?", chunk)

        if len(file_paths) >= vacuum_threshold:
            self.vacuum()

    def vacuum(self) -> None:
        """Reclaim unused SQLite disk pages and truncate WAL."""
        try:
            self.conn.execute("VACUUM;")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.OperationalError:
            pass

    def upsert_parse_results(self, results: list[ParseResult]) -> None:
        """Batch update parsed files in a single transaction."""
        if not results:
            return
        now = time.time()
        with self.conn:
            for res in results:
                self.conn.execute(
                    "DELETE FROM nodes WHERE file_path = ? AND kind != 'event'",
                    (res.file_path,),
                )
                self.conn.execute(
                    "DELETE FROM edges WHERE file_path = ?",
                    (res.file_path,),
                )
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO files (file_path, file_hash, mtime, language, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (res.file_path, res.file_hash, res.mtime, res.language, now),
                )
                self.conn.executemany(
                    """
                    INSERT OR REPLACE INTO nodes
                    (id, name, qualified_name, kind, file_path, start_line, end_line, signature, language)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            n.id,
                            n.name,
                            n.qualified_name,
                            n.kind,
                            n.file_path,
                            n.start_line,
                            n.end_line,
                            n.signature,
                            n.language,
                        )
                        for n in res.nodes
                    ],
                )
                self.conn.executemany(
                    """
                    INSERT INTO edges
                    (source_id, target_name, receiver, target_id, edge_type, confidence, file_path, line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            e.source_id,
                            e.target_name,
                            e.receiver,
                            e.target_id,
                            e.edge_type,
                            e.confidence,
                            e.file_path,
                            e.line,
                        )
                        for e in res.edges
                    ],
                )

    # -------------------------------------------------------------------------
    # Scoped, Language-Safe Edge Resolution
    # -------------------------------------------------------------------------

    def resolve_edges_and_cleanup(
        self,
        autoload_map: dict[str, str] | None = None,
        tsconfig_aliases: dict[str, str] | None = None,
        go_modules: dict[str, str] | None = None,
    ) -> None:
        """
        Resolve symbolic edges using strict language isolation and scope hierarchy:
          1. Same file / class (`exact`)
          2. Ecosystem config resolution (`project.godot` Autoloads, `tsconfig.json` paths, `go.mod` packages, Python relative imports) (`exact`)
          3. Inheritance (`extends`) chain or same Go package directory (`exact` / `scoped`)
          4. Imported / preloaded modules (`scoped`)
          5. Unique symbol in the same language family (`scoped` if unambiguous, else `name_only`)
        NEVER links calls across different language families (e.g. GDScript <-> JS or Go).
        """
        if autoload_map is None:
            autoload_map = self.get_meta("autoloads", {}) or {}
        else:
            self.set_meta("autoloads", autoload_map)

        if tsconfig_aliases is None:
            tsconfig_aliases = self.get_meta("tsconfig_aliases", {}) or {}
        else:
            self.set_meta("tsconfig_aliases", tsconfig_aliases)

        if go_modules is None:
            go_modules = self.get_meta("go_modules", {}) or {}
        else:
            self.set_meta("go_modules", go_modules)

        with self.conn:
            # Reset non-event target_ids before re-resolving so stale links don't persist
            self.conn.execute(
                "UPDATE edges SET target_id = NULL WHERE edge_type IN ('calls', 'imports', 'inherits')"
            )

            all_nodes = self.conn.execute(
                "SELECT id, name, qualified_name, kind, file_path, language FROM nodes"
            ).fetchall()

            file_lang_family: dict[str, str] = {}
            by_file_and_name: dict[tuple[str, str], list[sqlite3.Row]] = {}
            by_lang_and_name: dict[tuple[str, str], list[sqlite3.Row]] = {}
            by_lang_and_qual: dict[tuple[str, str], list[sqlite3.Row]] = {}
            class_to_file: dict[tuple[str, str], str] = {}
            file_nodes_by_stem: dict[tuple[str, str], list[sqlite3.Row]] = {}

            for n in all_nodes:
                fam = LANGUAGE_FAMILY.get(n["language"], n["language"])
                fp = n["file_path"]
                if n["kind"] == "file":
                    file_lang_family[fp] = fam
                    file_nodes_by_stem.setdefault((fam, n["name"]), []).append(n)
                    continue
                if n["kind"] == "event":
                    continue

                file_lang_family.setdefault(fp, fam)
                by_file_and_name.setdefault((fp, n["name"]), []).append(n)
                by_lang_and_name.setdefault((fam, n["name"]), []).append(n)
                by_lang_and_qual.setdefault((fam, n["qualified_name"]), []).append(n)
                if n["kind"] == "class":
                    class_to_file[(fam, n["name"])] = fp

            # 1. Resolve `ref_file::<script_path>::<method>` (from .tscn connections)
            ref_file_rows = self.conn.execute(
                "SELECT id, source_id FROM edges WHERE source_id LIKE 'ref_file::%'"
            ).fetchall()
            for r in ref_file_rows:
                parts = r["source_id"].split("::", 2)
                if len(parts) == 3:
                    _, target_fp, mname = parts
                    cands = by_file_and_name.get((target_fp, mname), [])
                    if cands:
                        self.conn.execute(
                            "UPDATE edges SET source_id = ?, confidence = 'exact' WHERE id = ?",
                            (cands[0]["id"], r["id"]),
                        )
                    else:
                        self.conn.execute("DELETE FROM edges WHERE id = ?", (r["id"],))

            # 2. Resolve `ref::<handler_name>` (from signal.connect / bus.on callbacks)
            ref_rows = self.conn.execute(
                "SELECT id, source_id, file_path FROM edges WHERE source_id LIKE 'ref::%'"
            ).fetchall()
            for r in ref_rows:
                short_name = r["source_id"][5:]
                fp = r["file_path"]
                cands = by_file_and_name.get((fp, short_name), [])
                if cands:
                    self.conn.execute(
                        "UPDATE edges SET source_id = ?, confidence = 'exact' WHERE id = ?",
                        (cands[0]["id"], r["id"]),
                    )
                else:
                    self.conn.execute("DELETE FROM edges WHERE id = ?", (r["id"],))

            # 3. Resolve `inherits` and `imports` edges first to build inheritance & import scopes
            file_parents: dict[str, list[str]] = {}  # file_path -> [base_file_path, ...]
            file_imports: dict[str, set[str]] = {}  # file_path -> {imported_file_path, ...}

            def resolve_module_specifier(source_file: str, fam: str, spec: str) -> list[str]:
                """Resolve an import path / alias (`@/utils`, `./foo`, `pkg/sub`, `crate::x`) to project file paths."""
                if not spec:
                    return []
                if spec in file_lang_family:
                    return [spec]

                # TypeScript / JavaScript (`tsconfig.json` aliases and `./` relative imports)
                if fam == "js":
                    mapped = ""
                    for alias_prefix, target_prefix in tsconfig_aliases.items():
                        if spec.startswith(alias_prefix):
                            mapped = target_prefix + spec[len(alias_prefix) :]
                            break
                    if not mapped and spec.startswith("."):
                        src_dir = os.path.dirname(source_file)
                        mapped = os.path.normpath(os.path.join(src_dir, spec)).replace("\\", "/")
                    if mapped:
                        for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
                            cand = f"{mapped}{ext}"
                            if cand in file_lang_family:
                                return [cand]

                # Go (`go.mod` module prefix -> package directory files)
                if fam == "go":
                    for mod_name, mod_prefix in go_modules.items():
                        if spec == mod_name or spec.startswith(f"{mod_name}/"):
                            sub_pkg = spec[len(mod_name) :].lstrip("/")
                            pkg_dir = f"{mod_prefix}{sub_pkg}".rstrip("/")
                            matched_files = [
                                fp for fp, f_fam in file_lang_family.items()
                                if f_fam == "go" and os.path.dirname(fp) == pkg_dir and not fp.endswith("_test.go")
                            ]
                            if matched_files:
                                return matched_files

                # Python (dotted module path -> `.py` file)
                if fam == "python":
                    mod_rel = spec.strip(".").replace(".", "/")
                    if mod_rel:
                        for cand in (f"{mod_rel}.py", f"src/{mod_rel}.py", f"{mod_rel}/__init__.py", f"src/{mod_rel}/__init__.py"):
                            if cand in file_lang_family:
                                return [cand]

                # Rust (`crate::a::b` -> `src/a/b.rs` or `src/a.rs`)
                if fam == "rust" and spec.startswith("crate::"):
                    parts = [p for p in spec.split("::")[1:] if p]
                    for cut in range(len(parts), 0, -1):
                        sub = "/".join(parts[:cut])
                        for cand in (f"src/{sub}.rs", f"src/{sub}/mod.rs", f"{sub}.rs"):
                            if cand in file_lang_family:
                                return [cand]

                return []

            inh_imp_rows = self.conn.execute(
                "SELECT id, source_id, target_name, receiver, edge_type, file_path FROM edges WHERE edge_type IN ('inherits', 'imports')"
            ).fetchall()

            inh_imp_updates: list[tuple[str | None, str, int]] = []
            for e in inh_imp_rows:
                efile = e["file_path"]
                efam = file_lang_family.get(efile, "")
                tname = e["target_name"]
                recv = e["receiver"] or ""
                etype = e["edge_type"]

                resolved_node: sqlite3.Row | None = None
                conf = "scoped"

                # Try ecosystem module specifier resolution first (`res://`, `@/...`, `./...`, `go.mod`, `.py`)
                spec_files = resolve_module_specifier(efile, efam, recv)
                if spec_files:
                    for sfp in spec_files:
                        if etype == "inherits":
                            file_parents.setdefault(efile, []).append(sfp)
                        else:
                            file_imports.setdefault(efile, set()).add(sfp)
                        cands = by_file_and_name.get((sfp, tname), [])
                        if cands:
                            resolved_node = cands[0]
                            conf = "exact"
                            break
                    if resolved_node is None:
                        sfp0 = spec_files[0]
                        for n in all_nodes:
                            if n["file_path"] == sfp0 and n["kind"] in ("class", "file"):
                                resolved_node = n
                                conf = "exact"
                                break

                if resolved_node is None:
                    # Match class/function in the same language family
                    cands = [
                        c for c in by_lang_and_name.get((efam, tname), [])
                        if c["kind"] in ("class", "function") and c["file_path"] != efile
                    ]
                    if len(cands) == 1:
                        resolved_node = cands[0]
                        conf = "exact" if etype == "inherits" else "scoped"
                    elif not cands and etype == "imports":
                        f_cands = [
                            f for f in file_nodes_by_stem.get((efam, tname), [])
                            if f["file_path"] != efile
                        ]
                        if len(f_cands) == 1:
                            resolved_node = f_cands[0]
                            conf = "scoped"

                if resolved_node is not None:
                    target_fp = resolved_node["file_path"]
                    if etype == "inherits":
                        if target_fp not in file_parents.get(efile, []):
                            file_parents.setdefault(efile, []).append(target_fp)
                    else:
                        file_imports.setdefault(efile, set()).add(target_fp)
                    inh_imp_updates.append((resolved_node["id"], conf, e["id"]))
                else:
                    if recv and ("/" in recv or recv.endswith(".gd")):
                        if etype == "inherits":
                            file_parents.setdefault(efile, []).append(recv)
                        else:
                            file_imports.setdefault(efile, set()).add(recv)
                    inh_imp_updates.append((None, "name_only", e["id"]))

            if inh_imp_updates:
                self.conn.executemany(
                    "UPDATE edges SET target_id = ?, confidence = ? WHERE id = ?",
                    inh_imp_updates,
                )

            # Helper to search a file's inheritance chain (up to 5 levels)
            def find_in_inheritance(start_file: str, symbol_name: str) -> sqlite3.Row | None:
                visited: set[str] = {start_file}
                queue: deque[str] = deque(file_parents.get(start_file, []))
                while queue and len(visited) < 8:
                    curr_fp = queue.popleft()
                    if curr_fp in visited:
                        continue
                    visited.add(curr_fp)
                    cands = [
                        c for c in by_file_and_name.get((curr_fp, symbol_name), [])
                        if c["kind"] in ("method", "function")
                    ]
                    if cands:
                        return cands[0]
                    for p in file_parents.get(curr_fp, []):
                        if p not in visited:
                            queue.append(p)
                return None

            # If an autoload points to a .tscn file, map it to the .tscn's imported script if possible
            effective_autoloads: dict[str, str] = dict(autoload_map)
            for auto_name, auto_fp in list(effective_autoloads.items()):
                if auto_fp.endswith(".tscn"):
                    imps = file_imports.get(auto_fp, set())
                    gd_imps = [fp for fp in imps if fp.endswith(".gd")]
                    if gd_imps:
                        effective_autoloads[auto_name] = gd_imps[0]

            # 4. Resolve `calls` edges strictly within the same language family
            call_rows = self.conn.execute(
                "SELECT id, source_id, target_name, receiver, file_path FROM edges WHERE edge_type = 'calls'"
            ).fetchall()

            call_updates: list[tuple[str | None, str, int]] = []
            for e in call_rows:
                efile = e["file_path"]
                efam = file_lang_family.get(efile, "")
                tname = e["target_name"]
                recv = (e["receiver"] or "").strip()
                src_id = e["source_id"]

                matched_node: sqlite3.Row | None = None
                confidence = "name_only"

                # A) Is receiver a known Godot Autoload, explicit file path, or ecosystem module specifier (`go.mod` / `tsconfig.json`)?
                recv_root = recv.split(".")[0] if recv else ""
                explicit_target_files: list[str] = []
                if efam == "godot" and recv_root in effective_autoloads:
                    explicit_target_files = [effective_autoloads[recv_root]]
                elif recv:
                    spec_matches = resolve_module_specifier(efile, efam, recv)
                    if spec_matches:
                        explicit_target_files = spec_matches
                    elif "/" in recv or recv.endswith(".gd"):
                        explicit_target_files = [recv]

                if explicit_target_files:
                    for etf in explicit_target_files:
                        cands = [
                            c for c in by_file_and_name.get((etf, tname), [])
                            if c["kind"] in ("method", "function", "class")
                        ]
                        if cands:
                            matched_node = cands[0]
                            confidence = "exact"
                            break
                        inh_match = find_in_inheritance(etf, tname)
                        if inh_match:
                            matched_node = inh_match
                            confidence = "exact"
                            break
                    call_updates.append((matched_node["id"] if matched_node else None, confidence, e["id"]))
                    continue

                # B) Local / self / super / unqualified call (`recv in ("", "self", "this", "super")`)
                if recv in ("", "self", "this", "super"):
                    # B1. Same file (`exact`)
                    if recv != "super":
                        same_file_cands = [
                            c for c in by_file_and_name.get((efile, tname), [])
                            if c["kind"] in ("method", "function", "class") and c["id"] != src_id
                        ]
                        if same_file_cands:
                            matched_node = same_file_cands[0]
                            confidence = "exact"

                    # B2. Inheritance (`extends` / `super`) chain (`scoped` or `exact` for super)
                    if matched_node is None:
                        inh_match = find_in_inheritance(efile, tname)
                        if inh_match:
                            matched_node = inh_match
                            confidence = "exact" if recv == "super" else "scoped"

                    # B3. Explicitly imported/preloaded files (`scoped`)
                    if matched_node is None:
                        for imp_fp in file_imports.get(efile, set()):
                            imp_cands = [
                                c for c in by_file_and_name.get((imp_fp, tname), [])
                                if c["kind"] in ("function", "method", "class")
                            ]
                            if imp_cands:
                                matched_node = imp_cands[0]
                                confidence = "scoped"
                                break

                    # B4. Go same-package directory function call (`exact`)
                    if matched_node is None and efam == "go":
                        e_dir = os.path.dirname(efile)
                        pkg_cands = [
                            c for c in by_lang_and_name.get(("go", tname), [])
                            if os.path.dirname(c["file_path"]) == e_dir and c["kind"] in ("function", "class")
                        ]
                        if len(pkg_cands) == 1:
                            matched_node = pkg_cands[0]
                            confidence = "exact"

                    # B5. Unqualified call with a single unique definition in the same language family (`scoped`)
                    if matched_node is None and recv == "" and len(tname) >= 4:
                        lang_cands = [
                            c for c in by_lang_and_name.get((efam, tname), [])
                            if c["kind"] in ("function", "class") and c["id"] != src_id
                        ]
                        if len(lang_cands) == 1:
                            matched_node = lang_cands[0]
                            confidence = "scoped"

                    call_updates.append((matched_node["id"] if matched_node else None, confidence, e["id"]))
                    continue

                # C) Qualified call with a receiver (`recv` is non-empty, e.g. `Party.has`, `panel.refresh`, `s.handleLogin`)
                # C1. Check if `recv.tname` matches an exact `ClassName.method` in the same file or same Go package
                qual_cands = by_lang_and_qual.get((efam, f"{recv_root}.{tname}"), [])
                if qual_cands:
                    same_f = [c for c in qual_cands if c["file_path"] == efile]
                    if same_f:
                        matched_node = same_f[0]
                        confidence = "exact"
                    elif efam == "go":
                        e_dir = os.path.dirname(efile)
                        same_pkg = [c for c in qual_cands if os.path.dirname(c["file_path"]) == e_dir]
                        if same_pkg:
                            matched_node = same_pkg[0]
                            confidence = "exact"
                    elif len(qual_cands) == 1:
                        matched_node = qual_cands[0]
                        confidence = "scoped"

                # C2. Check if receiver name matches an imported file/class in `file_imports[efile]`
                if matched_node is None:
                    norm_recv = recv_root.lstrip("$%").lower().replace("_", "")
                    for imp_fp in file_imports.get(efile, set()):
                        imp_stem = Path(imp_fp).stem.lower().replace("_", "")
                        if norm_recv and (norm_recv == imp_stem or norm_recv.endswith(imp_stem)):
                            imp_cands = [
                                c for c in by_file_and_name.get((imp_fp, tname), [])
                                if c["kind"] in ("method", "function")
                            ]
                            if imp_cands:
                                matched_node = imp_cands[0]
                                confidence = "scoped"
                                break

                # C3. In Go: if receiver method is in the same package directory and unambiguous within that package
                if matched_node is None and efam == "go":
                    e_dir = os.path.dirname(efile)
                    pkg_methods = [
                        c for c in by_lang_and_name.get(("go", tname), [])
                        if os.path.dirname(c["file_path"]) == e_dir and c["kind"] in ("method", "function")
                    ]
                    if len(pkg_methods) == 1:
                        matched_node = pkg_methods[0]
                        confidence = "scoped"

                # C4. If `tname` is a specific method name (>= 6 chars, not generic) with ONLY 1 definition in the same language family
                if matched_node is None and len(tname) >= 6:
                    lang_methods = [
                        c for c in by_lang_and_name.get((efam, tname), [])
                        if c["kind"] in ("method", "function") and c["id"] != src_id
                    ]
                    if len(lang_methods) == 1:
                        matched_node = lang_methods[0]
                        confidence = "scoped"

                # Otherwise: leave `target_id = None` and `confidence = 'name_only'` so we NEVER link false cross-file/cross-language methods!
                call_updates.append((matched_node["id"] if matched_node else None, confidence, e["id"]))

            if call_updates:
                self.conn.executemany(
                    "UPDATE edges SET target_id = ?, confidence = ? WHERE id = ?",
                    call_updates,
                )

            # 5. Remove orphaned virtual event nodes
            self.conn.execute(
                """
                DELETE FROM nodes
                WHERE kind = 'event'
                  AND id NOT IN (SELECT DISTINCT target_id FROM edges WHERE target_id IS NOT NULL)
                """
            )
            self.set_meta("last_indexed_at", time.time())

    def get_counts(self) -> tuple[int, int, int]:
        """Return (file_count, node_count, edge_count)."""
        f_cnt = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_cnt = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        e_cnt = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return f_cnt, n_cnt, e_cnt

    # -------------------------------------------------------------------------
    # Symbol Lookup & Disambiguation (`file::symbol`, `Class.method`, `Autoload.method`)
    # -------------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            id=row["id"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            kind=row["kind"],
            file_path=row["file_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            signature=row["signature"],
            language=row["language"],
        )

    def find_symbol_nodes(self, symbol: str) -> list[GraphNode]:
        """
        Locate one or more nodes matching a symbol query.
        Supports:
          - `path/to/file.gd::symbol` or `file.gd::symbol`
          - `AutoloadName.method` (resolves via `project.godot` autoloads!)
          - `ClassName.method`
          - `symbol` or `event::SIGNAL_NAME`
        """
        q = symbol.strip()
        if not q:
            return []

        # 0. Explicit `file::symbol` syntax
        if "::" in q and not q.startswith("event::"):
            file_part, sym_part = q.split("::", 1)
            rows = self.conn.execute(
                """
                SELECT * FROM nodes
                WHERE (file_path = ? OR file_path LIKE ?)
                  AND (qualified_name = ? OR name = ?)
                  AND kind != 'file'
                ORDER BY CASE WHEN file_path = ? THEN 0 ELSE 1 END
                """,
                (file_part, f"%/{file_part}", sym_part, sym_part, file_part),
            ).fetchall()
            if rows:
                return [self._row_to_node(r) for r in rows]

        # 0b. Check if `Prefix.method` matches a Godot Autoload singleton (e.g. `Game._on_packet`)
        if "." in q and "::" not in q:
            prefix, mname = q.split(".", 1)
            autoloads = self.get_meta("autoloads", {}) or {}
            if prefix in autoloads:
                auto_fp = autoloads[prefix]
                rows = self.conn.execute(
                    """
                    SELECT * FROM nodes
                    WHERE file_path = ? AND (name = ? OR qualified_name = ?) AND kind != 'file'
                    """,
                    (auto_fp, mname, q),
                ).fetchall()
                if rows:
                    return [self._row_to_node(r) for r in rows]

        # 1. Exact match on id, qualified_name, or name
        rows = self.conn.execute(
            """
            SELECT * FROM nodes
            WHERE (id = ? OR qualified_name = ? OR name = ? OR id = ?)
              AND kind != 'file'
            ORDER BY CASE
                WHEN id = ? THEN 0
                WHEN qualified_name = ? THEN 1
                WHEN name = ? THEN 2
                ELSE 3
            END
            """,
            (q, q, q, f"event::{q}", q, q, q),
        ).fetchall()

        if rows:
            return [self._row_to_node(r) for r in rows]

        # 2. Case-insensitive match
        rows = self.conn.execute(
            """
            SELECT * FROM nodes
            WHERE (LOWER(qualified_name) = LOWER(?) OR LOWER(name) = LOWER(?))
              AND kind != 'file'
            """,
            (q, q),
        ).fetchall()
        if rows:
            return [self._row_to_node(r) for r in rows]

        # 3. Substring / partial match
        like_q = f"%{q}%"
        rows = self.conn.execute(
            """
            SELECT * FROM nodes
            WHERE (qualified_name LIKE ? OR name LIKE ?) AND kind != 'file'
            ORDER BY LENGTH(name) ASC
            LIMIT 10
            """,
            (like_q, like_q),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def search_symbols(self, query: str = "", kind: str | None = None, limit: int = 30) -> list[GraphNode]:
        """Search symbols in the graph by name/substring and optional kind filter."""
        sql = "SELECT * FROM nodes WHERE kind != 'file'"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if query:
            sql += " AND (name LIKE ? OR qualified_name LIKE ? OR file_path LIKE ?)"
            like_q = f"%{query}%"
            params.extend([like_q, like_q, like_q])
        sql += " ORDER BY kind, file_path, start_line LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    # -------------------------------------------------------------------------
    # Impact Subgraph Query (Scoped, Confidence-Aware, Windowed, Budgeted)
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_call_windows(
        func_start: int,
        func_end: int,
        call_lines: list[int],
        radius: int = CALL_WINDOW_RADIUS,
    ) -> list[tuple[int, int]]:
        """Compute merged ±radius line windows around call sites inside a large function."""
        if not call_lines:
            return [(func_start, min(func_end, func_start + 30))]
        intervals: list[tuple[int, int]] = []
        for ln in sorted(set(call_lines)):
            w_start = max(func_start, ln - radius)
            w_end = min(func_end, ln + radius)
            if intervals and w_start <= intervals[-1][1] + 2:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], w_end))
            else:
                intervals.append((w_start, w_end))
        return intervals

    def get_symbol_impact(
        self,
        symbol: str,
        depth: int = 2,
        max_lines: int = 300,
        max_tokens: int = 800,
        include_uncertain: bool = False,
        refreshed_files: int = 0,
    ) -> dict[str, Any]:
        """
        Extract the high-confidence subgraph around `symbol` up to `depth` hops.
        - Excludes `name_only` edges by default (summarizes them as uncertain counts).
        - Windows large caller/callee functions (>80 lines) to ±5 lines around the call site.
        - Enforces `max_lines` / `max_tokens` budgets and relevance ordering.
        """
        from .context_builder import scan_coverage_warnings

        depth = max(1, min(depth, 5))
        roots = self.find_symbol_nodes(symbol)
        if not roots:
            cov = scan_coverage_warnings(self.project_root, symbol, [], set())
            return {
                "symbol": symbol,
                "found": False,
                "refreshed_files": refreshed_files,
                "message": f"Symbol '{symbol}' not found in .code_graph.sqlite.",
                "definitions": [],
                "upstream": [],
                "downstream": [],
                "events": [],
                "files_to_read": [],
                "coverage": cov,
            }

        # Disambiguation check: if >1 distinct files define this short name and user didn't specify file/class
        disambiguation_candidates: list[dict[str, Any]] = []
        if len(roots) > 1:
            for r in roots:
                disambiguation_candidates.append({
                    "selector": f"{r.file_path}::{r.name}",
                    "qualified_name": r.qualified_name,
                    "kind": r.kind,
                    "location": f"{r.file_path}:{r.start_line}-{r.end_line}",
                    "language": r.language,
                    "signature": r.signature,
                })
            # If highly ambiguous (>3 unrelated definitions across files), return disambiguation list + top match only
            if len(roots) > 3 and "::" not in symbol and "." not in symbol:
                roots = [roots[0]]

        # Check small file hint (<200 lines) for the primary target definition
        small_file_hints: list[str] = []
        for r in roots:
            if r.kind != "event":
                frow = self.conn.execute(
                    "SELECT end_line FROM nodes WHERE file_path = ? AND kind = 'file'",
                    (r.file_path,),
                ).fetchone()
                if frow and int(frow["end_line"]) < SMALL_FILE_THRESHOLD:
                    small_file_hints.append(
                        f"`{r.file_path}` is small ({frow['end_line']} lines total) — reading the entire file directly is cheaper than multiple partial reads."
                    )

        # Expand class root to include its methods ONLY if the user specifically queried a class (not a file-level script)
        seed_nodes: dict[str, GraphNode] = {n.id: n for n in roots}
        for r in list(roots):
            if r.kind == "class" and not r.signature.startswith("script "):
                prefix = f"{r.file_path}::{r.qualified_name}."
                method_rows = self.conn.execute(
                    "SELECT * FROM nodes WHERE id LIKE ?",
                    (f"{prefix}%",),
                ).fetchall()
                for mr in method_rows:
                    mn = self._row_to_node(mr)
                    seed_nodes[mn.id] = mn

        allowed_confidences = ("exact", "scoped", "name_only") if include_uncertain else ("exact", "scoped")

        nodes_by_id: dict[str, GraphNode] = dict(seed_nodes)
        node_min_depth: dict[str, int] = {nid: 0 for nid in seed_nodes}
        node_best_conf: dict[str, str] = {nid: "exact" for nid in seed_nodes}
        # Track call lines inside each node so large functions (>80 lines) can be windowed to ±5 lines!
        node_focus_lines: dict[str, list[int]] = {}

        upstream_relations: list[dict[str, Any]] = []
        downstream_relations: list[dict[str, Any]] = []
        seen_relations: set[tuple[str, str, str]] = set()
        uncertain_calls_counter: Counter[str] = Counter()

        # --- 1. UPSTREAM BFS (Who calls/triggers/imports the target?) ---
        visited_up: set[str] = set(seed_nodes.keys())
        queue_up: deque[tuple[str, int]] = deque((nid, 1) for nid in seed_nodes)

        while queue_up:
            curr_id, curr_depth = queue_up.popleft()
            if curr_depth > depth:
                continue

            curr_node = nodes_by_id.get(curr_id)

            # Direct incoming resolved edges ONLY (never match unresolved `target_id IS NULL` across the repo!)
            in_edges = self.conn.execute(
                "SELECT * FROM edges WHERE target_id = ?",
                (curr_id,),
            ).fetchall()

            for e in in_edges:
                src_id = e["source_id"]
                etype = e["edge_type"]
                conf = e["confidence"] or "exact"
                line = int(e["line"])

                if conf not in allowed_confidences:
                    uncertain_calls_counter[e["target_name"]] += 1
                    continue

                if curr_node and curr_node.kind == "event" and etype == "listens_to":
                    continue

                src_row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (src_id,)).fetchone()
                if not src_row:
                    continue
                src_node = self._row_to_node(src_row)
                nodes_by_id[src_node.id] = src_node
                node_min_depth[src_node.id] = min(node_min_depth.get(src_node.id, 99), curr_depth)
                if conf == "exact" or src_node.id not in node_best_conf:
                    node_best_conf[src_node.id] = conf
                node_focus_lines.setdefault(src_node.id, []).append(line)

                rel_key = (src_node.id, curr_id, etype)
                if rel_key not in seen_relations and src_node.id not in seed_nodes:
                    seen_relations.add(rel_key)
                    upstream_relations.append({
                        "depth": curr_depth,
                        "confidence": conf,
                        "from": src_node.qualified_name,
                        "from_id": src_node.id,
                        "from_kind": src_node.kind,
                        "from_file": f"{src_node.file_path}:{src_node.start_line}-{src_node.end_line}",
                        "from_lines": src_node.line_count,
                        "from_signature": src_node.signature,
                        "edge_type": etype,
                        "to": curr_node.qualified_name if curr_node else curr_id,
                        "call_site": f"{e['file_path']}:{line}",
                        "call_line": line,
                    })

                if src_node.id not in visited_up and curr_depth < depth:
                    visited_up.add(src_node.id)
                    queue_up.append((src_node.id, curr_depth + 1))

            # If curr_node listens_to an event, the event and its publishers are upstream triggers!
            listened_events = self.conn.execute(
                """
                SELECT * FROM edges
                WHERE source_id = ? AND edge_type = 'listens_to' AND target_id IS NOT NULL
                """,
                (curr_id,),
            ).fetchall()
            for le in listened_events:
                evt_id = le["target_id"]
                line = int(le["line"])
                evt_row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (evt_id,)).fetchone()
                if evt_row:
                    evt_node = self._row_to_node(evt_row)
                    nodes_by_id[evt_node.id] = evt_node
                    node_min_depth[evt_node.id] = min(node_min_depth.get(evt_node.id, 99), curr_depth)
                    rel_key = (evt_node.id, curr_id, "triggers_listener")
                    if rel_key not in seen_relations:
                        seen_relations.add(rel_key)
                        upstream_relations.append({
                            "depth": curr_depth,
                            "confidence": "exact",
                            "from": evt_node.qualified_name,
                            "from_id": evt_node.id,
                            "from_kind": "event",
                            "from_file": f"{le['file_path']}:{line}",
                            "from_lines": 1,
                            "from_signature": evt_node.signature,
                            "edge_type": "listens_to",
                            "to": curr_node.qualified_name if curr_node else curr_id,
                            "call_site": f"{le['file_path']}:{line}",
                            "call_line": line,
                        })
                    if evt_node.id not in visited_up and curr_depth < depth:
                        visited_up.add(evt_node.id)
                        queue_up.append((evt_node.id, curr_depth + 1))

        # --- 2. DOWNSTREAM BFS (What does the target call/publish/trigger?) ---
        visited_down: set[str] = set(seed_nodes.keys())
        queue_down: deque[tuple[str, int]] = deque((nid, 1) for nid in seed_nodes)

        while queue_down:
            curr_id, curr_depth = queue_down.popleft()
            if curr_depth > depth:
                continue

            curr_node = nodes_by_id.get(curr_id)
            out_edges = self.conn.execute(
                "SELECT * FROM edges WHERE source_id = ?",
                (curr_id,),
            ).fetchall()

            for e in out_edges:
                tgt_id = e["target_id"]
                tname = e["target_name"]
                etype = e["edge_type"]
                conf = e["confidence"] or "exact"
                line = int(e["line"])

                if etype in ("listens_to", "imports"):
                    continue

                if conf not in allowed_confidences or tgt_id is None:
                    if conf == "name_only" or tgt_id is None:
                        uncertain_calls_counter[tname] += 1
                    continue

                tgt_row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (tgt_id,)).fetchone()
                if not tgt_row:
                    continue
                tgt_node = self._row_to_node(tgt_row)
                nodes_by_id[tgt_node.id] = tgt_node
                node_min_depth[tgt_node.id] = min(node_min_depth.get(tgt_node.id, 99), curr_depth)
                if conf == "exact" or tgt_node.id not in node_best_conf:
                    node_best_conf[tgt_node.id] = conf
                node_focus_lines.setdefault(curr_id, []).append(line)

                rel_key = (curr_id, tgt_node.id, etype)
                if rel_key not in seen_relations and tgt_node.id not in seed_nodes:
                    seen_relations.add(rel_key)
                    downstream_relations.append({
                        "depth": curr_depth,
                        "confidence": conf,
                        "from": curr_node.qualified_name if curr_node else curr_id,
                        "edge_type": etype,
                        "to": tgt_node.qualified_name,
                        "to_id": tgt_node.id,
                        "to_kind": tgt_node.kind,
                        "to_file": f"{tgt_node.file_path}:{tgt_node.start_line}-{tgt_node.end_line}",
                        "to_lines": tgt_node.line_count,
                        "to_signature": tgt_node.signature,
                        "call_site": f"{e['file_path']}:{line}",
                        "call_line": line,
                    })

                if tgt_node.id not in visited_down and curr_depth < depth:
                    visited_down.add(tgt_node.id)
                    queue_down.append((tgt_node.id, curr_depth + 1))

            # If curr_node is an event node, find all subscribers (`listens_to` edges pointing to curr_id)
            if curr_node and curr_node.kind == "event":
                sub_edges = self.conn.execute(
                    "SELECT * FROM edges WHERE target_id = ? AND edge_type = 'listens_to'",
                    (curr_id,),
                ).fetchall()
                for se in sub_edges:
                    listener_id = se["source_id"]
                    line = int(se["line"])
                    l_row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (listener_id,)).fetchone()
                    if not l_row:
                        continue
                    l_node = self._row_to_node(l_row)
                    nodes_by_id[l_node.id] = l_node
                    node_min_depth[l_node.id] = min(node_min_depth.get(l_node.id, 99), curr_depth)
                    node_best_conf[l_node.id] = "exact"
                    node_focus_lines.setdefault(l_node.id, []).append(line)

                    rel_key = (curr_id, l_node.id, "listens_to")
                    if rel_key not in seen_relations and l_node.id not in seed_nodes:
                        seen_relations.add(rel_key)
                        downstream_relations.append({
                            "depth": curr_depth,
                            "confidence": "exact",
                            "from": curr_node.qualified_name,
                            "edge_type": "listens_to",
                            "to": l_node.qualified_name,
                            "to_id": l_node.id,
                            "to_kind": l_node.kind,
                            "to_file": f"{l_node.file_path}:{l_node.start_line}-{l_node.end_line}",
                            "to_lines": l_node.line_count,
                            "to_signature": l_node.signature,
                            "call_site": f"{se['file_path']}:{line}",
                            "call_line": line,
                        })

                    if l_node.id not in visited_down and curr_depth < depth:
                        visited_down.add(l_node.id)
                        queue_down.append((l_node.id, curr_depth + 1))

        # Sort upstream & downstream by (depth, confidence=='scoped', call_site)
        upstream_relations.sort(key=lambda r: (r["depth"], 0 if r["confidence"] == "exact" else 1, r["call_site"]))
        downstream_relations.sort(key=lambda r: (r["depth"], 0 if r["confidence"] == "exact" else 1, r["call_site"]))

        # Build sorted, windowed, budget-constrained `files_to_read`
        sorted_nodes = sorted(
            [n for n in nodes_by_id.values() if n.kind not in ("file", "event")],
            key=lambda n: (
                node_min_depth.get(n.id, 99),
                0 if node_best_conf.get(n.id) == "exact" else 1,
                n.line_count,
                n.file_path,
                n.start_line,
            ),
        )

        files_to_read: list[dict[str, Any]] = []
        omitted_locations = 0
        cumulative_lines = 0
        seen_ranges: set[tuple[str, int, int]] = set()

        for n in sorted_nodes:
            d = node_min_depth.get(n.id, 1)
            focus_lns = node_focus_lines.get(n.id, [])

            # If caller/callee function is large (>80 lines) and we have call-site lines inside it,
            # give ±5 line windows instead of a 250-line function range!
            if d >= 1 and n.line_count > LARGE_FUNCTION_THRESHOLD and focus_lns:
                windows = self._compute_call_windows(n.start_line, n.end_line, focus_lns)
                for w_start, w_end in windows:
                    w_lines = max(1, w_end - w_start + 1)
                    key = (n.file_path, w_start, w_end)
                    if key in seen_ranges:
                        continue
                    if cumulative_lines + w_lines > max_lines and files_to_read:
                        omitted_locations += 1
                        continue
                    seen_ranges.add(key)
                    cumulative_lines += w_lines
                    files_to_read.append({
                        "symbol": n.qualified_name,
                        "kind": n.kind,
                        "depth": d,
                        "confidence": node_best_conf.get(n.id, "exact"),
                        "location": f"{n.file_path}:{w_start}-{w_end}",
                        "line_count": w_lines,
                        "window_note": f"window in {n.name} ({n.start_line}-{n.end_line})",
                        "signature": n.signature,
                    })
            else:
                # For target definition (d=0), cap single massive function window if >120 lines
                start_l, end_l = n.start_line, n.end_line
                window_note = ""
                if n.line_count > 120 and focus_lns and d == 0:
                    end_l = min(n.end_line, n.start_line + 80)
                    window_note = f"first 80 of {n.line_count} lines"
                loc_lines = max(1, end_l - start_l + 1)
                key = (n.file_path, start_l, end_l)
                if key in seen_ranges:
                    continue
                if cumulative_lines + loc_lines > max_lines and files_to_read:
                    omitted_locations += 1
                    continue
                seen_ranges.add(key)
                cumulative_lines += loc_lines
                files_to_read.append({
                    "symbol": n.qualified_name,
                    "kind": n.kind,
                    "depth": d,
                    "confidence": node_best_conf.get(n.id, "exact"),
                    "location": f"{n.file_path}:{start_l}-{end_l}",
                    "line_count": loc_lines,
                    "window_note": window_note,
                    "signature": n.signature,
                })

        events_involved = [n.to_dict() for n in nodes_by_id.values() if n.kind == "event"]

        # Summarize any skipped uncertain (`name_only`) calls compactly with candidate counts in the same language (max 5)
        root_langs = {r.language for r in roots if r.language}
        uncertain_summary: list[dict[str, Any]] = []
        for sym_name, call_cnt in uncertain_calls_counter.most_common(5):
            if root_langs:
                placeholders = ",".join("?" for _ in root_langs)
                cand_cnt = self.conn.execute(
                    f"SELECT COUNT(*) FROM nodes WHERE name = ? AND kind != 'file' AND language IN ({placeholders})",
                    (sym_name, *root_langs),
                ).fetchone()[0]
            else:
                cand_cnt = self.conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE name = ? AND kind != 'file'",
                    (sym_name,),
                ).fetchone()[0]
            if cand_cnt > 1:
                uncertain_summary.append({
                    "symbol": sym_name,
                    "call_occurrences": call_cnt,
                    "candidate_definitions": cand_cnt,
                })

        known_sites: set[str] = set()
        for u in upstream_relations:
            if u.get("call_site"):
                known_sites.add(u["call_site"])
        for d in downstream_relations:
            if d.get("call_site"):
                known_sites.add(d["call_site"])

        coverage = scan_coverage_warnings(
            project_root=self.project_root,
            symbol=roots[0].name if roots else symbol,
            target_nodes=roots,
            known_call_sites=known_sites,
        )

        return {
            "symbol": symbol,
            "found": True,
            "refreshed_files": refreshed_files,
            "depth": depth,
            "definitions": [r.to_dict() for r in roots],
            "disambiguation": disambiguation_candidates if len(disambiguation_candidates) > 1 else [],
            "small_file_hints": small_file_hints,
            "upstream": upstream_relations,
            "downstream": downstream_relations,
            "events": events_involved,
            "files_to_read": files_to_read,
            "total_context_lines": cumulative_lines,
            "omitted_locations": omitted_locations,
            "max_lines_budget": max_lines,
            "max_tokens_budget": max_tokens,
            "uncertain_name_only": uncertain_summary[:5],
            "coverage": coverage,
        }

    # -------------------------------------------------------------------------
    # Shortest Flow Finder (High-Confidence Calls & Events)
    # -------------------------------------------------------------------------

    def find_flow(
        self,
        from_symbol: str,
        to_symbol: str,
        max_depth: int = 8,
        refreshed_files: int = 0,
    ) -> dict[str, Any]:
        """Find the shortest high-confidence (`exact`/`scoped`) call or event path between two symbols."""
        from .context_builder import scan_coverage_warnings

        src_roots = self.find_symbol_nodes(from_symbol)
        dst_roots = self.find_symbol_nodes(to_symbol)

        if not src_roots or not dst_roots:
            missing = []
            if not src_roots:
                missing.append(from_symbol)
            if not dst_roots:
                missing.append(to_symbol)
            cov = scan_coverage_warnings(self.project_root, to_symbol, dst_roots, set())
            return {
                "from": from_symbol,
                "to": to_symbol,
                "found": False,
                "refreshed_files": refreshed_files,
                "message": f"Symbol(s) not found: {', '.join(missing)}",
                "steps": [],
                "coverage": cov,
            }

        start_ids: set[str] = set()
        target_ids: set[str] = set()

        for r in src_roots:
            start_ids.add(r.id)
            if r.kind == "class":
                rows = self.conn.execute(
                    "SELECT id FROM nodes WHERE id LIKE ?",
                    (f"{r.file_path}::{r.qualified_name}.%",),
                ).fetchall()
                start_ids.update(row["id"] for row in rows)

        for r in dst_roots:
            target_ids.add(r.id)
            if r.kind == "class":
                rows = self.conn.execute(
                    "SELECT id FROM nodes WHERE id LIKE ?",
                    (f"{r.file_path}::{r.qualified_name}.%",),
                ).fetchall()
                target_ids.update(row["id"] for row in rows)

        node_rows = self.conn.execute("SELECT * FROM nodes").fetchall()
        node_map: dict[str, GraphNode] = {r["id"]: self._row_to_node(r) for r in node_rows}

        edge_rows = self.conn.execute(
            """
            SELECT source_id, target_id, target_name, edge_type, confidence, file_path, line
            FROM edges
            WHERE target_id IS NOT NULL AND confidence IN ('exact', 'scoped')
            """
        ).fetchall()

        forward_adj: dict[str, list[tuple[str, str, str, int]]] = {}
        undirected_adj: dict[str, list[tuple[str, str, str, int]]] = {}

        for e in edge_rows:
            u = e["source_id"]
            v = e["target_id"]
            etype = e["edge_type"]
            fp = e["file_path"]
            ln = e["line"]

            if etype == "listens_to":
                forward_adj.setdefault(v, []).append((u, "listens_to", fp, ln))
            else:
                forward_adj.setdefault(u, []).append((v, etype, fp, ln))

            undirected_adj.setdefault(u, []).append((v, etype, fp, ln))
            undirected_adj.setdefault(v, []).append((u, f"rev:{etype}", fp, ln))

        def run_bfs(adj: dict[str, list[tuple[str, str, str, int]]]) -> list[dict[str, Any]] | None:
            queue: deque[tuple[str, list[dict[str, Any]]]] = deque()
            visited: set[str] = set()

            for sid in start_ids:
                if sid not in node_map:
                    continue
                snode = node_map[sid]
                initial_step = {
                    "node_id": snode.id,
                    "symbol": snode.qualified_name,
                    "kind": snode.kind,
                    "location": f"{snode.file_path}:{snode.start_line}-{snode.end_line}",
                    "signature": snode.signature,
                    "via_edge": None,
                    "call_site": None,
                }
                if sid in target_ids:
                    return [initial_step]
                visited.add(sid)
                queue.append((sid, [initial_step]))

            while queue:
                curr_id, path = queue.popleft()
                if len(path) > max_depth:
                    continue

                for next_id, etype, fp, ln in adj.get(curr_id, []):
                    if next_id not in node_map or next_id in visited:
                        continue
                    nnode = node_map[next_id]
                    next_step = {
                        "node_id": nnode.id,
                        "symbol": nnode.qualified_name,
                        "kind": nnode.kind,
                        "location": f"{nnode.file_path}:{nnode.start_line}-{nnode.end_line}",
                        "signature": nnode.signature,
                        "via_edge": etype,
                        "call_site": f"{fp}:{ln}",
                    }
                    new_path = path + [next_step]
                    if next_id in target_ids:
                        return new_path
                    visited.add(next_id)
                    queue.append((next_id, new_path))

            return None

        path_steps = run_bfs(forward_adj)
        direction_mode = "directed"

        if path_steps is None:
            path_steps = run_bfs(undirected_adj)
            direction_mode = "undirected"

        known_flow_sites = {
            s["call_site"] for s in (path_steps or []) if s.get("call_site")
        }
        cov = scan_coverage_warnings(
            project_root=self.project_root,
            symbol=dst_roots[0].name if dst_roots else to_symbol,
            target_nodes=dst_roots,
            known_call_sites=known_flow_sites,
        )

        if path_steps is None:
            return {
                "from": from_symbol,
                "to": to_symbol,
                "found": False,
                "refreshed_files": refreshed_files,
                "message": f"No call or event flow found between '{from_symbol}' and '{to_symbol}' within depth {max_depth}.",
                "steps": [],
                "coverage": cov,
            }

        return {
            "from": from_symbol,
            "to": to_symbol,
            "found": True,
            "refreshed_files": refreshed_files,
            "mode": direction_mode,
            "hop_count": len(path_steps) - 1,
            "steps": path_steps,
            "coverage": cov,
        }

    # -------------------------------------------------------------------------
    # Diagnostics (`stats` / `doctor`)
    # -------------------------------------------------------------------------

    def get_diagnostics(self) -> dict[str, Any]:
        """Collect comprehensive diagnostics for `code-graph stats` / `doctor`."""
        f_cnt, n_cnt, e_cnt = self.get_counts()
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        lang_rows = self.conn.execute(
            "SELECT language, COUNT(*) as cnt FROM files GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        files_by_language = {r["language"]: r["cnt"] for r in lang_rows}

        conf_rows = self.conn.execute(
            "SELECT confidence, COUNT(*) as cnt FROM edges GROUP BY confidence ORDER BY cnt DESC"
        ).fetchall()
        edges_by_confidence = {r["confidence"]: r["cnt"] for r in conf_rows}

        all_files = self.conn.execute("SELECT file_path FROM files").fetchall()
        dir_counter: Counter[str] = Counter()
        for r in all_files:
            parts = r["file_path"].split("/")
            top_dir = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")
            dir_counter[top_dir] += 1

        last_indexed_ts = self.get_meta("last_indexed_at")
        autoloads = self.get_meta("autoloads", {}) or {}

        return {
            "project_root": str(self.project_root),
            "db_path": str(self.db_path),
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "total_files": f_cnt,
            "total_nodes": n_cnt,
            "total_edges": e_cnt,
            "files_by_language": files_by_language,
            "edges_by_confidence": edges_by_confidence,
            "top_directories": dict(dir_counter.most_common(8)),
            "godot_autoloads": autoloads,
            "last_indexed_at": last_indexed_ts,
        }
