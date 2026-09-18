"""Shared test fixtures. Nothing in this suite makes a real cloud call."""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
# ...and then forced, unlike its neighbors: deployers/aws.py reads this once
# into REGION, and the cross-region tests in test_deployers.py are about
# REGION *differing* from a runtime ARN's own region. A developer with
# AWS_REGION=us-west-2 exported (which is how the cross-region bug those
# tests cover got found in the first place) would silently turn them into
# same-region no-ops that pass for the wrong reason -- confirmed, not
# hypothetical: with that export, replacing the ARN's region with REGION in
# get_trace() didn't fail a single test.
os.environ["AWS_REGION"] = "us-east-1"
os.environ.setdefault("AGENTCORE_EXECUTION_ROLE_ARN", "arn:aws:iam::000000000000:role/test-role")
os.environ.setdefault("AGENTCORE_STAGING_BUCKET", "test-bucket")
# Optional in production (most installs never run the one-time AgentCore Gateway
# setup), but set here for the same reason as every value above: routes now
# refuse a tool whose backing endpoint isn't configured (server.py's
# _tool_status), and tests about the platform allowlist or the tool catalog
# shouldn't depend on whether a developer happens to have a real Gateway. The
# tests that cover the refusal itself unset this explicitly.
os.environ.setdefault(
    "AGENTCORE_WEB_SEARCH_GATEWAY_URL", "https://test-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)

# Real bug, found live: every test using the `client` fixture below opens
# server.app as a TestClient context manager, which runs the real
# lifespan() startup -- including its webbrowser.open() call -- once per
# test. With ~20 tests using that fixture, a single `pytest` run opened
# ~20 real browser tabs. This must be forced here, unconditionally, not
# left as something a developer has to remember to export before running
# pytest -- that's exactly what didn't happen for an entire session.
os.environ["AGENT_PORTAL_NO_BROWSER"] = "true"

# Forced off for the same reason, in the other direction: a developer whose
# real .env sets AGENT_PORTAL_NO_SEED (to keep unrelated pre-existing cloud
# agents out of their own portal) would otherwise silently turn the two
# seeding tests below into no-ops that still pass for the wrong reason.
# The test covering the flag's own effect sets it explicitly instead.
os.environ["AGENT_PORTAL_NO_SEED"] = ""

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite file instead of touching a
    developer's real agent_portal.db. db._connect() reads db.DB_PATH at
    call time (not a value captured at import time), so monkeypatching the
    module attribute is enough."""
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_agent_portal.db")
    db.init_db()
    return db


class FakeDeployer:
    """In-memory stand-in for deployers/aws.py,
    implementing the same interface (see deployers/__init__.py's
    docstring) purely in Python with no network calls. Used to test
    server.py's routes/WebSocket protocol/seeding logic in complete
    isolation from any real cloud platform."""

    SUPPORTS_TRACING = True
    # Nothing to configure -- a fake deployer makes no cloud calls, so it's
    # always available and route-level tests aren't coupled to which .env
    # variables a real platform happens to need. A test about the
    # unconfigured-platform gate sets this on its own instance instead.
    REQUIRED_CONFIG = ()
    # Both packaging modes, so route-level tests can exercise the choice
    # without depending on which platforms happen to offer one in production.
    # A test needing a platform with *no* choice sets this to () on its own
    # instance (see test_server_api.py).
    DEPLOYMENT_MODES = ("code", "container")
    # Two regions, for the same reason as the two modes above: route-level tests
    # can exercise the choice (and its rejection) without depending on what any
    # real account is configured for. A test needing a platform with no region
    # choice sets this to () on its own instance.
    REGIONS = ("us-east-1", "us-west-2")

    def deployment_regions(self):
        return self.REGIONS

    def __init__(self, seed=()):
        self._next_id = 0
        self._deployed = {}  # resource_id -> {"name", "model", "description", "agent_instructions", "tools"}
        for entry in seed:
            self._deployed[entry["resource_id"]] = entry

    def deploy(
        self,
        name,
        model,
        description,
        agent_instructions,
        tool_ids,
        mcp_server_ids=(),
        deployment_mode=None,
        region=None,
        runtime_version=None,
    ):
        self._next_id += 1
        resource_id = f"fake-resource-{self._next_id}"
        self._deployed[resource_id] = {
            "name": name,
            "model": model,
            "description": description,
            "agent_instructions": agent_instructions,
            "tools": tool_ids,
            "mcp_servers": list(mcp_server_ids),
            # Both recorded so a test can assert what server.py actually
            # forwarded -- and that it forwards nothing for a platform with no
            # such choice -- rather than only that the deploy succeeded.
            "deployment_mode": deployment_mode,
            # Same reason as deployment_mode above: what server.py forwarded is
            # the thing worth asserting, including that it forwards nothing on a
            # platform with no region choice.
            "region": region,
            # AWS-only (deployers/aws.py's platform version).
            "runtime_version": runtime_version,
        }
        return resource_id

    def undeploy(self, resource_id):
        if resource_id not in self._deployed:
            raise RuntimeError(f"no such resource: {resource_id}")
        del self._deployed[resource_id]

    def list_deployed(self):
        return [{"name": v["name"], "resource_id": rid} for rid, v in self._deployed.items()]

    async def create_session(self, resource_id, user_id):
        return {"resource_id": resource_id, "user_id": user_id}

    async def close_session(self, session_state):
        pass

    async def stream_chat(self, resource_id, message, user_id, session_state):
        yield {"text": f"echo: {message}"}
        session_state["last_usage"] = {"input_tokens": 100, "output_tokens": 20}
        session_state["last_trace_id"] = f"fake-trace-{message}"

    async def latest_usage(self, session_state):
        return session_state.get("last_usage")

    async def latest_trace_id(self, session_state):
        return session_state.get("last_trace_id")

    async def latest_warmup_ms(self, session_state):
        return session_state.get("last_warmup_ms")

    async def latest_agent_init_ms(self, session_state):
        return session_state.get("last_agent_init_ms")

    async def latest_platform_startup_ms(self, session_state):
        return session_state.get("last_platform_startup_ms")

    async def latest_cold_start_ms(self, session_state):
        return session_state.get("last_cold_start_ms")

    async def latest_client_queue_ms(self, session_state):
        return session_state.get("last_client_queue_ms")

    async def latest_warmup_error(self, session_state):
        return session_state.get("last_warmup_error")

    async def latest_retries(self, session_state):
        return session_state.get("last_retries")

    async def wait_for_ready(self, session_state):
        session_state["last_warmup_ms"] = 0
        session_state["last_cold_start_ms"] = 0
        session_state["last_client_queue_ms"] = 0
        return 0

    def get_trace(self, resource_id, trace_id):
        return {"trace_id": trace_id, "spans": [{"name": "conversation", "duration_ms": None, "lines": [f"You: fake line for {trace_id}"]}]}


class FailingFakeDeployer(FakeDeployer):
    """Fails every deploy -- for testing the failure path (status flips to
    'failed', error_message is set, nothing hosted is left behind)."""

    def deploy(
        self,
        name,
        model,
        description,
        agent_instructions,
        tool_ids,
        mcp_server_ids=(),
        deployment_mode=None,
        region=None,
        runtime_version=None,
    ):
        raise RuntimeError("simulated deploy failure")


@pytest.fixture
def fake_deployers():
    return {"aws": FakeDeployer()}


@pytest.fixture
def client(monkeypatch, fake_deployers, isolated_db):
    """A TestClient wired to fake, in-memory deployers -- see FakeDeployer
    above. Every platform is faked, including 'aws' (a real
    NotImplementedError stub in production), so route-level tests aren't
    gated on which platforms happen to be implemented yet."""
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "DEPLOYERS", fake_deployers)
    with TestClient(server.app) as test_client:
        yield test_client
