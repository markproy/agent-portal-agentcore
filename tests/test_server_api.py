"""API-level tests against the real FastAPI app, with every deployer
faked (see conftest.FakeDeployer) so nothing here touches a real cloud.
Covers the CRUD lifecycle, validation, startup seeding, and the chat
WebSocket protocol -- the contract the frontend (static/app.js) depends on."""

from tests.conftest import FailingFakeDeployer, FakeDeployer


def test_lifespan_never_opens_a_real_browser(monkeypatch, isolated_db, fake_deployers):
    """Regression test for a real bug: every test using the `client`
    fixture runs server.app's actual lifespan (via TestClient's context
    manager), which calls webbrowser.open() unless AGENT_PORTAL_NO_BROWSER
    is set -- conftest.py sets it unconditionally for exactly this reason.
    Without that, every test using `client` opened a real browser tab;
    with ~20 such tests, a single `pytest` run flooded the OS with ~20 of
    them. Patches webbrowser.open() *before* constructing TestClient
    (unlike the `client` fixture, which has already run lifespan by the
    time a test body using it executes) so this actually exercises the
    real startup path instead of asserting on a no-op."""
    import webbrowser

    import server
    from fastapi.testclient import TestClient

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(server, "DEPLOYERS", fake_deployers)

    with TestClient(server.app):
        pass

    assert opened == []


def test_get_config_lists_platforms_and_tools(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["platforms"]) == {"aws"}
    assert set(body["tools"]) == {"web_search", "web_search_aws", "stock_data"}
    assert set(body["mcp_servers"]) == set()
    # Per-platform packaging choices come from each deployer module, not from
    # PLATFORMS -- the form needs both the platform's list and the catalog of
    # labels/descriptions to render the Deployment field.
    assert body["platforms"]["aws"]["deployment_modes"] == ["code", "container"]
    assert set(body["deployment_modes"]) >= {"code", "container"}
    assert body["deployment_modes"]["container"]["label"]
    # Every choice carries whether it can actually be used, so the form can grey
    # out what it can't offer instead of accepting it and failing at deploy.
    # conftest configures all of these, so they're all available here.
    for entry in [*body["platforms"].values(), *body["tools"].values(), *body["mcp_servers"].values()]:
        assert entry["available"] is True
        assert entry["unavailable_reason"] == ""


def test_get_config_reports_unconfigured_tool_and_mcp_server_with_the_variable_to_set(client, monkeypatch):
    """The real failure this prevents: an agent with an unconfigured remote tool
    deploys fine, reaches READY, then fails every invoke (the hosted agent treats
    a tool that won't load as fatal). The entry stays listed -- it's something you
    could set up -- but says which variable to set."""
    from deployers import AVAILABLE_MCP_SERVERS

    monkeypatch.delenv("AGENTCORE_WEB_SEARCH_GATEWAY_URL")
    monkeypatch.delenv("TEST_MCP_SERVER_URL", raising=False)
    monkeypatch.setitem(
        AVAILABLE_MCP_SERVERS,
        "test-mcp",
        {"label": "Test MCP", "env_var": "TEST_MCP_SERVER_URL", "url": ""},
    )

    body = client.get("/api/config").json()

    gateway_tool = body["tools"]["web_search_aws"]
    assert gateway_tool["available"] is False
    assert "AGENTCORE_WEB_SEARCH_GATEWAY_URL" in gateway_tool["unavailable_reason"]
    assert body["mcp_servers"]["test-mcp"]["available"] is False
    assert "TEST_MCP_SERVER_URL" in body["mcp_servers"]["test-mcp"]["unavailable_reason"]
    # A tool with no external setup at all is unaffected -- only entries that
    # declare a "requires_env" can be unconfigured.
    assert body["tools"]["web_search"]["available"] is True


def test_get_config_reports_platform_unconfigured_when_its_required_config_is_missing(client, fake_deployers, monkeypatch):
    """A platform's own REQUIRED_CONFIG decides this, not a hardcoded list here
    -- which is what lets a fresh clone see "set these in .env" instead of three
    equally-inviting platforms, two of which fail on deploy."""
    monkeypatch.setattr(fake_deployers["aws"], "REQUIRED_CONFIG", ("MADE_UP_UNSET_VARIABLE",), raising=False)

    body = client.get("/api/config").json()

    assert body["platforms"]["aws"]["available"] is False
    assert "MADE_UP_UNSET_VARIABLE" in body["platforms"]["aws"]["unavailable_reason"]



def test_create_agent_passes_deployment_mode_through_and_stores_it(client, fake_deployers):
    resp = client.post(
        "/api/agents",
        json={
            "name": "container-agent",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": [],
            "deployment_mode": "container",
        },
    )
    assert resp.status_code == 201
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["deployment_mode"] == "container"
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["deployment_mode"] == "container"


def test_create_agent_records_the_platforms_default_deployment_mode(client, fake_deployers):
    """An agent created without an explicit choice still gets a mode recorded,
    because the deployer deploys *something* -- its first mode. A blank here
    would show up as an unlabelled card next to labelled ones, which is
    exactly what makes a code-vs-container comparison unreadable."""
    resp = client.post(
        "/api/agents",
        json={"name": "default-mode", "platform": "aws", "model": "m", "tools": []},
    )
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["deployment_mode"] == "code"
    # Nothing forwarded to deploy(), though: the deployer's own default is the
    # single source of truth for what unspecified means.
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["deployment_mode"] is None


def test_create_agent_rejects_a_mode_the_platform_does_not_offer(client, fake_deployers):
    resp = client.post(
        "/api/agents",
        json={
            "name": "x",
            "platform": "aws",
            "model": "m",
            "tools": [],
            "deployment_mode": "not-a-mode",
        },
    )
    assert resp.status_code == 400


def test_create_agent_on_a_platform_with_no_deployment_choice(client, fake_deployers):
    """A deployer with DEPLOYMENT_MODES = () means its deploy()
    takes no deployment_mode argument at all, so for those platforms the route
    must forward nothing (a stale value would TypeError inside the deployer)
    and store a blank -- while still rejecting a mode that was asked for and
    can't be honored, rather than quietly deploying something else."""
    fake_deployers["aws"].DEPLOYMENT_MODES = ()

    rejected = client.post(
        "/api/agents",
        json={
            "name": "no-choice",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": [],
            "deployment_mode": "container",
        },
    )
    assert rejected.status_code == 400

    resp = client.post(
        "/api/agents",
        json={"name": "no-choice", "platform": "aws", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "tools": []},
    )
    assert resp.status_code == 201
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["deployment_mode"] == ""
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["deployment_mode"] is None


def test_get_config_lists_each_platforms_deploy_regions(client, fake_deployers):
    """The form can only offer regions the deployer says it can actually deploy
    into (each one needs a staging bucket, and container mode an ECR repository),
    so the list comes from the module rather than being hardcoded in app.js. A
    platform that doesn't offer the choice reports an empty list, which is how
    the field knows to hide itself."""
    fake_deployers["aws"].REGIONS = ()

    body = client.get("/api/config").json()

    assert body["platforms"]["aws"]["regions"] == []


def test_create_agent_passes_region_through_and_stores_it(client, fake_deployers):
    resp = client.post(
        "/api/agents",
        json={
            "name": "west-agent",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": [],
            "region": "us-west-2",
        },
    )
    assert resp.status_code == 201
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["region"] == "us-west-2"
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["region"] == "us-west-2"


def test_create_agent_records_the_platforms_default_region(client, fake_deployers):
    """Same reasoning as the default deployment mode: the deploy landed in a real
    region, so the row has to name it even when the request didn't -- otherwise a
    recreate of that agent has nothing to prefill from and would silently move it
    to whatever the portal's default happens to be at the time."""
    resp = client.post(
        "/api/agents",
        json={"name": "default-region", "platform": "aws", "model": "m", "tools": []},
    )
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["region"] == "us-east-1"
    # Nothing forwarded to deploy(): the deployer's own default stays the single
    # source of truth for what an unspecified region means.
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["region"] is None


def test_create_agent_rejects_a_region_the_platform_does_not_offer(client, fake_deployers):
    """Caught here rather than at the API call, because in container mode a
    typo'd region isn't cheap: the deploy builds and pushes an image before
    CreateAgentRuntime ever sees the bad region."""
    resp = client.post(
        "/api/agents",
        json={
            "name": "x",
            "platform": "aws",
            "model": "m",
            "tools": [],
            "region": "us-west-1",
        },
    )
    assert resp.status_code == 400
    assert "us-west-1" in resp.json()["detail"]


def test_create_agent_on_a_platform_with_no_region_choice(client, fake_deployers):
    """A deployer with no deploy regions -- deploy() takes no
    region argument, so the route must forward nothing and store a blank -- while
    still rejecting a region that was asked for and can't be honored."""
    fake_deployers["aws"].REGIONS = ()

    rejected = client.post(
        "/api/agents",
        json={
            "name": "no-choice",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": [],
            "region": "us-west-2",
        },
    )
    assert rejected.status_code == 400

    resp = client.post(
        "/api/agents",
        json={"name": "no-choice", "platform": "aws", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "tools": []},
    )
    assert resp.status_code == 201
    agent = client.get(f"/api/agents/{resp.json()['id']}").json()
    assert agent["region"] == ""
    assert fake_deployers["aws"]._deployed[agent["platform_resource_id"]]["region"] is None


def test_list_agents_starts_empty(client):
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_agent_deploys_and_becomes_active(client):
    resp = client.post(
        "/api/agents",
        json={
            "name": "test-agent",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "description": "desc",
            "agent_instructions": "prompt",
            "tools": ["web_search"],
        },
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    # TestClient runs BackgroundTasks (the deploy call) before the request
    # completes, so the fake deploy has already resolved by the time we
    # check -- a real deploy takes minutes; this fake one is instant.
    agent = client.get(f"/api/agents/{agent_id}").json()
    assert agent["status"] == "active"
    assert agent["platform_resource_id"] == "fake-resource-1"


def test_create_agent_passes_mcp_servers_through_to_deploy(client, fake_deployers, monkeypatch):
    from deployers import AVAILABLE_MCP_SERVERS

    monkeypatch.setenv("TEST_MCP_SERVER_URL", "https://test-mcp.example.com/mcp")
    monkeypatch.setitem(
        AVAILABLE_MCP_SERVERS,
        "test-mcp",
        {"label": "Test MCP", "env_var": "TEST_MCP_SERVER_URL", "url": "https://test-mcp.example.com/mcp"},
    )

    resp = client.post(
        "/api/agents",
        json={
            "name": "mcp-agent",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": ["stock_data"],
            "mcp_servers": ["test-mcp"],
        },
    )
    agent_id = resp.json()["id"]

    agent = client.get(f"/api/agents/{agent_id}").json()
    assert agent["mcp_servers"] == ["test-mcp"]
    resource_id = agent["platform_resource_id"]
    assert fake_deployers["aws"]._deployed[resource_id]["mcp_servers"] == ["test-mcp"]


def test_create_agent_defaults_mcp_servers_to_empty(client):
    resp = client.post(
        "/api/agents",
        json={"name": "no-mcp-agent", "platform": "aws", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "tools": []},
    )
    agent_id = resp.json()["id"]
    assert client.get(f"/api/agents/{agent_id}").json()["mcp_servers"] == []


def test_create_agent_failure_marks_failed_with_message(client, monkeypatch, fake_deployers):
    fake_deployers["aws"] = FailingFakeDeployer()

    resp = client.post(
        "/api/agents",
        json={"name": "will-fail", "platform": "aws", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "tools": []},
    )
    agent_id = resp.json()["id"]

    agent = client.get(f"/api/agents/{agent_id}").json()
    assert agent["status"] == "failed"
    assert "simulated deploy failure" in agent["error_message"]
    assert agent["platform_resource_id"] is None


def test_create_agent_rejects_unknown_platform(client):
    resp = client.post("/api/agents", json={"name": "x", "platform": "not-a-platform", "model": "m", "tools": []})
    assert resp.status_code == 400


def test_create_agent_rejects_unavailable_platform(client, monkeypatch):
    # All three platforms happen to be available today -- this guards the
    # gate logic itself (server.py checks PLATFORMS[...]["available"])
    # independent of which platforms are currently implemented.
    import server

    monkeypatch.setitem(server.PLATFORMS, "aws", {"label": "AWS", "available": False})

    resp = client.post("/api/agents", json={"name": "x", "platform": "aws", "model": "m", "tools": []})
    assert resp.status_code == 400


def test_create_agent_rejects_empty_name(client):
    resp = client.post("/api/agents", json={"name": "   ", "platform": "aws", "model": "m", "tools": []})
    assert resp.status_code == 400


def test_create_agent_allows_platform_restricted_tool_on_matching_platform(client):
    resp = client.post(
        "/api/agents",
        json={
            "name": "x",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": ["web_search_aws"],
        },
    )
    assert resp.status_code == 201


def test_create_agent_rejects_tool_whose_setup_isnt_configured(client, monkeypatch):
    """Rejected server-side, not just greyed out in the form: the form isn't the
    only caller, and this particular mistake produces an agent that deploys
    successfully and is broken forever afterward."""
    monkeypatch.delenv("AGENTCORE_WEB_SEARCH_GATEWAY_URL")

    resp = client.post(
        "/api/agents",
        json={
            "name": "x",
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "tools": ["web_search_aws"],
        },
    )
    assert resp.status_code == 400
    assert "AGENTCORE_WEB_SEARCH_GATEWAY_URL" in resp.json()["detail"]


def test_create_agent_rejects_mcp_server_whose_url_isnt_configured(client, monkeypatch):
    from deployers import AVAILABLE_MCP_SERVERS

    monkeypatch.delenv("TEST_MCP_SERVER_URL", raising=False)
    monkeypatch.setitem(
        AVAILABLE_MCP_SERVERS,
        "test-mcp",
        {"label": "Test MCP", "env_var": "TEST_MCP_SERVER_URL", "url": ""},
    )

    resp = client.post(
        "/api/agents",
        json={"name": "x", "platform": "aws", "model": "m", "tools": [], "mcp_servers": ["test-mcp"]},
    )
    assert resp.status_code == 400
    assert "TEST_MCP_SERVER_URL" in resp.json()["detail"]


def test_create_agent_rejects_unknown_mcp_server(client):
    resp = client.post(
        "/api/agents",
        json={"name": "x", "platform": "aws", "model": "m", "tools": [], "mcp_servers": ["not-a-server"]},
    )
    assert resp.status_code == 400


def test_create_agent_rejects_platform_whose_required_config_is_missing(client, fake_deployers, monkeypatch):
    monkeypatch.setattr(fake_deployers["aws"], "REQUIRED_CONFIG", ("MADE_UP_UNSET_VARIABLE",), raising=False)

    resp = client.post("/api/agents", json={"name": "x", "platform": "aws", "model": "m", "tools": []})
    assert resp.status_code == 400
    assert "MADE_UP_UNSET_VARIABLE" in resp.json()["detail"]


def test_get_agent_404_for_missing(client):
    resp = client.get("/api/agents/does-not-exist")
    assert resp.status_code == 404


def test_delete_agent_removes_it(client):
    agent_id = client.post(
        "/api/agents", json={"name": "to-delete", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    assert client.get(f"/api/agents/{agent_id}").json()["status"] == "active"

    resp = client.delete(f"/api/agents/{agent_id}")
    assert resp.status_code == 202

    assert client.get(f"/api/agents/{agent_id}").status_code == 404


def test_delete_agent_404_for_missing(client):
    resp = client.delete("/api/agents/does-not-exist")
    assert resp.status_code == 404


def test_wait_until_actually_gone_polls_until_resource_disappears():
    # A platform whose undeploy() call returning success doesn't mean the
    # resource is actually gone yet (AWS's delete_agent_runtime() is
    # documented as asynchronous -- see docs/aws.md's "How AWS deploy works")
    # needs list_deployed() re-checked, not trusted on the first look.
    import server

    calls = {"n": 0}

    class StubDeployer:
        def list_deployed(self):
            calls["n"] += 1
            if calls["n"] < 3:
                return [{"name": "x", "resource_id": "still-there"}]
            return []

    server._wait_until_actually_gone(StubDeployer(), "still-there", attempts=10, interval_seconds=0)
    assert calls["n"] == 3


def test_wait_until_actually_gone_gives_up_on_timeout():
    import server

    class StubDeployer:
        def list_deployed(self):
            return [{"name": "x", "resource_id": "still-there"}]

    # Best-effort: returns (doesn't raise or hang) once attempts are
    # exhausted, even though the resource never actually disappeared.
    server._wait_until_actually_gone(StubDeployer(), "still-there", attempts=3, interval_seconds=0)


def test_wait_until_actually_gone_tolerates_list_deployed_failure():
    import server

    class StubDeployer:
        def list_deployed(self):
            raise RuntimeError("boom")

    # Shouldn't propagate -- a broken check shouldn't block the delete
    # that already succeeded on the platform's own undeploy() call.
    server._wait_until_actually_gone(StubDeployer(), "some-id", attempts=10, interval_seconds=0)


def test_wait_until_actually_gone_prefers_a_deployers_own_existence_check():
    """list_deployed() is per-region for AWS, so for an agent outside the portal's
    default region it comes back without the runtime and the guard reads "already
    gone" on the first look -- exactly when a recreate then collides with a name
    that still exists. A deployer that can answer authoritatively (aws.py's
    resource_exists(), which reads the region off the ARN) is asked instead."""
    import server

    calls = {"exists": 0, "list": 0}

    class StubDeployer:
        def resource_exists(self, resource_id):
            calls["exists"] += 1
            return calls["exists"] < 3

        def list_deployed(self):
            calls["list"] += 1
            return []

    server._wait_until_actually_gone(StubDeployer(), "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x", attempts=10, interval_seconds=0)
    assert calls["exists"] == 3
    assert calls["list"] == 0


def test_clamp_loadtest_params_respects_per_field_caps():
    import server

    users, iterations = server._clamp_loadtest_params(9999, 9999)
    assert users == server.LOADTEST_MAX_USERS
    assert iterations <= server.LOADTEST_MAX_ITERATIONS


def test_clamp_loadtest_params_respects_combined_cap():
    # Regression test for a real bug found live: users=9999/iterations=9999
    # was silently accepted and clamped down to a real 50 x 50 = 2500-
    # session run under an earlier version of this with only per-field caps
    # (each individually "reasonable"). The product must never exceed
    # LOADTEST_MAX_TOTAL_SESSIONS, no matter how each field clamps on its own.
    import server

    users, iterations = server._clamp_loadtest_params(9999, 9999)
    assert users * iterations <= server.LOADTEST_MAX_TOTAL_SESSIONS


def test_clamp_loadtest_params_leaves_small_requests_untouched():
    import server

    assert server._clamp_loadtest_params(3, 2) == (3, 2)


def test_clamp_loadtest_params_never_clamps_iterations_below_one():
    import server

    # A users value at the cap, with a combined cap that isn't an exact
    # multiple of it, must still leave at least 1 iteration -- not 0.
    users, iterations = server._clamp_loadtest_params(server.LOADTEST_MAX_USERS, 1)
    assert iterations >= 1


def test_clamp_loadtest_params_allows_a_long_loop_run_of_startup_only_sessions():
    # The point of the higher warmup-only ceiling: a loop run is users=1 with
    # the session count as iterations, and 500 of them has to survive the
    # clamp intact -- that sample size is the whole reason the ceiling moved.
    import server

    assert server._clamp_loadtest_params(1, 500, server.loadtest.MODE_WARMUP_ONLY) == (1, 500)


def test_clamp_loadtest_params_warmup_only_ceiling_still_binds_on_concurrency():
    # The raised ceiling is about sample size, not concurrency: 20 users x 25
    # iterations is 500 sessions and allowed, but LOADTEST_MAX_USERS is
    # unchanged, so nothing gets to fire more than 20 at once at one agent.
    import server

    users, iterations = server._clamp_loadtest_params(9999, 9999, server.loadtest.MODE_WARMUP_ONLY)
    assert users == server.LOADTEST_MAX_USERS
    assert users * iterations <= server.LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY


def test_clamp_loadtest_params_keeps_chat_at_the_lower_ceiling():
    # Every chat session is a real LLM call, so the higher ceiling must not
    # leak across modes -- 500 of these is real money, which is the entire
    # reason there are two numbers instead of one.
    import server

    users, iterations = server._clamp_loadtest_params(1, 500, server.loadtest.MODE_CHAT)
    assert users * iterations <= server.LOADTEST_MAX_TOTAL_SESSIONS


def test_clamp_loadtest_params_treats_an_unknown_mode_as_chat():
    # Fail closed: anything that isn't recognizably the cheap mode gets the
    # conservative ceiling, so a future mode can't inherit 500 by accident.
    import server

    users, iterations = server._clamp_loadtest_params(1, 500, "some_future_mode")
    assert users * iterations <= server.LOADTEST_MAX_TOTAL_SESSIONS
    assert server._clamp_loadtest_params(1, 500) == (users, iterations)


def test_frontend_mirrors_the_servers_loadtest_caps():
    """Cap constants live in server.py, the hint and input max live in app.js
    and index.html. Three places saying 20 are three places to update; this
    test makes them a checked constraint instead."""
    import server
    from pathlib import Path

    app_js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
    index_html = (Path(__file__).parent.parent / "static" / "index.html").read_text()

    assert f"const LOADTEST_MAX_USERS = {server.LOADTEST_MAX_USERS};" in app_js
    assert f'max="{server.LOADTEST_MAX_USERS}"' in index_html
    assert f"warmup_only: {server.LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY}" in app_js


def test_exc_detail_falls_back_to_repr_for_empty_str_exceptions():
    # Regression test for a real bug found live: a genuine 60s client
    # timeout during a load test surfaced as a fake "0.0s success" instead
    # of a visible error, because TimeoutError()'s own __str__ is "" --
    # falsy, so every downstream truthiness check treated it as no error
    # at all.
    import asyncio

    import server

    assert str(TimeoutError()) == ""  # confirms the premise this test guards against
    assert server._exc_detail(TimeoutError()) == repr(TimeoutError())
    assert server._exc_detail(asyncio.TimeoutError()) == repr(asyncio.TimeoutError())
    assert server._exc_detail(asyncio.TimeoutError()) != ""


def test_exc_detail_prefers_a_real_message_when_present():
    import server

    assert server._exc_detail(ValueError("bad agent_id")) == "bad agent_id"


def test_chat_websocket_echoes_through_fake_deployer(client):
    agent_id = client.post(
        "/api/agents", json={"name": "chat-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "hello"})
        assert ws.receive_json() == {"type": "answer_start", "turn": 1}
        # First turn gets the date-context preamble prepended (see
        # test_chat_websocket_prepends_date_context_once_per_session) --
        # just check the tail here, not the whole echoed string.
        msg = ws.receive_json()
        assert msg["type"] == "answer_delta"
        assert msg["text"].endswith("hello")
        end_msg = ws.receive_json()
        assert end_msg["type"] == "answer_end"
        assert end_msg["turn"] == 1
        assert end_msg["elapsed_seconds"] >= 0


def test_chat_websocket_sends_usage_after_answer_end(client):
    """Token usage isn't always ready by answer_end (see
    deployers/__init__.py's latest_usage docstring),
    so it's a separate message, not part of answer_end itself."""
    agent_id = client.post(
        "/api/agents", json={"name": "usage-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "hello"})
        ws.receive_json()  # answer_start
        ws.receive_json()  # answer_delta
        ws.receive_json()  # answer_end
        usage_msg = ws.receive_json()

    assert usage_msg == {"type": "usage", "turn": 1, "input_tokens": 100, "output_tokens": 20}


def test_chat_websocket_answer_end_includes_latency_breakdown(client, fake_deployers):
    """answer_end should carry everything needed to diagnose "why was this
    turn slow" inline -- warmup_ms/tool_calls/retries -- without a separate
    round trip, since (unlike token usage) all three are plain session_state
    reads that are already resolved by the time the turn's stream ends."""

    class ToolCallingFakeDeployer(FakeDeployer):
        async def stream_chat(self, resource_id, message, user_id, session_state):
            yield {"tool_call": {"name": "get_stock_price"}}
            yield {"tool_call": {"name": "get_stock_price"}}
            yield {"text": f"echo: {message}"}
            session_state["last_usage"] = {"input_tokens": 100, "output_tokens": 20}
            session_state["last_warmup_ms"] = 42
            session_state["last_cold_start_ms"] = 7500
            session_state["last_client_queue_ms"] = 35
            session_state["last_retries"] = 1

    fake_deployers["aws"] = ToolCallingFakeDeployer()
    agent_id = client.post(
        "/api/agents", json={"name": "latency-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "hello"})
        ws.receive_json()  # answer_start
        ws.receive_json()  # tool_call
        ws.receive_json()  # tool_call
        ws.receive_json()  # answer_delta
        end_msg = ws.receive_json()

    assert end_msg["type"] == "answer_end"
    assert end_msg["warmup_ms"] == 42
    assert end_msg["retries"] == 1
    assert end_msg["tool_calls"] == ["get_stock_price", "get_stock_price"]
    # Both warmup numbers, kept separate all the way to the client: 42ms is
    # what this turn waited on, 7.5s is what the warmup cost. The panel needs
    # both to show what pre-warming absorbed, so a turn that reported only
    # one of them (or collapsed them into one field) would break that row.
    assert end_msg["cold_start_ms"] == 7500
    # And the third: how much of that 7.5s was this process getting the call
    # out rather than the platform answering it. The panel only shows this row
    # when it's large enough to matter, but the turn always reports it.
    assert end_msg["client_queue_ms"] == 35


def test_chat_websocket_warmup_only_includes_platform_startup_split(client, fake_deployers):
    """The "warmup_only" message (what the load test's platform-startup-only
    mode drives) should surface the AWS-only platform_startup_ms/
    agent_init_ms split alongside warmup_ms, straight from wait_for_ready's
    session_state writes -- see deployers/aws.py's latest_platform_startup_ms.

    cold_start_ms rides along the same way. It's distinct from warmup_ms on
    purpose (the warmup's whole duration vs. just the part this caller waited
    on), so the fake reports a larger value here to pin down that the load
    test's view reads the full cold start and not the wait.

    client_queue_ms is the fourth, and the one that says whether any of the
    other three describe the platform at all: it's what the session spent
    inside this process before the call went out (see
    deployers/__init__.py's latest_client_queue_ms). A load test that couldn't
    see it read a stalled laptop as a 30s platform startup."""

    class SplitWarmupFakeDeployer(FakeDeployer):
        async def wait_for_ready(self, session_state):
            session_state["last_warmup_ms"] = 500
            session_state["last_agent_init_ms"] = 300
            session_state["last_platform_startup_ms"] = 500
            session_state["last_cold_start_ms"] = 800
            session_state["last_client_queue_ms"] = 40
            return 500

    fake_deployers["aws"] = SplitWarmupFakeDeployer()
    agent_id = client.post(
        "/api/agents", json={"name": "split-warmup-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "warmup_only"})
        done_msg = ws.receive_json()

    assert done_msg == {
        "type": "warmup_done",
        "warmup_ms": 500,
        # 500 + 300 = the 800ms cold start: the platform's own share and the
        # agent's own share, which is the whole point of the split.
        "platform_startup_ms": 500,
        "agent_init_ms": 300,
        "cold_start_ms": 800,
        # Not part of that sum, and deliberately so: it's the time before the
        # call the other three describe was even issued.
        "client_queue_ms": 40,
    }


def test_chat_websocket_warmup_only_omits_split_on_platforms_without_it(client, fake_deployers):
    """When the deployer returns None for the split (nothing
    there measures the moment the agent's own code got control -- see each
    module's own latest_platform_startup_ms docstring); the default
    FakeDeployer mirrors that by simply never setting last_agent_init_ms/
    last_platform_startup_ms."""
    agent_id = client.post(
        "/api/agents", json={"name": "plain-warmup-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "warmup_only"})
        done_msg = ws.receive_json()

    assert done_msg["agent_init_ms"] is None
    assert done_msg["platform_startup_ms"] is None


def test_chat_websocket_warmup_only_reports_a_swallowed_warmup_failure(client, fake_deployers):
    """The failure mode this branch exists to catch: wait_for_ready returned
    normally, with a plausible duration, for a session that never started --
    which is exactly what AWS does with a failed warmup ping on purpose
    (see deployers/__init__.py's latest_warmup_error). Found live: a 40-session
    run with expired credentials reported all-green successes at a p50 of
    218ms, because being told your signature is invalid is fast. The load test
    already records an "error" frame properly, so sending one is the whole
    fix; what must not happen is a warmup_done."""

    class FailedWarmupFakeDeployer(FakeDeployer):
        async def wait_for_ready(self, session_state):
            session_state["last_warmup_ms"] = 218
            session_state["last_cold_start_ms"] = 218
            session_state["last_warmup_error"] = RuntimeError("token is expired")
            return 218

    fake_deployers["aws"] = FailedWarmupFakeDeployer()
    agent_id = client.post(
        "/api/agents", json={"name": "failed-warmup-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "warmup_only"})
        msg = ws.receive_json()

    assert msg["type"] == "error"
    assert "token is expired" in msg["detail"]


def test_chat_websocket_usage_includes_input_token_delta_on_later_turns(client, fake_deployers):
    """The first turn has no prior turn to diff against; the second turn's
    usage message should carry input_tokens_delta against the first."""

    class GrowingUsageFakeDeployer(FakeDeployer):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream_chat(self, resource_id, message, user_id, session_state):
            self._turn += 1
            yield {"text": f"echo: {message}"}
            session_state["last_usage"] = {"input_tokens": 100 * self._turn, "output_tokens": 20}

    fake_deployers["aws"] = GrowingUsageFakeDeployer()
    agent_id = client.post(
        "/api/agents", json={"name": "growing-usage-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    def send_turn_and_collect_usage(ws, text):
        ws.send_json({"type": "user_message", "text": text})
        for _ in range(3):
            ws.receive_json()  # answer_start, answer_delta, answer_end
        # Usage and trace are both separate background tasks (see
        # test_chat_websocket_sends_trace_after_answer_end) -- don't assume
        # which of the next two messages is which.
        by_type = {}
        for _ in range(2):
            msg = ws.receive_json()
            by_type[msg["type"]] = msg
        return by_type["usage"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        usage1 = send_turn_and_collect_usage(ws, "first")
        usage2 = send_turn_and_collect_usage(ws, "second")

    assert "input_tokens_delta" not in usage1
    assert usage1["input_tokens"] == 100
    assert usage2["input_tokens"] == 200
    assert usage2["input_tokens_delta"] == 100


def test_chat_websocket_sends_trace_after_answer_end(client):
    """Mirrors the usage flow: one automatic fetch attempt after
    answer_end, tagged by turn. Usage and trace are both sent as separate
    background tasks, so don't assume which one a client sees first --
    just that both eventually arrive."""
    agent_id = client.post(
        "/api/agents", json={"name": "trace-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "first"})  # gets the date preamble -- see below
        for _ in range(5):
            ws.receive_json()

        ws.send_json({"type": "user_message", "text": "hello"})
        ws.receive_json()  # answer_start
        ws.receive_json()  # answer_delta
        ws.receive_json()  # answer_end
        by_type = {}
        for _ in range(2):
            msg = ws.receive_json()
            by_type[msg["type"]] = msg

    assert by_type["usage"]["turn"] == 2
    trace_msg = by_type["trace"]
    assert trace_msg["turn"] == 2
    assert trace_msg["trace_id"] == "fake-trace-hello"
    assert trace_msg["spans"][0]["lines"] == ["You: fake line for fake-trace-hello"]


def test_chat_websocket_get_trace_refetches_a_specific_turn(client):
    """The Refresh button re-requests a specific turn's trace by number --
    confirms the server looks up *that* turn's trace id (captured once,
    right after its own answer_end), not whatever's currently in
    session_state (which a later turn would have overwritten)."""
    agent_id = client.post(
        "/api/agents", json={"name": "refresh-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        # Turn 1 gets the date preamble prepended (see
        # test_chat_websocket_prepends_date_context_once_per_session) --
        # a throwaway turn here keeps "first"/"second" below clean, since
        # what matters for this test is which turn *number* gets refetched.
        ws.send_json({"type": "user_message", "text": "throwaway"})
        for _ in range(5):
            ws.receive_json()

        for text in ("first", "second"):
            ws.send_json({"type": "user_message", "text": text})
            # answer_start, answer_delta, answer_end, then usage and trace
            # in either order (both are separate background tasks) --
            # drain all five before sending the next turn.
            for _ in range(5):
                ws.receive_json()

        ws.send_json({"type": "get_trace", "turn": 2})
        trace_msg = ws.receive_json()

    assert trace_msg == {
        "type": "trace",
        "turn": 2,
        "trace_id": "fake-trace-first",
        "spans": [{"name": "conversation", "duration_ms": None, "lines": ["You: fake line for fake-trace-first"]}],
    }


def test_chat_websocket_get_trace_not_ready_for_bad_turn(client):
    """A turn number that never happened has no trace id to resolve, same
    as one whose telemetry just hasn't landed yet -- both are "not_ready"
    (Refresh-able), distinct from "not_supported" (this platform doesn't
    implement tracing at all, see the next test)."""
    agent_id = client.post(
        "/api/agents", json={"name": "no-trace-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "get_trace", "turn": 99})
        msg = ws.receive_json()

    assert msg == {"type": "trace_unavailable", "turn": 99, "reason": "not_ready"}


def test_chat_websocket_retries_a_trace_that_has_not_indexed_yet(client, fake_deployers, monkeypatch):
    """Telemetry isn't queryable the instant a turn ends -- on AgentCore the
    gen_ai.* records take 60-90s to land in CloudWatch Logs Insights. The
    server keeps trying on its own; the client only gets a Refresh button
    once the schedule is exhausted. Before this, the single immediate attempt
    always lost that race and Refresh was the only path to a trace.

    Reports "indexing" (a plain wait in the UI) while more attempts are
    queued, then the trace itself, with nothing requested by the client in
    between."""

    class SlowIndexingDeployer(FakeDeployer):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def get_trace(self, resource_id, trace_id):
            self.attempts += 1
            if self.attempts < 3:
                return None
            return super().get_trace(resource_id, trace_id)

    import server

    deployer = SlowIndexingDeployer()
    fake_deployers["aws"] = deployer
    monkeypatch.setattr(server, "_TRACE_RETRY_DELAYS", (0, 0, 0, 0))
    agent_id = client.post(
        "/api/agents", json={"name": "slow-trace-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    reasons, trace_msg = [], None
    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "hello"})
        # Read until the trace lands rather than counting messages: the
        # answer/usage frames and the trace attempts interleave freely.
        while trace_msg is None:
            msg = ws.receive_json()
            if msg["type"] == "trace_unavailable":
                reasons.append(msg["reason"])
            elif msg["type"] == "trace":
                trace_msg = msg

    assert deployer.attempts == 3
    assert reasons == ["indexing", "indexing"], reasons
    assert trace_msg["spans"][0]["lines"][0].startswith("You: fake line for fake-trace-")


def test_chat_websocket_stops_retrying_a_trace_and_offers_refresh(client, fake_deployers, monkeypatch):
    """The last automatic attempt reports "not_ready", not "indexing" --
    that's what puts the Refresh button back, so a trace that indexes even
    later than the schedule allows is still reachable by hand."""

    class NeverIndexesDeployer(FakeDeployer):
        def get_trace(self, resource_id, trace_id):
            return None

    import server

    fake_deployers["aws"] = NeverIndexesDeployer()
    monkeypatch.setattr(server, "_TRACE_RETRY_DELAYS", (0, 0))
    agent_id = client.post(
        "/api/agents", json={"name": "never-trace-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    reasons = []
    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "hello"})
        while len(reasons) < 2:  # one per scheduled attempt
            msg = ws.receive_json()
            if msg["type"] == "trace_unavailable":
                reasons.append(msg["reason"])

    assert reasons == ["indexing", "not_ready"], reasons


def test_chat_websocket_get_trace_not_supported_for_untraced_platform(client, fake_deployers):
    class NoTracingFakeDeployer(FakeDeployer):
        SUPPORTS_TRACING = False

    fake_deployers["aws"] = NoTracingFakeDeployer()
    agent_id = client.post(
        "/api/agents", json={"name": "untraced-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "get_trace", "turn": 1})
        msg = ws.receive_json()

    assert msg == {"type": "trace_unavailable", "turn": 1, "reason": "not_supported"}


def test_chat_websocket_prepends_date_context_once_per_session(client):
    """Every platform's session lifecycle is different enough (see
    server.py's chat_ws comment) that there's no uniform place in the
    deployed agent code to tell it today's date -- so the portal injects
    it once, here, on the first message of each WebSocket session, not on
    every turn."""
    agent_id = client.post(
        "/api/agents", json={"name": "date-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        ws.send_json({"type": "user_message", "text": "first"})
        ws.receive_json()  # answer_start
        first_answer = ws.receive_json()["text"]
        ws.receive_json()  # answer_end
        ws.receive_json()  # usage or trace (either order)
        ws.receive_json()  # trace or usage

        ws.send_json({"type": "user_message", "text": "second"})
        ws.receive_json()  # answer_start
        second_answer = ws.receive_json()["text"]
        ws.receive_json()  # answer_end
        ws.receive_json()  # usage or trace (either order)
        ws.receive_json()  # trace or usage

    assert "today's date is" in first_answer
    assert first_answer.endswith("first")
    assert second_answer == "echo: second"  # no context on later turns


def test_chat_websocket_rejects_inactive_agent(client):
    agent_id = client.post(
        "/api/agents", json={"name": "never-active", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    # Force it back to "creating" so the WebSocket handler's active check fails.
    import db

    db.set_status(agent_id, db.STATUS_CREATING)

    with client.websocket_connect(f"/ws/agents/{agent_id}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"


# /ws/loadtest's validation runs entirely before it ever opens a real
# WebSocket back against SELF_HOST -- see server.py's loadtest_ws -- so
# these exercise it directly via the fake in-process DB, same as the
# chat_ws tests above. The real run_load_test() path (the part that opens
# real loopback connections and drives real sessions) isn't reachable this
# way and is instead exercised manually against a real running server --
# see docs/latency.md's "Interactive load test".
def test_loadtest_websocket_rejects_non_start_message(client):
    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "not_start"})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert "start" in msg["detail"]


def test_loadtest_websocket_rejects_missing_agent_ids(client):
    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "start"})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert "1-2 agent_ids" in msg["detail"]


def test_loadtest_websocket_rejects_too_many_agent_ids(client):
    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "start", "agent_ids": ["a", "b", "c"]})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert "1-2 agent_ids" in msg["detail"]


def test_loadtest_websocket_rejects_duplicate_agent_ids(client):
    agent_id = client.post(
        "/api/agents", json={"name": "compare-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]

    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "start", "agent_ids": [agent_id, agent_id]})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert "two different agents" in msg["detail"]


def test_loadtest_websocket_rejects_inactive_agent_in_comparison(client):
    active_id = client.post(
        "/api/agents", json={"name": "active-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    inactive_id = client.post(
        "/api/agents", json={"name": "inactive-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    import db

    db.set_status(inactive_id, db.STATUS_CREATING)

    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "start", "agent_ids": [active_id, inactive_id]})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert inactive_id in msg["detail"]


# The two tests below drive a real run through loadtest_ws, which the
# validation tests above deliberately don't: a genuine run would have
# run_load_test open WebSockets back against SELF_HOST, and TestClient serves
# no port for it to reach. So run_load_test itself is replaced with a stand-in
# that feeds the same records through the same on_result callback -- the
# streaming protocol (interim summaries, stop, partial results) is what's
# under test here, not the load generation, which tests/test_loadtest.py
# already covers directly.
def _fake_run_load_test(count, per_session_delay=0.0):
    async def fake(host, agent_id, users, iterations, on_result, **kwargs):
        import asyncio

        for i in range(count):
            if per_session_delay:
                await asyncio.sleep(per_session_delay)
            await on_result(
                {
                    "user": 0,
                    "iteration": i,
                    "cold_start_ms": 2000.0 + i,
                    "warmup_ms": 1900.0 + i,
                    "agent_init_ms": 100.0,
                    "platform_startup_ms": 1900.0 + i,
                    "ttfa_ms": None,
                    "elapsed_ms": None,
                    "error": None,
                }
            )

    return fake


def test_loadtest_websocket_streams_interim_summaries_during_a_long_run(client, monkeypatch):
    """A loop run of hundreds of sessions is otherwise a blind wait -- the
    portal fills in its charts from these interim summaries. They're built with
    the same compute_summary the final one uses, so a number read mid-run and
    the same number at the end can't mean different things."""
    import server

    agent_id = client.post(
        "/api/agents", json={"name": "loop-agent", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    monkeypatch.setattr(server.loadtest, "run_load_test", _fake_run_load_test(25))

    interim = []
    with client.websocket_connect("/ws/loadtest") as ws:
        # The loop shape as the frontend sends it: one user, N sessions.
        ws.send_json({"type": "start", "agent_ids": [agent_id], "mode": "warmup_only", "users": 1, "iterations": 25})
        assert ws.receive_json()["type"] == "started"
        while True:
            msg = ws.receive_json()
            if msg["type"] == "progress_summary":
                interim.append(msg)
            elif msg["type"] == "done":
                done = msg
                break

    # 25 sessions at one summary every LOADTEST_PROGRESS_SUMMARY_EVERY.
    every = server.LOADTEST_PROGRESS_SUMMARY_EVERY
    assert [m["summaries"][agent_id]["total"] for m in interim] == list(range(every, 25 + 1, every))
    assert all(m["summaries"][agent_id]["platform_startup_ms"]["p50"] is not None for m in interim)
    assert done["summaries"][agent_id]["total"] == 25
    assert done["stopped"] is False


def test_loadtest_websocket_stop_keeps_the_sessions_already_completed(client, monkeypatch):
    """Stopping a long run must be a shorter run, not a lost one: before this,
    the only way out mid-run was closing the socket, which threw away every
    completed session -- unacceptable once a run can take half an hour."""
    import server

    agent_id = client.post(
        "/api/agents", json={"name": "stoppable", "platform": "aws", "model": "m", "tools": []}
    ).json()["id"]
    monkeypatch.setattr(server.loadtest, "run_load_test", _fake_run_load_test(200, per_session_delay=0.01))

    with client.websocket_connect("/ws/loadtest") as ws:
        ws.send_json({"type": "start", "agent_ids": [agent_id], "mode": "warmup_only", "users": 1, "iterations": 200})
        assert ws.receive_json()["type"] == "started"
        for _ in range(3):
            assert ws.receive_json()["type"] == "session_result"
        ws.send_json({"type": "stop"})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "done":
                break

    assert msg["stopped"] is True
    summary = msg["summaries"][agent_id]
    assert 0 < summary["total"] < 200
    # The kept sessions are real, summarized ones -- not an empty shell with a
    # count on it.
    assert summary["platform_startup_ms"]["p50"] is not None


def test_seeds_known_and_unknown_agents_on_startup(monkeypatch, isolated_db):
    """Startup discovery (seed_existing_agents) should import agents the
    portal didn't create itself -- with accurate backfilled config for
    known legacy agents, and a generic placeholder for unrecognized ones."""
    import server
    from fastapi.testclient import TestClient

    seeded_aws = FakeDeployer(
        seed=[
            {"name": "stock-analysis-agent", "resource_id": "legacy-res-1"},
            {"name": "some-other-agent", "resource_id": "legacy-res-2"},
        ]
    )
    monkeypatch.setattr(server, "DEPLOYERS", {"aws": seeded_aws})

    with TestClient(server.app) as test_client:
        agents = {a["name"]: a for a in test_client.get("/api/agents").json()}

    assert agents["stock-analysis-agent"]["tools"] == ["web_search", "stock_data"]
    # stock-analysis-agent seeded without full instructions when deployed outside the portal
    assert agents["stock-analysis-agent"]["tools"] == ["web_search", "stock_data"]

    assert agents["some-other-agent"]["description"] == "(imported — original config unknown)"


def test_no_seed_flag_skips_startup_discovery(monkeypatch, isolated_db):
    """AGENT_PORTAL_NO_SEED exists so an account hosting unrelated agents
    isn't force-imported into the portal, where an imported row's Delete
    button would issue a real delete against another project's resource."""
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_PORTAL_NO_SEED", "true")
    seeded_aws = FakeDeployer(seed=[{"name": "stock-analysis-agent", "resource_id": "legacy-res-1"}])
    monkeypatch.setattr(server, "DEPLOYERS", {"aws": seeded_aws})

    with TestClient(server.app) as test_client:
        assert test_client.get("/api/agents").json() == []


def test_seeding_does_not_duplicate_on_second_startup(monkeypatch, isolated_db):
    import server
    from fastapi.testclient import TestClient

    seeded_aws = FakeDeployer(seed=[{"name": "stock-analysis-agent", "resource_id": "legacy-res-1"}])
    deployers = {"aws": seeded_aws}
    monkeypatch.setattr(server, "DEPLOYERS", deployers)

    with TestClient(server.app) as test_client:
        pass
    with TestClient(server.app) as test_client:
        agents = test_client.get("/api/agents").json()

    assert len(agents) == 1
