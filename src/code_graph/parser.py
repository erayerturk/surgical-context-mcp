"""Universal multi-language AST parser (Python, TS/JS/React, Go, Rust, C#, Java, Godot) & ecosystem config resolvers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser

import tree_sitter_c_sharp
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

try:
    import tree_sitter_rust
except ImportError:
    tree_sitter_rust = None  # type: ignore[assignment]

try:
    import tree_sitter_java
except ImportError:
    tree_sitter_java = None  # type: ignore[assignment]

from .models import GraphEdge, GraphNode, ParseResult

# Supported file extensions mapped to language identifiers
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".java": "java",
    ".gd": "gdscript",
    ".tscn": "tscn",
    ".json": "json",
    ".po": "po",
}

SKIPPED_JSON_FILENAMES: frozenset[str] = frozenset({
    "tsconfig.json",
    "jsconfig.json",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "composer.json",
    "composer.lock",
    "settings.json",
    "settings.local.json",
    "launch.json",
    "tasks.json",
    ".mcp.json",
})

# Language family groups (edges of type 'calls'/'imports'/'inherits' NEVER cross families)
LANGUAGE_FAMILY: dict[str, str] = {
    "python": "python",
    "javascript": "js",
    "typescript": "js",
    "tsx": "js",
    "go": "go",
    "rust": "rust",
    "csharp": "dotnet",
    "java": "jvm",
    "gdscript": "godot",
    "tscn": "godot",
    "json": "data",
    "po": "data",
    "contract": "contract",
}

I18N_KEY_FIRST_CALLS: frozenset[str] = frozenset({
    "tr",
    "tr_n",
    "i18n",
    "translate",
    "T",
    "Tr",
})

I18N_CTX_FIRST_CALLS: frozenset[str] = frozenset({
    "noticeT",
    "NoticeT",
    "sendNoticeT",
    "SendNoticeT",
    "formatT",
    "FormatT",
    "broadcastT",
    "BroadcastT",
    "replyT",
    "ReplyT",
})

# Method/function names recognized as publishing/emitting events
PUBLISH_METHODS: frozenset[str] = frozenset({
    "emit",
    "dispatch",
    "publish",
    "trigger",
    "broadcast",
    "fire",
    "emit_signal",
    "emitsignal",
    "send_event",
    "sendevent",
    "dispatchevent",
    "emitevent",
    "post_event",
    "notify_event",
})

# Method/function names recognized as subscribing/listening to events
SUBSCRIBE_METHODS: frozenset[str] = frozenset({
    "on",
    "once",
    "subscribe",
    "listen",
    "addeventlistener",
    "add_listener",
    "addlistener",
    "connect",
    "on_event",
    "onevent",
    "handle_event",
    "register_listener",
})

SIGNAL_KEYWORDS: frozenset[str] = frozenset({
    "signal",
    "Signal",
    "Event",
    "Subject",
    "EventEmitter",
})

# Ignored built-in, standard library, or engine-native calls that shouldn't pollute the call graph
IGNORED_CALLS: frozenset[str] = frozenset({
    # Python / General
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "range", "enumerate", "zip", "isinstance", "issubclass", "getattr", "setattr",
    "hasattr", "super", "type", "repr", "max", "min", "sum", "abs", "round",
    "sorted", "reversed", "any", "all", "open", "chr", "ord", "hex", "id", "hash",
    "callable", "iter", "next", "vars", "dir",
    # JS / TS / React / DOM
    "console.log", "console.warn", "console.error", "console.info",
    "Math.max", "Math.min", "Math.floor", "Math.ceil", "Math.round", "Math.abs", "Math.random",
    "JSON.stringify", "JSON.parse", "Object.keys", "Object.values", "Object.entries",
    "Object.assign", "Array.isArray", "parseInt", "parseFloat", "isNaN", "isFinite",
    "String", "Number", "Boolean", "Date", "RegExp", "Error", "Map", "Set",
    "Promise.all", "Promise.resolve", "Promise.reject", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "requestAnimationFrame", "fetch",
    "useState", "useEffect", "useCallback", "useMemo", "useRef", "useContext", "useReducer",
    "getElementById", "querySelector", "querySelectorAll", "createElement",
    "appendChild", "removeChild", "setAttribute", "getAttribute", "classList.add",
    "classList.remove", "classList.toggle", "preventDefault", "stopPropagation",
    # Go built-ins & stdlib
    "fmt.Println", "fmt.Printf", "fmt.Sprintf", "fmt.Errorf", "fmt.Fprintln",
    "log.Println", "log.Printf", "log.Fatal", "log.Fatalf",
    "make", "append", "cap", "close", "delete", "copy", "panic", "recover", "new",
    "Lock", "Unlock", "RLock", "RUnlock", "Wait", "Done", "Add",
    "Marshal", "Unmarshal", "Encode", "Decode", "NewEncoder", "NewDecoder",
    # Rust / Java / C# common std methods
    "unwrap", "expect", "unwrap_or", "unwrap_or_else", "unwrap_or_default",
    "clone", "to_string", "to_owned", "as_str", "as_ref", "as_mut", "into", "from",
    "iter", "into_iter", "iter_mut", "collect", "is_some", "is_none", "is_ok", "is_err",
    "ok", "err", "Box.new", "Arc.new", "Rc.new", "Vec.new", "String.from", "HashMap.new",
    "println", "eprintln", "format", "vec", " panic", "assert_eq", "assert_ne",
    "System.out.println", "System.err.println", "equals", "hashCode", "compareTo",
    # Common container / string / DB / path methods
    "get", "set", "has", "setdefault", "extend", "insert", "pop", "popleft",
    "remove", "clear", "update", "keys", "values", "items", "add",
    "split", "splitlines", "join", "strip", "lstrip", "rstrip",
    "lower", "upper", "replace", "startswith", "endswith",
    "encode", "decode", "read_bytes", "read_text", "write_text",
    "resolve", "expanduser", "exists", "as_posix", "relative_to",
    "fetchone", "fetchall", "execute", "executemany", "executescript",
    "push", "slice", "splice", "shift", "unshift", "concat",
    "map", "filter", "forEach", "reduce", "find", "findIndex", "some", "every",
    "includes", "indexOf", "trim", "toLowerCase", "toUpperCase", "toString",
    "valueOf", "charAt", "substring", "substr", "match", "search",
    # Godot / GDScript Engine & UI Built-ins
    "prints", "printerr", "print_debug", "push_error", "push_warning", "assert",
    "typeof", "weakref", "is_instance_valid", "is_instance_of", "var_to_str", "str_to_var",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Color", "Rect2", "Rect2i",
    "Transform2D", "Transform3D", "Basis", "Quaternion", "Callable", "Signal", "NodePath",
    "PackedScene", "Resource", "RefCounted", "Object", "Dictionary", "Array",
    "PackedByteArray", "PackedStringArray", "PackedVector2Array", "PackedColorArray",
    "clamp", "clampf", "clampi", "lerp", "lerpf", "lerp_angle", "inverse_lerp", "remap",
    "floor", "floorf", "floori", "ceil", "ceilf", "ceili", "snapped", "snappedf",
    "SIGN", "sign", "signf", "signi", "sqrt", "pow", "exp", "log", "sin", "cos", "tan",
    "asin", "acos", "atan", "atan2", "deg_to_rad", "rad_to_deg", "fmod", "fposmod", "posmod",
    "randi", "randf", "randf_range", "randi_range", "randomize", "seed",
    "free", "queue_free", "instantiate", "duplicate",
    "get_node", "get_node_or_null", "has_node", "find_child", "find_children",
    "get_parent", "get_tree", "get_viewport", "get_window", "get_last_exclusive_window",
    "add_child", "remove_child", "move_child", "reparent", "get_children", "get_child",
    "get_child_count", "get_index", "is_inside_tree", "is_node_ready",
    "set_process", "set_physics_process", "set_process_input", "set_process_unhandled_input",
    "show", "hide", "set_visible", "is_visible", "is_visible_in_tree",
    "grab_focus", "release_focus", "has_focus", "set_disabled", "is_disabled",
    "set_text", "get_text", "set_tooltip_text", "set_modulate", "set_self_modulate",
    "set_position", "get_position", "set_global_position", "get_global_position",
    "set_size", "get_size", "set_custom_minimum_size", "set_anchors_preset",
    "add_theme_color_override", "add_theme_font_size_override", "add_theme_stylebox_override",
    "add_theme_constant_override", "add_theme_font_override", "add_theme_icon_override",
    "remove_theme_color_override", "remove_theme_stylebox_override",
    "get_theme_color", "get_theme_font", "get_theme_stylebox",
    "create_tween", "tween_property", "tween_callback", "tween_interval", "tween_method",
    "set_trans", "set_ease", "set_parallel", "set_loops", "kill",
    "create_timer", "get_ticks_msec", "get_ticks_usec", "get_unix_time_from_system",
    "move_and_slide", "move_and_collide", "is_on_floor", "is_on_wall", "is_on_ceiling",
    "to_local", "to_global", "distance_to", "distance_squared_to", "direction_to",
    "normalized", "length", "length_squared", "rotated", "dot", "cross", "angle", "angle_to",
    "queue_redraw", "draw_rect", "draw_circle", "draw_line", "draw_string", "draw_texture",
    "draw_texture_rect", "draw_style_box", "draw_arc", "draw_polygon", "draw_polyline",
    "play", "stop", "pause", "is_playing", "seek",
    "is_empty", "size", "front", "back", "pick_random", "shuffle", "sort", "sort_custom",
    "fill", "resize", "erase", "rfind", "count", "bsearch",
    "begins_with", "ends_with", "contains", "get_slice", "to_int", "to_float",
    "to_lower", "to_upper", "capitalize", "strip_edges", "pad_zeros", "pad_decimals",
    "md5_text", "sha1_text", "sha256_text", "to_utf8_buffer", "get_string_from_utf8",
    "get_u8", "get_u16", "get_u32", "get_u64", "get_8", "get_16", "get_32", "get_64",
    "get_float", "get_double", "get_var", "put_u8", "put_u16", "put_u32", "put_u64",
    "put_var", "get_available_bytes", "get_error", "get_status", "poll",
    "is_action_pressed", "is_action_just_pressed", "is_action_just_released",
    "get_vector", "get_axis", "get_mouse_position", "get_global_mouse_position",
    "set_mouse_mode", "set_input_as_handled",
})

# Language-specific standard library modules, engine singletons, and built-in objects
# Calls on these receivers (when not aliased to a project file) are filtered out to prevent false cross-file edges and noise
LANGUAGE_STDLIB_RECEIVERS: dict[str, frozenset[str]] = {
    "go": frozenset({
        "fmt", "strings", "strconv", "bytes", "bufio", "os", "io", "fs", "filepath", "path",
        "time", "math", "rand", "cmplx", "sync", "atomic", "context", "errors", "sort", "slices",
        "maps", "reflect", "regexp", "runtime", "debug", "net", "http", "url", "tls", "json",
        "xml", "csv", "gob", "hex", "base64", "binary", "utf8", "unicode", "log", "slog", "flag",
        "testing", "sql", "exec", "signal", "syscall", "unsafe", "hash", "sha1", "sha256", "md5",
        "hmac", "cipher", "aes", "rsa", "gzip", "zlib", "tar", "zip", "html", "template", "t", "b",
    }),
    "python": frozenset({
        "os", "sys", "re", "json", "time", "datetime", "math", "random", "logging", "logger", "log",
        "pathlib", "Path", "shutil", "subprocess", "itertools", "functools", "collections", "typing",
        "asyncio", "unittest", "pytest", "hashlib", "base64", "uuid", "copy", "io", "csv", "sqlite3",
        "urllib", "http", "socket", "struct", "enum", "dataclasses", "abc", "contextlib", "inspect",
        "traceback", "warnings", "gc", "platform", "stat", "glob", "fnmatch", "tempfile", "argparse",
    }),
    "javascript": frozenset({
        "Math", "JSON", "Object", "Array", "String", "Number", "Boolean", "Symbol", "BigInt",
        "Promise", "Date", "RegExp", "Reflect", "Proxy", "Intl", "Error", "TypeError", "RangeError",
        "Map", "Set", "WeakMap", "WeakSet", "ArrayBuffer", "DataView", "Uint8Array", "Int32Array",
        "Float64Array", "console", "window", "document", "navigator", "location", "history",
        "localStorage", "sessionStorage", "performance", "crypto", "URL", "URLSearchParams",
        "FormData", "Headers", "Request", "Response", "AbortController", "TextEncoder", "TextDecoder",
        "process", "Buffer", "fs", "path", "os", "util", "stream", "zlib", "child_process", "React", "ReactDOM",
    }),
    "gdscript": frozenset({
        "Input", "Time", "OS", "Engine", "ProjectSettings", "ResourceLoader", "ResourceSaver",
        "FileAccess", "DirAccess", "DisplayServer", "RenderingServer", "AudioServer", "TranslationServer",
        "PhysicsServer2D", "PhysicsServer3D", "NavigationServer2D", "NavigationServer3D", "XRServer",
        "CameraServer", "ThemeDB", "ClassDB", "Marshalls", "JSON", "RegEx", "Crypto", "HashingContext",
        "AESContext", "TLSOptions", "IP", "WorkerThreadPool", "JavaScriptBridge", "Geometry2D",
        "Geometry3D", "ResourceUID", "GD", "Color", "Vector2", "Vector2i", "Vector3", "Vector3i",
        "Vector4", "Rect2", "Transform2D", "Transform3D", "Basis", "Quaternion", "Plane", "AABB",
        "RID", "Callable", "Signal", "String", "Array", "Dictionary", "PackedByteArray", "PackedStringArray",
    }),
    "rust": frozenset({
        "std", "core", "alloc", "Vec", "String", "str", "Option", "Result", "Box", "Rc", "Arc",
        "Cell", "RefCell", "Mutex", "RwLock", "HashMap", "HashSet", "BTreeMap", "BTreeSet", "VecDeque",
        "Path", "PathBuf", "File", "OpenOptions", "Command", "Instant", "Duration", "SystemTime",
        "env", "fs", "io", "mem", "ptr", "slice", "iter", "fmt", "thread", "sync", "time", "process",
        "tokio", "serde", "serde_json", "tracing", "log",
    }),
    "csharp": frozenset({
        "System", "Console", "Math", "MathF", "Mathf", "Convert", "BitConverter", "String", "Guid",
        "DateTime", "DateTimeOffset", "TimeSpan", "Task", "ValueTask", "Thread", "Interlocked",
        "Monitor", "SemaphoreSlim", "CancellationTokenSource", "Enumerable", "Queryable", "Array",
        "List", "Dictionary", "HashSet", "File", "Directory", "Path", "Stream", "MemoryStream",
        "JsonSerializer", "JsonUtility", "Regex", "Encoding", "Debug", "Time", "Input", "Physics",
        "Physics2D", "GameObject", "Object", "Component", "Transform", "Vector2", "Vector3", "Vector4",
        "Quaternion", "Color", "Rect", "Bounds", "Ray", "Gizmos", "Handles", "Application",
        "SceneManager", "PlayerPrefs", "Resources", "Shader", "Material", "AudioSource", "Animator", "GD",
    }),
    "java": frozenset({
        "System", "Math", "StrictMath", "Objects", "Collections", "Arrays", "Optional", "Stream",
        "IntStream", "LongStream", "DoubleStream", "Collectors", "Files", "Paths", "Path", "String",
        "StringBuilder", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short", "Character",
        "BigInteger", "BigDecimal", "Thread", "CompletableFuture", "Executors", "TimeUnit", "UUID",
        "Instant", "Duration", "Period", "LocalDate", "LocalTime", "LocalDateTime", "ZonedDateTime",
        "DateTimeFormatter", "Pattern", "Matcher", "Logger", "LoggerFactory", "log", "logger",
        "Assertions", "Assert",
    }),
}
LANGUAGE_STDLIB_RECEIVERS["typescript"] = LANGUAGE_STDLIB_RECEIVERS["javascript"]
LANGUAGE_STDLIB_RECEIVERS["tsx"] = LANGUAGE_STDLIB_RECEIVERS["javascript"]

# Language-specific noisy helper/assertion/framework method names to ignore per language
LANGUAGE_IGNORED_CALLS: dict[str, frozenset[str]] = {
    "go": frozenset({
        "Errorf", "Fatalf", "Logf", "Helper", "Parallel", "Skip", "Skipf", "Fail", "FailNow",
        "Cleanup", "TempDir", "Setenv", "Run", "WriteHeader", "Header", "Context", "WithCancel",
        "WithTimeout", "WithDeadline", "WithValue", "Background", "TODO", "Sprintf", "Fprintf",
        "Println", "Printf", "TrimSpace", "TrimPrefix", "TrimSuffix", "HasPrefix", "HasSuffix",
        "Contains", "Split", "Join", "ReplaceAll", "ToLower", "ToUpper", "Atoi", "Itoa",
        "ParseInt", "ParseFloat", "ParseBool", "FormatInt", "ReadAll", "ReadFile", "WriteFile",
        "Stat", "MkdirAll", "RemoveAll", "Now", "Since", "Until", "Sleep", "After", "NewTicker",
    }),
    "python": frozenset({
        "info", "debug", "warning", "error", "critical", "exception", "getLogger",
        "dumps", "loads", "dump", "load", "compile", "findall", "finditer", "sub",
        "getenv", "makedirs", "walk", "isdir", "isfile", "abspath", "dirname", "basename",
        "dataclass", "field", "Field",
    }),
    "javascript": frozenset({
        "then", "catch", "finally", "bind", "call", "apply", "freeze", "seal", "create",
        "defineProperty", "hasOwnProperty", "isPrototypeOf", "getItem", "setItem", "removeItem",
        "addEventListener", "removeEventListener", "dispatchEvent", "requestAnimationFrame",
        "cancelAnimationFrame", "encodeURIComponent", "decodeURIComponent", "atob", "btoa",
        "describe", "it", "test", "expect", "beforeEach", "afterEach", "beforeAll", "afterAll", "vi", "jest",
    }),
    "rust": frozenset({
        "new", "default", "with_capacity", "len", "is_empty", "push", "pop", "insert", "remove",
        "get", "get_mut", "contains", "contains_key", "entry", "or_insert", "or_insert_with",
        "or_default", "map", "and_then", "or_else", "map_err", "ok_or", "ok_or_else", " filter",
        "filter_map", "flat_map", "fold", "enumerate", "zip", "take", "skip", "chain", "cloned",
        "copied", "chars", "bytes", "lines", "trim", "starts_with", "ends_with", "parse",
        "read_to_string", "write_all", "flush", "spawn", "block_on", "sleep",
    }),
    "csharp": frozenset({
        "Log", "LogWarning", "LogError", "LogException", "GetComponent", "GetComponents",
        "GetComponentInChildren", "GetComponentInParent", "TryGetComponent", "AddComponent",
        "Instantiate", "Destroy", "DestroyImmediate", "DontDestroyOnLoad", "FindObjectOfType",
        "FindObjectsOfType", "CompareTag", "SetActive", "StartCoroutine", "StopCoroutine",
        "StopAllCoroutines", "Where", "Select", "FirstOrDefault", "SingleOrDefault", "Any", "All",
        "ToList", "ToArray", "ToDictionary", "OrderBy", "OrderByDescending", "GroupBy", "Count",
        "ConfigureAwait", "WhenAll", "WhenAny", "Delay", "FromResult", "IsNullOrEmpty", "IsNullOrWhiteSpace",
    }),
    "java": frozenset({
        "of", "ofNullable", "empty", "isPresent", "isEmpty", "ifPresent", "orElse", "orElseGet",
        "orElseThrow", "stream", "parallelStream", "collect", "toList", "toSet", "toMap",
        "forEach", "filter", "map", "flatMap", "reduce", "findFirst", "findAny", "anyMatch",
        "allMatch", "noneMatch", "count", "sorted", "distinct", "limit", "skip", "builder",
        "build", "getLogger", "info", "debug", "warn", "error", "trace", "assertEquals",
        "assertTrue", "assertFalse", "assertNotNull", "assertNull", "assertThrows",
    }),
    "gdscript": frozenset({
        "is_editor_hint", "get_frames_per_second", "get_main_loop", "get_version_info",
    }),
}
LANGUAGE_IGNORED_CALLS["typescript"] = LANGUAGE_IGNORED_CALLS["javascript"]
LANGUAGE_IGNORED_CALLS["tsx"] = LANGUAGE_IGNORED_CALLS["javascript"]

COMMON_IGNORED_CONSTANTS: frozenset[str] = frozenset({
    "TRUE", "FALSE", "NULL", "NONE", "NIL", "NAN", "INF", "INFINITY", "PI", "TAU", "EOF",
    "OK", "ERR", "TODO", "FIXME", "NOTE", "HACK", "XXX", "HTTP", "HTTPS", "UTF8", "ASCII",
    "JSON", "HTML", "XML", "CSV", "SQL", "UUID", "URL", "URI", "API", "TCP", "UDP", "TLS",
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
    "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "TRACE",
    "TYPE_CHECKING", "SIGINT", "SIGTERM", "SIGKILL",
})

GODOT_BUILTIN_CONST_PREFIXES: tuple[str, ...] = (
    "KEY_", "MOUSE_", "JOY_", "MIDI_", "NOTIFICATION_", "ERR_", "PROPERTY_", "METHOD_",
    "CONNECT_", "PROCESS_MODE_", "PAUSE_MODE_", "SIZE_", "PRESET_", "ANCHOR_", "GROW_",
    "ALIGNMENT_", "HORIZONTAL_ALIGNMENT_", "VERTICAL_ALIGNMENT_", "AUTOWRAP_", "TEXTURE_",
    "BLEND_", "COLLISION_", "BODY_MODE_", "TRANS_", "EASE_", "LOOP_", "TYPE_", "CORNER_",
    "SIDE_", "MARGIN_", "CLOCKWISE", "COUNTERCLOCKWISE", "INLINE_ALIGNMENT_", "CURSOR_",
    "WINDOW_MODE_", "DISPLAY_SERVER_", "RENDERING_", "AUDIO_",
)


def _make_parser(lang: Language) -> Parser:
    """Create a tree-sitter Parser compatible across 0.22+ and 0.23+ APIs."""
    try:
        return Parser(lang)
    except TypeError:
        p = Parser()
        p.set_language(lang)  # type: ignore[attr-defined]
        return p


# -------------------------------------------------------------------------
# Ecosystem Config Resolvers (`project.godot`, `tsconfig.json`, `go.mod`)
# -------------------------------------------------------------------------

def parse_godot_autoloads(project_root: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Scan for `project.godot` in `project_root` or immediate subdirectories."""
    autoload_map: dict[str, str] = {}
    res_roots: dict[str, Path] = {}

    candidates = [project_root / "project.godot"]
    try:
        for child in project_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                pg = child / "project.godot"
                if pg.exists():
                    candidates.append(pg)
    except OSError:
        pass

    for pg_path in candidates:
        if not pg_path.exists():
            continue
        godot_dir = pg_path.parent
        try:
            rel_dir = godot_dir.relative_to(project_root).as_posix()
        except ValueError:
            rel_dir = ""
        res_prefix = "" if rel_dir in ("", ".") else f"{rel_dir}/"
        res_roots[res_prefix] = godot_dir

        try:
            content = pg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        in_autoload = False
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                in_autoload = line.lower() == "[autoload]"
                continue
            if in_autoload and "=" in line:
                key, val = line.split("=", 1)
                singleton_name = key.strip()
                res_val = val.strip().strip('"').strip("'").lstrip("*")
                if res_val.startswith("res://"):
                    sub_rel = res_val[len("res://") :]
                    mapped_rel = f"{res_prefix}{sub_rel}"
                    autoload_map[singleton_name] = mapped_rel

    return autoload_map, res_roots


def parse_tsconfig_aliases(project_root: Path) -> dict[str, str]:
    """
    Parse `tsconfig.json` / `jsconfig.json` path aliases (e.g. `@/*` -> `src/`).
    Returns mapping of `{alias_prefix: target_rel_dir_prefix}`.
    """
    aliases: dict[str, str] = {}
    candidates = [project_root / "tsconfig.json", project_root / "jsconfig.json"]
    try:
        for child in project_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                for cfg in ("tsconfig.json", "jsconfig.json"):
                    p = child / cfg
                    if p.exists():
                        candidates.append(p)
    except OSError:
        pass

    for cfg_path in candidates:
        if not cfg_path.exists():
            continue
        try:
            raw = cfg_path.read_text(encoding="utf-8", errors="replace")
            # Strip // and /* */ comments from JSON-with-comments
            clean_json = re.sub(r"//.*?$", "", raw, flags=re.MULTILINE)
            clean_json = re.sub(r"/\*.*?\*/", "", clean_json, flags=re.DOTALL)
            clean_json = re.sub(r",\s*([}\]])", r"\1", clean_json)
            data = json.loads(clean_json)
        except Exception:
            continue

        base_dir = cfg_path.parent
        try:
            rel_base = base_dir.relative_to(project_root).as_posix()
        except ValueError:
            rel_base = ""
        rel_base_prefix = "" if rel_base in ("", ".") else f"{rel_base}/"

        compiler_opts = data.get("compilerOptions", {}) or {}
        base_url = (compiler_opts.get("baseUrl") or ".").strip("./")
        paths_obj = compiler_opts.get("paths", {}) or {}

        for pat, targets in paths_obj.items():
            if not isinstance(targets, list) or not targets:
                continue
            alias_key = pat.rstrip("*")
            target_val = str(targets[0]).lstrip("./").rstrip("*")
            full_target = "/".join(p for p in (rel_base_prefix.rstrip("/"), base_url, target_val.rstrip("/")) if p)
            if alias_key:
                aliases[alias_key] = f"{full_target}/" if full_target else ""

    return aliases


def parse_go_modules(project_root: Path) -> dict[str, str]:
    """
    Scan for `go.mod` in `project_root` or immediate subdirectories (like `server/go.mod`).
    Returns `{module_name: relative_dir_prefix}` (e.g. `{"pixelko/server": "server/"}`).
    """
    go_mods: dict[str, str] = {}
    candidates = [project_root / "go.mod"]
    try:
        for child in project_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                gm = child / "go.mod"
                if gm.exists():
                    candidates.append(gm)
    except OSError:
        pass

    for gm_path in candidates:
        if not gm_path.exists():
            continue
        try:
            content = gm_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^\s*module\s+([^\s]+)", content, flags=re.MULTILINE)
        if m:
            mod_name = m.group(1).strip()
            try:
                rel_dir = gm_path.parent.relative_to(project_root).as_posix()
            except ValueError:
                rel_dir = ""
            prefix = "" if rel_dir in ("", ".") else f"{rel_dir}/"
            go_mods[mod_name] = prefix

    return go_mods


def resolve_res_path(res_str: str, current_file_rel: str) -> str:
    """Convert a Godot `res://foo/bar.gd` path into a project-relative path based on `current_file_rel`."""
    s = res_str.strip().strip('"').strip("'").lstrip("*")
    if not s.startswith("res://"):
        return s
    sub = s[len("res://") :]
    parts = current_file_rel.split("/")
    if len(parts) > 1 and parts[0] in ("client", "game", "godot", "src"):
        return f"{parts[0]}/{sub}"
    return sub


class ASTAnalyzer:
    """Universal multi-language static analyzer backed by tree-sitter + ecosystem resolvers."""

    def __init__(self) -> None:
        self._languages: dict[str, Language] = {
            "python": Language(tree_sitter_python.language()),
            "javascript": Language(tree_sitter_javascript.language()),
            "typescript": Language(tree_sitter_typescript.language_typescript()),
            "tsx": Language(tree_sitter_typescript.language_tsx()),
            "go": Language(tree_sitter_go.language()),
            "csharp": Language(tree_sitter_c_sharp.language()),
        }
        if tree_sitter_rust is not None:
            self._languages["rust"] = Language(tree_sitter_rust.language())
        if tree_sitter_java is not None:
            self._languages["java"] = Language(tree_sitter_java.language())

        self._parsers: dict[str, Parser] = {
            name: _make_parser(lang) for name, lang in self._languages.items()
        }

    @staticmethod
    def compute_hash(content: bytes) -> str:
        """Compute SHA1 hash of file bytes for incremental indexing."""
        return hashlib.sha1(content).hexdigest()

    @staticmethod
    def detect_language(file_path: str | Path) -> str | None:
        ext = Path(file_path).suffix.lower()
        return EXTENSION_TO_LANGUAGE.get(ext)

    def parse_file(self, full_path: Path, rel_path: str, mtime: float = 0.0) -> ParseResult | None:
        """Parse a single file into GraphNodes and GraphEdges."""
        lang = self.detect_language(full_path)
        if not lang:
            return None

        try:
            source_bytes = full_path.read_bytes()
            if mtime == 0.0:
                mtime = full_path.stat().st_mtime
        except OSError:
            return None

        file_hash = self.compute_hash(source_bytes)
        result = ParseResult(
            file_path=rel_path,
            file_hash=file_hash,
            language=lang,
            mtime=mtime,
        )

        file_node_id = f"{rel_path}::<file>"
        module_name = Path(rel_path).stem
        line_count = source_bytes.count(b"\n") + 1
        result.nodes.append(
            GraphNode(
                id=file_node_id,
                name=module_name,
                qualified_name=rel_path,
                kind="file",
                file_path=rel_path,
                start_line=1,
                end_line=line_count,
                signature=f"file {rel_path} ({line_count} lines)",
                language=lang,
            )
        )

        source_text = source_bytes.decode("utf-8", errors="replace")

        if lang == "json":
            self._parse_json_file(source_text, rel_path, file_node_id, result)
            return result

        if lang == "po":
            self._parse_po_file(source_text, rel_path, file_node_id, result)
            return result

        if lang == "gdscript":
            self._parse_gdscript(source_text, rel_path, file_node_id, result)
            self._enrich_comments_and_contracts(source_text, rel_path, lang, file_node_id, result)
            return result

        if lang == "tscn":
            self._parse_tscn(source_text, rel_path, file_node_id, result)
            return result

        parser = self._parsers.get(lang)
        if not parser:
            return None

        tree = parser.parse(source_bytes)
        root = tree.root_node

        if lang == "python":
            self._walk_python(root, source_bytes, rel_path, file_node_id, result)
        elif lang in ("javascript", "typescript", "tsx"):
            self._walk_js_ts(root, source_bytes, rel_path, file_node_id, lang, result)
        elif lang == "go":
            self._walk_go(root, source_bytes, rel_path, file_node_id, result)
        elif lang == "rust":
            self._walk_rust(root, source_bytes, rel_path, file_node_id, result)
        elif lang == "csharp":
            self._walk_csharp(root, source_bytes, rel_path, file_node_id, result)
        elif lang == "java":
            self._walk_java(root, source_bytes, rel_path, file_node_id, result)

        self._enrich_comments_and_contracts(source_text, rel_path, lang, file_node_id, result)
        return result

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _text(node: Node | None, source: bytes) -> str:
        if node is None:
            return ""
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()

    @staticmethod
    def _first_line_signature(node: Node, source: bytes, max_len: int = 140) -> str:
        raw = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        first_line = raw.splitlines()[0].strip() if raw else ""
        first_line = re.sub(r"\s+", " ", first_line)
        if len(first_line) > max_len:
            first_line = first_line[: max_len - 3] + "..."
        return first_line

    @staticmethod
    def _clean_string_literal(raw: str) -> str:
        s = raw.strip()
        if len(s) >= 2 and s[0] in ('"', "'", "`") and s[-1] == s[0]:
            return s[1:-1].strip()
        if len(s) >= 3 and s[0] in ("f", "r", "b", "@") and s[1] in ('"', "'") and s[-1] == s[1]:
            return s[2:-1].strip()
        return s

    @staticmethod
    def _extract_callable_handler(raw_arg: str) -> str:
        s = raw_arg.strip()
        m_call = re.match(r"^Callable\s*\(\s*[^,]+\s*,\s*(['\"][A-Za-z0-9_]+['\"])\s*\)$", s)
        if m_call:
            return m_call.group(1).strip("'\"")
        if any(tok in s for tok in ("(", ")", "=>", "lambda", "function", "{", " ")):
            return ""
        short = s.split(".")[-1].split("::")[-1].strip("'\"")
        if short in ("self", "this", "null", "nil", "None", "true", "false"):
            return ""
        return short

    def _ensure_event_node(self, event_name: str, rel_path: str, line: int, result: ParseResult) -> str:
        clean_name = self._clean_string_literal(event_name)
        if not clean_name or len(clean_name) > 80:
            clean_name = "unknown_event"
        event_id = f"event::{clean_name}"
        if not any(n.id == event_id for n in result.nodes):
            result.nodes.append(
                GraphNode(
                    id=event_id,
                    name=clean_name,
                    qualified_name=f"event::{clean_name}",
                    kind="event",
                    file_path=rel_path,
                    start_line=line,
                    end_line=line,
                    signature=f"event {clean_name}",
                    language=result.language,
                )
            )
        return event_id

    def _record_call_or_event(
        self,
        scope_id: str,
        callee_full: str,
        callee_short: str,
        receiver_name: str,
        args_texts: list[str],
        rel_path: str,
        line: int,
        result: ParseResult,
        known_signals: set[str] | None = None,
    ) -> None:
        if not callee_short or callee_full in IGNORED_CALLS or callee_short in IGNORED_CALLS:
            return

        method_lower = callee_short.lower()

        # 1. Event publishing patterns
        if method_lower in PUBLISH_METHODS:
            event_name = ""
            recv_tail = receiver_name.split(".")[-1] if receiver_name else ""
            if method_lower == "emit" and recv_tail and (
                (known_signals and recv_tail in known_signals)
                or (recv_tail not in ("bus", "event_bus", "events", "emitter", "self", "this", "socket", "io", "Game", "EventBus"))
            ):
                first_is_str = bool(args_texts and len(args_texts[0]) >= 2 and args_texts[0][0] in ('"', "'", "`"))
                if (known_signals and recv_tail in known_signals) or not first_is_str:
                    event_name = recv_tail

            if not event_name and args_texts:
                first_arg = args_texts[0]
                m = re.match(r"^(?:new\s+)?[A-Za-z0-9_.]+\(\s*(['\"`][^'\"`]+['\"`])", first_arg)
                if m:
                    event_name = self._clean_string_literal(m.group(1))
                else:
                    event_name = self._clean_string_literal(first_arg.split(",")[0])
            elif not event_name and recv_tail:
                event_name = recv_tail

            if event_name and re.match(r"^[A-Za-z0-9_:.-]+$", event_name):
                event_id = self._ensure_event_node(event_name, rel_path, line, result)
                result.edges.append(
                    GraphEdge(
                        source_id=scope_id,
                        target_name=event_name,
                        receiver=receiver_name,
                        target_id=event_id,
                        edge_type="publishes_event",
                        file_path=rel_path,
                        line=line,
                        confidence="exact",
                    )
                )
                return

        # 2. Event subscription patterns
        if method_lower in SUBSCRIBE_METHODS:
            event_name = ""
            handler_name = ""
            recv_tail = receiver_name.split(".")[-1] if receiver_name else ""

            if method_lower == "connect":
                first_is_str = bool(args_texts and len(args_texts[0]) >= 2 and args_texts[0][0] in ('"', "'", "`"))
                if first_is_str and args_texts:
                    event_name = self._clean_string_literal(args_texts[0])
                    if len(args_texts) >= 3 and args_texts[2] and args_texts[2][0] in ('"', "'"):
                        handler_name = self._clean_string_literal(args_texts[2])
                    elif len(args_texts) >= 2:
                        handler_name = self._extract_callable_handler(args_texts[1])
                elif recv_tail and args_texts:
                    candidate_handler = self._extract_callable_handler(args_texts[0])
                    if candidate_handler:
                        event_name = recv_tail
                        handler_name = candidate_handler
            elif args_texts:
                event_name = self._clean_string_literal(args_texts[0])
                if len(args_texts) >= 2:
                    handler_name = self._extract_callable_handler(args_texts[1])

            if event_name and re.match(r"^[A-Za-z0-9_:.-]+$", event_name):
                event_id = self._ensure_event_node(event_name, rel_path, line, result)
                result.edges.append(
                    GraphEdge(
                        source_id=scope_id,
                        target_name=event_name,
                        receiver=receiver_name,
                        target_id=event_id,
                        edge_type="listens_to",
                        file_path=rel_path,
                        line=line,
                        confidence="exact",
                    )
                )
                if handler_name:
                    result.edges.append(
                        GraphEdge(
                            source_id=scope_id,
                            target_name=handler_name,
                            receiver="self",
                            edge_type="calls",
                            file_path=rel_path,
                            line=line,
                            confidence="exact",
                        )
                    )
                    result.edges.append(
                        GraphEdge(
                            source_id=f"ref::{handler_name}",
                            target_name=event_name,
                            receiver=receiver_name,
                            target_id=event_id,
                            edge_type="listens_to",
                            file_path=rel_path,
                            line=line,
                            confidence="exact",
                        )
                    )
                return

        # 3. Signal creation
        if callee_short in SIGNAL_KEYWORDS and args_texts:
            event_name = self._clean_string_literal(args_texts[0])
            if event_name and re.match(r"^[A-Za-z0-9_:.-]+$", event_name):
                self._ensure_event_node(event_name, rel_path, line, result)

        # 3b. Translation / i18n calls (`tr("key")`, `noticeT(client, "n.x", a, b)`, `i18n.T(...)`)
        if (callee_short in I18N_KEY_FIRST_CALLS or callee_short in I18N_CTX_FIRST_CALLS) and args_texts:
            key_idx = -1
            if callee_short in I18N_KEY_FIRST_CALLS:
                if args_texts[0].strip().startswith(('"', "'", "`")):
                    key_idx = 0
            else:
                for idx_a, raw_a in enumerate(args_texts[:3]):
                    if raw_a.strip().startswith(('"', "'", "`")):
                        key_idx = idx_a
                        break
            if key_idx >= 0:
                i18n_key = self._clean_string_literal(args_texts[key_idx])
                if i18n_key and len(i18n_key) <= 100:
                    fmt_args_count = max(0, len(args_texts) - (key_idx + 1))
                    i18n_node_id = f"i18n::{i18n_key}"
                    if not any(n.id == i18n_node_id for n in result.nodes):
                        result.nodes.append(
                            GraphNode(
                                id=i18n_node_id,
                                name=i18n_key,
                                qualified_name=f"i18n::{i18n_key}",
                                kind="i18n",
                                file_path=rel_path,
                                start_line=line,
                                end_line=line,
                                signature=f"i18n key '{i18n_key}'",
                                language="i18n",
                            )
                        )
                    result.edges.append(
                        GraphEdge(
                            source_id=scope_id,
                            target_name=i18n_key,
                            receiver=f"args:{fmt_args_count}",
                            target_id=i18n_node_id,
                            edge_type="uses_i18n",
                            file_path=rel_path,
                            line=line,
                            confidence="exact",
                        )
                    )

        # 4. Regular function/method call (with language-specific stdlib/builtin filtering)
        lang = result.language
        if callee_short in LANGUAGE_IGNORED_CALLS.get(lang, ()) or callee_full in LANGUAGE_IGNORED_CALLS.get(lang, ()):
            return

        if receiver_name and receiver_name not in result.aliases:
            recv_root = receiver_name.split(".")[0].split("::")[0].lstrip("$%@&*")
            if recv_root in LANGUAGE_STDLIB_RECEIVERS.get(lang, ()):
                return

        resolved_receiver = result.aliases.get(receiver_name, receiver_name)
        result.edges.append(
            GraphEdge(
                source_id=scope_id,
                target_name=callee_short,
                receiver=resolved_receiver,
                edge_type="calls",
                file_path=rel_path,
                line=line,
            )
        )

    # -------------------------------------------------------------------------
    # Python AST Walk
    # -------------------------------------------------------------------------

    def _walk_python(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_class: str | None) -> None:
            ntype = node.type

            if ntype == "decorated_definition":
                decorators: list[Node] = [c for c in node.children if c.type == "decorator"]
                inner_def = node.child_by_field_name("definition")
                if inner_def:
                    def_name_node = inner_def.child_by_field_name("name")
                    def_name = self._text(def_name_node, source)
                    qual_name = f"{current_class}.{def_name}" if current_class else def_name
                    target_scope_id = f"{rel_path}::{qual_name}"
                    for dec in decorators:
                        self._extract_python_decorator_event(dec, source, target_scope_id, rel_path, result)
                    visit(inner_def, current_scope_id, current_class)
                return

            if ntype == "class_definition":
                name_node = node.child_by_field_name("name")
                cls_name = self._text(name_node, source)
                if cls_name:
                    qual_name = f"{current_class}.{cls_name}" if current_class else cls_name
                    node_id = f"{rel_path}::{qual_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=cls_name,
                            qualified_name=qual_name,
                            kind="class",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="python",
                        )
                    )
                    superclasses = node.child_by_field_name("superclasses")
                    if superclasses:
                        for arg in superclasses.named_children:
                            base_name = self._text(arg, source).split(".")[-1].split("[")[0]
                            if base_name and base_name not in ("object", "ABC", "BaseModel", "Enum"):
                                result.edges.append(
                                    GraphEdge(
                                        source_id=node_id,
                                        target_name=base_name,
                                        edge_type="inherits",
                                        file_path=rel_path,
                                        line=node.start_point[0] + 1,
                                    )
                                )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, qual_name)
                    return

            if ntype == "function_definition":
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    qual_name = f"{current_class}.{fn_name}" if current_class else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    kind = "method" if current_class else "function"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind=kind,
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="python",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, current_class)
                    return

            if ntype == "import_statement":
                for child in node.named_children:
                    raw_imp = self._text(child, source)
                    parts = raw_imp.split(" as ")
                    imp_text = parts[0].strip()
                    short_imp = imp_text.split(".")[-1]
                    if len(parts) == 2:
                        result.aliases[parts[1].strip()] = short_imp
                    if short_imp:
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=short_imp,
                                receiver=imp_text,
                                edge_type="imports",
                                file_path=rel_path,
                                line=node.start_point[0] + 1,
                            )
                        )
                return

            if ntype == "import_from_statement":
                mod_node = node.child_by_field_name("module_name")
                mod_name = self._text(mod_node, source)
                # Resolve relative Python import (e.g. `from .db import CodeGraphDB`) to file path
                resolved_mod_rel = ""
                raw_stmt = self._text(node, source)
                m_rel = re.match(r"^from\s+(\.+)([A-Za-z0-9_.]*)\s+import", raw_stmt)
                if m_rel:
                    dots = len(m_rel.group(1))
                    sub_mod = m_rel.group(2).replace(".", "/")
                    parent_dir = Path(rel_path).parent
                    for _ in range(dots - 1):
                        parent_dir = parent_dir.parent
                    if sub_mod:
                        cand_py = (parent_dir / f"{sub_mod}.py").as_posix().lstrip("./")
                        resolved_mod_rel = cand_py

                for child in node.named_children:
                    if child == mod_node:
                        continue
                    raw_sym = self._text(child, source)
                    parts = raw_sym.split(" as ")
                    sym = parts[0].strip()
                    if len(parts) == 2:
                        result.aliases[parts[1].strip()] = sym
                    if sym and sym != "*":
                        if resolved_mod_rel:
                            result.aliases[sym] = resolved_mod_rel
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=sym,
                                receiver=resolved_mod_rel or mod_name,
                                edge_type="imports",
                                file_path=rel_path,
                                line=node.start_point[0] + 1,
                            )
                        )
                if mod_name:
                    result.edges.append(
                        GraphEdge(
                            source_id=current_scope_id,
                            target_name=mod_name.split(".")[-1],
                            receiver=resolved_mod_rel or mod_name,
                            edge_type="imports",
                            file_path=rel_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                return

            if ntype == "call":
                fn_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node:
                    callee_full = self._text(fn_node, source)
                    if fn_node.type == "attribute":
                        obj_node = fn_node.child_by_field_name("object")
                        attr_node = fn_node.child_by_field_name("attribute")
                        receiver = self._text(obj_node, source)
                        callee_short = self._text(attr_node, source)
                    else:
                        receiver = ""
                        callee_short = callee_full.split(".")[-1]

                    args_texts: list[str] = []
                    if args_node:
                        for arg in args_node.named_children:
                            if arg.type == "keyword_argument":
                                val = arg.child_by_field_name("value")
                                args_texts.append(self._text(val, source))
                            else:
                                args_texts.append(self._text(arg, source))

                    self._record_call_or_event(
                        scope_id=current_scope_id,
                        callee_full=callee_full,
                        callee_short=callee_short,
                        receiver_name=receiver,
                        args_texts=args_texts,
                        rel_path=rel_path,
                        line=node.start_point[0] + 1,
                        result=result,
                    )

            for child in node.children:
                visit(child, current_scope_id, current_class)

        visit(root, file_node_id, None)

    def _extract_python_decorator_event(
        self,
        dec_node: Node,
        source: bytes,
        target_scope_id: str,
        rel_path: str,
        result: ParseResult,
    ) -> None:
        for child in dec_node.named_children:
            if child.type == "call":
                fn_node = child.child_by_field_name("function")
                args_node = child.child_by_field_name("arguments")
                fn_text = self._text(fn_node, source)
                short_fn = fn_text.split(".")[-1].lower()
                if short_fn in SUBSCRIBE_METHODS or "event" in short_fn or "listen" in short_fn:
                    if args_node and args_node.named_children:
                        first_arg = self._text(args_node.named_children[0], source)
                        event_name = self._clean_string_literal(first_arg)
                        if event_name:
                            line = dec_node.start_point[0] + 1
                            event_id = self._ensure_event_node(event_name, rel_path, line, result)
                            result.edges.append(
                                GraphEdge(
                                    source_id=target_scope_id,
                                    target_name=event_name,
                                    target_id=event_id,
                                    edge_type="listens_to",
                                    file_path=rel_path,
                                    line=line,
                                    confidence="exact",
                                )
                            )

    # -------------------------------------------------------------------------
    # JavaScript / TypeScript / TSX AST Walk
    # -------------------------------------------------------------------------

    def _walk_js_ts(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        lang: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_class: str | None) -> None:
            ntype = node.type

            if ntype in ("class_declaration", "interface_declaration"):
                name_node = node.child_by_field_name("name")
                cls_name = self._text(name_node, source)
                if cls_name:
                    node_id = f"{rel_path}::{cls_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=cls_name,
                            qualified_name=cls_name,
                            kind="class",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language=lang,
                        )
                    )
                    # Check extends / implements
                    for child in node.children:
                        if child.type == "class_heritage":
                            for h_child in child.named_children:
                                base_txt = self._text(h_child, source)
                                for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", base_txt):
                                    result.edges.append(
                                        GraphEdge(
                                            source_id=node_id,
                                            target_name=tok,
                                            edge_type="inherits",
                                            file_path=rel_path,
                                            line=node.start_point[0] + 1,
                                        )
                                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, cls_name)
                    return

            if ntype in ("function_declaration", "generator_function_declaration", "method_definition"):
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    qual_name = f"{current_class}.{fn_name}" if current_class else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    kind = "method" if current_class or ntype == "method_definition" else "function"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind=kind,
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language=lang,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, current_class)
                    return

            if ntype in ("variable_declarator", "public_field_definition", "field_definition"):
                name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
                val_node = node.child_by_field_name("value")
                if name_node and val_node and val_node.type in ("arrow_function", "function_expression", "function"):
                    fn_name = self._text(name_node, source)
                    if fn_name:
                        qual_name = f"{current_class}.{fn_name}" if current_class else fn_name
                        node_id = f"{rel_path}::{qual_name}"
                        kind = "method" if current_class else "function"
                        sig = self._first_line_signature(node, source)
                        result.nodes.append(
                            GraphNode(
                                id=node_id,
                                name=fn_name,
                                qualified_name=qual_name,
                                kind=kind,
                                file_path=rel_path,
                                start_line=node.start_point[0] + 1,
                                end_line=node.end_point[0] + 1,
                                signature=sig,
                                language=lang,
                            )
                        )
                        for child in val_node.children:
                            visit(child, node_id, current_class)
                        return

            if ntype == "import_statement":
                src_node = node.child_by_field_name("source")
                mod_str = ""
                mod_short = ""
                if src_node:
                    mod_str = self._clean_string_literal(self._text(src_node, source))
                    mod_short = Path(mod_str).stem if mod_str else ""
                    if mod_short:
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=mod_short,
                                receiver=mod_str,
                                edge_type="imports",
                                file_path=rel_path,
                                line=node.start_point[0] + 1,
                            )
                        )
                for child in node.named_children:
                    if child.type == "import_clause":
                        for sub in child.named_children:
                            if sub.type == "identifier":
                                sym = self._text(sub, source)
                                if mod_str:
                                    result.aliases[sym] = mod_str
                                result.edges.append(
                                    GraphEdge(
                                        source_id=current_scope_id,
                                        target_name=sym,
                                        receiver=mod_str or mod_short,
                                        edge_type="imports",
                                        file_path=rel_path,
                                        line=node.start_point[0] + 1,
                                    )
                                )
                            elif sub.type == "named_imports":
                                for spec in sub.named_children:
                                    if spec.type == "import_specifier":
                                        orig = spec.child_by_field_name("name")
                                        alias_n = spec.child_by_field_name("alias")
                                        if orig:
                                            sym = self._text(orig, source)
                                            local_sym = self._text(alias_n, source) if alias_n else sym
                                            if mod_str:
                                                result.aliases[local_sym] = mod_str
                                            result.edges.append(
                                                GraphEdge(
                                                    source_id=current_scope_id,
                                                    target_name=sym,
                                                    receiver=mod_str or mod_short,
                                                    edge_type="imports",
                                                    file_path=rel_path,
                                                    line=node.start_point[0] + 1,
                                                )
                                            )
                return

            if ntype in ("call_expression", "new_expression"):
                fn_node = node.child_by_field_name("function") or node.child_by_field_name("constructor")
                args_node = node.child_by_field_name("arguments")
                if fn_node:
                    callee_full = self._text(fn_node, source)
                    if fn_node.type == "member_expression":
                        obj_node = fn_node.child_by_field_name("object")
                        prop_node = fn_node.child_by_field_name("property")
                        receiver = self._text(obj_node, source)
                        callee_short = self._text(prop_node, source)
                    else:
                        receiver = ""
                        callee_short = callee_full.split(".")[-1]

                    args_texts = [self._text(a, source) for a in args_node.named_children] if args_node else []

                    if callee_short == "require" and args_texts:
                        req_mod = self._clean_string_literal(args_texts[0])
                        req_short = Path(req_mod).stem if req_mod else ""
                        if req_short:
                            result.edges.append(
                                GraphEdge(
                                    source_id=current_scope_id,
                                    target_name=req_short,
                                    receiver=req_mod,
                                    edge_type="imports",
                                    file_path=rel_path,
                                    line=node.start_point[0] + 1,
                                )
                            )
                    else:
                        self._record_call_or_event(
                            scope_id=current_scope_id,
                            callee_full=callee_full,
                            callee_short=callee_short,
                            receiver_name=receiver,
                            args_texts=args_texts,
                            rel_path=rel_path,
                            line=node.start_point[0] + 1,
                            result=result,
                        )

            for child in node.children:
                visit(child, current_scope_id, current_class)

        visit(root, file_node_id, None)

    # -------------------------------------------------------------------------
    # Go AST Walk
    # -------------------------------------------------------------------------

    def _walk_go(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_recv_type: str = "") -> None:
            ntype = node.type

            if ntype == "type_declaration":
                for child in node.named_children:
                    if child.type == "type_spec":
                        name_node = child.child_by_field_name("name")
                        type_name = self._text(name_node, source)
                        if type_name:
                            node_id = f"{rel_path}::{type_name}"
                            sig = self._first_line_signature(child, source)
                            result.nodes.append(
                                GraphNode(
                                    id=node_id,
                                    name=type_name,
                                    qualified_name=type_name,
                                    kind="class",
                                    file_path=rel_path,
                                    start_line=child.start_point[0] + 1,
                                    end_line=child.end_point[0] + 1,
                                    signature=f"type {sig}",
                                    language="go",
                                )
                            )
                return

            if ntype == "function_declaration":
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    node_id = f"{rel_path}::{fn_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=fn_name,
                            kind="function",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="go",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, "")
                    return

            if ntype == "method_declaration":
                name_node = node.child_by_field_name("name")
                recv_node = node.child_by_field_name("receiver")
                fn_name = self._text(name_node, source)
                recv_type = ""
                recv_var = ""
                if recv_node:
                    recv_text = self._text(recv_node, source).strip("()")
                    parts = recv_text.split()
                    if len(parts) >= 2:
                        recv_var = parts[0]
                        recv_type = parts[-1].lstrip("*")
                    elif len(parts) == 1:
                        recv_type = parts[0].lstrip("*")
                if fn_name:
                    qual_name = f"{recv_type}.{fn_name}" if recv_type else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    sig = self._first_line_signature(node, source)
                    if recv_var and recv_type:
                        result.aliases[recv_var] = recv_type
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind="method",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="go",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, recv_type)
                    return

            if ntype == "import_spec":
                path_node = node.child_by_field_name("path")
                name_node = node.child_by_field_name("name")
                if path_node:
                    imp_path = self._clean_string_literal(self._text(path_node, source))
                    pkg_name = imp_path.split("/")[-1]
                    if name_node:
                        alias = self._text(name_node, source)
                        if alias and alias not in (".", "_"):
                            result.aliases[alias] = imp_path
                    elif pkg_name:
                        result.aliases[pkg_name] = imp_path
                    if pkg_name:
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=pkg_name,
                                receiver=imp_path,
                                edge_type="imports",
                                file_path=rel_path,
                                line=node.start_point[0] + 1,
                            )
                        )
                return

            if ntype == "call_expression":
                fn_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node:
                    callee_full = self._text(fn_node, source)
                    if fn_node.type == "selector_expression":
                        operand = fn_node.child_by_field_name("operand")
                        field_node = fn_node.child_by_field_name("field")
                        receiver = self._text(operand, source)
                        callee_short = self._text(field_node, source)
                        if receiver in result.aliases:
                            receiver = result.aliases[receiver]
                    else:
                        receiver = ""
                        callee_short = callee_full.split(".")[-1]

                    args_texts = [self._text(a, source) for a in args_node.named_children] if args_node else []
                    self._record_call_or_event(
                        scope_id=current_scope_id,
                        callee_full=callee_full,
                        callee_short=callee_short,
                        receiver_name=receiver,
                        args_texts=args_texts,
                        rel_path=rel_path,
                        line=node.start_point[0] + 1,
                        result=result,
                    )

            for child in node.children:
                visit(child, current_scope_id, current_recv_type)

        visit(root, file_node_id)

    # -------------------------------------------------------------------------
    # Rust AST Walk (`struct`, `enum`, `trait`, `impl Type`, `fn`, `use`)
    # -------------------------------------------------------------------------

    def _walk_rust(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_impl_type: str | None) -> None:
            ntype = node.type

            if ntype in ("struct_item", "enum_item", "trait_item"):
                name_node = node.child_by_field_name("name")
                type_name = self._text(name_node, source)
                if type_name:
                    node_id = f"{rel_path}::{type_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=type_name,
                            qualified_name=type_name,
                            kind="class",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="rust",
                        )
                    )
                return

            if ntype == "impl_item":
                type_node = node.child_by_field_name("type")
                trait_node = node.child_by_field_name("trait")
                impl_type = self._text(type_node, source).split("<")[0].strip() if type_node else ""
                if trait_node and impl_type:
                    trait_name = self._text(trait_node, source).split("<")[0].split("::")[-1].strip()
                    if trait_name:
                        result.edges.append(
                            GraphEdge(
                                source_id=f"{rel_path}::{impl_type}",
                                target_name=trait_name,
                                edge_type="inherits",
                                file_path=rel_path,
                                line=node.start_point[0] + 1,
                            )
                        )
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, current_scope_id, impl_type or current_impl_type)
                return

            if ntype == "function_item":
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    qual_name = f"{current_impl_type}.{fn_name}" if current_impl_type else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    kind = "method" if current_impl_type else "function"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind=kind,
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="rust",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, current_impl_type)
                    return

            if ntype == "use_declaration":
                use_txt = self._text(node, source).replace("use ", "").rstrip(";").strip()
                short_sym = use_txt.split("::")[-1].strip("{}")
                if short_sym and short_sym != "*":
                    result.edges.append(
                        GraphEdge(
                            source_id=current_scope_id,
                            target_name=short_sym,
                            receiver=use_txt,
                            edge_type="imports",
                            file_path=rel_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                return

            if ntype == "call_expression":
                fn_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node:
                    callee_full = self._text(fn_node, source)
                    if fn_node.type == "field_expression":
                        val_node = fn_node.child_by_field_name("value")
                        field_n = fn_node.child_by_field_name("field")
                        receiver = self._text(val_node, source)
                        callee_short = self._text(field_n, source)
                    elif "::" in callee_full:
                        parts = callee_full.split("::")
                        receiver = "::".join(parts[:-1])
                        callee_short = parts[-1]
                    else:
                        receiver = ""
                        callee_short = callee_full

                    args_texts = [self._text(a, source) for a in args_node.named_children] if args_node else []
                    self._record_call_or_event(
                        scope_id=current_scope_id,
                        callee_full=callee_full,
                        callee_short=callee_short,
                        receiver_name=receiver,
                        args_texts=args_texts,
                        rel_path=rel_path,
                        line=node.start_point[0] + 1,
                        result=result,
                    )

            for child in node.children:
                visit(child, current_scope_id, current_impl_type)

        visit(root, file_node_id, None)

    # -------------------------------------------------------------------------
    # C# AST Walk
    # -------------------------------------------------------------------------

    def _walk_csharp(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_class: str | None) -> None:
            ntype = node.type

            if ntype in ("class_declaration", "struct_declaration", "interface_declaration"):
                name_node = node.child_by_field_name("name")
                cls_name = self._text(name_node, source)
                if cls_name:
                    node_id = f"{rel_path}::{cls_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=cls_name,
                            qualified_name=cls_name,
                            kind="class",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="csharp",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, cls_name)
                    return

            if ntype in ("method_declaration", "constructor_declaration"):
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    qual_name = f"{current_class}.{fn_name}" if current_class else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind="method",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="csharp",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, current_class)
                    return

            if ntype == "using_directive":
                imp_text = self._text(node, source).replace("using", "").rstrip(";").strip()
                short_imp = imp_text.split(".")[-1]
                if short_imp:
                    result.edges.append(
                        GraphEdge(
                            source_id=current_scope_id,
                            target_name=short_imp,
                            edge_type="imports",
                            file_path=rel_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                return

            if ntype == "invocation_expression":
                fn_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node:
                    callee_full = self._text(fn_node, source)
                    if fn_node.type == "member_access_expression":
                        expr_node = fn_node.child_by_field_name("expression")
                        name_n = fn_node.child_by_field_name("name")
                        receiver = self._text(expr_node, source)
                        callee_short = self._text(name_n, source)
                    else:
                        receiver = ""
                        callee_short = callee_full.split(".")[-1]

                    args_texts = [self._text(a, source) for a in args_node.named_children] if args_node else []
                    self._record_call_or_event(
                        scope_id=current_scope_id,
                        callee_full=callee_full,
                        callee_short=callee_short,
                        receiver_name=receiver,
                        args_texts=args_texts,
                        rel_path=rel_path,
                        line=node.start_point[0] + 1,
                        result=result,
                    )

            for child in node.children:
                visit(child, current_scope_id, current_class)

        visit(root, file_node_id, None)

    # -------------------------------------------------------------------------
    # Java AST Walk
    # -------------------------------------------------------------------------

    def _walk_java(
        self,
        root: Node,
        source: bytes,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        def visit(node: Node, current_scope_id: str, current_class: str | None) -> None:
            ntype = node.type

            if ntype in ("class_declaration", "interface_declaration", "enum_declaration"):
                name_node = node.child_by_field_name("name")
                cls_name = self._text(name_node, source)
                if cls_name:
                    node_id = f"{rel_path}::{cls_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=cls_name,
                            qualified_name=cls_name,
                            kind="class",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="java",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, cls_name)
                    return

            if ntype in ("method_declaration", "constructor_declaration"):
                name_node = node.child_by_field_name("name")
                fn_name = self._text(name_node, source)
                if fn_name:
                    qual_name = f"{current_class}.{fn_name}" if current_class else fn_name
                    node_id = f"{rel_path}::{qual_name}"
                    sig = self._first_line_signature(node, source)
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=fn_name,
                            qualified_name=qual_name,
                            kind="method",
                            file_path=rel_path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            signature=sig,
                            language="java",
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            visit(child, node_id, current_class)
                    return

            if ntype == "import_declaration":
                imp_text = self._text(node, source).replace("import", "").replace("static", "").rstrip(";").strip()
                short_imp = imp_text.split(".")[-1]
                if short_imp and short_imp != "*":
                    result.edges.append(
                        GraphEdge(
                            source_id=current_scope_id,
                            target_name=short_imp,
                            receiver=imp_text,
                            edge_type="imports",
                            file_path=rel_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                return

            if ntype == "method_invocation":
                obj_node = node.child_by_field_name("object")
                name_n = node.child_by_field_name("name")
                args_node = node.child_by_field_name("arguments")
                callee_short = self._text(name_n, source)
                receiver = self._text(obj_node, source) if obj_node else ""
                callee_full = f"{receiver}.{callee_short}" if receiver else callee_short
                args_texts = [self._text(a, source) for a in args_node.named_children] if args_node else []
                self._record_call_or_event(
                    scope_id=current_scope_id,
                    callee_full=callee_full,
                    callee_short=callee_short,
                    receiver_name=receiver,
                    args_texts=args_texts,
                    rel_path=rel_path,
                    line=node.start_point[0] + 1,
                    result=result,
                )

            for child in node.children:
                visit(child, current_scope_id, current_class)

        visit(root, file_node_id, None)

    # -------------------------------------------------------------------------
    # GDScript Support (Godot 3 & 4, Signals, Autoloads, Preload, @rpc)
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_calls_from_gd_line(line: str) -> list[tuple[str, list[str]]]:
        clean_chars: list[str] = []
        in_quote: str | None = None
        for ch in line:
            if in_quote:
                clean_chars.append(ch)
                if ch == in_quote:
                    in_quote = None
            else:
                if ch in ('"', "'"):
                    in_quote = ch
                    clean_chars.append(ch)
                elif ch == "#":
                    break
                else:
                    clean_chars.append(ch)
        code = "".join(clean_chars)

        results: list[tuple[str, list[str]]] = []
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(", code):
            callee = m.group(1)
            if callee in ("func", "if", "elif", "while", "for", "match", "return", "await", "when"):
                continue
            open_paren_idx = m.end() - 1
            depth = 0
            in_str: str | None = None
            curr_arg: list[str] = []
            args: list[str] = []

            for i in range(open_paren_idx, len(code)):
                c = code[i]
                if in_str:
                    curr_arg.append(c)
                    if c == in_str and (i == 0 or code[i - 1] != "\\"):
                        in_str = None
                else:
                    if c in ('"', "'"):
                        in_str = c
                        curr_arg.append(c)
                    elif c == "(":
                        depth += 1
                        if depth > 1:
                            curr_arg.append(c)
                    elif c in ("[", "{"):
                        depth += 1
                        curr_arg.append(c)
                    elif c in ("]", "}"):
                        depth = max(1, depth - 1)
                        curr_arg.append(c)
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            arg_s = "".join(curr_arg).strip()
                            if arg_s:
                                args.append(arg_s)
                            break
                        else:
                            curr_arg.append(c)
                    elif c == "," and depth == 1:
                        args.append("".join(curr_arg).strip())
                        curr_arg = []
                    else:
                        curr_arg.append(c)

            results.append((callee, args))
        return results

    def _parse_gdscript(
        self,
        source_text: str,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        lines = source_text.splitlines()
        stem_name = Path(rel_path).stem
        current_class = stem_name
        declared_signals: set[str] = set()

        explicit_class_line = 1
        has_explicit_class = False

        for idx, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            m_cls = re.match(r"^class_name\s+([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if m_cls:
                current_class = m_cls.group(1)
                explicit_class_line = idx
                has_explicit_class = True
                continue

            m_sig = re.match(r"^signal\s+([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if m_sig:
                sig_name = m_sig.group(1)
                declared_signals.add(sig_name)
                self._ensure_event_node(sig_name, rel_path, idx, result)
                continue

        cls_id = f"{rel_path}::{current_class}"
        result.nodes.append(
            GraphNode(
                id=cls_id,
                name=current_class,
                qualified_name=current_class,
                kind="class",
                file_path=rel_path,
                start_line=explicit_class_line,
                end_line=len(lines),
                signature=f"class_name {current_class}" if has_explicit_class else f"script {rel_path}",
                language="gdscript",
            )
        )

        current_scope_id = cls_id
        active_fn_node: GraphNode | None = None
        active_fn_indent: int = 0
        pending_rpc: bool = False

        for idx, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(raw_line) - len(raw_line.lstrip(" \t"))

            if active_fn_node is not None and indent <= active_fn_indent and not stripped.startswith("@"):
                active_fn_node = None
                current_scope_id = cls_id

            if stripped.startswith("@rpc"):
                pending_rpc = True
                if "(" not in stripped:
                    continue

            m_ext = re.match(r"^extends\s+(['\"][^'\"]+['\"]|[A-Za-z_][A-Za-z0-9_]*)", stripped)
            if m_ext:
                raw_base = m_ext.group(1)
                if raw_base.startswith(('"', "'")):
                    resolved_res = resolve_res_path(raw_base, rel_path)
                    base_stem = Path(resolved_res).stem
                    result.edges.append(
                        GraphEdge(
                            source_id=cls_id,
                            target_name=base_stem,
                            receiver=resolved_res,
                            edge_type="inherits",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )
                else:
                    if raw_base not in (
                        "Node", "Node2D", "Node3D", "Control", "CanvasItem", "Resource", "RefCounted",
                        "CharacterBody2D", "CharacterBody3D", "RigidBody2D", "StaticBody2D", "Area2D",
                        "Panel", "PanelContainer", "MarginContainer", "VBoxContainer", "HBoxContainer",
                        "Button", "Label", "LineEdit", "RichTextLabel", "TextureRect", "Window",
                        "AcceptDialog", "ConfirmationDialog", "SubViewport", "Camera2D", "Sprite2D",
                        "AnimatedSprite2D", "TileMap", "TileMapLayer", "AudioStreamPlayer", "HTTPRequest",
                        "SceneTree", "MainLoop",
                    ):
                        result.edges.append(
                            GraphEdge(
                                source_id=cls_id,
                                target_name=raw_base,
                                edge_type="inherits",
                                file_path=rel_path,
                                line=idx,
                                confidence="scoped",
                            )
                        )
                continue

            m_alias = re.match(
                r"^(?:const|var|@onready\s+var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*(?:preload|load)\(\s*(['\"][^'\"]+['\"])\s*\)",
                stripped,
            )
            if m_alias:
                alias_name = m_alias.group(1)
                res_target = resolve_res_path(m_alias.group(2), rel_path)
                result.aliases[alias_name] = res_target
                result.edges.append(
                    GraphEdge(
                        source_id=current_scope_id,
                        target_name=Path(res_target).stem,
                        receiver=res_target,
                        edge_type="imports",
                        file_path=rel_path,
                        line=idx,
                        confidence="exact",
                    )
                )
                continue

            m_fn = re.match(r"^(?:static\s+)?func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
            if m_fn:
                fn_name = m_fn.group(1)
                qual = f"{current_class}.{fn_name}"
                fn_id = f"{rel_path}::{qual}"
                sig = f"@rpc {stripped}" if pending_rpc else stripped
                fn_node = GraphNode(
                    id=fn_id,
                    name=fn_name,
                    qualified_name=qual,
                    kind="method",
                    file_path=rel_path,
                    start_line=idx,
                    end_line=idx,
                    signature=sig,
                    language="gdscript",
                )
                result.nodes.append(fn_node)
                if pending_rpc:
                    evt_id = self._ensure_event_node(f"rpc:{fn_name}", rel_path, idx, result)
                    result.edges.append(
                        GraphEdge(
                            source_id=fn_id,
                            target_name=f"rpc:{fn_name}",
                            target_id=evt_id,
                            edge_type="listens_to",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )
                pending_rpc = False
                active_fn_node = fn_node
                active_fn_indent = indent
                current_scope_id = fn_id
                continue

            if active_fn_node is not None:
                active_fn_node.end_line = idx

            for full_callee, args_raw in self._extract_calls_from_gd_line(stripped):
                if full_callee in ("preload", "load"):
                    if args_raw:
                        res_target = resolve_res_path(args_raw[0], rel_path)
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=Path(res_target).stem,
                                receiver=res_target,
                                edge_type="imports",
                                file_path=rel_path,
                                line=idx,
                                confidence="exact",
                            )
                        )
                    continue

                parts = full_callee.split(".")
                short_callee = parts[-1]
                receiver = ".".join(parts[:-1]) if len(parts) > 1 else ""

                if short_callee in ("rpc", "rpc_id"):
                    if receiver and receiver not in ("self", "multiplayer"):
                        rpc_fn = receiver.split(".")[-1]
                        evt_id = self._ensure_event_node(f"rpc:{rpc_fn}", rel_path, idx, result)
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=f"rpc:{rpc_fn}",
                                target_id=evt_id,
                                edge_type="publishes_event",
                                file_path=rel_path,
                                line=idx,
                                confidence="exact",
                            )
                        )
                        result.edges.append(
                            GraphEdge(
                                source_id=current_scope_id,
                                target_name=rpc_fn,
                                receiver=receiver,
                                edge_type="calls",
                                file_path=rel_path,
                                line=idx,
                                confidence="scoped",
                            )
                        )
                        continue
                    elif args_raw:
                        arg_idx = 0 if short_callee == "rpc" else (1 if len(args_raw) > 1 else 0)
                        rpc_target = self._clean_string_literal(args_raw[arg_idx])
                        if rpc_target and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", rpc_target):
                            evt_id = self._ensure_event_node(f"rpc:{rpc_target}", rel_path, idx, result)
                            result.edges.append(
                                GraphEdge(
                                    source_id=current_scope_id,
                                    target_name=f"rpc:{rpc_target}",
                                    target_id=evt_id,
                                    edge_type="publishes_event",
                                    file_path=rel_path,
                                    line=idx,
                                    confidence="exact",
                                )
                            )
                            result.edges.append(
                                GraphEdge(
                                    source_id=current_scope_id,
                                    target_name=rpc_target,
                                    edge_type="calls",
                                    file_path=rel_path,
                                    line=idx,
                                    confidence="scoped",
                                )
                            )
                            continue

                self._record_call_or_event(
                    scope_id=current_scope_id,
                    callee_full=full_callee,
                    callee_short=short_callee,
                    receiver_name=receiver,
                    args_texts=args_raw,
                    rel_path=rel_path,
                    line=idx,
                    result=result,
                    known_signals=declared_signals,
                )

    # -------------------------------------------------------------------------
    # Godot .tscn Scene File Parser
    # -------------------------------------------------------------------------

    def _parse_tscn(
        self,
        source_text: str,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        lines = source_text.splitlines()
        ext_scripts: dict[str, str] = {}
        node_scripts: dict[str, str] = {}
        root_script_rel: str = ""
        current_node_path: str | None = None

        for idx, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue

            if line.startswith("[ext_resource") and 'type="Script"' in line:
                m_path = re.search(r'path="([^"]+)"', line)
                m_id = re.search(r'id="([^"]+)"', line)
                if m_path and m_id:
                    resolved = resolve_res_path(m_path.group(1), rel_path)
                    ext_scripts[m_id.group(1)] = resolved
                    result.edges.append(
                        GraphEdge(
                            source_id=file_node_id,
                            target_name=Path(resolved).stem,
                            receiver=resolved,
                            edge_type="imports",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )
                continue

            if line.startswith("[node "):
                m_name = re.search(r'name="([^"]+)"', line)
                m_parent = re.search(r'parent="([^"]+)"', line)
                if m_name:
                    nname = m_name.group(1)
                    if not m_parent:
                        current_node_path = "."
                    elif m_parent.group(1) == ".":
                        current_node_path = nname
                    else:
                        current_node_path = f"{m_parent.group(1)}/{nname}"
                continue

            if current_node_path is not None and line.startswith("script"):
                m_ext = re.search(r'ExtResource\(\s*"([^"]+)"\s*\)', line)
                if m_ext and m_ext.group(1) in ext_scripts:
                    scr = ext_scripts[m_ext.group(1)]
                    node_scripts[current_node_path] = scr
                    if current_node_path == ".":
                        root_script_rel = scr
                continue

            if line.startswith("[connection "):
                current_node_path = None
                m_sig = re.search(r'signal="([^"]+)"', line)
                m_from = re.search(r'from="([^"]+)"', line)
                m_to = re.search(r'to="([^"]+)"', line)
                m_method = re.search(r'method="([^"]+)"', line)

                if m_sig and m_method:
                    sig_name = m_sig.group(1)
                    from_node = m_from.group(1) if m_from else "."
                    to_node = m_to.group(1) if m_to else "."
                    method_name = m_method.group(1)

                    target_script = node_scripts.get(to_node) or root_script_rel
                    scene_stem = Path(rel_path).stem
                    conn_node_id = f"{rel_path}::{scene_stem}.{from_node}"

                    if not any(n.id == conn_node_id for n in result.nodes):
                        result.nodes.append(
                            GraphNode(
                                id=conn_node_id,
                                name=f"{scene_stem}:{from_node}",
                                qualified_name=f"{scene_stem}.{from_node}",
                                kind="class",
                                file_path=rel_path,
                                start_line=idx,
                                end_line=idx,
                                signature=line,
                                language="tscn",
                            )
                        )

                    result.edges.append(
                        GraphEdge(
                            source_id=conn_node_id,
                            target_name=method_name,
                            receiver=target_script or to_node,
                            edge_type="calls",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )

                    evt_id = self._ensure_event_node(sig_name, rel_path, idx, result)
                    result.edges.append(
                        GraphEdge(
                            source_id=conn_node_id,
                            target_name=sig_name,
                            receiver=target_script,
                            target_id=evt_id,
                            edge_type="publishes_event",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )
                    if target_script:
                        result.edges.append(
                            GraphEdge(
                                source_id=f"ref_file::{target_script}::{method_name}",
                                target_name=sig_name,
                                receiver=target_script,
                                target_id=evt_id,
                                edge_type="listens_to",
                                file_path=rel_path,
                                line=idx,
                                confidence="exact",
                            )
                        )

    # -------------------------------------------------------------------------
    # Cross-Language Contracts, Comments/Docstrings & Data/Locale Indexing
    # -------------------------------------------------------------------------

    def _ensure_contract_node(
        self,
        contract_kind: str,
        key: str,
        rel_path: str,
        line: int,
        result: ParseResult,
        doc: str = "",
    ) -> str:
        cid = f"contract::{contract_kind}::{key}"
        if not any(n.id == cid for n in result.nodes):
            result.nodes.append(
                GraphNode(
                    id=cid,
                    name=key,
                    qualified_name=cid,
                    kind="contract",
                    file_path=rel_path,
                    start_line=line,
                    end_line=line,
                    signature=f"contract {contract_kind}::{key}",
                    language="contract",
                    doc_comment=doc,
                )
            )
        return cid

    @staticmethod
    def _count_format_placeholders(text: str) -> int:
        if not text:
            return 0
        # Matches printf-style (%s, %d, %v, %.2f, %1$s) and brace-style ({0}, {name}, {})
        matches = re.findall(r"%(?!%)(?:\(\w+\)|\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdvfxXobq]|{[0-9a-zA-Z_]*}", text)
        return len(matches)

    def _enrich_comments_and_contracts(
        self,
        source_text: str,
        rel_path: str,
        lang: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        """
        Universal post-pass on code files to extract:
          1. Preceding/inline comments, docstrings, and notable string literals into `node.doc_comment` (for concept search).
          2. Constants & cross-language hex opcodes (`OpSGMOnline = 0xCC` <-> `contract::opcode::0xCC` <-> `S_GM_ONLINE = 0xCC`).
          3. Go struct `json:"max_chars_per_ip"` tags, field accesses, and JS/TS/GDScript `'multi_guard.max_chars_per_ip'` config contracts.
          4. GDScript/Python `tr("key") % [a, b]` placeholder argument counts.
        """
        lines = source_text.splitlines()
        if not lines:
            return

        # Build line -> innermost scope node ID map & track function body lines
        line_to_scope: dict[int, str] = {}
        func_body_lines: set[int] = set()
        class_nodes: list[GraphNode] = []

        for n in result.nodes:
            if n.kind == "class":
                class_nodes.append(n)
            elif n.kind in ("function", "method"):
                for ln in range(n.start_line, min(len(lines), n.end_line) + 1):
                    line_to_scope[ln] = n.id
                    if ln > n.start_line:
                        func_body_lines.add(ln)

        def enclosing_class_at(ln: int) -> GraphNode | None:
            best: GraphNode | None = None
            for cn in class_nodes:
                if cn.start_line <= ln <= cn.end_line and not cn.signature.startswith("script "):
                    best = cn
            return best

        def extract_comment_for_line(ln: int) -> str:
            parts: list[str] = []
            # Check up to 4 preceding lines for contiguous comments
            k = ln - 2
            prec: list[str] = []
            while k >= 0 and len(prec) < 4:
                s = lines[k].strip()
                if s.startswith(("#", "//", "///", "/*", "*")) and not s.startswith("#!"):
                    cleaned = s.lstrip("#/* \t").rstrip("*/ \t")
                    if cleaned:
                        prec.append(cleaned)
                    k -= 1
                else:
                    break
            prec.reverse()
            parts.extend(prec)

            # Inline comment on line `ln`
            if 1 <= ln <= len(lines):
                raw_l = lines[ln - 1]
                m_in = re.search(r"(?://|#)\s*(.+)$", raw_l)
                if m_in:
                    parts.append(m_in.group(1).strip())

            # Docstring on line `ln + 1` (Python/GDScript)
            if ln < len(lines):
                nxt = lines[ln].strip()
                if nxt.startswith(('"""', "'''")):
                    doc_chunk = nxt.strip("\"' ")
                    if not doc_chunk and ln + 1 < len(lines):
                        doc_chunk = lines[ln + 1].strip().strip("\"' ")
                    if doc_chunk:
                        parts.append(doc_chunk)
            return " | ".join(p for p in parts if p)[:260]

        # 1. Enrich existing nodes with comments/docstrings + notable string literals inside functions
        existing_node_ids: set[str] = {n.id for n in result.nodes}
        for n in result.nodes:
            if n.kind in ("file", "event", "contract", "i18n"):
                continue
            c_txt = extract_comment_for_line(n.start_line)
            if n.kind in ("function", "method"):
                body_slice = "\n".join(lines[max(0, n.start_line - 1) : min(len(lines), n.end_line)])
                str_lits = re.findall(r'["\']([A-Za-z0-9_.\- ]{3,40})["\']', body_slice)
                if str_lits:
                    uniq_s = list(dict.fromkeys(str_lits))[:8]
                    c_txt = f"{c_txt} [literals: {', '.join(uniq_s)}]".strip()
            if c_txt:
                n.doc_comment = c_txt[:300]

        # 2. Extract Constants, Cross-Language Opcode Contracts (`0xCC`), & Multi-Language Serialization Field Tags
        file_constants: dict[str, str] = {}  # const_name -> node_id
        go_field_to_json: dict[str, str] = {}  # GoFieldName -> json_key
        pending_serde_key: str | None = None

        def is_ignored_const(name: str) -> bool:
            if name in COMMON_IGNORED_CONSTANTS:
                return True
            if lang == "gdscript" and name.startswith(GODOT_BUILTIN_CONST_PREFIXES):
                return True
            return False

        def register_serialized_field(field_name: str, field_sig: str, ser_key: str, ln: int) -> None:
            if not ser_key or ser_key == "-":
                return
            go_field_to_json[field_name] = ser_key
            enc_cls = enclosing_class_at(ln)
            struct_name = enc_cls.name if enc_cls else Path(rel_path).stem
            field_node_id = f"{rel_path}::{struct_name}.{field_name}"
            c_doc = extract_comment_for_line(ln)
            if field_node_id not in existing_node_ids:
                existing_node_ids.add(field_node_id)
                result.nodes.append(
                    GraphNode(
                        id=field_node_id,
                        name=field_name,
                        qualified_name=f"{struct_name}.{field_name}",
                        kind="field",
                        file_path=rel_path,
                        start_line=ln,
                        end_line=ln,
                        signature=field_sig[:140],
                        language=lang,
                        doc_comment=f"json:{ser_key} {c_doc}".strip(),
                    )
                )
            cid = self._ensure_contract_node("config", ser_key, rel_path, ln, result, doc=f"{lang} field {struct_name}.{field_name}")
            result.edges.append(
                GraphEdge(
                    source_id=field_node_id,
                    target_name=ser_key,
                    target_id=cid,
                    edge_type="shares_contract",
                    file_path=rel_path,
                    line=ln,
                    confidence="exact",
                )
            )
            result.edges.append(
                GraphEdge(
                    source_id=cid,
                    target_name=f"{struct_name}.{field_name}",
                    target_id=field_node_id,
                    edge_type="shares_contract",
                    file_path=rel_path,
                    line=ln,
                    confidence="exact",
                )
            )

        for idx, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", "//", "/*", "*")):
                continue

            # Update GDScript/Python `tr("key") % [a, b]` placeholder count on `uses_i18n` edges
            if "%" in stripped and ("tr(" in stripped or "tr_n(" in stripped):
                m_tr_mod = re.search(r'\btr(?:_n)?\s*\(\s*["\']([^"\']+)["\']\s*\)\s*%\s*(\[[^\]]*\]|[^\s,)]+)', stripped)
                if m_tr_mod:
                    t_key = m_tr_mod.group(1)
                    rhs_mod = m_tr_mod.group(2).strip()
                    arg_cnt = rhs_mod.count(",") + 1 if rhs_mod.startswith("[") and rhs_mod != "[]" else 1
                    for e in result.edges:
                        if e.edge_type == "uses_i18n" and e.line == idx and e.target_name == t_key:
                            e.receiver = f"args:{arg_cnt}"

            # A1) Go struct field with `json:"..."` / `yaml:"..."` / `toml:"..."` / `mapstructure:"..."` / `env:"..."` tag
            if lang == "go" and "`" in stripped:
                m_go_tag = re.match(
                    r"^([A-Z][A-Za-z0-9_]*)\s+([^`]+)`[^`]*(?:json|yaml|toml|mapstructure|env):\"([a-zA-Z0-9_.-]+)(?:,[^\"]*)?\"",
                    stripped,
                )
                if m_go_tag:
                    f_name, f_type, j_key = m_go_tag.group(1), m_go_tag.group(2).strip(), m_go_tag.group(3)
                    register_serialized_field(f_name, f'{f_name} {f_type} `json:"{j_key}"`', j_key, idx)

            # A2) Rust `#[serde(rename = "...")]`, C# `[JsonPropertyName("...")]` / `[JsonProperty("...")]`, Java `@JsonProperty("...")` / `@SerializedName("...")`
            if lang in ("rust", "csharp", "java"):
                m_attr = re.search(
                    r'(?:serde\s*\([^)]*rename\s*=\s*|JsonPropertyName\s*\(\s*|JsonProperty\s*\(\s*|SerializedName\s*\(\s*)["\']([a-zA-Z0-9_.-]+)["\']',
                    stripped,
                )
                if m_attr:
                    pending_serde_key = m_attr.group(1)
                elif pending_serde_key:
                    m_f = (
                        re.match(r"^(?:pub(?:\([^)]*\))?\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([^,=;]+)", stripped)
                        if lang == "rust"
                        else re.match(r"^(?:public|private|protected|internal|readonly|final|\s)+[A-Za-z0-9_<>,.?\[\]]+\s+([A-Za-z_][A-Za-z0-9_]*)\b", stripped)
                    )
                    if m_f:
                        register_serialized_field(m_f.group(1), stripped.rstrip(",;{").strip(), pending_serde_key, idx)
                        pending_serde_key = None
                    elif stripped.startswith(("}", "fn ", "pub fn ")):
                        pending_serde_key = None

            # A3) Python Pydantic `Field(..., alias="...")`
            if lang == "python" and "alias=" in stripped:
                m_py_f = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*[^=]+=\s*Field\([^)]*alias\s*=\s*['\"]([a-zA-Z0-9_.-]+)['\"]", stripped)
                if m_py_f:
                    register_serialized_field(m_py_f.group(1), stripped, m_py_f.group(2), idx)

            # B) Top-level / Const-block Constant & Opcode definitions across all 7 languages
            if idx not in func_body_lines:
                m_const = (
                    re.match(
                        r"^(?:pub(?:\([^)]*\))?\s+|export\s+|public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|readonly\s+|const\s+|var\s+|(?:byte|uint8|uint16|uint32|int|u8|u16|u32)\s+)+([A-Z][A-Za-z0-9_]{1,})\s*(?::[^=]+|\s+[a-z0-9_]+)?=\s*(.+)$",
                        stripped,
                    )
                    or re.match(r"^([A-Z][A-Za-z0-9_]{2,})\s*(?::[^=]+|\s+[a-z0-9_]+)?=\s*(.+)$", stripped)
                )
                if m_const:
                    cname = m_const.group(1)
                    rhs_raw = m_const.group(2).strip()
                    if not is_ignored_const(cname) and not rhs_raw.startswith(("func(", "function(", "async ")):
                        c_node_id = f"{rel_path}::{cname}"
                        c_doc = extract_comment_for_line(idx)
                        sig_clean = re.sub(r"\s*(?://|#).*$", "", stripped).rstrip(";").strip()[:140]
                        if c_node_id not in existing_node_ids:
                            existing_node_ids.add(c_node_id)
                            result.nodes.append(
                                GraphNode(
                                    id=c_node_id,
                                    name=cname,
                                    qualified_name=cname,
                                    kind="constant",
                                    file_path=rel_path,
                                    start_line=idx,
                                    end_line=idx,
                                    signature=sig_clean,
                                    language=lang,
                                    doc_comment=c_doc,
                                )
                            )
                        file_constants[cname] = c_node_id

                        # Check if constant defines a hex opcode (e.g. `0xCC`, `0xCC_u8;`, `byte(0xCC)`, `0xCC as const`)
                        rhs_no_comment = re.sub(r"(?://|#).*$", "", rhs_raw).rstrip(";,").strip()
                        m_hex = re.match(
                            r"^(?:(?:byte|uint8|uint16|uint32|int|u8|u16|u32)\s*\(\s*)?0[xX]([0-9a-fA-F]{1,8})(?:_[a-zA-Z0-9]+)?\b",
                            rhs_no_comment,
                        )
                        if m_hex:
                            hex_val = int(m_hex.group(1), 16)
                            hex_norm = f"0x{hex_val:02X}" if hex_val <= 0xFF else f"0x{hex_val:04X}"
                            cid = self._ensure_contract_node(
                                "opcode",
                                hex_norm,
                                rel_path,
                                idx,
                                result,
                                doc=f"{cname} = {hex_norm} ({rel_path}:{idx})",
                            )
                            result.edges.append(
                                GraphEdge(
                                    source_id=c_node_id,
                                    target_name=hex_norm,
                                    target_id=cid,
                                    edge_type="shares_contract",
                                    file_path=rel_path,
                                    line=idx,
                                    confidence="exact",
                                )
                            )
                            result.edges.append(
                                GraphEdge(
                                    source_id=cid,
                                    target_name=cname,
                                    target_id=c_node_id,
                                    edge_type="shares_contract",
                                    file_path=rel_path,
                                    line=idx,
                                    confidence="exact",
                                )
                            )

        # 3. Scan function/method bodies (and module lines) for:
        #    - Opcode / Constant references (`OpSGMOnline`, `S_GM_ONLINE`, `KOAP`)
        #    - Struct field accesses (`.MaxCharsPerIP`)
        #    - Config / schema string keys (`'multi_guard.max_chars_per_ip'`)
        #    - Cross-language REST / HTTP API route strings (`'/api/v1/login'`)
        NON_CONFIG_EXTS = {
            "gd", "tscn", "tres", "godot", "go", "py", "js", "ts", "tsx", "jsx",
            "rs", "cs", "java", "json", "png", "jpg", "webp", "svg", "ogg", "wav",
            "mp3", "ttf", "otf", "shader", "gdshader", "html", "css", "sql", "md", "txt", "exe", "sh",
        }
        for idx, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", "//", "/*", "*")):
                continue
            scope_id = line_to_scope.get(idx, file_node_id)
            code_part = re.sub(r"(?://|#).*$", "", stripped)

            # 3A. References to constants/opcodes inside functions (filtering out built-in constants per language)
            if idx in func_body_lines:
                for tok in re.findall(r"\b(?:Op[A-Z][A-Za-z0-9_]+|[CS]_[A-Z][A-Z0-9_]{2,}|[A-Z][A-Z0-9_]{2,})\b", code_part):
                    if is_ignored_const(tok):
                        continue
                    if tok in file_constants or tok.startswith(("Op", "C_", "S_")):
                        result.edges.append(
                            GraphEdge(
                                source_id=scope_id,
                                target_name=tok,
                                receiver="",
                                target_id=file_constants.get(tok),
                                edge_type="calls",
                                file_path=rel_path,
                                line=idx,
                                confidence="exact" if tok in file_constants else "scoped",
                            )
                        )

            # 3B. Go struct field references (`.MaxCharsPerIP`)
            if lang == "go" and idx in func_body_lines and "." in code_part:
                for f_tok in re.findall(r"\.([A-Z][A-Za-z0-9_]{2,})\b", code_part):
                    # Avoid recording method calls `.Foo(` or stdlib package calls (`strings.TrimSpace`)
                    if f".{f_tok}(" in code_part:
                        continue
                    if f_tok in go_field_to_json:
                        j_key = go_field_to_json[f_tok]
                        cid = self._ensure_contract_node("config", j_key, rel_path, idx, result)
                        result.edges.append(
                            GraphEdge(
                                source_id=scope_id,
                                target_name=j_key,
                                target_id=cid,
                                edge_type="shares_contract",
                                file_path=rel_path,
                                line=idx,
                                confidence="exact",
                            )
                        )
                    else:
                        result.edges.append(
                            GraphEdge(
                                source_id=scope_id,
                                target_name=f_tok,
                                receiver="struct_field",
                                edge_type="calls",
                                file_path=rel_path,
                                line=idx,
                            )
                        )

            # 3C. Dotted config / schema keys in string literals (e.g. `'multi_guard.max_chars_per_ip'`)
            for m_str in re.finditer(r'["\']([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)["\']', code_part):
                full_key = m_str.group(1)
                parts = full_key.split(".")
                leaf = parts[-1]
                if leaf in NON_CONFIG_EXTS:
                    continue
                if any(e.edge_type == "uses_i18n" and e.line == idx and e.target_name == full_key for e in result.edges):
                    continue
                if "_" in full_key:
                    cid = self._ensure_contract_node("config", leaf, rel_path, idx, result, doc=f"schema key '{full_key}'")
                    result.edges.append(
                        GraphEdge(
                            source_id=scope_id,
                            target_name=leaf,
                            receiver=full_key,
                            target_id=cid,
                            edge_type="shares_contract",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )
                    result.edges.append(
                        GraphEdge(
                            source_id=cid,
                            target_name=scope_id.split("::")[-1],
                            receiver=full_key,
                            target_id=scope_id,
                            edge_type="shares_contract",
                            file_path=rel_path,
                            line=idx,
                            confidence="exact",
                        )
                    )

            # 3D. Cross-Language HTTP / REST API Route Contracts (`"/api/v1/..."`)
            for m_api in re.finditer(r'["\'`](/api/[A-Za-z0-9_./:{}-]+)["\'`]', code_part):
                raw_route = m_api.group(1).rstrip("/")
                norm_route = re.sub(r"(?::[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})", "{param}", raw_route)
                target_scope = scope_id
                # If this is a route decorator above a function (`@app.get("/api/...")`, `[HttpGet("/api/...")]`, `@GetMapping("/api/...")`), bind to the function below it
                if target_scope == file_node_id:
                    for look_ln in range(idx + 1, min(len(lines) + 1, idx + 5)):
                        if look_ln in line_to_scope:
                            target_scope = line_to_scope[look_ln]
                            break
                cid = self._ensure_contract_node("api", norm_route, rel_path, idx, result, doc=f"API endpoint {norm_route}")
                result.edges.append(
                    GraphEdge(
                        source_id=target_scope,
                        target_name=norm_route,
                        receiver=raw_route,
                        target_id=cid,
                        edge_type="shares_contract",
                        file_path=rel_path,
                        line=idx,
                        confidence="exact",
                    )
                )
                result.edges.append(
                    GraphEdge(
                        source_id=cid,
                        target_name=target_scope.split("::")[-1],
                        receiver=raw_route,
                        target_id=target_scope,
                        edge_type="shares_contract",
                        file_path=rel_path,
                        line=idx,
                        confidence="exact",
                    )
                )

    # -------------------------------------------------------------------------
    # JSON & PO Data / Seed / Map / Locale File Parsers
    # -------------------------------------------------------------------------

    def _parse_json_file(
        self,
        source_text: str,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        try:
            data = json.loads(source_text)
        except Exception:
            return

        lines = source_text.splitlines()
        rel_lower = rel_path.lower()
        is_locale = any(tok in rel_lower for tok in ("locale", "locales", "i18n", "translation", "lang/"))

        def find_line_of(needle: str, start_from: int = 1) -> int:
            q = f'"{needle}"'
            for i in range(max(1, start_from), len(lines) + 1):
                if q in lines[i - 1]:
                    return i
            for i in range(1, len(lines) + 1):
                if needle in lines[i - 1]:
                    return i
            return 1

        if is_locale and isinstance(data, dict):
            # Flatten nested locale dictionaries (`{"n": {"x": "msg"}}` -> `"n.x": "msg"`)
            def walk_locale(obj: dict[str, Any], prefix: str = "", last_ln: int = 1) -> None:
                for k, v in obj.items():
                    full_k = f"{prefix}.{k}" if prefix else str(k)
                    ln = find_line_of(str(k), last_ln)
                    if isinstance(v, dict):
                        walk_locale(v, full_k, ln)
                    elif isinstance(v, str):
                        ph_cnt = self._count_format_placeholders(v)
                        i18n_id = f"i18n::{full_k}"
                        val_preview = v[:90] + ("..." if len(v) > 90 else "")
                        result.nodes.append(
                            GraphNode(
                                id=f"{rel_path}::{full_k}",
                                name=full_k,
                                qualified_name=f"i18n::{full_k}",
                                kind="i18n",
                                file_path=rel_path,
                                start_line=ln,
                                end_line=ln,
                                signature=f'{full_k} = "{val_preview}"',
                                language="json",
                                doc_comment=f"{full_k}: {v}"[:300],
                            )
                        )
                        if not any(n.id == i18n_id for n in result.nodes):
                            result.nodes.append(
                                GraphNode(
                                    id=i18n_id,
                                    name=full_k,
                                    qualified_name=f"i18n::{full_k}",
                                    kind="i18n",
                                    file_path=rel_path,
                                    start_line=ln,
                                    end_line=ln,
                                    signature=f'{full_k} = "{val_preview}"',
                                    language="i18n",
                                    doc_comment=f"{full_k}: {v}"[:300],
                                )
                            )
                        result.edges.append(
                            GraphEdge(
                                source_id=file_node_id,
                                target_name=full_k,
                                receiver=f"placeholders:{ph_cnt}",
                                target_id=i18n_id,
                                edge_type="defines_i18n",
                                file_path=rel_path,
                                line=ln,
                                confidence="exact",
                            )
                        )

            walk_locale(data)
            return

        # Data / Seed / Map / Config JSON indexing
        stem = Path(rel_path).stem

        def index_record_object(obj: dict[str, Any], path_prefix: str, approx_ln: int) -> None:
            # Determine primary label for this JSON object
            label = (
                obj.get("name")
                or obj.get("title")
                or obj.get("key")
                or obj.get("code")
                or obj.get("map_name")
                or obj.get("zone_name")
                or obj.get("id")
            )
            flat_pairs: list[str] = []
            for k, v in obj.items():
                if isinstance(v, (str, int, float, bool)):
                    flat_pairs.append(f"{k}={v}")
                    # If key looks like a snake_case config setting, link to `contract::config::<k>`
                    if isinstance(k, str) and "_" in k and k.islower() and k not in ("map_id", "zone_id", "item_id", "npc_id", "created_at", "updated_at"):
                        kl = find_line_of(k, approx_ln)
                        full_cfg_path = f"{path_prefix}.{k}" if path_prefix else k
                        cid = self._ensure_contract_node("config", k, rel_path, kl, result, doc=f"{rel_path}: {full_cfg_path}={v}")
                        rec_node_id = f"{rel_path}::{full_cfg_path}"
                        result.nodes.append(
                            GraphNode(
                                id=rec_node_id,
                                name=k,
                                qualified_name=f"{stem}.{full_cfg_path}",
                                kind="data",
                                file_path=rel_path,
                                start_line=kl,
                                end_line=kl,
                                signature=f'"{k}": {json.dumps(v)}',
                                language="json",
                                doc_comment=f"{full_cfg_path}: {k}={v}",
                            )
                        )
                        result.edges.append(
                            GraphEdge(
                                source_id=rec_node_id,
                                target_name=k,
                                receiver=full_cfg_path,
                                target_id=cid,
                                edge_type="shares_contract",
                                file_path=rel_path,
                                line=kl,
                                confidence="exact",
                            )
                        )
                        result.edges.append(
                            GraphEdge(
                                source_id=cid,
                                target_name=f"{stem}.{full_cfg_path}",
                                receiver=full_cfg_path,
                                target_id=rec_node_id,
                                edge_type="shares_contract",
                                file_path=rel_path,
                                line=kl,
                                confidence="exact",
                            )
                        )
                elif isinstance(v, dict):
                    sub_ln = find_line_of(str(k), approx_ln)
                    index_record_object(v, f"{path_prefix}.{k}" if path_prefix else str(k), sub_ln)
                elif isinstance(v, list):
                    sub_ln = find_line_of(str(k), approx_ln)
                    for idx_item, item in enumerate(v[:200]):
                        if isinstance(item, dict):
                            index_record_object(item, f"{path_prefix}.{k}[{idx_item}]" if path_prefix else f"{k}[{idx_item}]", sub_ln)

            if label is not None:
                label_str = str(label)
                ln = find_line_of(label_str, approx_ln)
                summary = ", ".join(flat_pairs[:12])
                node_id = f"{rel_path}::{path_prefix or label_str}"
                if not any(n.id == node_id for n in result.nodes):
                    result.nodes.append(
                        GraphNode(
                            id=node_id,
                            name=label_str,
                            qualified_name=f"{stem}::{label_str}",
                            kind="data",
                            file_path=rel_path,
                            start_line=ln,
                            end_line=ln,
                            signature=f"{stem} entry '{label_str}' ({summary[:100]})",
                            language="json",
                            doc_comment=f"{rel_path} {path_prefix}: {summary}"[:300],
                        )
                    )

        if isinstance(data, dict):
            index_record_object(data, "", 1)
        elif isinstance(data, list):
            for idx_item, item in enumerate(data[:300]):
                if isinstance(item, dict):
                    index_record_object(item, f"[{idx_item}]", 1)

    def _parse_po_file(
        self,
        source_text: str,
        rel_path: str,
        file_node_id: str,
        result: ParseResult,
    ) -> None:
        lines = source_text.splitlines()
        curr_msgid: str | None = None
        curr_ln = 1
        for idx, raw in enumerate(lines, start=1):
            s = raw.strip()
            if s.startswith("msgid "):
                curr_msgid = self._clean_string_literal(s[len("msgid ") :])
                curr_ln = idx
            elif s.startswith("msgstr ") and curr_msgid:
                msgstr = self._clean_string_literal(s[len("msgstr ") :])
                ph_cnt = self._count_format_placeholders(msgstr or curr_msgid)
                i18n_id = f"i18n::{curr_msgid}"
                preview = (msgstr or curr_msgid)[:80]
                result.nodes.append(
                    GraphNode(
                        id=f"{rel_path}::{curr_msgid}",
                        name=curr_msgid,
                        qualified_name=f"i18n::{curr_msgid}",
                        kind="i18n",
                        file_path=rel_path,
                        start_line=curr_ln,
                        end_line=idx,
                        signature=f'msgid "{curr_msgid}" -> "{preview}"',
                        language="po",
                        doc_comment=f"{curr_msgid}: {msgstr}",
                    )
                )
                if not any(n.id == i18n_id for n in result.nodes):
                    result.nodes.append(
                        GraphNode(
                            id=i18n_id,
                            name=curr_msgid,
                            qualified_name=f"i18n::{curr_msgid}",
                            kind="i18n",
                            file_path=rel_path,
                            start_line=curr_ln,
                            end_line=idx,
                            signature=f'msgid "{curr_msgid}" -> "{preview}"',
                            language="i18n",
                            doc_comment=f"{curr_msgid}: {msgstr}",
                        )
                    )
                result.edges.append(
                    GraphEdge(
                        source_id=file_node_id,
                        target_name=curr_msgid,
                        receiver=f"placeholders:{ph_cnt}",
                        target_id=i18n_id,
                        edge_type="defines_i18n",
                        file_path=rel_path,
                        line=curr_ln,
                        confidence="exact",
                    )
                )
                curr_msgid = None
