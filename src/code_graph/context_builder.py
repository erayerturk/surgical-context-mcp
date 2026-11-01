"""Edit context builder, coverage/blind-spot text scanner, style analyzer, and rename/delete safety checker."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .db import CodeGraphDB
from .indexer import _is_default_ignored_dir, _ScopedIgnoreMatcher
from .models import GraphNode
from .parser import EXTENSION_TO_LANGUAGE, LANGUAGE_FAMILY

TEXT_SCAN_EXTENSIONS: frozenset[str] = frozenset({
    ".gd", ".tscn", ".tres", ".godot",
    ".go", ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".rs", ".cs", ".java",
    ".html", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".proto", ".toml",
})

SMALL_EDIT_FILE_LINES = 300
SHORT_CALLEE_MAX_LINES = 25

# Language-specific engine / framework / runtime lifecycle & virtual methods
# When AST upstream is empty for these methods, text-match noise is suppressed and lifecycle info is shown.
LANGUAGE_LIFECYCLE_METHODS: dict[str, frozenset[str]] = {
    "gdscript": frozenset({
        "_ready", "_enter_tree", "_exit_tree", "_process", "_physics_process",
        "_input", "_shortcut_input", "_unhandled_input", "_unhandled_key_input",
        "_draw", "_gui_input", "_notification", "_init", "_to_string",
        "_get_configuration_warnings", "_make_custom_tooltip", "_get_minimum_size",
        "_can_drop_data", "_drop_data", "_get_drag_data", "_has_point", "_clips_input",
        "_integrate_forces", "_validate_property", "_get_property_list", "_get", "_set",
    }),
    "python": frozenset({
        "__init__", "__new__", "__del__", "__repr__", "__str__", "__bytes__", "__format__",
        "__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__", "__hash__", "__bool__",
        "__getattr__", "__getattribute__", "__setattr__", "__delattr__", "__dir__",
        "__get__", "__set__", "__delete__", "__set_name__", "__len__", "__getitem__",
        "__setitem__", "__delitem__", "__iter__", "__next__", "__reversed__", "__contains__",
        "__call__", "__enter__", "__exit__", "__aenter__", "__aexit__", "__await__",
        "__aiter__", "__anext__", "__post_init__", "model_post_init",
        "setUp", "tearDown", "setUpClass", "tearDownClass", "setup_method", "teardown_method",
        "pytest_configure", "pytest_collection_modifyitems",
    }),
    "go": frozenset({
        "init", "main", "ServeHTTP", "Error", "String", "MarshalJSON", "UnmarshalJSON",
        "MarshalText", "UnmarshalText", "MarshalBinary", "UnmarshalBinary", "Scan", "Value",
        "TestMain",
    }),
    "javascript": frozenset({
        "constructor", "render", "componentDidMount", "componentDidUpdate", "componentWillUnmount",
        "shouldComponentUpdate", "getDerivedStateFromProps", "getSnapshotBeforeUpdate", "componentDidCatch",
        "connectedCallback", "disconnectedCallback", "attributeChangedCallback", "adoptedCallback",
        "ngOnInit", "ngOnDestroy", "ngOnChanges", "ngAfterViewInit", "mounted", "unmounted",
        "beforeMount", "beforeUnmount", "created", "getServerSideProps", "getStaticProps",
        "getStaticPaths", "generateMetadata", "generateStaticParams", "middleware",
        "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "toJSON", "valueOf", "toString",
    }),
    "rust": frozenset({
        "main", "new", "default", "drop", "fmt", "from", "try_from", "into",
        "deref", "deref_mut", "index", "index_mut", "poll", "clone",
    }),
    "csharp": frozenset({
        "Main", "Awake", "Start", "OnEnable", "OnDisable", "OnDestroy",
        "Update", "FixedUpdate", "LateUpdate", "OnGUI", "OnValidate", "Reset",
        "OnApplicationQuit", "OnApplicationPause", "OnApplicationFocus",
        "OnBecameVisible", "OnBecameInvisible", "OnPreRender", "OnPostRender", "OnRenderObject",
        "OnDrawGizmos", "OnDrawGizmosSelected",
        "OnTriggerEnter", "OnTriggerExit", "OnTriggerStay",
        "OnCollisionEnter", "OnCollisionExit", "OnCollisionStay",
        "OnTriggerEnter2D", "OnTriggerExit2D", "OnTriggerStay2D",
        "OnCollisionEnter2D", "OnCollisionExit2D", "OnCollisionStay2D",
        "OnAnimatorIK", "OnAnimatorMove", "OnMouseDown", "OnMouseUp", "OnMouseEnter", "OnMouseExit",
        "Dispose", "DisposeAsync", "ToString", "Equals", "GetHashCode",
        "_Ready", "_Process", "_PhysicsProcess", "_Input", "_UnhandledInput", "_EnterTree", "_ExitTree", "_Draw",
    }),
    "java": frozenset({
        "main", "toString", "equals", "hashCode", "compareTo", "clone", "finalize",
        "run", "call", "close", "onCreate", "onStart", "onResume", "onPause", "onStop",
        "onDestroy", "onBind", "afterPropertiesSet", "destroy", "setUp", "tearDown",
    }),
}
LANGUAGE_LIFECYCLE_METHODS["typescript"] = LANGUAGE_LIFECYCLE_METHODS["javascript"]
LANGUAGE_LIFECYCLE_METHODS["tsx"] = LANGUAGE_LIFECYCLE_METHODS["javascript"]

# Union for backwards-compatibility / fallback when target_nodes is empty
ENGINE_LIFECYCLE_METHODS: frozenset[str] = frozenset().union(*LANGUAGE_LIFECYCLE_METHODS.values())


def is_language_lifecycle_method(short_name: str, target_nodes: list[GraphNode]) -> bool:
    """Return True if `short_name` is a runtime/engine/framework lifecycle callback for its language."""
    if not short_name:
        return False
    if not target_nodes:
        return short_name in ENGINE_LIFECYCLE_METHODS
    for n in target_nodes:
        lang = n.language
        if short_name in LANGUAGE_LIFECYCLE_METHODS.get(lang, ()):
            return True
        if lang == "python" and short_name.startswith("__") and short_name.endswith("__") and len(short_name) >= 5:
            return True
        if lang == "go" and n.file_path.endswith("_test.go") and short_name.startswith(("Test", "Benchmark", "Fuzz", "Example")):
            return True
    return False


def _read_file_lines(full_path: Path) -> list[str]:
    try:
        return full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _format_numbered_lines(lines: list[str], start_line: int, end_line: int) -> str:
    """Return 1-indexed numbered lines `start_line..end_line` inclusive."""
    s = max(1, start_line)
    e = min(len(lines), end_line)
    out: list[str] = []
    for idx in range(s, e + 1):
        out.append(f"{idx:4d}: {lines[idx - 1]}")
    return "\n".join(out)


# -------------------------------------------------------------------------
# 1. Style Hint Extractor (1-3 compact lines)
# -------------------------------------------------------------------------

def extract_style_hint(lines: list[str], language: str) -> list[str]:
    """Infer indentation, type-hinting, naming, and language-specific idioms from file lines."""
    if not lines:
        return ["indent: unknown"]

    tab_indents = 0
    space_indents = 0
    space_widths: list[int] = []
    typed_vars = 0
    untyped_vars = 0
    typed_funcs = 0
    private_funcs = 0
    modern_connect = 0
    legacy_connect = 0
    async_funcs = 0
    semicolons = 0
    no_semicolons = 0

    for raw in lines[:400]:
        if raw.startswith("\t"):
            tab_indents += 1
        elif raw.startswith("  "):
            space_indents += 1
            leading = len(raw) - len(raw.lstrip(" "))
            if leading in (2, 4):
                space_widths.append(leading)

        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue

        if language == "gdscript":
            if re.match(r"^(?:@onready\s+|@export\s+)?var\s+[A-Za-z0-9_]+\s*:", s):
                typed_vars += 1
            elif re.match(r"^(?:@onready\s+|@export\s+)?var\s+[A-Za-z0-9_]+\s*=", s):
                untyped_vars += 1
            if re.match(r"^(?:static\s+)?func\s+[A-Za-z0-9_]+\s*\([^)]*\)\s*->", s):
                typed_funcs += 1
            if re.match(r"^func\s+_[A-Za-z0-9_]+", s):
                private_funcs += 1
            if ".connect(" in s and not re.search(r'\bconnect\(\s*["\']', s):
                modern_connect += 1
            elif re.search(r'\bconnect\(\s*["\']', s):
                legacy_connect += 1
        elif language == "python":
            if re.match(r"^(?:async\s+)?def\s+[A-Za-z0-9_]+\s*\([^)]*\)\s*->", s):
                typed_funcs += 1
            elif re.match(r"^(?:async\s+)?def\s+[A-Za-z0-9_]+\s*\(", s):
                untyped_vars += 1
            if s.startswith("async def "):
                async_funcs += 1
            if re.match(r"^(?:async\s+)?def\s+_[A-Za-z0-9_]+", s):
                private_funcs += 1
        elif language in ("javascript", "typescript", "tsx"):
            if s.endswith(";"):
                semicolons += 1
            elif not s.endswith(("{", "}", ",", "(", ":")):
                no_semicolons += 1
            if "async " in s:
                async_funcs += 1
        elif language == "rust":
            if "Result<" in s or "->" in s:
                typed_funcs += 1
            if "async fn " in s:
                async_funcs += 1

    indent_str = "tabs (`\\t`)" if tab_indents >= space_indents else f"{ (min(space_widths) if space_widths else 4) } spaces"
    hints = [f"indent: {indent_str}"]

    if language == "gdscript":
        type_str = "static type hints (`: Type`, `-> void`)" if (typed_vars + typed_funcs) > untyped_vars else "dynamic/mixed typing"
        priv_str = "`_private` prefix for internal methods" if private_funcs > 0 else "standard naming"
        hints.append(f"types & naming: {type_str}; {priv_str}")
        if modern_connect > 0 or legacy_connect > 0:
            sig_style = "Godot 4 `signal.connect(callable)`" if modern_connect >= legacy_connect else 'legacy `connect("signal", ...)`'
            hints.append(f"signals: {sig_style}")
    elif language == "python":
        type_str = "type annotations (`-> ReturnType`)" if typed_funcs >= untyped_vars else "minimal type annotations"
        priv_str = "`_private` helpers" if private_funcs > 0 else "`snake_case` functions"
        async_str = ", `async/await`" if async_funcs > 0 else ""
        hints.append(f"conventions: {type_str}; {priv_str}{async_str}")
    elif language in ("javascript", "typescript", "tsx"):
        semi_str = "semicolons (`;`)" if semicolons >= no_semicolons else "no semicolons (ASI)"
        ts_str = "TypeScript strict types" if language in ("typescript", "tsx") else "ES module syntax"
        hints.append(f"conventions: {ts_str}; {semi_str}")
    elif language == "go":
        hints.append("conventions: standard `gofmt` tabs; explicit `(val, error)` returns")
    elif language == "rust":
        hints.append(f"conventions: `snake_case` fns, `Result<T, E>` error handling{', `async fn`' if async_funcs else ''}")
    elif language in ("csharp", "java"):
        hints.append(f"conventions: `PascalCase` types/methods (`csharp`)" if language == "csharp" else "conventions: `camelCase` methods, `PascalCase` classes (`java`)")

    return hints[:3]


# -------------------------------------------------------------------------
# 2. File Skeleton Extractor (Declarations without Function Bodies)
# -------------------------------------------------------------------------

def extract_file_skeleton(lines: list[str], language: str, nodes: list[GraphNode]) -> list[tuple[int, str]]:
    """
    Extract file-level / class-level declarations across all supported languages
    (GDScript, Python, TS/JS/React, Go, Rust, C#, Java) WITHOUT function bodies.
    Returns list of `(line_number, line_text)`.
    """
    body_lines: set[int] = set()
    init_body_lines: set[int] = set()
    for n in nodes:
        if n.kind in ("function", "method") and n.end_line > n.start_line:
            for ln in range(n.start_line, n.end_line + 1):
                body_lines.add(ln)
                if language == "python" and n.name in ("__init__", "__post_init__"):
                    init_body_lines.add(ln)

    skeleton: list[tuple[int, str]] = []
    in_decl_block = False
    brace_depth = 0

    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        # In Python `__init__`, capture `self.attr = ...` instance attribute definitions!
        if language == "python" and idx in init_body_lines:
            if re.match(r"^self\.[A-Za-z_][A-Za-z0-9_]*\s*(?::[^=]+)?=", stripped):
                skeleton.append((idx, raw))
            continue

        if idx in body_lines and not in_decl_block:
            continue

        if language == "gdscript":
            indent = len(raw) - len(raw.lstrip(" \t"))
            if in_decl_block:
                skeleton.append((idx, raw))
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0:
                    in_decl_block = False
                    brace_depth = 0
                continue

            if indent == 0 and re.match(
                r"^(?:extends|class_name|signal|const|enum|@export|@onready|var)\b",
                stripped,
            ):
                skeleton.append((idx, raw))
                if stripped.startswith(("enum", "const")) and "{" in stripped and "}" not in stripped:
                    in_decl_block = True
                    brace_depth = stripped.count("{") - stripped.count("}")

        elif language == "go":
            if in_decl_block:
                skeleton.append((idx, raw))
                brace_depth += stripped.count("{") + stripped.count("(") - stripped.count("}") - stripped.count(")")
                if brace_depth <= 0:
                    in_decl_block = False
                    brace_depth = 0
                continue

            if re.match(r"^(?:package|import|const|var|type)\b", stripped):
                skeleton.append((idx, raw))
                opens = stripped.count("{") + stripped.count("(") - stripped.count("}") - stripped.count(")")
                if opens > 0:
                    in_decl_block = True
                    brace_depth = opens

        elif language == "python":
            indent = len(raw) - len(raw.lstrip(" \t"))
            if indent <= 4 and (
                re.match(r"^(?:import|from|class|@[A-Za-z_])\b", stripped)
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*(?::\s*[^=]+)?(?:=\s*.+)?$", stripped)
            ) and not stripped.startswith(("def ", "async def ", "return ", "yield ", "raise ", "if ", "for ", "while ")):
                skeleton.append((idx, raw))

        elif language in ("javascript", "typescript", "tsx"):
            if in_decl_block:
                skeleton.append((idx, raw))
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0:
                    in_decl_block = False
                    brace_depth = 0
                continue

            indent = len(raw) - len(raw.lstrip(" \t"))
            if indent <= 4 and re.match(
                r"^(?:import|export\s+(?:type|interface|enum|const|let|class|abstract)|interface|type|enum|const|let|var|class|abstract\s+class|readonly|private|public|protected)\b",
                stripped,
            ) and not re.search(r"=\s*(?:async\s*)?\([^)]*\)\s*=>", stripped):
                skeleton.append((idx, raw))
                if re.match(r"^(?:export\s+)?(?:interface|type|enum)\b", stripped):
                    opens = stripped.count("{") - stripped.count("}")
                    if opens > 0:
                        in_decl_block = True
                        brace_depth = opens

        elif language == "rust":
            if in_decl_block:
                skeleton.append((idx, raw))
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0:
                    in_decl_block = False
                    brace_depth = 0
                continue

            if re.match(r"^(?:use|pub\s+use|mod|pub\s+mod|const|pub\s+const|static|pub\s+static|type|pub\s+type|struct|pub\s+struct|enum|pub\s+enum|trait|pub\s+trait|impl|#\[)\b", stripped):
                skeleton.append((idx, raw))
                if re.match(r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum)\b", stripped):
                    opens = stripped.count("{") - stripped.count("}")
                    if opens > 0:
                        in_decl_block = True
                        brace_depth = opens

        elif language in ("csharp", "java"):
            indent = len(raw) - len(raw.lstrip(" \t"))
            if indent <= 8 and re.match(
                r"^(?:using|package|import|namespace|\[|@[A-Z]|public|private|protected|internal|static|readonly|final|abstract|sealed|class|interface|enum|record|struct)\b",
                stripped,
            ):
                skeleton.append((idx, raw))

        else:
            indent = len(raw) - len(raw.lstrip(" \t"))
            if indent <= 4 and re.match(
                r"^(?:import|from|export|const|let|var|class|interface|type|struct|public|private|protected)\b",
                stripped,
            ) and not stripped.startswith(("def ", "function ", "async ")):
                skeleton.append((idx, raw))

    return skeleton


# -------------------------------------------------------------------------
# 3. Coverage / Blind-Spot Scanner
# -------------------------------------------------------------------------

def scan_coverage_warnings(
    project_root: Path,
    symbol: str,
    target_nodes: list[GraphNode],
    known_call_sites: set[str],
    max_matches: int = 10,
) -> dict[str, Any]:
    """
    Scan workspace files for dynamic / unresolved references to `symbol`:
      - String literal dynamic calls: `call("x")`, `call_deferred("x")`, `has_method("x")`,
        `connect("x", ...)`, `Callable(self, "x")`, `rpc("x")`, `get_node(".../x")`
      - `.tscn` / `.tres` / `project.godot` connections (`[connection signal=... method="x"]`, autoload)
      - JSON / protocol definitions
      - General textual occurrences when upstream is empty
    """
    short_name = symbol.split("::")[-1].split(".")[-1].strip()
    is_lifecycle = is_language_lifecycle_method(short_name, target_nodes)
    if not short_name or len(short_name) < 2:
        return {
            "is_engine_lifecycle": is_lifecycle,
            "dynamic_string_refs": [],
            "scene_and_resource_refs": [],
            "protocol_json_refs": [],
            "other_text_matches": [],
            "total_text_matches": 0,
        }

    # Determine target language families so we NEVER scan unrelated code or scene formats
    target_families: set[str] = {
        LANGUAGE_FAMILY.get(n.language, n.language)
        for n in target_nodes
        if n.language and n.kind not in ("event", "contract", "i18n", "data")
    }

    # Exclude definition lines themselves
    def_Ranges: list[tuple[str, int, int]] = [
        (n.file_path, n.start_line, n.end_line) for n in target_nodes if n.kind != "event"
    ]

    def is_in_def(rel_fp: str, ln: int) -> bool:
        for dfp, s, e in def_Ranges:
            if dfp == rel_fp and s <= ln <= e:
                return True
        return False

    dynamic_string_refs: list[dict[str, Any]] = []
    scene_and_resource_refs: list[dict[str, Any]] = []
    protocol_json_refs: list[dict[str, Any]] = []
    other_text_matches: list[dict[str, Any]] = []
    total_text_matches = 0

    escaped = re.escape(short_name)
    # Word-boundary regex for the symbol name
    word_re = re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])")
    # Language-aware dynamic string call pattern across Godot, Python, JS/TS, Go, Rust, C#/Unity, Java
    dyn_str_re = re.compile(
        rf'(?:call|call_deferred|callv|has_method|connect|disconnect|is_connected|Callable|rpc|rpc_id|emit_signal|get_node|get_node_or_null'
        rf'|getattr|hasattr|setattr|methodcaller|SendMessage|SendMessageUpwards|BroadcastMessage|Invoke|InvokeRepeating|StartCoroutine|StopCoroutine|GetMethod'
        rf'|getMethod|getDeclaredMethod|getField|getDeclaredField|MethodByName|FieldByName|addEventListener|\bon)\s*\([^)]*["\'](?:[^"\']*\/)?{escaped}["\']'
        rf'|\[\s*["\']{escaped}["\']\s*\]\s*\('
    )

    ignore_matcher = _ScopedIgnoreMatcher(project_root)

    for root, dirs, files in os.walk(project_root):
        root_path = Path(root)
        try:
            rel_dir = root_path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        if rel_dir == ".":
            rel_dir = ""

        ignore_matcher.load_for_directory(root_path, rel_dir)

        kept_dirs: list[str] = []
        for d in dirs:
            if _is_default_ignored_dir(d):
                continue
            sub_full = root_path / d
            if (sub_full / ".git").exists():
                continue
            sub_rel = f"{rel_dir}/{d}" if rel_dir else d
            if ignore_matcher.is_ignored(sub_rel, is_dir=True):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext not in TEXT_SCAN_EXTENSIONS and fname != "project.godot":
                continue

            # Filter file extension by target language family to avoid cross-language noise
            if target_families:
                if ext in (".tscn", ".tres", ".godot") or fname == "project.godot":
                    if not target_families.intersection({"godot", "dotnet"}):
                        continue
                elif ext in (".html", ".vue", ".svelte"):
                    if not target_families.intersection({"js", "python"}):
                        continue
                elif ext in EXTENSION_TO_LANGUAGE and ext not in (".json", ".po"):
                    file_lang = EXTENSION_TO_LANGUAGE[ext]
                    file_fam = LANGUAGE_FAMILY.get(file_lang, file_lang)
                    if file_fam not in target_families:
                        continue

            rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
            if ignore_matcher.is_ignored(rel_path, is_dir=False):
                continue

            full_path = root_path / fname
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Quick substring check before line-by-line regex
            if short_name not in content:
                continue

            for idx, raw_line in enumerate(content.splitlines(), start=1):
                if short_name not in raw_line:
                    continue
                if not word_re.search(raw_line):
                    continue
                if is_in_def(rel_path, idx):
                    continue

                site_key = f"{rel_path}:{idx}"
                snippet = raw_line.strip()
                if len(snippet) > 110:
                    snippet = snippet[:107] + "..."

                total_text_matches += 1
                item = {"location": site_key, "file": rel_path, "line": idx, "snippet": snippet}

                if ext in (".tscn", ".tres", ".godot", ".html", ".vue", ".svelte") or fname == "project.godot":
                    if len(scene_and_resource_refs) < max_matches:
                        scene_and_resource_refs.append(item)
                elif ext in (".json", ".yaml", ".yml", ".proto", ".toml") or "protocol" in rel_path.lower():
                    if len(protocol_json_refs) < max_matches and site_key not in known_call_sites:
                        protocol_json_refs.append(item)
                elif dyn_str_re.search(raw_line):
                    # Ignore simple comment lines
                    if not snippet.startswith(("#", "//")) and len(dynamic_string_refs) < max_matches:
                        dynamic_string_refs.append(item)
                elif site_key not in known_call_sites and not snippet.startswith(("#", "//")):
                    if not is_lifecycle and len(other_text_matches) < max_matches:
                        other_text_matches.append(item)

    return {
        "is_engine_lifecycle": is_lifecycle,
        "dynamic_string_refs": dynamic_string_refs,
        "scene_and_resource_refs": scene_and_resource_refs,
        "protocol_json_refs": protocol_json_refs,
        "other_text_matches": other_text_matches,
        "total_text_matches": 0 if is_lifecycle else total_text_matches,
    }


def format_coverage_warnings_section(
    coverage: dict[str, Any],
    upstream_empty: bool = False,
) -> list[str]:
    """Format `### Coverage Warnings` section for impact, flow, and get_edit_context."""
    lines: list[str] = ["\n### Coverage Warnings"]
    dyn = coverage.get("dynamic_string_refs", [])
    scene = coverage.get("scene_and_resource_refs", [])
    proto = coverage.get("protocol_json_refs", [])
    other = coverage.get("other_text_matches", [])
    total_txt = int(coverage.get("total_text_matches", 0))
    is_lifecycle = bool(coverage.get("is_engine_lifecycle"))

    if is_lifecycle:
        lines.append(
            "- **Engine Lifecycle Callback:** Invoked automatically by the engine runtime (unrelated textual occurrences suppressed)."
        )
    elif upstream_empty:
        lines.append(
            f"- **Upstream Note:** Grafta çağıran bulunamadı; metin taramasında **{total_txt}** eşleşme bulundu."
        )

    combined: list[tuple[str, dict[str, Any]]] = []
    for x in scene:
        combined.append(("scene/godot", x))
    for x in dyn:
        combined.append(("string/dynamic", x))
    for x in proto:
        combined.append(("protocol/json", x))
    if upstream_empty:
        for x in other:
            combined.append(("text-match", x))

    if not combined:
        lines.append("- No unresolved textual references found.")
    else:
        for tag, item in combined[:10]:
            lines.append(f"- [{tag}] `{item['location']}` — `{item['snippet']}`")
        if len(combined) > 10:
            lines.append(f"- *(+{len(combined) - 10} more textual references omitted)*")

    return lines


# -------------------------------------------------------------------------
# 4. `get_edit_context` Builder (Priority: 2 > 1 > 4 > 5 > 3)
# -------------------------------------------------------------------------

def build_edit_context(
    db: CodeGraphDB,
    symbol: str,
    budget_tokens: int = 4000,
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Build comprehensive, budget-aware editing context for `symbol`:
      1. File skeleton (extends, class_name, signal, const, enum, @export/@onready/var, preload)
      2. Target function full body
      3. Other function signatures in the same file
      4. Member variables used by target function + same-file callees (full body if <=25L, else signature)
      5. Direct callers (d=1, ±5 line window around call site)
      6. Priority truncation: 2 > 1 > 4 > 5 > 3
      7. If file < 300 lines and fits in budget, return full file directly.
    """
    roots = db.find_symbol_nodes(symbol)
    if not roots:
        return {
            "symbol": symbol,
            "found": False,
            "refreshed_files": refreshed_files,
            "message": f"Symbol '{symbol}' not found in .code_graph.sqlite.",
        }

    target = roots[0]
    disamb = [
        {"selector": f"{r.file_path}::{r.name}", "location": f"{r.file_path}:{r.start_line}-{r.end_line}", "language": r.language}
        for r in roots
    ] if len(roots) > 1 else []

    full_file_path = db.project_root / target.file_path
    file_lines = _read_file_lines(full_file_path)
    total_file_lines = len(file_lines)

    # All nodes in the target file
    file_node_rows = db.conn.execute(
        "SELECT * FROM nodes WHERE file_path = ? ORDER BY start_line ASC",
        (target.file_path,),
    ).fetchall()
    file_nodes = [db._row_to_node(r) for r in file_node_rows]

    style_hints = extract_style_hint(file_lines, target.language)

    # Get impact subgraph (depth=1) for callers & callees
    impact = db.get_symbol_impact(symbol= f"{target.file_path}::{target.name}", depth=1, max_lines=500, max_tokens=budget_tokens)
    known_sites: set[str] = set()
    for u in impact.get("upstream", []):
        if u.get("call_site"):
            known_sites.add(u["call_site"])
    for d in impact.get("downstream", []):
        if d.get("call_site"):
            known_sites.add(d["call_site"])

    coverage = scan_coverage_warnings(
        project_root=db.project_root,
        symbol=target.name,
        target_nodes=[target],
        known_call_sites=known_sites,
    )

    # Section 2 (Priority #1): Target Function Full Body
    target_body_text = _format_numbered_lines(file_lines, target.start_line, target.end_line)

    # Section 1 (Priority #2): File Skeleton
    skeleton_tuples = extract_file_skeleton(file_lines, target.language, file_nodes)

    # Section 4 (Priority #3): Used Member Variables & Same-File Callees
    target_raw_code = "\n".join(file_lines[max(0, target.start_line - 1) : min(len(file_lines), target.end_line)])
    target_identifiers = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", target_raw_code))

    used_member_lines: list[str] = []
    for ln_num, ln_txt in skeleton_tuples:
        s_txt = ln_txt.strip()
        if s_txt.startswith(("import ", "from ", "package ", "use ", "using ")):
            continue
        m_var = (
            re.match(
                r"^(?:@export[^\s]*\s+|@onready\s+|pub(?:\([^)]*\))?\s+|export\s+|readonly\s+|private\s+|public\s+|protected\s+|static\s+|const\s+|let\s+|var\s+|signal\s+|type\s+|interface\s+|struct\s+|enum\s+)+([A-Za-z_][A-Za-z0-9_]*)",
                s_txt,
            )
            or re.match(r"^self\.([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=", s_txt)
            or re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+|=\s*.+)$", s_txt)
            or re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+[A-Za-z0-9_*\[\].]+(?:\s+`[^`]*`)?$", s_txt)
        )
        if m_var and m_var.group(1) in target_identifiers:
            used_member_lines.append(f"{ln_num:4d}: {s_txt}")

    same_file_callees: list[dict[str, Any]] = []
    external_callees: list[dict[str, Any]] = []
    seen_callee_ids: set[str] = set()

    out_edges = db.conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND confidence IN ('exact', 'scoped') AND target_id IS NOT NULL",
        (target.id,),
    ).fetchall()
    for e in out_edges:
        tid = e["target_id"]
        if tid in seen_callee_ids or tid == target.id:
            continue
        seen_callee_ids.add(tid)
        trow = db.conn.execute("SELECT * FROM nodes WHERE id = ?", (tid,)).fetchone()
        if not trow:
            continue
        tnode = db._row_to_node(trow)
        if tnode.kind == "event":
            continue
        if tnode.file_path == target.file_path:
            is_short = tnode.line_count <= SHORT_CALLEE_MAX_LINES
            body_str = (
                _format_numbered_lines(file_lines, tnode.start_line, tnode.end_line)
                if is_short
                else f"{tnode.start_line:4d}-{tnode.end_line}: {tnode.signature} ({tnode.line_count} lines)"
            )
            same_file_callees.append({
                "symbol": tnode.qualified_name,
                "confidence": e["confidence"],
                "location": f"{tnode.file_path}:{tnode.start_line}-{tnode.end_line}",
                "is_full_body": is_short,
                "content": body_str,
            })
        else:
            external_callees.append({
                "symbol": tnode.qualified_name,
                "confidence": e["confidence"],
                "location": f"{tnode.file_path}:{tnode.start_line}-{tnode.end_line}",
                "signature": tnode.signature,
            })

    # Section 5 (Priority #4): Direct Callers (d=1, ±5 Line Windows)
    caller_windows: list[dict[str, Any]] = []
    in_edges = db.conn.execute(
        "SELECT * FROM edges WHERE target_id = ? AND confidence IN ('exact', 'scoped') ORDER BY file_path, line",
        (target.id,),
    ).fetchall()
    seen_caller_sites: set[tuple[str, int]] = set()
    for e in in_edges:
        c_fp = e["file_path"]
        c_ln = int(e["line"])
        if (c_fp, c_ln) in seen_caller_sites:
            continue
        seen_caller_sites.add((c_fp, c_ln))
        src_row = db.conn.execute("SELECT * FROM nodes WHERE id = ?", (e["source_id"],)).fetchone()
        caller_name = src_row["qualified_name"] if src_row else c_fp
        c_lines = file_lines if c_fp == target.file_path else _read_file_lines(db.project_root / c_fp)
        w_start = max(1, c_ln - 5)
        w_end = min(len(c_lines), c_ln + 5) if c_lines else c_ln
        snippet_block = _format_numbered_lines(c_lines, w_start, w_end) if c_lines else f"{c_ln:4d}: (call site)"
        caller_windows.append({
            "caller": caller_name,
            "confidence": e["confidence"],
            "edge_type": e["edge_type"],
            "call_site": f"{c_fp}:{c_ln}",
            "window": f"{c_fp}:{w_start}-{w_end}",
            "snippet": snippet_block,
        })

    # Section 3 (Priority #5): Other Function Signatures in the Same File
    other_signatures: list[str] = []
    for fn in file_nodes:
        if fn.kind in ("function", "method") and fn.id != target.id and fn.id not in seen_callee_ids:
            other_signatures.append(f"L{fn.start_line}-{fn.end_line} ({fn.line_count}L): `{fn.signature}`")

    # Uncertain (name_only) summary (max 5)
    uncertain_list = impact.get("uncertain_name_only", [])[:5]
    contracts_list = impact.get("cross_language_contracts", [])

    return {
        "symbol": symbol,
        "found": True,
        "refreshed_files": refreshed_files,
        "budget_tokens": budget_tokens,
        "target": target.to_dict(),
        "disambiguation": disamb,
        "total_file_lines": total_file_lines,
        "is_small_file": total_file_lines > 0 and total_file_lines < SMALL_EDIT_FILE_LINES,
        "full_file_numbered": _format_numbered_lines(file_lines, 1, total_file_lines) if total_file_lines < SMALL_EDIT_FILE_LINES else "",
        "style_hints": style_hints,
        "target_body": target_body_text,
        "skeleton": [f"{ln:4d}: {txt}" for ln, txt in skeleton_tuples],
        "used_members": used_member_lines,
        "same_file_callees": same_file_callees,
        "external_callees": external_callees,
        "caller_windows": caller_windows,
        "other_signatures": other_signatures,
        "cross_language_contracts": contracts_list,
        "uncertain_name_only": uncertain_list,
        "coverage": coverage,
        "upstream_empty": len(caller_windows) == 0,
    }


def format_edit_context_markdown(ctx: dict[str, Any]) -> str:
    """
    Format `get_edit_context` into Markdown within `budget_tokens` using priority order:
      2 (Target Body) > 1 (File Skeleton) > 4 (Used Members & Callees) > 5 (Caller Windows) > 3 (Other Signatures).
    """
    refreshed = int(ctx.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"

    if not ctx.get("found"):
        return f"{fresh_header}\n### Edit Context: `{ctx.get('symbol', '')}`\n\n> {ctx.get('message', 'Not found.')}\n"

    budget_tokens = int(ctx.get("budget_tokens", 4000))
    max_chars = max(1600, budget_tokens * 4)

    t = ctx["target"]
    header_lines: list[str] = [
        fresh_header,
        f"## Edit Context: `{t['qualified_name']}` (`{t['file']}:{t['lines']}`, {t['line_count']}L / file {ctx['total_file_lines']}L)",
    ]

    disamb = ctx.get("disambiguation", [])
    if len(disamb) > 1:
        header_lines.append(f"> **Note:** {len(disamb)} definitions matched `{ctx['symbol']}`; showing `{t['file']}::{t['name']}`.")

    style = ctx.get("style_hints", [])
    if style:
        header_lines.append("\n### Style Hint")
        for s in style:
            header_lines.append(f"- {s}")

    cov_lines = format_coverage_warnings_section(
        ctx.get("coverage", {}),
        upstream_empty=bool(ctx.get("upstream_empty")),
    )

    uncertain = ctx.get("uncertain_name_only", [])[:5]
    unc_lines: list[str] = []
    if uncertain:
        unc_str = ", ".join(f"`{u['symbol']}` ({u['candidate_definitions']} candidates)" for u in uncertain)
        unc_lines = ["\n### Uncertain (`name_only`, max 5)", f"- {unc_str}"]

    base_text = "\n".join(header_lines)
    footer_text = "\n".join(unc_lines + cov_lines)
    remaining_chars = max_chars - len(base_text) - len(footer_text) - 120

    # Rule 7: If file < 300 lines and fits in budget, return the full file directly!
    if ctx.get("is_small_file") and ctx.get("full_file_numbered"):
        full_file_block = (
            f"\n### Full File Content (`{t['file']}`, {ctx['total_file_lines']} lines < {SMALL_EDIT_FILE_LINES}L threshold)\n"
            f"> Full file returned directly — no skeleton needed.\n"
            f"```{t['language']}\n{ctx['full_file_numbered']}\n```"
        )
        if len(full_file_block) <= remaining_chars:
            remaining_chars -= len(full_file_block)
            caller_sec = _fit_caller_windows(ctx.get("caller_windows", []), remaining_chars)
            return "\n".join([base_text, full_file_block, caller_sec, footer_text]) + "\n"

    # Otherwise, allocate budget strictly in priority order: 2 > 1 > 4 > 5 > 3
    # Priority #1 (Item 2): Target Function Full Body
    sec2_body = (
        f"\n### 1. Target Function Body (`{t['file']}:{t['lines']}`, exact)\n"
        f"```{t['language']}\n{ctx.get('target_body', '')}\n```"
    )
    remaining_chars -= len(sec2_body)

    # Priority #2 (Item 1): File Skeleton (budget share: up to 35% of remaining or 4500 chars)
    skel_items: list[str] = ctx.get("skeleton", [])
    sec1_skeleton = ""
    if skel_items:
        skel_budget = max(600, min(int(remaining_chars * 0.42), 5000))
        kept_skel: list[str] = []
        used_c = 0
        for sline in skel_items:
            if used_c + len(sline) + 1 > skel_budget and len(kept_skel) >= 10:
                break
            kept_skel.append(sline)
            used_c += len(sline) + 1
        omitted_skel = len(skel_items) - len(kept_skel)
        omitted_note = f"\n# ... ({omitted_skel} skeleton declarations omitted due to budget)" if omitted_skel > 0 else ""
        sec1_skeleton = (
            f"\n### 2. File Skeleton (`{t['file']}` — imports, signals, vars, consts)\n"
            f"```{t['language']}\n" + "\n".join(kept_skel) + omitted_note + "\n```"
        )
        remaining_chars -= len(sec1_skeleton)

    # Priority #3 (Item 4): Used Member Variables & Callees
    sec4_parts: list[str] = []
    used_members = ctx.get("used_members", [])
    same_callees = ctx.get("same_file_callees", [])
    ext_callees = ctx.get("external_callees", [])
    if used_members or same_callees or ext_callees:
        sec4_parts.append("\n### 3. Used Members & Direct Callees")
        if used_members:
            sec4_parts.append("**Referenced File Members:**")
            for um in used_members[:20]:
                sec4_parts.append(f"- `{um}`")
        if same_callees:
            sec4_parts.append("**Same-File Callees:**")
            omitted_sc = 0
            for sc in same_callees:
                block = (
                    f"- **`{sc['symbol']}`** [{sc['confidence']}] (`{sc['location']}`):\n"
                    f"```{t['language']}\n{sc['content']}\n```"
                    if sc["is_full_body"]
                    else f"- **`{sc['symbol']}`** [{sc['confidence']}] (`{sc['location']}`): `{sc['content']}`"
                )
                if sum(len(x) for x in sec4_parts) + len(block) > max(400, int(remaining_chars * 0.45)):
                    omitted_sc += 1
                    continue
                sec4_parts.append(block)
            if omitted_sc > 0:
                sec4_parts.append(f"- *({omitted_sc} same-file callees omitted due to budget)*")
        if ext_callees:
            sec4_parts.append("**External Callees:**")
            for ec in ext_callees[:12]:
                sec4_parts.append(f"- `{ec['symbol']}` [{ec['confidence']}] (`{ec['location']}`) — `{ec['signature']}`")
            if len(ext_callees) > 12:
                sec4_parts.append(f"- *({len(ext_callees) - 12} external callees omitted)*")
    sec4_text = "\n".join(sec4_parts)
    remaining_chars -= len(sec4_text)

    # Priority #4 (Item 5): Direct Callers (d=1, ±5 Line Windows)
    sec5_text = _fit_caller_windows(ctx.get("caller_windows", []), max(350, int(remaining_chars * 0.55)))
    remaining_chars -= len(sec5_text)

    # Priority #5 (Item 3): Other Function Signatures in the Same File
    other_sigs: list[str] = ctx.get("other_signatures", [])
    sec3_text = ""
    if other_sigs and remaining_chars > 150:
        kept_sigs: list[str] = []
        used_s = 0
        for sig_line in other_sigs:
            entry = f"- {sig_line}"
            if used_s + len(entry) + 1 > remaining_chars - 80 and len(kept_sigs) >= 5:
                break
            kept_sigs.append(entry)
            used_s += len(entry) + 1
        omitted_sigs = len(other_sigs) - len(kept_sigs)
        if omitted_sigs > 0:
            kept_sigs.append(f"- *({omitted_sigs} more function signatures in `{t['file']}` omitted due to budget)*")
        sec3_text = f"\n### 5. Other Function Signatures in `{t['file']}`\n" + "\n".join(kept_sigs)
    elif other_sigs:
        sec3_text = f"\n### 5. Other Function Signatures in `{t['file']}`\n- *({len(other_sigs)} signatures omitted due to budget)*"

    sections = [s for s in (base_text, sec2_body, sec1_skeleton, sec4_text, sec5_text, sec3_text, footer_text) if s]
    return "\n".join(sections) + "\n"


def _fit_caller_windows(caller_windows: list[dict[str, Any]], char_budget: int) -> str:
    if not caller_windows:
        return "\n### 4. Direct Callers (d=1)\n- *(No callers found in AST graph)*"
    lines: list[str] = ["\n### 4. Direct Callers (d=1, ±5 Line Windows)"]
    kept = 0
    used = 45
    for cw in caller_windows:
        block = (
            f"- **`{cw['caller']}`** [{cw['confidence']}, {cw['edge_type']} @ `{cw['call_site']}`] (window `{cw['window']}`):\n"
            f"```text\n{cw['snippet']}\n```"
        )
        if used + len(block) > char_budget and kept >= 1:
            break
        lines.append(block)
        used += len(block)
        kept += 1
    omitted = len(caller_windows) - kept
    if omitted > 0:
        lines.append(f"- *({omitted} caller windows omitted due to budget)*")
    return "\n".join(lines)


# -------------------------------------------------------------------------
# 5. `check_rename_or_delete` Safety Analyzer
# -------------------------------------------------------------------------

def build_rename_or_delete_check(
    db: CodeGraphDB,
    symbol: str,
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Comprehensive pre-delete / pre-rename safety check for `symbol`:
      - All call sites (`exact` / `scoped`) with source line snippet
      - Textual / dynamic matches (`call("x")`, `has_method("x")`, string literals, JSON)
      - Override methods in `extends` / `inherits` hierarchy (parents & children)
      - `.tscn` scene signal connections
      - Signal `emit` and `connect` sites
    """
    roots = db.find_symbol_nodes(symbol)
    short_name = symbol.split("::")[-1].split(".")[-1].strip()

    definitions: list[dict[str, Any]] = []
    call_sites: list[dict[str, Any]] = []
    signal_sites: list[dict[str, Any]] = []
    tscn_connections: list[dict[str, Any]] = []
    inheritance_overrides: list[dict[str, Any]] = []
    known_site_keys: set[str] = set()

    file_cache: dict[str, list[str]] = {}

    def get_line_snippet(rel_fp: str, ln: int) -> str:
        if rel_fp not in file_cache:
            file_cache[rel_fp] = _read_file_lines(db.project_root / rel_fp)
        flines = file_cache[rel_fp]
        if 1 <= ln <= len(flines):
            return flines[ln - 1].strip()
        return ""

    for r in roots:
        definitions.append({
            "location": f"{r.file_path}:{r.start_line}-{r.end_line}",
            "symbol": r.qualified_name,
            "kind": r.kind,
            "signature": r.signature,
        })

        # Incoming edges to this definition
        in_rows = db.conn.execute(
            "SELECT * FROM edges WHERE target_id = ? ORDER BY file_path, line",
            (r.id,),
        ).fetchall()
        for e in in_rows:
            fp = e["file_path"]
            ln = int(e["line"])
            conf = e["confidence"] or "exact"
            etype = e["edge_type"]
            site_key = f"{fp}:{ln}"
            known_site_keys.add(site_key)
            snippet = get_line_snippet(fp, ln)

            if fp.endswith(".tscn"):
                tscn_connections.append({
                    "location": site_key,
                    "confidence": conf,
                    "snippet": snippet,
                })
            elif etype in ("publishes_event", "listens_to"):
                signal_sites.append({
                    "location": site_key,
                    "type": etype,
                    "confidence": conf,
                    "snippet": snippet,
                })
            elif conf in ("exact", "scoped"):
                call_sites.append({
                    "location": site_key,
                    "confidence": conf,
                    "type": etype,
                    "snippet": snippet,
                })

    # Also check if `short_name` is an `event::<short_name>` signal!
    evt_id = f"event::{short_name}"
    evt_edges = db.conn.execute(
        "SELECT * FROM edges WHERE target_id = ? OR target_name = ? ORDER BY file_path, line",
        (evt_id, short_name),
    ).fetchall()
    for e in evt_edges:
        if e["edge_type"] in ("publishes_event", "listens_to"):
            fp = e["file_path"]
            ln = int(e["line"])
            site_key = f"{fp}:{ln}"
            if site_key not in known_site_keys:
                known_site_keys.add(site_key)
                snippet = get_line_snippet(fp, ln)
                if fp.endswith(".tscn"):
                    tscn_connections.append({
                        "location": site_key,
                        "confidence": e["confidence"] or "exact",
                        "snippet": snippet,
                    })
                else:
                    signal_sites.append({
                        "location": site_key,
                        "type": e["edge_type"],
                        "confidence": e["confidence"] or "exact",
                        "snippet": snippet,
                    })

    # Check inheritance (`extends`) overrides: find any parent or child files via `inherits` edges
    # that define a method with the same `short_name`!
    target_files = {r.file_path for r in roots if r.kind != "event"}
    if target_files:
        inh_rows = db.conn.execute(
            "SELECT source_id, target_id, file_path, receiver FROM edges WHERE edge_type = 'inherits'"
        ).fetchall()
        related_files: set[str] = set()
        for ir in inh_rows:
            src_fp = ir["file_path"]
            tgt_fp = ir["receiver"] or ""
            if not tgt_fp and ir["target_id"]:
                t_row = db.conn.execute("SELECT file_path FROM nodes WHERE id = ?", (ir["target_id"],)).fetchone()
                if t_row:
                    tgt_fp = t_row["file_path"]
            if src_fp in target_files and tgt_fp:
                related_files.add(tgt_fp)
            elif tgt_fp in target_files:
                related_files.add(src_fp)

        for rfp in related_files:
            ov_rows = db.conn.execute(
                "SELECT * FROM nodes WHERE file_path = ? AND name = ? AND kind IN ('method', 'function')",
                (rfp, short_name),
            ).fetchall()
            for ov in ov_rows:
                inheritance_overrides.append({
                    "location": f"{ov['file_path']}:{ov['start_line']}-{ov['end_line']}",
                    "symbol": ov["qualified_name"],
                    "signature": ov["signature"],
                })

    coverage = scan_coverage_warnings(
        project_root=db.project_root,
        symbol=short_name,
        target_nodes=roots,
        known_call_sites=known_site_keys,
        max_matches=15,
    )

    for sref in coverage.get("scene_and_resource_refs", []):
        if sref["location"] not in {x["location"] for x in tscn_connections}:
            tscn_connections.append({
                "location": sref["location"],
                "confidence": "scene_text",
                "snippet": sref["snippet"],
            })

    return {
        "symbol": symbol,
        "found": bool(roots or signal_sites or tscn_connections or coverage.get("total_text_matches", 0) > 0),
        "refreshed_files": refreshed_files,
        "definitions": definitions,
        "call_sites": call_sites,
        "inheritance_overrides": inheritance_overrides,
        "tscn_connections": tscn_connections,
        "signal_sites": signal_sites,
        "coverage": coverage,
    }


def format_rename_or_delete_markdown(data: dict[str, Any]) -> str:
    """Format `check_rename_or_delete` output in compact `file:line — context_line` form."""
    refreshed = int(data.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"
    sym = data.get("symbol", "")

    lines: list[str] = [
        fresh_header,
        f"## Change Safety Report (`check_rename_or_delete`): `{sym}`",
    ]

    defs = data.get("definitions", [])
    lines.append(f"\n### Definitions ({len(defs)})")
    if defs:
        for d in defs:
            lines.append(f"- `{d['location']}` — `{d['signature']}`")
    else:
        lines.append("- *(No AST definition found)*")

    calls = data.get("call_sites", [])
    lines.append(f"\n### Resolved Call Sites ({len(calls)})")
    if calls:
        for c in calls[:25]:
            lines.append(f"- `{c['location']}` [{c['confidence']}] — `{c['snippet']}`")
        if len(calls) > 25:
            lines.append(f"- *(+{len(calls) - 25} more call sites)*")
    elif data.get("coverage", {}).get("is_engine_lifecycle"):
        lines.append("- Engine lifecycle callback (invoked automatically by engine runtime).")
    else:
        total_txt = data.get("coverage", {}).get("total_text_matches", 0)
        lines.append(f"- Grafta çağıran bulunamadı; metin taramasında **{total_txt}** eşleşme bulundu.")

    overrides = data.get("inheritance_overrides", [])
    lines.append(f"\n### Inheritance / Overrides (`extends` chain) ({len(overrides)})")
    if overrides:
        for o in overrides:
            lines.append(f"- `{o['location']}` — `{o['symbol']}`: `{o['signature']}`")
    else:
        lines.append("- None")

    tscns = data.get("tscn_connections", [])
    lines.append(f"\n### Scene / Resource Connections (`.tscn`/`.godot`) ({len(tscns)})")
    if tscns:
        for t in tscns:
            lines.append(f"- `{t['location']}` — `{t['snippet']}`")
    else:
        lines.append("- None")

    sigs = data.get("signal_sites", [])
    lines.append(f"\n### Signal `emit` & `connect` Sites ({len(sigs)})")
    if sigs:
        for s in sigs[:20]:
            lines.append(f"- `{s['location']}` [{s['type']}] — `{s['snippet']}`")
    else:
        lines.append("- None")

    lines.extend(
        format_coverage_warnings_section(
            data.get("coverage", {}),
            upstream_empty=(len(calls) == 0 and len(sigs) == 0 and len(tscns) == 0),
        )
    )
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# 6. Graphify-Style Architecture Map (`god_nodes`, Clusters & Event Hubs)
# -------------------------------------------------------------------------

def build_architecture_map(
    db: CodeGraphDB,
    top_n: int = 15,
    refreshed_files: int = 0,
) -> dict[str, Any]:
    """
    Compute the high-level architectural map of the codebase:
      - `god_nodes`: Most heavily connected classes/functions/methods (by weighted degree centrality)
      - `clusters`: Directory/module communities with file counts, line counts, and key symbols
      - `cross_cluster_links`: Strongest call/event bridges between module clusters
      - `event_hubs`: Most connected signals / event channels
      - `godot_autoloads`: Singleton autoloads with line counts and method counts
    """
    f_cnt, n_cnt, e_cnt = db.get_counts()

    # 1. Compute in-degree and out-degree per symbol node across high-confidence edges
    in_deg_rows = db.conn.execute(
        """
        SELECT target_id, COUNT(*) as cnt
        FROM edges
        WHERE target_id IS NOT NULL AND confidence IN ('exact', 'scoped')
        GROUP BY target_id
        """
    ).fetchall()
    in_deg: dict[str, int] = {r["target_id"]: int(r["cnt"]) for r in in_deg_rows}

    out_deg_rows = db.conn.execute(
        """
        SELECT source_id, COUNT(*) as cnt
        FROM edges
        WHERE target_id IS NOT NULL AND confidence IN ('exact', 'scoped')
        GROUP BY source_id
        """
    ).fetchall()
    out_deg: dict[str, int] = {r["source_id"]: int(r["cnt"]) for r in out_deg_rows}

    all_nodes = db.conn.execute("SELECT * FROM nodes").fetchall()
    node_map: dict[str, GraphNode] = {r["id"]: db._row_to_node(r) for r in all_nodes}

    # Rank non-file, non-event production symbols by hub centrality
    scored_symbols: list[tuple[int, int, int, GraphNode]] = []
    for nid, n in node_map.items():
        if n.kind in ("file", "event"):
            continue
        if n.kind == "class" and n.signature.startswith("script "):
            continue
        # Exclude test files and 1-2 character utility macros from architectural God Nodes
        if (
            n.file_path.endswith("_test.go")
            or "/tests/" in f"/{n.file_path}"
            or Path(n.file_path).name.startswith("test_")
            or len(n.name) <= 2
        ):
            continue
        i_d = in_deg.get(nid, 0)
        o_d = out_deg.get(nid, 0)
        if i_d == 0 and o_d == 0:
            continue
        # True architectural hubs have both incoming callers and outgoing calls/events
        bridge_bonus = min(i_d, o_d) * 3 if (i_d > 0 and o_d > 0) else 0
        score = i_d + o_d + bridge_bonus
        scored_symbols.append((score, i_d, o_d, n))

    scored_symbols.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    god_nodes: list[dict[str, Any]] = []
    for score, i_d, o_d, n in scored_symbols[:top_n]:
        god_nodes.append({
            "symbol": n.qualified_name,
            "kind": n.kind,
            "location": f"{n.file_path}:{n.start_line}-{n.end_line}",
            "lines": n.line_count,
            "in_degree": i_d,
            "out_degree": o_d,
            "score": score,
            "language": n.language,
        })

    # 2. Module / Directory Clusters (Communities)
    cluster_files: dict[str, int] = {}
    cluster_lines: dict[str, int] = {}
    cluster_top_syms: dict[str, list[tuple[int, str]]] = {}

    for n in node_map.values():
        parts = n.file_path.split("/")
        cluster_name = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "root")
        if n.kind == "file":
            cluster_files[cluster_name] = cluster_files.get(cluster_name, 0) + 1
            cluster_lines[cluster_name] = cluster_lines.get(cluster_name, 0) + n.end_line
        elif (
            n.kind in ("class", "method", "function")
            and not n.signature.startswith("script ")
            and not n.file_path.endswith("_test.go")
            and len(n.name) > 2
        ):
            i_d = in_deg.get(n.id, 0)
            o_d = out_deg.get(n.id, 0)
            sc = i_d + o_d + (min(i_d, o_d) * 2)
            if sc > 0:
                cluster_top_syms.setdefault(cluster_name, []).append((sc, n.qualified_name))

    clusters: list[dict[str, Any]] = []
    for cname, fcount in sorted(cluster_files.items(), key=lambda x: x[1], reverse=True)[:12]:
        top_s = sorted(cluster_top_syms.get(cname, []), reverse=True)
        seen_s: list[str] = []
        for _, sname in top_s:
            if sname not in seen_s:
                seen_s.append(sname)
            if len(seen_s) >= 4:
                break
        clusters.append({
            "cluster": cname,
            "files": fcount,
            "total_lines": cluster_lines.get(cname, 0),
            "hub_symbols": seen_s,
        })

    # 3. Top Event / Signal Hubs (prioritize signals that have both publishers & subscribers)
    event_hubs: list[dict[str, Any]] = []
    evt_rows = db.conn.execute(
        """
        SELECT target_name,
               SUM(CASE WHEN edge_type = 'publishes_event' THEN 1 ELSE 0 END) as pubs,
               SUM(CASE WHEN edge_type = 'listens_to' THEN 1 ELSE 0 END) as subs
        FROM edges
        WHERE edge_type IN ('publishes_event', 'listens_to')
        GROUP BY target_name
        ORDER BY (CASE WHEN pubs > 0 AND subs > 0 THEN 1 ELSE 0 END) DESC, (pubs + subs) DESC
        LIMIT 12
        """
    ).fetchall()
    for er in evt_rows:
        event_hubs.append({
            "event": er["target_name"],
            "publishers": int(er["pubs"]),
            "subscribers": int(er["subs"]),
        })

    # 4. Godot Autoloads Summary
    autoloads_raw = db.get_meta("autoloads", {}) or {}
    autoloads_info: list[dict[str, Any]] = []
    for sname, fp in autoloads_raw.items():
        f_node = node_map.get(f"{fp}::<file>")
        m_cnt = db.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE file_path = ? AND kind IN ('method', 'function')",
            (fp,),
        ).fetchone()[0]
        autoloads_info.append({
            "name": sname,
            "file": fp,
            "lines": f_node.end_line if f_node else 0,
            "methods": m_cnt,
        })

    return {
        "project_root": str(db.project_root),
        "refreshed_files": refreshed_files,
        "total_files": f_cnt,
        "total_nodes": n_cnt,
        "total_edges": e_cnt,
        "god_nodes": god_nodes,
        "clusters": clusters,
        "event_hubs": event_hubs,
        "godot_autoloads": autoloads_info,
    }


def format_architecture_map_markdown(data: dict[str, Any]) -> str:
    """Format the architecture map (`god_nodes`, clusters, event hubs, autoloads) into compact Markdown."""
    refreshed = int(data.get("refreshed_files", 0))
    fresh_header = "index: fresh" if refreshed == 0 else f"index: refreshed {refreshed} files"

    lines: list[str] = [
        fresh_header,
        f"# Codebase Architecture Map & God Nodes",
        f"- **Totals:** {data.get('total_files', 0)} files | {data.get('total_nodes', 0)} nodes | {data.get('total_edges', 0)} edges",
    ]

    autoloads = data.get("godot_autoloads", [])
    if autoloads:
        lines.append("\n## 1. Godot Autoload Singletons (Global Hubs)")
        for a in autoloads:
            lines.append(f"- **`{a['name']}`** -> `{a['file']}` ({a['lines']}L, {a['methods']} methods)")

    clusters = data.get("clusters", [])
    if clusters:
        lines.append("\n## 2. Subsystem Clusters (Directory Communities)")
        for c in clusters:
            hubs = ", ".join(f"`{s}`" for s in c["hub_symbols"]) if c["hub_symbols"] else "n/a"
            lines.append(f"- **`{c['cluster']}`** ({c['files']} files, {c['total_lines']:,}L) — Key hubs: {hubs}")

    god_nodes = data.get("god_nodes", [])
    if god_nodes:
        lines.append("\n## 3. God Nodes (Most Connected Functions/Classes)")
        for idx, g in enumerate(god_nodes, start=1):
            lines.append(
                f"{idx}. **`{g['symbol']}`** ({g['kind']}) @ `{g['location']}` ({g['lines']}L) "
                f"— `{g['in_degree']}` in / `{g['out_degree']}` out"
            )

    events = data.get("event_hubs", [])
    if events:
        lines.append("\n## 4. Top Event / Signal Bus Channels")
        for ev in events:
            lines.append(f"- **`{ev['event']}`** — {ev['publishers']} publisher(s), {ev['subscribers']} listener(s)")

    return "\n".join(lines) + "\n"

