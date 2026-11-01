"""Data models for the Code Knowledge Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GraphNode:
    """Represents a symbol or entity in the codebase."""

    id: str  # Unique identifier, e.g., "path/to/file.py::ClassName.method" or "event::PLAYER_DIED"
    name: str  # Short symbol name, e.g., "method" or "ClassName" or "PLAYER_DIED"
    qualified_name: str  # e.g., "ClassName.method"
    kind: str  # "file" | "class" | "function" | "method" | "event" | "module"
    file_path: str  # Relative path from project root
    start_line: int
    end_line: int
    signature: str  # Compact signature line
    language: str = ""

    @property
    def line_count(self) -> int:
        return max(1, self.end_line - self.start_line + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "file": self.file_path,
            "lines": f"{self.start_line}-{self.end_line}" if self.end_line > self.start_line else str(self.start_line),
            "line_count": self.line_count,
            "signature": self.signature,
            "language": self.language,
        }


@dataclass(slots=True)
class GraphEdge:
    """Represents a directed relationship between two symbols."""

    source_id: str  # Node ID of caller / importer / publisher / listener
    target_name: str  # Target symbol name or event name
    edge_type: str  # "calls" | "imports" | "publishes_event" | "listens_to" | "inherits"
    file_path: str  # File where this edge originates
    line: int  # Line number where call/import/event occurs
    receiver: str = ""  # Optional receiver object/module/autoload name (e.g. "Game", "self", "party")
    target_id: str | None = None  # Resolved node ID if known
    confidence: str = "exact"  # "exact" | "scoped" | "name_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "receiver": self.receiver,
            "type": self.edge_type,
            "confidence": self.confidence,
            "file": self.file_path,
            "line": self.line,
        }


@dataclass(slots=True)
class ParseResult:
    """Output of parsing a single source file."""

    file_path: str
    file_hash: str
    language: str
    mtime: float = 0.0
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # Local alias -> target path/module


@dataclass(slots=True)
class IndexStats:
    """Summary statistics from an indexing run."""

    project_root: str
    db_path: str
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    db_size_bytes: int = 0
    elapsed_ms: float = 0.0
    warning: str | None = None
