# Changelog

## 0.2.0 — 2026-11-01

### Added
- **New MCP tools & CLI commands**
  - `tests_for` / `code-graph tests-for` — finds tests exercising a symbol and returns exact runnable commands for all 7 language families (`go test -run`, `pytest -k`, `npx vitest`/`jest -t`, `cargo test`, `dotnet test --filter`, `mvn test -Dtest` / `./gradlew test --tests`, `godot --headless -s`).
  - `get_diff_impact` / `code-graph diff-impact` — parses `git diff` hunks and reports modified symbols, upstream callers, affected cross-language contracts and runnable tests in one call.
  - `check_translations` / `code-graph i18n` — audits `tr()` / `noticeT()` calls against `.json` / `.po` locales: missing keys, raw non-key strings, `%d`/`%s`/`{}` placeholder mismatches, unused keys.
  - `get_token_savings` / `code-graph savings` — measured token savings (returned tokens vs raw file sizes), stored locally in `.code_graph.sqlite`. Nothing is sent over the network.
- **Language-aware noise filters** — standard library and engine singletons (e.g. `strings.TrimSpace`, `Mathf.Clamp`, `re.search`, `Input.is_action_pressed`, `Collections.emptyList`) no longer create false-positive edges to same-named project symbols, across Go, GDScript, Python, TS/JS, Rust, C# and Java. Godot built-in constant prefixes (`KEY_*`, `MOUSE_*`, `ERR_*`, …) are filtered too.
- **Framework lifecycle awareness** — runtime-invoked methods (Godot `_ready`/`_process`, Unity `Awake`/`Update`, React/Angular/Vue hooks, Next.js route handlers, Go `ServeHTTP`/`MarshalJSON`, Python dunders, Rust `Drop`/`Default`, Spring/Android/JUnit hooks) no longer produce misleading "0 callers" warnings.
- **Cross-language contracts (`shares_contract`)** — serialization/config tags (Go struct tags, `#[serde(rename)]`, `[JsonPropertyName]`, `@JsonProperty`/`@SerializedName`, Pydantic `Field(alias=)`, JS/TS schema keys, `seed/*.json`) and REST routes (`/api/v1/players/:id` ↔ `fetch("/api/v1/players/{id}")`) are linked across server and client code.

### Changed
- Coverage/text scans are isolated per language family: a GDScript symbol no longer scans `.go`/`.ts`/`.py` files; `.tscn`/`project.godot` are scanned only for Godot symbols; `.html`/`.vue`/`.svelte` templates only for JS/TS and Python symbols.
- `search_symbols` also searches data/i18n entries and comments/docstrings.

## 0.1.0 — 2026-11-01

- First public release: `get_edit_context`, `check_rename_or_delete`, `get_symbol_impact`, `find_flow`, `search_symbols`, `get_architecture_map`, `reindex_project`, Claude Code `PreToolUse` guard hook.
