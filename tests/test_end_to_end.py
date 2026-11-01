"""Comprehensive test suite for code-graph-mcp covering all features and acceptance criteria."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from code_graph.hooks import install_project_hooks_and_report
from code_graph.indexer import ProjectIndexer
from code_graph.mcp_server import (
    check_rename_or_delete,
    find_flow,
    get_architecture_map,
    get_edit_context,
    get_symbol_impact,
    reindex_project,
    search_symbols,
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


if __name__ == "__main__":
    test_1_ignore_and_worktrees()
    test_2_3_4_5_6_godot_go_js_scoping_and_features()
    test_3_universal_polyglot_languages()
    print("ALL TESTS PASSED SUCCESSFULLY!")
