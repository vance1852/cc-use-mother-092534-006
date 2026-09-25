"""在基础层数据库之上创建家风故事专属表，并提供短事务。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from festival_foundation.storage import Database


FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS story_versions (
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    outline TEXT NOT NULL,
    summary TEXT NOT NULL,
    persons_json TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_record_id TEXT,
    change_reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes INTEGER,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(story_id, version)
);
CREATE TABLE IF NOT EXISTS story_consents (
    consent_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    subject_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    scope TEXT NOT NULL,
    channel TEXT NOT NULL,
    note TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_story_subject ON story_consents(story_id, subject_id);
CREATE TABLE IF NOT EXISTS review_leases (
    lease_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    leased_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0, 1)),
    UNIQUE(story_id, version, stage)
);
CREATE TABLE IF NOT EXISTS review_opinions (
    review_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'returned')),
    note TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(story_id, version, stage)
);
CREATE TABLE IF NOT EXISTS publications (
    story_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    content_json TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY(story_id, version)
);
CREATE TABLE IF NOT EXISTS access_audit (
    access_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    view_scope TEXT NOT NULL,
    effective_scope TEXT NOT NULL,
    visible_fields_json TEXT NOT NULL,
    hidden_fields_json TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_story ON access_audit(story_id, version);
CREATE TABLE IF NOT EXISTS batch_imports (
    source_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    total INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
"""


class FamilyStorage:
    """复用基础层连接，追加家风故事表结构。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection
        self.connection.executescript(FAMILY_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator:
        with self.database.transaction(immediate=immediate) as connection:
            yield connection

    def close(self) -> None:
        self.database.close()
