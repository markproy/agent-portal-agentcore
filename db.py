"""SQLite storage for portal-managed agents. One table, no ORM -- stdlib
sqlite3 is plenty for a single-process local app with a handful of rows."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "agent_portal.db"

# A real cloud deploy/delete takes real time, so status tracks where an
# agent is in that lifecycle rather than assuming create/delete are instant.
STATUS_CREATING = "creating"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"
STATUS_DELETING = "deleting"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                model TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                agent_instructions TEXT NOT NULL DEFAULT '',
                tools TEXT NOT NULL DEFAULT '[]',
                mcp_servers TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                platform_resource_id TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                deployment_mode TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Renamed from "system_prompt" to "agent_instructions" for
        # terminology consistency across the portal -- CREATE TABLE IF NOT
        # EXISTS above only applies to a fresh DB, so an existing local
        # agent_portal.db (real deployed-agent rows, not just test fixtures)
        # needs an explicit one-time rename instead of silently losing its
        # data to a schema mismatch.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if "system_prompt" in columns:
            conn.execute("ALTER TABLE agents RENAME COLUMN system_prompt TO agent_instructions")
        # Same one-time-migration reason as the rename above: an existing
        # local DB predates this column, and a real deployed-agent row must
        # not be lost to a schema mismatch. Empty string, not NULL, means
        # "no packaging choice recorded" -- every agent on a platform that
        # has no such choice (Gemini/Azure), plus any AWS agent created
        # before this column existed or imported by startup seeding, where
        # the portal genuinely doesn't know.
        if "deployment_mode" not in columns:
            conn.execute("ALTER TABLE agents ADD COLUMN deployment_mode TEXT NOT NULL DEFAULT ''")
        # Where the agent was deployed, recorded even though a deployed agent's
        # resource id already contains it: a *failed* create has no resource id,
        # and that row is exactly the one where the region matters most -- both
        # to explain the failure and so a retry doesn't silently relocate the
        # agent. Same one-time-migration reason as the two above, and the same
        # meaning for '': not recorded (Gemini/Azure rows, rows imported by
        # startup seeding, rows predating this column).
        if "region" not in columns:
            conn.execute("ALTER TABLE agents ADD COLUMN region TEXT NOT NULL DEFAULT ''")


def _row_to_dict(row):
    d = dict(row)
    d["tools"] = json.loads(d["tools"])
    d["mcp_servers"] = json.loads(d["mcp_servers"])
    return d


def list_agents():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_agent(agent_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_agent_by_resource_id(platform_resource_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE platform_resource_id = ?", (platform_resource_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_agent(
    name,
    platform,
    model,
    description,
    agent_instructions,
    tools,
    mcp_servers=(),
    deployment_mode="",
    region="",
):
    agent_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO agents
               (id, name, platform, model, description, agent_instructions, tools, mcp_servers, status,
                deployment_mode, region, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                name,
                platform,
                model,
                description,
                agent_instructions,
                json.dumps(tools),
                json.dumps(list(mcp_servers)),
                STATUS_CREATING,
                deployment_mode,
                region,
                time.time(),
            ),
        )
    return agent_id


def insert_seeded_agent(name, platform, model, description, agent_instructions, tools, platform_resource_id, mcp_servers=()):
    """Like create_agent, but for an agent discovered already-deployed on the
    platform rather than one the portal is about to create -- inserted
    straight into ACTIVE status with its resource id already known."""
    agent_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO agents
               (id, name, platform, model, description, agent_instructions, tools, mcp_servers, status,
                platform_resource_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                name,
                platform,
                model,
                description,
                agent_instructions,
                json.dumps(tools),
                json.dumps(list(mcp_servers)),
                STATUS_ACTIVE,
                platform_resource_id,
                time.time(),
            ),
        )
    return agent_id


def set_status(agent_id, status, *, platform_resource_id=None, error_message=None):
    with _connect() as conn:
        conn.execute(
            """UPDATE agents SET status = ?, platform_resource_id = COALESCE(?, platform_resource_id),
               error_message = ? WHERE id = ?""",
            (status, platform_resource_id, error_message, agent_id),
        )


def delete_agent(agent_id):
    with _connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
