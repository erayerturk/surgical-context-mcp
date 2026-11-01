"""Incremental directory scanner with .gitignore / .codegraphignore / worktree support."""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

import pathspec

from .db import CodeGraphDB
from .models import IndexStats, ParseResult
from .parser import (
    EXTENSION_TO_LANGUAGE,
    ASTAnalyzer,
    parse_go_modules,
    parse_godot_autoloads,
    parse_tsconfig_aliases,
)

# Default ignored directories (covers Python, JS/TS, Go, Unity, Godot, Unreal, IDEs, AI tools)
IGNORED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".claude",
    ".idea",
    ".vscode",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    ".next",
    ".nuxt",
    "coverage",
    # Game engine cache/artifact directories
    "Library",
    "Temp",
    "Obj",
    "Logs",
    "Packages",
    ".godot",
    "Intermediate",
    "Binaries",
    "Saved",
})

FILE_WARNING_THRESHOLD = 5000


def _is_default_ignored_dir(dirname: str) -> bool:
    if dirname in IGNORED_DIRS:
        return True
    if dirname.startswith(".aider") or dirname.startswith(".code_graph"):
        return True
    return False


class _ScopedIgnoreMatcher:
    """Evaluates root and nested `.gitignore` and `.codegraphignore` rules using `pathspec`."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        # List of (base_rel_prefix, PathSpec) where base_rel_prefix is "" for root or "subdir/"
        self._specs: list[tuple[str, pathspec.PathSpec]] = []

    def load_for_directory(self, dir_path: Path, rel_dir: str) -> None:
        prefix = "" if rel_dir in ("", ".") else f"{rel_dir.rstrip('/')}/"
        patterns: list[str] = []
        for ignore_name in (".gitignore", ".codegraphignore"):
            ig_file = dir_path / ignore_name
            if ig_file.is_file():
                try:
                    lines = ig_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    patterns.extend(lines)
                except OSError:
                    pass
        if patterns:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
            self._specs.append((prefix, spec))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        check_target = f"{rel_path}/" if is_dir and not rel_path.endswith("/") else rel_path
        for prefix, spec in self._specs:
            if not prefix:
                if spec.match_file(check_target):
                    return True
            elif check_target.startswith(prefix):
                sub_rel = check_target[len(prefix) :]
                if sub_rel and spec.match_file(sub_rel):
                    return True
        return False


class ProjectIndexer:
    """Scans a project workspace and incrementally updates `.code_graph.sqlite`."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.db = CodeGraphDB(self.project_root)
        self.analyzer = ASTAnalyzer()

    def index(self, force: bool = False) -> IndexStats:
        """Run fast mtime+SHA1 incremental (or full if force=True) indexing on the project root."""
        t0 = time.perf_counter()
        stats = IndexStats(
            project_root=str(self.project_root),
            db_path=str(self.db.db_path),
        )

        existing_records = {} if force else self.db.get_file_records()
        discovered_files: set[str] = set()
        results_to_upsert: list[ParseResult] = []
        dir_file_counts: Counter[str] = Counter()

        ignore_matcher = _ScopedIgnoreMatcher(self.project_root)
        autoload_map, _ = parse_godot_autoloads(self.project_root)
        tsconfig_aliases = parse_tsconfig_aliases(self.project_root)
        go_modules = parse_go_modules(self.project_root)

        for root, dirs, files in os.walk(self.project_root):
            root_path = Path(root)
            try:
                rel_dir = root_path.relative_to(self.project_root).as_posix()
            except ValueError:
                continue
            if rel_dir == ".":
                rel_dir = ""

            # Load `.gitignore` and `.codegraphignore` in this directory if present
            ignore_matcher.load_for_directory(root_path, rel_dir)

            # Prune ignored directories and nested git worktrees / submodules in-place
            kept_dirs: list[str] = []
            for d in dirs:
                if _is_default_ignored_dir(d):
                    continue
                sub_full = root_path / d
                # Skip nested git repositories / worktrees (any subdir containing its own `.git`)
                if (sub_full / ".git").exists():
                    continue
                sub_rel = f"{rel_dir}/{d}" if rel_dir else d
                if ignore_matcher.is_ignored(sub_rel, is_dir=True):
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs

            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext not in EXTENSION_TO_LANGUAGE:
                    continue

                rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
                if ignore_matcher.is_ignored(rel_path, is_dir=False):
                    continue

                full_path = root_path / fname
                try:
                    st = full_path.stat()
                except OSError:
                    continue

                discovered_files.add(rel_path)
                stats.files_scanned += 1

                top_bucket = rel_dir.split("/")[0] if rel_dir else "."
                dir_file_counts[top_bucket] += 1

                # Fast path: if mtime hasn't changed, skip reading file bytes!
                if not force and rel_path in existing_records:
                    old_hash, old_mtime = existing_records[rel_path]
                    if old_mtime > 0.0 and abs(st.st_mtime - old_mtime) < 1e-4:
                        stats.files_skipped += 1
                        continue

                try:
                    raw_bytes = full_path.read_bytes()
                except OSError:
                    continue

                current_hash = self.analyzer.compute_hash(raw_bytes)
                if not force and rel_path in existing_records and existing_records[rel_path][0] == current_hash:
                    stats.files_skipped += 1
                    continue

                parsed = self.analyzer.parse_file(full_path, rel_path, mtime=st.st_mtime)
                if parsed is not None:
                    results_to_upsert.append(parsed)
                    stats.files_indexed += 1

        # Detect deleted or newly-ignored files
        removed_files = [fp for fp in existing_records if fp not in discovered_files]
        stats.files_removed = len(removed_files)

        if removed_files:
            self.db.remove_files(removed_files)

        if results_to_upsert:
            self.db.upsert_parse_results(results_to_upsert)

        prev_autoloads = self.db.get_meta("autoloads", {}) or {}
        prev_tsconfig = self.db.get_meta("tsconfig_aliases", {}) or {}
        prev_gomods = self.db.get_meta("go_modules", {}) or {}
        if (
            results_to_upsert
            or removed_files
            or force
            or (autoload_map != prev_autoloads)
            or (tsconfig_aliases != prev_tsconfig)
            or (go_modules != prev_gomods)
        ):
            self.db.resolve_edges_and_cleanup(
                autoload_map=autoload_map,
                tsconfig_aliases=tsconfig_aliases,
                go_modules=go_modules,
            )

        _, total_nodes, total_edges = self.db.get_counts()
        stats.total_nodes = total_nodes
        stats.total_edges = total_edges
        stats.db_size_bytes = self.db.db_path.stat().st_size if self.db.db_path.exists() else 0
        stats.elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

        if stats.files_scanned > FILE_WARNING_THRESHOLD:
            top5 = ", ".join(f"{d} ({c})" for d, c in dir_file_counts.most_common(5))
            stats.warning = (
                f"Uyarı: {stats.files_scanned} dosya tarandı (>{FILE_WARNING_THRESHOLD} eşiği). "
                f"Büyük ihtimalle ignore eksik; en çok dosya içeren ilk 5 dizin: {top5}"
            )

        return stats

    def close(self) -> None:
        self.db.close()
