"""Comprehensive test suite for code-graph-mcp covering all features and acceptance criteria."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from code_graph.hooks import install_project_hooks_and_report
from code_graph.indexer import ProjectIndexer
from code_graph.mcp_server import (
    check_rename_or_delete,
    check_translations,
    find_flow,
    get_architecture_map,
    get_diff_impact,
    get_edit_context,
    get_symbol_impact,
    get_token_savings,
    reindex_project,
    search_symbols,
    tests_for,
)


def test_1_ignore_and_worktrees() -> None:
    """Test .gitignore, nested .gitignore, .codegraphignore, .claude/worktrees, and sub-repo .git skipping."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".gitignore").write_text("ignored_dir/\n*.gen.py\n", encoding="utf-8")
        (root / ".codegraphignore").write_text("secret_tools/\n", encoding="utf-8")

        (root / "app.py").write_text("def main_entry():\n    pass\n", encoding="utf-8")

        (root / "ignored_dir").mkdir()
        (root / "ignored_dir" / "bad.py").write_text("def should_not_index(): pass\n", encoding="utf-8")
        (root / "schema.gen.py").write_text("def generated_noise(): pass\n", encoding="utf-8")

        (root / "secret_tools").mkdir()
        (root / "secret_tools" / "tool.py").write_text("def secret_tool(): pass\n", encoding="utf-8")

        wt = root / ".claude" / "worktrees" / "copy1"
        wt.mkdir(parents=True)
        (wt / "dup.py").write_text("def duplicate_worktree(): pass\n", encoding="utf-8")

        aider_dir = root / ".aider.cache"
        aider_dir.mkdir()
        (aider_dir / "cache.py").write_text("def aider_noise(): pass\n", encoding="utf-8")

        subrepo = root / "external_clone"
        (subrepo / ".git").mkdir(parents=True)
        (subrepo / "sub.py").write_text("def subrepo_func(): pass\n", encoding="utf-8")

        sub = root / "subdir"
        sub.mkdir()
        (sub / ".gitignore").write_text("local_skip.py\n", encoding="utf-8")
        (sub / "local_skip.py").write_text("def skipped_nested(): pass\n", encoding="utf-8")
        (sub / "kept.py").write_text("def kept_nested(): pass\n", encoding="utf-8")

        indexer = ProjectIndexer(root)
        stats = indexer.index()
        try:
            assert stats.files_scanned == 2, f"Expected 2 files, got {stats.files_scanned}"
            syms = search_symbols("", project_path=str(root))
            assert "index: fresh" in syms
            assert "main_entry" in syms
            assert "kept_nested" in syms
            assert "should_not_index" not in syms
            assert "duplicate_worktree" not in syms
            assert "subrepo_func" not in syms
            assert "skipped_nested" not in syms
        finally:
            indexer.close()


def test_2_3_4_5_6_godot_go_js_scoping_and_features() -> None:
    """
    Test:
      - No false cross-language edges (GDScript _show_account must NOT link to JS ui.js:refresh or Go Party.has)
      - Godot `project.godot` [autoload] (`Game.send_packet`)
      - Godot `.tscn` `[connection signal=...]` and Coverage Warnings
      - Dynamic string call `call("dynamic_secret_hook")` reported when upstream is empty
      - `get_edit_context` for both small (<300L) and large (>=300L) files
      - `check_rename_or_delete` for call sites, overrides, `.tscn` connections, signals, and dynamic calls
      - Freshness header (`index: fresh` vs `index: refreshed N files`)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        client_dir = root / "client"
        (client_dir / "autoload").mkdir(parents=True)
        (client_dir / "scenes").mkdir(parents=True)

        # 1. client/project.godot with [autoload]
        (client_dir / "project.godot").write_text(
            """
config_version=5

[application]
config/name="PixelKO"

[autoload]
Game="*res://autoload/game.gd"
""",
            encoding="utf-8",
        )

        # 2. client/autoload/game.gd with a large (>80 line) _on_packet function
        filler_lines = "\n".join(f"\tvar dummy_{i} = {i}" for i in range(1, 95))
        (client_dir / "autoload" / "game.gd").write_text(
            f"""extends Node

signal account_loaded(data)

func send_packet(pkt: Dictionary) -> void:
\tpass

func _on_packet(pkt: Dictionary) -> void:
{filler_lines}
\tif pkt.has("account"):
\t\taccount_loaded.emit(pkt)
\t\tcall("dynamic_secret_hook", pkt)
""",
            encoding="utf-8",
        )

        # 3. client/scenes/base_scene.gd (small file < 300 lines, defines overridable method `on_scene_enter`)
        (client_dir / "scenes" / "base_scene.gd").write_text(
            """extends Control

func base_helper() -> void:
\tpass

func on_scene_enter() -> void:
\tpass
""",
            encoding="utf-8",
        )

        # 4. client/scenes/main.gd
        (client_dir / "scenes" / "main.gd").write_text(
            """extends "res://scenes/base_scene.gd"

signal status_updated(msg)
const MAX_SLOTS: int = 4
@onready var account_panel: Control = $AccountPanel
var active_slot: int = 0

func _ready() -> void:
\tGame.account_loaded.connect(_show_account)

func _show_account(data: Dictionary) -> void:
\tif data.has("name") and active_slot < MAX_SLOTS:
\t\taccount_panel.visible = true
\t\tGame.send_packet({"ack": true})
\t\tbase_helper()
\t\t_local_short_helper()
\t\tsome_widget.refresh()
\t\tsome_label.label()

func _local_short_helper() -> void:
\tstatus_updated.emit("ok")

func _on_login_btn_pressed() -> void:
\t_show_account({"name": "hero"})

func on_scene_enter() -> void:
\t_local_short_helper()

func dynamic_secret_hook(pkt: Dictionary) -> void:
\tpass
""",
            encoding="utf-8",
        )

        # 5. client/scenes/main.tscn with [connection signal="pressed" ... method="_on_login_btn_pressed"]
        (client_dir / "scenes" / "main.tscn").write_text(
            """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scenes/main.gd" id="1_main"]

[node name="Main" type="Control"]
script = ExtResource("1_main")

[node name="LoginBtn" type="Button" parent="."]

[connection signal="pressed" from="LoginBtn" to="." method="_on_login_btn_pressed"]
""",
            encoding="utf-8",
        )

        # 6. Unrelated JS & Go files with colliding method names (`refresh`, `has`, `label`)
        js_dir = root / "server" / "web" / "admin" / "js"
        js_dir.mkdir(parents=True)
        (js_dir / "ui.js").write_text(
            """
export function refresh() {
    console.log("admin ui refresh");
}
export function label() {
    return "config label";
}
""",
            encoding="utf-8",
        )

        go_dir = root / "server" / "internal" / "game"
        go_dir.mkdir(parents=True)
        (go_dir / "party.go").write_text(
            """package game

type Party struct {
    Members []string
}

func (p *Party) has(id string) bool {
    return len(p.Members) > 0
}
""",
            encoding="utf-8",
        )

        # First query indexes all 6 files -> `index: refreshed 6 files`
        impact_out = get_symbol_impact("_show_account", depth=2, project_path=str(root))
        assert impact_out.startswith("index: refreshed 6 files"), f"Unexpected header:\n{impact_out[:120]}"

        # Second query without file changes -> `index: fresh`
        impact_out2 = get_symbol_impact("_show_account", depth=2, project_path=str(root))
        assert impact_out2.startswith("index: fresh"), f"Expected 'index: fresh', got:\n{impact_out2[:120]}"

        # Criterion A: NO JS/Go false matches!
        assert "ui.js" not in impact_out2
        assert "party.go" not in impact_out2
        assert "Party.has" not in impact_out2
        assert "### Coverage Warnings" in impact_out2

        # Criterion B: `.tscn` signal connection in `impact _on_login_btn_pressed` Coverage Warnings!
        btn_impact = get_symbol_impact("_on_login_btn_pressed", depth=1, project_path=str(root))
        assert "main.tscn" in btn_impact
        assert "[scene/godot]" in btn_impact

        # Criterion C: Function called only via `call("dynamic_secret_hook")` (upstream empty in AST graph)
        dyn_impact = get_symbol_impact("dynamic_secret_hook", depth=1, project_path=str(root))
        assert "Grafta çağıran bulunamadı; metin taramasında **1** eşleşme bulundu." in dyn_impact, (
            f"Expected empty-upstream text match message, got:\n{dyn_impact}"
        )
        assert '[string/dynamic] `client/autoload/game.gd:' in dyn_impact

        # Criterion D: `get_edit_context` on small file (<300 lines) returns full file content
        small_edit_ctx = get_edit_context("_show_account", project_path=str(root), budget_tokens=4000)
        assert "Full File Content" in small_edit_ctx
        assert "Style Hint" in small_edit_ctx
        assert "### Coverage Warnings" in small_edit_ctx

        # Criterion E: `get_edit_context` on a large file (>= 300 lines) returns skeleton + body + used members + caller windows + other signatures
        large_gd = client_dir / "scenes" / "large_scene.gd"
        many_helpers = "\n".join(
            f"func _helper_{i}(x: int) -> int:\n\tvar a = x + {i}\n\tvar b = a * 2\n\tvar c = b - 1\n\treturn c\n"
            for i in range(1, 65)
        )
        large_gd.write_text(
            f"""extends "res://scenes/base_scene.gd"
class_name LargeScene

signal quest_completed(quest_id)
const QUEST_REWARD: int = 500
@onready var quest_label: Label = $QuestLabel
var current_quest_id: int = 1

{many_helpers}

func complete_active_quest() -> void:
\tif current_quest_id > 0:
\t\tquest_label.text = str(QUEST_REWARD)
\t\t_helper_1(current_quest_id)
\t\tquest_completed.emit(current_quest_id)

func trigger_completion() -> void:
\tcomplete_active_quest()
""",
            encoding="utf-8",
        )

        large_edit_ctx = get_edit_context("complete_active_quest", project_path=str(root), budget_tokens=4000)
        assert large_edit_ctx.startswith("index: refreshed 1 files")
        assert "### 1. Target Function Body" in large_edit_ctx
        assert "### 2. File Skeleton" in large_edit_ctx
        assert "@onready var quest_label" in large_edit_ctx
        assert "const QUEST_REWARD" in large_edit_ctx
        assert "### 3. Used Members & Direct Callees" in large_edit_ctx
        assert "_helper_1" in large_edit_ctx
        assert "### 4. Direct Callers (d=1, ±5 Line Windows)" in large_edit_ctx
        assert "trigger_completion" in large_edit_ctx
        assert "### 5. Other Function Signatures" in large_edit_ctx
        assert len(large_edit_ctx) <= 4000 * 4

        # Criterion F: `check_rename_or_delete`
        # Check on `on_scene_enter` (has inheritance override between base_scene.gd and main.gd)
        check_ov = check_rename_or_delete("on_scene_enter", project_path=str(root))
        assert "### Inheritance / Overrides (`extends` chain)" in check_ov
        assert "base_scene.gd" in check_ov and "main.gd" in check_ov

        # Check on `_on_login_btn_pressed` (has .tscn connection)
        check_btn = check_rename_or_delete("_on_login_btn_pressed", project_path=str(root))
        assert "main.tscn:10" in check_btn

        # Check on `account_loaded` signal (has emit and connect sites)
        check_sig = check_rename_or_delete("account_loaded", project_path=str(root))
        assert "publishes_event" in check_sig
        assert "listens_to" in check_sig

        # Criterion G: Architecture Map (`god_nodes`, clusters, autoloads, event hubs) & Hook Installation
        arch_map = get_architecture_map(project_path=str(root), top_n=10)
        assert "God Nodes" in arch_map
        assert "Subsystem Clusters" in arch_map
        assert "Godot Autoload Singletons" in arch_map
        assert "Top Event / Signal Bus Channels" in arch_map

        hook_info = install_project_hooks_and_report(root, write_report=True)
        assert (root / "GRAPH_REPORT.md").exists()
        assert (root / ".claude" / "settings.local.json").exists()


def test_3_universal_polyglot_languages() -> None:
    """
    Test universal multi-language support:
      - TypeScript + `tsconfig.json` `@/*` path alias + `interface` skeleton
      - Go + `go.mod` cross-package call + `struct` skeleton
      - Python relative import (`from .models import ...`) + `self.x` attribute skeleton + `getattr` coverage warning
      - Rust (`struct`, `impl`, methods)
      - Java (`class`, methods)
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # 1. TypeScript with tsconfig.json `@/*` -> `src/*`
        (root / "tsconfig.json").write_text(
            '{\n  "compilerOptions": {\n    "baseUrl": ".",\n    "paths": {\n      "@/*": ["src/*"]\n    }\n  }\n}\n',
            encoding="utf-8",
        )
        ts_utils = root / "src" / "utils"
        ts_services = root / "src" / "services"
        ts_utils.mkdir(parents=True)
        ts_services.mkdir(parents=True)

        (ts_utils / "math.ts").write_text(
            "export function clampScore(val: number, maxVal: number): number {\n  return Math.min(val, maxVal);\n}\n",
            encoding="utf-8",
        )
        ts_padding = "\n".join(
            f"export function tsHelper{i}(n: number): number {{\n  const a = n + {i};\n  return a * 2;\n}}\n"
            for i in range(1, 80)
        )
        (ts_services / "leaderboard.ts").write_text(
            f"""import {{ clampScore }} from "@/utils/math";

export interface LeaderboardEntry {{
  playerId: string;
  score: number;
}}

const MAX_SCORE_CAP = 9999;

{ts_padding}

export function computeFinalScore(entry: LeaderboardEntry): number {{
  const adjusted = tsHelper1(entry.score);
  return clampScore(adjusted, MAX_SCORE_CAP);
}}
""",
            encoding="utf-8",
        )

        # 2. Go with go.mod cross-package call
        (root / "go.mod").write_text("module example.com/polyapp\n\ngo 1.22\n", encoding="utf-8")
        go_billing = root / "internal" / "billing"
        go_checkout = root / "internal" / "checkout"
        go_billing.mkdir(parents=True)
        go_checkout.mkdir(parents=True)

        (go_billing / "invoice.go").write_text(
            "package billing\n\nfunc CalculateTax(amount int) int {\n\treturn amount / 10\n}\n",
            encoding="utf-8",
        )
        go_padding = "\n".join(
            f"func goHelper{i}(v int) int {{\n\tx := v + {i}\n\treturn x\n}}\n"
            for i in range(1, 80)
        )
        (go_checkout / "order.go").write_text(
            f"""package checkout

import "example.com/polyapp/internal/billing"

type OrderConfig struct {{
\tCurrency string
\tTaxRate  int
}}

const DefaultTaxMultiplier = 2

{go_padding}

func ProcessOrder(total int) int {{
\ttax := billing.CalculateTax(total)
\treturn total + tax*DefaultTaxMultiplier
}}
""",
            encoding="utf-8",
        )

        # 3. Python with relative import (`from .models import ...`) & `self.secret_key` in `__init__`
        py_app = root / "pyapp"
        py_app.mkdir(parents=True)
        (py_app / "__init__.py").write_text("", encoding="utf-8")
        (py_app / "models.py").write_text(
            "def validate_token_signature(token: str) -> bool:\n    return token.startswith('valid_')\n",
            encoding="utf-8",
        )
        py_padding = "\n".join(
            f"def _py_helper_{i}(x: int) -> int:\n    a = x + {i}\n    return a * 2\n"
            for i in range(1, 80)
        )
        (py_app / "auth.py").write_text(
            f"""from .models import validate_token_signature

TOKEN_PREFIX = "valid_"

{py_padding}

class AuthService:
    def __init__(self) -> None:
        self.secret_key = "top_secret"

    def verify_request(self, token: str) -> bool:
        if not self.secret_key:
            return False
        hook = getattr(self, "dynamic_audit_hook", None)
        return validate_token_signature(token)

    def dynamic_audit_hook(self) -> None:
        pass
""",
            encoding="utf-8",
        )

        # 4. Rust (`struct` + `impl`)
        rs_dir = root / "rust_core"
        rs_dir.mkdir(parents=True)
        (rs_dir / "physics.rs").write_text(
            """pub struct PhysicsBody {
    pub mass: f32,
    pub velocity: f32,
}

impl PhysicsBody {
    pub fn apply_impulse(&mut self, force: f32) -> f32 {
        self.velocity += force / self.mass;
        self.velocity
    }

    pub fn step_world(&mut self) -> f32 {
        self.apply_impulse(9.81)
    }
}
""",
            encoding="utf-8",
        )

        # 5. Java (`class` + method call)
        java_dir = root / "java_core"
        java_dir.mkdir(parents=True)
        (java_dir / "PaymentGateway.java").write_text(
            """package com.example.pay;

public class PaymentGateway {
    private String merchantId;

    public boolean verifyMerchant(String id) {
        return id != null && !id.isEmpty();
    }

    public boolean authorizeCharge(String id, int cents) {
        if (!verifyMerchant(id)) {
            return false;
        }
        return cents > 0;
    }
}
""",
            encoding="utf-8",
        )

        idx = ProjectIndexer(root)
        stats = idx.index()
        idx.close()
        assert stats.files_indexed >= 8

        # Verify TypeScript `tsconfig.json` `@/utils/math` resolution & interface skeleton
        ts_ctx = get_edit_context("computeFinalScore", project_path=str(root), budget_tokens=4000)
        assert "interface LeaderboardEntry" in ts_ctx
        assert "MAX_SCORE_CAP" in ts_ctx
        assert "clampScore" in ts_ctx
        ts_imp = get_symbol_impact("clampScore", project_path=str(root))
        assert "computeFinalScore" in ts_imp

        # Verify Go `go.mod` cross-package call resolution & struct skeleton
        go_ctx = get_edit_context("ProcessOrder", project_path=str(root), budget_tokens=4000)
        assert "type OrderConfig struct" in go_ctx
        assert "CalculateTax" in go_ctx
        go_imp = get_symbol_impact("CalculateTax", project_path=str(root))
        assert "ProcessOrder" in go_imp

        # Verify Python relative import, `self.secret_key` skeleton, and `getattr` dynamic coverage warning
        py_ctx = get_edit_context("verify_request", project_path=str(root), budget_tokens=4000)
        assert "self.secret_key" in py_ctx
        assert "validate_token_signature" in py_ctx
        py_hook_imp = get_symbol_impact("dynamic_audit_hook", project_path=str(root))
        assert "getattr" in py_hook_imp

        # Verify Rust `impl` method indexing and call resolution
        rs_imp = get_symbol_impact("apply_impulse", project_path=str(root))
        assert "PhysicsBody.apply_impulse" in rs_imp
        assert "PhysicsBody.step_world" in rs_imp

        # Verify Java class method indexing and call resolution
        java_imp = get_symbol_impact("verifyMerchant", project_path=str(root))
        assert "PaymentGateway.verifyMerchant" in java_imp
        assert "PaymentGateway.authorizeCharge" in java_imp


def test_4_contracts_data_i18n_tests_diff_concepts_lifecycle_telemetry() -> None:
    """
    Test all 8 new workflows:
      1. Cross-language contracts:
         - Go `OpSGMOnline = 0xCC` <-> GDScript `const S_GM_ONLINE = 0xCC` via `contract::opcode::0xCC`
         - Go struct field `json:"max_chars_per_ip"` <-> JS schema `'multi_guard.max_chars_per_ip'` <-> `seed/config.json` `"max_chars_per_ip"`
      2. Data & locale file indexing (`seed/maps.json`, `locales/tr.json`) searchable alongside code (`"Moradon"`).
      3. Translation key graph (`check_translations`):
         - Missing keys & raw non-key literals (`"aday"`, `"oy"`)
         - Placeholder `%d` / `%s` count mismatches (`noticeT(c, "n.vote_win", one_arg)` when locale has 2 `%s`/`%d`)
         - Unused locale keys
      4. `tests_for`: maps `BroadcastGMOnline` / `CalculateTax` to exact runnable `go test -run ...` and `godot -s ...` commands.
      5. `get_diff_impact`: parses `git diff`, finds modified AST symbols, their callers, cross-language contracts, and runnable tests.
      6. Concept search: `search_symbols("attack stat")` finds `KOAP` via its inline comment `// attack power stat bonus`.
      7. Engine virtual lifecycle noise reduction: `get_symbol_impact("_draw")` does not emit false text-match noise warnings.
      8. Direct token savings telemetry (`get_token_savings`): records returned vs. raw touched file tokens in SQLite.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "go.mod").write_text("module example.com/game\n\ngo 1.22\n", encoding="utf-8")

        server_dir = root / "server" / "game"
        client_dir = root / "client" / "net"
        panel_dir = root / "panel"
        seed_dir = root / "seed"
        locales_dir = root / "locales"
        client_tests_dir = root / "client" / "tests"
        for d in (server_dir, client_dir, panel_dir, seed_dir, locales_dir, client_tests_dir):
            d.mkdir(parents=True, exist_ok=True)
        (root / "client" / "project.godot").write_text('[application]\nconfig/name="TestGame"\n', encoding="utf-8")

        # Go server protocol + config + i18n + concept comment
        (server_dir / "proto.go").write_text(
            """package game

const (
\t// OpSGMOnline notifies clients that a GM came online
\tOpSGMOnline = 0xCC
\t// attack power stat bonus for warrior class
\tKOAP = 100
)

type MultiGuardConfig struct {
\tMaxCharsPerIP int `json:"max_chars_per_ip"`
}

func EnforceMultiGuard(cfg MultiGuardConfig, ipCount int) bool {
\treturn ipCount <= cfg.MaxCharsPerIP
}

func BroadcastGMOnline(gmName string) byte {
\tnoticeT(nil, "n.gm_online", gmName)
\tnoticeT(nil, "aday")
\tnoticeT(nil, "n.vote_win", gmName)
\treturn OpSGMOnline
}

func SpawnInMoradon() string {
\treturn "Moradon"
}
""",
            encoding="utf-8",
        )

        # Go test exercising BroadcastGMOnline & EnforceMultiGuard
        (server_dir / "proto_test.go").write_text(
            """package game

import "testing"

func TestBroadcastGMOnline(t *testing.T) {
\top := BroadcastGMOnline("Admin")
\tif op != OpSGMOnline {
\t\tt.Fatalf("unexpected opcode: %v", op)
\t}
}
""",
            encoding="utf-8",
        )

        # GDScript client handler with matching opcode 0xCC, tr("oy"), and _draw lifecycle method
        (client_dir / "packet_handler.gd").write_text(
            """extends Node
class_name PacketHandler

const S_GM_ONLINE = 0xCC

func handle_packet(opcode: int) -> void:
\tif opcode == S_GM_ONLINE:
\t\tvar lbl = tr("oy")
\t\tvar title = tr("ui.moradon_title")
\t\tprint(lbl, title)

func _draw() -> void:
\tpass
""",
            encoding="utf-8",
        )

        # Another GDScript file mentioning `_draw` in comments/strings so text_matches > 0
        (client_dir / "hud_canvas.gd").write_text(
            """extends Control

func refresh_hud() -> void:
\t# calls _draw via engine queue_redraw, _draw is virtual
\tvar debug_note = "_draw"
\tprint(debug_note)
""",
            encoding="utf-8",
        )

        # Godot test script exercising handle_packet
        (client_tests_dir / "test_packets.gd").write_text(
            """extends Node

func test_gm_online_packet() -> void:
\tvar h = PacketHandler.new()
\th.handle_packet(PacketHandler.S_GM_ONLINE)
""",
            encoding="utf-8",
        )

        # Panel JS schema referencing 'multi_guard.max_chars_per_ip'
        (panel_dir / "schema.js").write_text(
            """export function renderMultiGuardSetting(form) {
  return form.getField('multi_guard.max_chars_per_ip');
}
""",
            encoding="utf-8",
        )

        # Seed JSON data files (`seed/config.json` and `seed/maps.json`)
        (seed_dir / "config.json").write_text(
            '{\n  "multi_guard": {\n    "max_chars_per_ip": 2\n  }\n}\n',
            encoding="utf-8",
        )
        (seed_dir / "maps.json").write_text(
            '{\n  "zones": [\n    {"id": 21, "name": "Moradon", "safe_zone": true}\n  ]\n}\n',
            encoding="utf-8",
        )

        # Locale JSON (`locales/tr.json`)
        (locales_dir / "tr.json").write_text(
            """{
  "n.gm_online": "GM %s oyuna girdi!",
  "n.vote_win": "Aday %s toplam %d oy ile kazandi!",
  "ui.moradon_title": "Moradon Kalesi",
  "ui.unused_legacy_key": "Bu anahtar hicbir yerde kullanilmiyor"
}
""",
            encoding="utf-8",
        )

        # Initialize git repo and commit initial state so we can test `get_diff_impact`
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)

        # 1A. Verify Cross-Language Opcode Contract: Go `OpSGMOnline` <-> GDScript `S_GM_ONLINE` (0xCC)
        op_impact = get_symbol_impact("OpSGMOnline", project_path=str(root))
        assert "contract::opcode::0xCC" in op_impact
        assert "S_GM_ONLINE" in op_impact
        assert "handle_packet" in op_impact
        assert "BroadcastGMOnline" in op_impact

        # 1B. Verify Cross-Language Config Contract: Go `MaxCharsPerIP` / `max_chars_per_ip` <-> JS `'multi_guard.max_chars_per_ip'` <-> `seed/config.json`
        cfg_impact = get_symbol_impact("max_chars_per_ip", project_path=str(root))
        assert "contract::config::max_chars_per_ip" in cfg_impact
        assert "MultiGuardConfig.MaxCharsPerIP" in cfg_impact
        assert "EnforceMultiGuard" in cfg_impact
        assert "renderMultiGuardSetting" in cfg_impact
        assert "seed/config.json" in cfg_impact

        # 2. Verify Data & Locale Indexing: searching `"Moradon"` returns Go code, `seed/maps.json`, and `locales/tr.json`
        moradon_search = search_symbols("Moradon", project_path=str(root))
        assert "SpawnInMoradon" in moradon_search
        assert "seed/maps.json" in moradon_search
        assert "locales/tr.json" in moradon_search

        # 3. Verify Translation Key Graph (`check_translations`)
        i18n_report = check_translations(project_path=str(root))
        # Catches suspicious raw non-key literals `"aday"` and `"oy"`
        assert "`aday`" in i18n_report
        assert "`oy`" in i18n_report
        assert "SUSPICIOUS RAW WORD / NOT A KEY" in i18n_report
        # Catches `%d` / `%s` placeholder count mismatch on `n.vote_win` (1 arg passed vs 2 expected)
        assert "`n.vote_win`" in i18n_report
        assert "passes 1 format arg(s)" in i18n_report
        # Catches unused locale key `ui.unused_legacy_key`
        assert "`ui.unused_legacy_key`" in i18n_report

        # 4. Verify `tests_for` on Go and GDScript symbols
        go_tests_out = tests_for("BroadcastGMOnline", project_path=str(root))
        assert "TestBroadcastGMOnline" in go_tests_out
        assert "go test ./server/game -run '^TestBroadcastGMOnline$'" in go_tests_out

        gd_tests_out = tests_for("handle_packet", project_path=str(root))
        assert "test_gm_online_packet" in gd_tests_out
        assert "godot --headless" in gd_tests_out

        # 5. Verify `get_diff_impact`: modify `BroadcastGMOnline` and check git diff impact
        (server_dir / "proto.go").write_text(
            (server_dir / "proto.go")
            .read_text(encoding="utf-8")
            .replace('noticeT(nil, "n.gm_online", gmName)', 'noticeT(nil, "n.gm_online", gmName + "!")'),
            encoding="utf-8",
        )
        diff_out = get_diff_impact(project_path=str(root), base_ref="HEAD")
        assert "BroadcastGMOnline" in diff_out
        assert "TestBroadcastGMOnline" in diff_out
        assert "go test ./server/game -run '^TestBroadcastGMOnline$'" in diff_out

        # 6. Verify Concept Search: `"attack stat"` matches `KOAP` via comment `// attack power stat bonus`
        concept_out = search_symbols("attack stat", project_path=str(root))
        assert "KOAP" in concept_out
        assert "attack power stat bonus" in concept_out

        # 7. Verify Engine Lifecycle Noise Suppression (`_draw`)
        draw_impact = get_symbol_impact("_draw", project_path=str(root))
        assert "Engine Lifecycle Callback" in draw_impact
        assert "text matches for `_draw` across project, but AST upstream is empty" not in draw_impact

        # 8. Verify Direct Token Savings Telemetry (`get_token_savings`)
        savings_out = get_token_savings(project_path=str(root))
        assert "Token Savings Telemetry" in savings_out
        assert "Net Tokens Saved" in savings_out
        assert "get_symbol_impact" in savings_out


def test_5_language_specific_noise_filtering_and_polyglot_contracts() -> None:
    """
    Test language-specific noise filtering, lifecycle hooks, serialization contracts, and test runners across all 7 languages:
      1. Stdlib receiver filtering: Go `strings.TrimSpace` does NOT link to an unrelated `TrimSpace` helper in another file.
      2. Godot built-in constants (`KEY_ESCAPE`, `MOUSE_BUTTON_LEFT`, `ERR_PARSE_ERROR`) are ignored and don't pollute the graph.
      3. Coverage scanner language isolation: querying a GDScript function does NOT scan `.go` or `.ts` files for text matches.
      4. Multi-language lifecycle methods (`ServeHTTP` in Go, `__enter__` in Python, `componentDidMount` in TS, `FixedUpdate` in C#, `drop` in Rust, `afterPropertiesSet` in Java).
      5. Multi-language serialization & API route contracts:
         - Rust `#[serde(rename = "session_timeout_sec")]` <-> C# `[JsonPropertyName("session_timeout_sec")]` <-> Java `@JsonProperty("session_timeout_sec")`
         - Go `"/api/v1/players/:id"` <-> TypeScript `fetch("/api/v1/players/{id}")` via `contract::api::/api/v1/players/{param}`
      6. Multi-language `tests_for` commands (`pytest`, `npx vitest run`, `cargo test`, `dotnet test --filter`, `mvn test -Dtest=`).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "go.mod").write_text("module example.com/poly\n\ngo 1.22\n", encoding="utf-8")

        # 1. Go: stdlib `strings.TrimSpace` should NOT link to custom `TrimSpace` in another package,
        #    Plus `ServeHTTP` lifecycle and `/api/v1/players/:id` API route contract
        go_api = root / "server" / "api"
        go_util = root / "server" / "util"
        go_api.mkdir(parents=True)
        go_util.mkdir(parents=True)

        (go_util / "text.go").write_text(
            """package util

func TrimSpace(custom string) string {
\treturn custom
}
""",
            encoding="utf-8",
        )
        (go_api / "handler.go").write_text(
            """package api

import (
\t"net/http"
\t"strings"
)

func RegisterPlayerRoutes() {
\thttp.HandleFunc("/api/v1/players/:id", nil)
}

func ParsePlayerInput(raw string) string {
\treturn strings.TrimSpace(raw)
}

func ServeHTTP(w http.ResponseWriter, r *http.Request) {
\t// ServeHTTP is invoked by net/http runtime
}
""",
            encoding="utf-8",
        )

        # 2. TypeScript: calls `/api/v1/players/{id}` + `componentDidMount` lifecycle + vitest test
        ts_dir = root / "web" / "src"
        ts_dir.mkdir(parents=True)
        (ts_dir / "player_client.ts").write_text(
            """export async function fetchPlayerProfile(id: string) {
  // Mention ServeHTTP and gd_only_inventory_sync in TS comment/string to verify language isolation
  const unrelatedNote = "gd_only_inventory_sync";
  return fetch(`/api/v1/players/{id}`);
}

export class PlayerView {
  componentDidMount(): void {}
}
""",
            encoding="utf-8",
        )
        (ts_dir / "player_client.test.ts").write_text(
            """import { fetchPlayerProfile } from "./player_client";

export async function test_fetchPlayerProfile() {
  await fetchPlayerProfile("42");
}
""",
            encoding="utf-8",
        )

        # 3. GDScript: uses `KEY_ESCAPE`, `MOUSE_BUTTON_LEFT`, and defines `gd_only_inventory_sync`
        gd_dir = root / "client"
        gd_dir.mkdir(parents=True)
        (gd_dir / "inventory.gd").write_text(
            """extends Node

func gd_only_inventory_sync(key_code: int) -> void:
\tif key_code == KEY_ESCAPE or key_code == MOUSE_BUTTON_LEFT:
\t\treturn
""",
            encoding="utf-8",
        )

        # 4. Rust + C# + Java sharing `session_timeout_sec` config contract + tests
        rs_dir = root / "rust_svc"
        cs_dir = root / "csharp_svc"
        java_dir = root / "java_svc"
        py_dir = root / "py_svc"
        for d in (rs_dir, cs_dir, java_dir, py_dir):
            d.mkdir(parents=True)

        (rs_dir / "config.rs").write_text(
            """pub struct RustConfig {
    #[serde(rename = "session_timeout_sec")]
    pub timeout_secs: u32,
}

pub fn verify_rust_session(c: &RustConfig) -> u32 {
    c.timeout_secs
}
""",
            encoding="utf-8",
        )
        (rs_dir / "test_config.rs").write_text(
            """#[test]
pub fn test_verify_rust_session() {
    let _ = verify_rust_session;
}
""",
            encoding="utf-8",
        )

        (cs_dir / "SessionConfig.cs").write_text(
            """public class SessionConfig {
    [JsonPropertyName("session_timeout_sec")]
    public int SessionTimeoutSec { get; set; }

    public void FixedUpdate() {}

    public bool ValidateSession(int elapsed) {
        return elapsed < SessionTimeoutSec;
    }
}
""",
            encoding="utf-8",
        )
        (cs_dir / "SessionConfigTests.cs").write_text(
            """public class SessionConfigTests {
    [Fact]
    public void TestValidateSession() {
        var s = new SessionConfig();
        s.ValidateSession(10);
    }
}
""",
            encoding="utf-8",
        )

        (java_dir / "AppConfig.java").write_text(
            """package com.example;

public class AppConfig {
    @JsonProperty("session_timeout_sec")
    private int sessionTimeoutSec;

    public void afterPropertiesSet() {}

    public boolean checkTimeout(int v) {
        return v < sessionTimeoutSec;
    }
}
""",
            encoding="utf-8",
        )
        (java_dir / "AppConfigTest.java").write_text(
            """package com.example;

public class AppConfigTest {
    @Test
    public void testCheckTimeout() {
        AppConfig cfg = new AppConfig();
        cfg.checkTimeout(5);
    }
}
""",
            encoding="utf-8",
        )

        (py_dir / "ctx.py").write_text(
            """class ManagedSession:
    def __enter__(self):
        return self

    def compute_session_hash(self, token: str) -> str:
        return token.strip()
""",
            encoding="utf-8",
        )
        (py_dir / "test_ctx.py").write_text(
            """from .ctx import ManagedSession

def test_compute_session_hash():
    s = ManagedSession()
    assert s.compute_session_hash(" abc ") == "abc"
""",
            encoding="utf-8",
        )

        # Verify 1: Go `strings.TrimSpace` in `ParsePlayerInput` does NOT link to `util.TrimSpace`
        trim_imp = get_symbol_impact("TrimSpace", project_path=str(root))
        assert "ParsePlayerInput" not in trim_imp

        # Verify 2: Godot `KEY_ESCAPE` and `MOUSE_BUTTON_LEFT` were NOT indexed as project constants
        esc_search = search_symbols("KEY_ESCAPE", project_path=str(root))
        assert "No symbols found" in esc_search

        # Verify 3: Language-isolated coverage scanner: `gd_only_inventory_sync` (GDScript) does NOT report `.ts` file text match!
        gd_imp = get_symbol_impact("gd_only_inventory_sync", project_path=str(root))
        assert "player_client.ts" not in gd_imp

        # Verify 4: Multi-language lifecycle methods (`ServeHTTP`, `__enter__`, `componentDidMount`, `FixedUpdate`, `afterPropertiesSet`)
        for lc_sym in ("ServeHTTP", "__enter__", "componentDidMount", "FixedUpdate", "afterPropertiesSet"):
            lc_imp = get_symbol_impact(lc_sym, project_path=str(root))
            assert "Engine Lifecycle Callback" in lc_imp

        # Verify 5A: Multi-language serialization contract (`session_timeout_sec` across Rust, C#, Java)
        ser_imp = get_symbol_impact("session_timeout_sec", project_path=str(root))
        assert "contract::config::session_timeout_sec" in ser_imp
        assert "RustConfig.timeout_secs" in ser_imp
        assert "SessionConfig.SessionTimeoutSec" in ser_imp
        assert "AppConfig.sessionTimeoutSec" in ser_imp

        # Verify 5B: Cross-language REST API route contract (`/api/v1/players/:id` in Go <-> `/api/v1/players/{id}` in TS)
        api_imp = get_symbol_impact("RegisterPlayerRoutes", project_path=str(root))
        assert "contract::api::/api/v1/players/{param}" in api_imp
        assert "fetchPlayerProfile" in api_imp

        # Verify 6: Multi-language `tests_for` commands (Python, TS, Rust, C#, Java)
        assert "pytest py_svc/test_ctx.py -k \"test_compute_session_hash\"" in tests_for("compute_session_hash", project_path=str(root))
        assert "npx vitest run web/src/player_client.test.ts" in tests_for("fetchPlayerProfile", project_path=str(root))
        assert "cargo test test_verify_rust_session" in tests_for("verify_rust_session", project_path=str(root))
        assert 'dotnet test --filter "FullyQualifiedName~TestValidateSession"' in tests_for("ValidateSession", project_path=str(root))
        assert "mvn test -Dtest=AppConfigTest#testCheckTimeout" in tests_for("checkTimeout", project_path=str(root))


if __name__ == "__main__":
    test_1_ignore_and_worktrees()
    test_2_3_4_5_6_godot_go_js_scoping_and_features()
    test_3_universal_polyglot_languages()
    test_4_contracts_data_i18n_tests_diff_concepts_lifecycle_telemetry()
    test_5_language_specific_noise_filtering_and_polyglot_contracts()
    print("ALL TESTS PASSED SUCCESSFULLY!")
