"""
Futaba Memory System — Persistent knowledge and context.

The memory system stores:
- User preferences (coding style, preferred apps, workflows)
- Project context (directories, languages, tools)
- Past task outcomes (what worked, what failed)
- Successful workflows (reusable patterns)
- Important facts (API keys in use, common paths, etc.)

Architecture:
    Memory entries are stored as structured JSON documents in a
    local SQLite database for fast querying. Each entry has:
    - A category (preferences, projects, tasks, workflows, facts)
    - A key (unique within category)
    - Content (structured data)
    - Relevance score (for retrieval ranking)
    - Timestamps (created, accessed, modified)
    - Optional expiry

The memory is queried by the agent controller to provide context
for planning and execution decisions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from futaba.core.config import get_memory_dir

logger = logging.getLogger("futaba.memory")


# ---------------------------------------------------------------------------
# Memory Entry
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    """A single memory entry."""
    category: str       # preferences, projects, tasks, workflows, facts
    key: str            # Unique within category
    content: dict[str, Any] = field(default_factory=dict)
    summary: str = ""   # Human-readable summary
    tags: list[str] = field(default_factory=list)
    relevance: float = 1.0
    access_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    modified_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    accessed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str = ""  # Empty = never expires

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "key": self.key,
            "content": self.content,
            "summary": self.summary,
            "tags": self.tags,
            "relevance": self.relevance,
            "access_count": self.access_count,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "accessed_at": self.accessed_at,
            "expires_at": self.expires_at,
        }


# ---------------------------------------------------------------------------
# Memory Store (SQLite-backed)
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    SQLite-backed persistent memory store.

    Uses a simple schema with JSON content for flexibility.
    Supports category-based querying, tag filtering, and relevance ranking.
    """

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or (get_memory_dir() / "memory.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    relevance REAL NOT NULL DEFAULT 1.0,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    modified_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (category, key)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_category
                ON memories(category)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_tags
                ON memories(tags)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_relevance
                ON memories(relevance DESC)
            """)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with proper cleanup."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def store(self, entry: MemoryEntry) -> None:
        """Store or update a memory entry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO memories (
                    category, key, content, summary, tags, relevance,
                    access_count, created_at, modified_at, accessed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, key) DO UPDATE SET
                    content = excluded.content,
                    summary = excluded.summary,
                    tags = excluded.tags,
                    relevance = excluded.relevance,
                    modified_at = ?
            """, (
                entry.category, entry.key,
                json.dumps(entry.content), entry.summary,
                json.dumps(entry.tags), entry.relevance,
                entry.access_count, entry.created_at, now, now,
                entry.expires_at, now,
            ))

    def get(self, category: str, key: str) -> MemoryEntry | None:
        """Get a specific memory entry."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE category = ? AND key = ?",
                (category, key),
            ).fetchone()
            if row:
                # Update access time
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 "
                    "WHERE category = ? AND key = ?",
                    (now, category, key),
                )
                return self._row_to_entry(row)
        return None

    def query(
        self,
        category: str = "",
        tags: list[str] | None = None,
        search: str = "",
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """
        Query memories with optional filtering.

        Args:
            category: Filter by category (empty = all)
            tags: Filter by tags (any match)
            search: Full-text search in summary and content
            limit: Maximum results
        """
        conditions = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)

        if tags:
            # Check if any tag matches
            tag_conditions = []
            for tag in tags:
                tag_conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")
            conditions.append(f"({' OR '.join(tag_conditions)})")

        if search:
            conditions.append("(summary LIKE ? OR content LIKE ?)")
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY relevance DESC, access_count DESC, modified_at DESC
            LIMIT ?
        """
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_entry(row) for row in rows]

    def delete(self, category: str, key: str) -> bool:
        """Delete a memory entry."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE category = ? AND key = ?",
                (category, key),
            )
            return cursor.rowcount > 0

    def clear_category(self, category: str) -> int:
        """Clear all entries in a category."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE category = ?",
                (category,),
            )
            return cursor.rowcount

    def clear_all(self) -> int:
        """Clear all memories."""
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM memories")
            return cursor.rowcount

    def cleanup_expired(self) -> int:
        """Remove expired entries."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE expires_at != '' AND expires_at < ?",
                (now,),
            )
            return cursor.rowcount

    def stats(self) -> dict[str, Any]:
        """Get memory statistics."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            categories = conn.execute(
                "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category"
            ).fetchall()
            return {
                "total_entries": total,
                "categories": {row["category"]: row["cnt"] for row in categories},
                "db_path": str(self._db_path),
                "db_size_kb": round(self._db_path.stat().st_size / 1024, 1) if self._db_path.exists() else 0,
            }

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """Convert a database row to a MemoryEntry."""
        return MemoryEntry(
            category=row["category"],
            key=row["key"],
            content=json.loads(row["content"]),
            summary=row["summary"],
            tags=json.loads(row["tags"]),
            relevance=row["relevance"],
            access_count=row["access_count"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
            accessed_at=row["accessed_at"],
            expires_at=row["expires_at"],
        )


# ---------------------------------------------------------------------------
# High-Level Memory Manager
# ---------------------------------------------------------------------------

class MemoryManager:
    """
    High-level memory interface used by the agent controller.

    Provides semantic operations:
    - Remember user preferences
    - Remember project context
    - Remember task outcomes
    - Remember successful workflows
    - Query relevant context for planning
    """

    def __init__(self, store: MemoryStore | None = None):
        self._store = store or MemoryStore()

    @property
    def store(self) -> MemoryStore:
        return self._store

    # --- User Preferences ---

    def set_preference(self, key: str, value: Any, summary: str = "") -> None:
        """Store a user preference."""
        self._store.store(MemoryEntry(
            category="preferences",
            key=key,
            content={"value": value},
            summary=summary or f"Preference: {key}",
            tags=["preference"],
            relevance=1.5,  # Preferences are high-relevance
        ))

    def get_preference(self, key: str) -> Any:
        """Get a user preference value."""
        entry = self._store.get("preferences", key)
        if entry:
            return entry.content.get("value")
        return None

    # --- Project Context ---

    def remember_project(
        self,
        project_path: str,
        language: str = "",
        tools: list[str] | None = None,
        notes: str = "",
    ) -> None:
        """Remember context about a project."""
        self._store.store(MemoryEntry(
            category="projects",
            key=project_path,
            content={
                "path": project_path,
                "language": language,
                "tools": tools or [],
                "notes": notes,
            },
            summary=f"Project at {project_path} ({language})",
            tags=["project", language] if language else ["project"],
        ))

    def get_project_context(self, project_path: str) -> dict[str, Any] | None:
        """Get stored context about a project."""
        entry = self._store.get("projects", project_path)
        return entry.content if entry else None

    # --- Task Outcomes ---

    def remember_task_outcome(
        self,
        task_description: str,
        success: bool,
        approach: str,
        result: str,
        tags: list[str] | None = None,
    ) -> None:
        """Remember the outcome of a task for future reference."""
        import hashlib
        key = hashlib.sha256(task_description.encode()).hexdigest()[:16]
        self._store.store(MemoryEntry(
            category="tasks",
            key=key,
            content={
                "description": task_description,
                "success": success,
                "approach": approach,
                "result": result,
            },
            summary=f"{'✓' if success else '✗'} {task_description[:80]}",
            tags=tags or ["task"],
            relevance=1.2 if success else 0.8,
        ))

    # --- Workflows ---

    def remember_workflow(
        self,
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        success_rate: float = 1.0,
    ) -> None:
        """Remember a successful workflow pattern."""
        self._store.store(MemoryEntry(
            category="workflows",
            key=name,
            content={
                "description": description,
                "steps": steps,
                "success_rate": success_rate,
            },
            summary=f"Workflow: {name} - {description[:60]}",
            tags=["workflow"],
            relevance=1.0 + success_rate,
        ))

    # --- Context Retrieval ---

    def get_relevant_context(
        self,
        query: str,
        max_entries: int = 10,
    ) -> list[MemoryEntry]:
        """
        Get memories relevant to a query.

        Used by the planner to incorporate past knowledge into plans.
        """
        # Search across all categories
        results = self._store.query(search=query, limit=max_entries)

        # Also include highly-accessed preferences
        prefs = self._store.query(
            category="preferences",
            limit=5,
        )

        # Combine and deduplicate
        seen = set()
        combined = []
        for entry in results + prefs:
            entry_key = (entry.category, entry.key)
            if entry_key not in seen:
                seen.add(entry_key)
                combined.append(entry)

        # Sort by relevance
        combined.sort(key=lambda e: e.relevance, reverse=True)
        return combined[:max_entries]

    def get_context_summary(self, query: str = "", max_tokens: int = 2000) -> str:
        """
        Get a concise text summary of relevant memories for injection
        into the agent's context window.
        """
        entries = self.get_relevant_context(query, max_entries=15)
        if not entries:
            return ""

        parts = ["# Known Context"]
        char_budget = max_tokens * 4  # Rough chars-to-tokens ratio

        for entry in entries:
            line = f"- [{entry.category}] {entry.summary}"
            if len("\n".join(parts)) + len(line) > char_budget:
                break
            parts.append(line)

        return "\n".join(parts)
