"""Pure SQLite CRUD tests -- no cloud calls, no server, just db.py against
an isolated throwaway file (see conftest.isolated_db)."""


def test_create_and_get_agent(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("my-agent", "gemini", "gemini-2.5-flash", "desc", "prompt", ["web_search"])

    agent = db.get_agent(agent_id)
    assert agent["name"] == "my-agent"
    assert agent["platform"] == "gemini"
    assert agent["status"] == db.STATUS_CREATING
    assert agent["tools"] == ["web_search"]
    assert agent["mcp_servers"] == []
    assert agent["platform_resource_id"] is None


def test_create_agent_with_mcp_servers(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("my-agent", "gemini", "gemini-2.5-flash", "", "", [], mcp_servers=["fred"])

    agent = db.get_agent(agent_id)
    assert agent["mcp_servers"] == ["fred"]


def test_create_agent_records_deployment_mode(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("aws-agent", "aws", "m", "", "", [], deployment_mode="container")
    assert db.get_agent(agent_id)["deployment_mode"] == "container"


def test_deployment_mode_defaults_to_blank_not_null(isolated_db):
    """Blank, never NULL: the column is NOT NULL, and every read path (the
    agent cards, the load test's agent picker) treats "" as "no packaging
    choice recorded" -- a None there would render as the string "null"."""
    db = isolated_db
    created = db.create_agent("gemini-agent", "gemini", "gemini-2.5-flash", "", "", [])
    seeded = db.insert_seeded_agent(
        name="imported", platform="aws", model="m", description="", agent_instructions="", tools=[],
        platform_resource_id="res-1",
    )
    assert db.get_agent(created)["deployment_mode"] == ""
    # Seeding discovers an already-deployed runtime and can't tell how it was
    # packaged, so it records nothing rather than guessing.
    assert db.get_agent(seeded)["deployment_mode"] == ""


def test_init_db_adds_deployment_mode_to_a_preexisting_db(isolated_db, monkeypatch, tmp_path):
    """The migration path, not the fresh-CREATE one: a developer's real
    agent_portal.db has rows for live deployed agents, and this column was
    added after those rows existed. Losing them to a schema mismatch would
    mean losing the portal's only record of what's deployed."""
    import sqlite3

    import db as db_module

    legacy_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db_module, "DB_PATH", legacy_path)
    with sqlite3.connect(legacy_path) as conn:
        # The pre-deployment_mode schema, with a row in it.
        conn.execute(
            """CREATE TABLE agents (
                   id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL, model TEXT NOT NULL,
                   description TEXT NOT NULL DEFAULT '', agent_instructions TEXT NOT NULL DEFAULT '',
                   tools TEXT NOT NULL DEFAULT '[]', mcp_servers TEXT NOT NULL DEFAULT '[]',
                   status TEXT NOT NULL, platform_resource_id TEXT, error_message TEXT, created_at REAL NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO agents (id, name, platform, model, status, created_at) VALUES ('old', 'legacy', 'aws', 'm', 'active', 1.0)"
        )

    db_module.init_db()

    agent = db_module.get_agent("old")
    assert agent["name"] == "legacy"
    assert agent["deployment_mode"] == ""


def test_get_agent_missing_returns_none(isolated_db):
    assert isolated_db.get_agent("does-not-exist") is None


def test_list_agents_orders_newest_first(isolated_db):
    db = isolated_db
    first = db.create_agent("first", "gemini", "gemini-2.5-flash", "", "", [])
    second = db.create_agent("second", "gemini", "gemini-2.5-flash", "", "", [])

    agents = db.list_agents()
    assert [a["id"] for a in agents] == [second, first]


def test_set_status_to_active_records_resource_id(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("my-agent", "gemini", "gemini-2.5-flash", "", "", [])

    db.set_status(agent_id, db.STATUS_ACTIVE, platform_resource_id="projects/x/reasoningEngines/1")

    agent = db.get_agent(agent_id)
    assert agent["status"] == db.STATUS_ACTIVE
    assert agent["platform_resource_id"] == "projects/x/reasoningEngines/1"
    assert agent["error_message"] is None


def test_set_status_to_failed_records_error_and_preserves_resource_id(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("my-agent", "gemini", "gemini-2.5-flash", "", "", [])
    db.set_status(agent_id, db.STATUS_ACTIVE, platform_resource_id="res-1")

    db.set_status(agent_id, db.STATUS_FAILED, error_message="boom")

    agent = db.get_agent(agent_id)
    assert agent["status"] == db.STATUS_FAILED
    assert agent["error_message"] == "boom"
    assert agent["platform_resource_id"] == "res-1"  # COALESCE keeps the prior value when None is passed


def test_delete_agent(isolated_db):
    db = isolated_db
    agent_id = db.create_agent("my-agent", "gemini", "gemini-2.5-flash", "", "", [])

    db.delete_agent(agent_id)

    assert db.get_agent(agent_id) is None
    assert db.list_agents() == []


def test_insert_seeded_agent_is_active_immediately(isolated_db):
    db = isolated_db
    agent_id = db.insert_seeded_agent(
        name="stock-analysis-agent",
        platform="gemini",
        model="gemini-2.5-flash",
        description="imported",
        agent_instructions="be terse",
        tools=["web_search", "stock_data"],
        platform_resource_id="projects/x/reasoningEngines/1",
    )

    agent = db.get_agent(agent_id)
    assert agent["status"] == db.STATUS_ACTIVE
    assert agent["platform_resource_id"] == "projects/x/reasoningEngines/1"


def test_get_agent_by_resource_id(isolated_db):
    db = isolated_db
    agent_id = db.insert_seeded_agent(
        name="a", platform="gemini", model="m", description="", agent_instructions="", tools=[], platform_resource_id="res-42"
    )

    found = db.get_agent_by_resource_id("res-42")
    assert found["id"] == agent_id
    assert db.get_agent_by_resource_id("no-such-resource") is None
