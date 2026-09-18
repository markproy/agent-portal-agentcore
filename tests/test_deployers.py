"""Tests for the pure, network-free parts of the deployer modules:
bridge_sync_iterable's thread/queue bridge, and each deployer's
name-mangling/packaging helpers. deploy()/undeploy()/list_deployed()/
create_session()/stream_chat() themselves make real cloud calls and are
covered by smoke/aws_smoke_test.py instead -- see this repo's
README for why those stay manual rather than running in CI."""

import ast
import asyncio
import base64
import inspect
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deployers import bridge_sync_iterable, is_configured, mcp_urls_for, tool_allowed_for_platform, unconfigured_reason
from deployers import aws as aws_deployer
from deployers.aws import _platform_version_kwargs
from deployers.aws import _slugify as aws_slugify


async def _collect(async_gen):
    return [item async for item in async_gen]


def test_bridge_sync_iterable_yields_all_items():
    def make_iter():
        return iter([1, 2, 3])

    items = asyncio.run(_collect(bridge_sync_iterable(make_iter)))
    assert items == [1, 2, 3]


def test_bridge_sync_iterable_propagates_exceptions():
    def make_iter():
        def gen():
            yield 1
            raise ValueError("boom")

        return gen()

    async def run():
        collected = []
        async for item in bridge_sync_iterable(make_iter):
            collected.append(item)
        return collected

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(run())




@pytest.mark.parametrize(
    "tool_id,platform,expected",
    [
        ("web_search", "aws", True),
        ("web_search_aws", "aws", True),
        ("not-a-real-tool", "aws", False),
    ],
)
def test_tool_allowed_for_platform(tool_id, platform, expected):
    assert tool_allowed_for_platform(tool_id, platform) is expected


@pytest.mark.parametrize(
    "value,expected,why",
    [
        ("https://real-service.run.app/mcp", True, "a genuinely filled-in value"),
        ("", False, "empty"),
        ("   ", False, "whitespace only"),
        ("your-gcp-project-id", False, "the bare your- placeholder"),
        # The real one: the AgentCore Gateway URL that killed a deployed agent
        # was the shipped placeholder with only its *region* edited, so a rule
        # that only compared against .env.example verbatim called it configured.
        ("https://gateway-xxxxxxxxxx.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp", False, "partial edit"),
        ("/subscriptions/<sub-id>/resourceGroups/rg", False, "an angle-bracket placeholder"),
        ("gs://my_bucket/path", True, "your_ is the marker, a bare underscore is not"),
    ],
)
def test_is_configured(monkeypatch, value, expected, why):
    monkeypatch.setenv("SOME_URL", value)
    assert is_configured("SOME_URL") is expected, why


def test_is_configured_false_when_variable_is_unset(monkeypatch):
    monkeypatch.delenv("SOME_URL", raising=False)
    assert is_configured("SOME_URL") is False


# Every assignment .env.example ships, split by whether the value is something
# only the account owner can supply. Keeping it keyed by variable name is the
# point: add a line to .env.example without classifying it here and this test
# fails, which is the only thing stopping _PLACEHOLDER_MARKERS from silently
# falling behind a placeholder shape it doesn't recognize.
_EXAMPLE_PLACEHOLDERS = (
    "AGENTCORE_EXECUTION_ROLE_ARN",
    "AGENTCORE_STAGING_BUCKET",
    "AGENTCORE_WEB_SEARCH_GATEWAY_URL",
)
_EXAMPLE_USABLE_DEFAULTS = (
    "AGENTCORE_REGION",
    "AWS_REGION",
    "AGENT_PORTAL_NO_SEED",
)


def _env_example_assignments():
    path = Path(__file__).parent.parent / ".env.example"
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_every_env_example_assignment_is_classified():
    assert set(_env_example_assignments()) == set(_EXAMPLE_PLACEHOLDERS) | set(_EXAMPLE_USABLE_DEFAULTS)


@pytest.mark.parametrize("env_var", _EXAMPLE_PLACEHOLDERS)
def test_placeholders_shipped_in_env_example_are_detected(monkeypatch, env_var):
    monkeypatch.setenv(env_var, _env_example_assignments()[env_var])
    assert is_configured(env_var) is False


@pytest.mark.parametrize("env_var", _EXAMPLE_USABLE_DEFAULTS)
def test_usable_defaults_shipped_in_env_example_are_not_flagged(monkeypatch, env_var):
    """The false positive this exists to prevent: GOOGLE_CLOUD_LOCATION=us-central1
    is a default meant to be kept, and an earlier rule that flagged any value
    matching .env.example marked a correctly-set variable as unconfigured."""
    monkeypatch.setenv(env_var, _env_example_assignments()[env_var])
    assert is_configured(env_var) is True


def test_unconfigured_reason_names_every_missing_variable(monkeypatch):
    monkeypatch.delenv("SOME_URL", raising=False)
    monkeypatch.setenv("OTHER_URL", "https://your-service.example.com/mcp")

    reason = unconfigured_reason(("SOME_URL", "OTHER_URL"))

    # Names them, because the fix is always "go set these in .env" -- a generic
    # "not configured" leaves the user to guess which variable.
    assert "SOME_URL" in reason and "OTHER_URL" in reason
    assert ".env" in reason


def test_unconfigured_reason_is_empty_when_everything_is_set(monkeypatch):
    monkeypatch.setenv("SOME_URL", "https://real.example.com/mcp")
    assert unconfigured_reason(("SOME_URL",)) == ""
    # () means "needs no configuration", not "nothing is configured".
    assert unconfigured_reason(()) == ""


def test_every_deployer_declares_required_config():
    """Mandatory like SUPPORTS_TRACING/DEPLOYMENT_MODES: a module that forgot
    would otherwise look exactly like one needing no setup, and get offered in
    the form regardless of whether this checkout can use it."""
    
    for module in (aws_deployer,):
        assert isinstance(module.REQUIRED_CONFIG, tuple)
        # Every name must be a variable the module actually reads, not a wish
        # list -- .env.example documents them all.
        for env_var in module.REQUIRED_CONFIG:
            assert env_var in Path(__file__).parent.parent.joinpath(".env.example").read_text()




@pytest.mark.parametrize(
    "name,expected",
    [
        ("stock_analysis_agent", "stock_analysis_agent"),
        ("My Cool Agent!", "My_Cool_Agent"),
        ("123-starts-with-digit", "a_123_starts_with_digit"),
        ("###", "agent"),
        ("", "agent"),
        ("x" * 60, "x" * 48),  # AgentRuntimeName caps at 48 chars
    ],
)
def test_aws_slugify(name, expected):
    assert aws_slugify(name) == expected


def _hosted_config_keys():
    """Every key aws_hosted/main.py reads as CONFIG["..."], read out of the file
    by AST rather than by importing it: that module is the code which runs
    *inside* the AgentCore container and imports strands/bedrock_agentcore/
    mcp_proxy_for_aws, none of which are portal dependencies (nor should they be
    -- see requirements-dev.txt)."""
    src = (Path(__file__).resolve().parents[1] / "aws_hosted" / "main.py").read_text()
    return {
        node.slice.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "CONFIG"
        and isinstance(node.slice, ast.Constant)
    }


def _hosted_gateway_region():
    """aws_hosted/main.py's _gateway_region, lifted out by AST and compiled on
    its own, for the same reason as _hosted_config_keys above: the module it
    lives in can't be imported here (strands/bedrock_agentcore aren't portal
    dependencies). The function is self-contained -- urlparse and os -- so
    executing just its definition tests the real code rather than a copy."""
    import os
    from urllib.parse import urlparse

    src = (Path(__file__).resolve().parents[1] / "aws_hosted" / "main.py").read_text()
    node = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.FunctionDef) and n.name == "_gateway_region"
    )
    namespace = {"os": os, "urlparse": urlparse}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["_gateway_region"]


@pytest.mark.parametrize(
    "gateway_url,expected",
    [
        # The shape the portal's own .env holds, and the case that matters: a
        # us-west-2 agent must sign for us-east-1 because that's where the one
        # shared gateway lives.
        ("https://agent-portal-web-search-pd3joeym9o.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp", "us-east-1"),
        ("https://gw.gateway.bedrock-agentcore.eu-central-1.amazonaws.com/mcp", "eu-central-1"),
        # Not that shape -> the agent's own region, which is both the old
        # behaviour and correct whenever the gateway is local to the agent.
        ("https://gateway.internal.example.com/mcp", "us-west-2"),
        ("", "us-west-2"),
        # Defends the "matched on the service label, not by position" comment:
        # nothing follows the service label here, so there's no region to read.
        ("https://bedrock-agentcore.amazonaws.com/mcp", "us-west-2"),
    ],
)
def test_aws_hosted_gateway_region_comes_from_the_gateway_url(gateway_url, expected, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert _hosted_gateway_region()(gateway_url) == expected


def test_aws_agent_config_matches_what_the_hosted_agent_reads():
    """The writer and the reader are two separately-deployed files, and nothing
    else in the suite would notice them disagreeing: main.py indexes CONFIG
    directly, so a key the deployer stops writing is a KeyError at container
    startup -- an agent that reaches READY and then fails every invoke, which is
    the worst failure shape this project has (see _check_web_search_gateway_url
    for the same lesson learned the expensive way).

    Equality both ways on purpose. A missing key breaks the agent; an extra one
    the agent never reads is dead weight in every artifact, and usually means a
    rename landed on one side only."""
    written = set(aws_deployer._agent_config("m", "i", ["web_search"], []))
    assert written == _hosted_config_keys()


def test_aws_agent_config_carries_instructions_verbatim():
    """The reason this moved out of environmentVariables at all: AgentCore
    rejects control characters in env var values, so a system prompt from a
    textarea needed an escape/unescape pair on both sides. JSON has no such
    limit, and the prompt must arrive at the model with its structure intact
    rather than as one run-on line."""
    instructions = "You are a helpful analyst.\n\nRules:\n1. Be brief.\n2. Cite sources.\ttabbed"
    assert aws_deployer._agent_config("m", instructions, [], [])["instructions"] == instructions


def test_aws_agent_config_omits_gateway_url_unless_the_tool_is_selected():
    """Configured in .env once, but it has no business travelling inside the
    artifact of every agent that didn't ask for web search."""
    with_tool = aws_deployer._agent_config("m", "i", [aws_deployer.WEB_SEARCH_TOOL_ID], [])
    without = aws_deployer._agent_config("m", "i", ["web_search"], [])
    assert with_tool["web_search_gateway_url"] == aws_deployer.WEB_SEARCH_GATEWAY_URL
    assert without["web_search_gateway_url"] == ""


def test_aws_agent_config_is_json_serializable_with_tuple_inputs():
    """deploy()'s tool_ids/mcp_server_ids arrive as tuples from some callers
    (smoke tests, a REPL) and json.dumps handles those -- but the agent side
    does `_WEB_SEARCH_TOOL_ID in tool_ids` and iterates, so this pins that the
    round trip through JSON gives back real lists either way."""
    config = aws_deployer._agent_config("m", "i", ("web_search", "stock_data"), ())
    assert json.loads(json.dumps(config))["tools"] == ["web_search", "stock_data"]


@pytest.mark.parametrize("runtime_version", [None, "V1"])
def test_aws_platform_version_omitted_for_v1(runtime_version):
    """V1 sends no platformVersion at all -- it's the platform default, so the
    field is redundant, and sending it would break creates on both the public
    SDK and an account not allowlisted for the preview. Passed
    supported=False to prove it doesn't even consult the SDK."""
    assert _platform_version_kwargs(runtime_version, supported=False) == {}


def test_aws_platform_version_v2_requires_preview_sdk():
    """The one case that must fail loudly rather than fall back: a V2 that
    silently deployed as V1 would be measured as V2 (see deployers/aws.py)."""
    assert _platform_version_kwargs("V2", supported=True) == {"platformVersion": "V2"}
    with pytest.raises(RuntimeError, match="private-preview SDK"):
        _platform_version_kwargs("V2", supported=False)


def test_aws_platform_version_rejects_unknown_version():
    """The preview models platformVersion as a free-form string, so nothing
    but this check stands between a typo and a service-side rejection paid for
    with a zip build and an S3 upload."""
    with pytest.raises(RuntimeError, match="must be one of"):
        _platform_version_kwargs("v2", supported=True)


def test_mcp_urls_for(monkeypatch):
    """See deployers/__init__.py."""
    from deployers import AVAILABLE_MCP_SERVERS

    monkeypatch.setitem(
        AVAILABLE_MCP_SERVERS,
        "test-mcp",
        {"label": "Test MCP", "env_var": "TEST_MCP_SERVER_URL", "url": "https://test-mcp.example.com/mcp"},
    )

    assert mcp_urls_for(["test-mcp"]) == [AVAILABLE_MCP_SERVERS["test-mcp"]["url"]]
    assert mcp_urls_for([]) == []
    assert mcp_urls_for(["unknown-server-id"]) == []


@pytest.mark.parametrize(
    "environ,expected,why",
    [
        ({"AGENTCORE_REGION": "us-east-1", "AWS_REGION": "us-west-2"}, "us-east-1", "the actual bug"),
        ({"AWS_REGION": "eu-west-1"}, "eu-west-1", "AWS_REGION alone still works"),
        ({}, "us-east-1", "neither set"),
        ({"AGENTCORE_REGION": "", "AWS_REGION": "eu-west-1"}, "eu-west-1", "empty is not a choice"),
        ({"AGENTCORE_REGION": "", "AWS_REGION": ""}, "us-east-1", "both empty"),
    ],
)
def test_resolve_region_precedence(monkeypatch, environ, expected, why):
    """AGENTCORE_REGION wins over AWS_REGION. The reason is the reverse case:
    AWS_REGION was exported machine-wide as us-west-2 by an unrelated tool, and
    because python-dotenv doesn't override an export, it beat .env's us-east-1
    and split this portal's agents across two regions with no error anywhere."""
    monkeypatch.delenv("AGENTCORE_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)

    assert aws_deployer._resolve_region() == expected, why


# --- AWS: the region comes from the runtime's own ARN --------------------
# The bug this covers, reported from the portal: an agent deployed while
# AWS_REGION was us-west-2 kept its us-west-2 ARN in the DB, but every
# client was built from the module-level REGION, so invoking it later from a
# us-east-1 portal answered "ResourceNotFoundException: No endpoint or agent
# found with qualifier 'DEFAULT' for agent 'arn:...us-west-2...'" -- a
# healthy runtime reported as a missing endpoint.

AWS_ARN_WEST = "arn:aws:bedrock-agentcore:us-west-2:355151823911:runtime/funny_analyst-WchV3q89Mv"
AWS_ARN_EAST = "arn:aws:bedrock-agentcore:us-east-1:355151823911:runtime/v1_analyst-aQEkbq2VBl"


@pytest.mark.parametrize(
    "resource_id,expected",
    [
        (AWS_ARN_WEST, "us-west-2"),
        (AWS_ARN_EAST, "us-east-1"),
        ("arn:aws:bedrock-agentcore:eu-central-1:1:runtime/x-y", "eu-central-1"),
        # Not an ARN at all: fall back to the configured region rather than
        # raising here, so a malformed id still fails at the API call.
        ("funny_analyst-WchV3q89Mv", aws_deployer.REGION),
        ("", aws_deployer.REGION),
        # Region field empty (a partition-level ARN) -- same fallback.
        ("arn:aws:bedrock-agentcore::1:runtime/x-y", aws_deployer.REGION),
    ],
)
def test_aws_region_of(resource_id, expected):
    assert aws_deployer._region_of(resource_id) == expected


def test_aws_clients_follow_the_arns_region():
    """boto3.client() itself makes no network call and needs no credentials,
    so this asserts the real client's own resolved region rather than a mock's
    recorded kwargs -- nothing to keep in sync with botocore's behavior."""
    assert aws_deployer._data_client(AWS_ARN_WEST).meta.region_name == "us-west-2"
    assert aws_deployer._data_client(AWS_ARN_EAST).meta.region_name == "us-east-1"
    assert aws_deployer._control_client(AWS_ARN_WEST).meta.region_name == "us-west-2"
    # No ARN to read: control-plane calls that aren't about one existing
    # runtime (create, list) belong to the region this portal deploys into.
    assert aws_deployer._control_client().meta.region_name == aws_deployer.REGION


def test_aws_clients_are_shared_per_region():
    """The whole point of the cache: one client per (service, region), which
    is what keeps _MAX_POOL_CONNECTIONS meaningful -- a fresh client per
    invoke would give every session its own 50-connection pool and quietly
    undo the load test's fix for pool-queueing time being measured as
    cold-start latency."""
    assert aws_deployer._data_client(AWS_ARN_WEST) is aws_deployer._data_client(AWS_ARN_WEST)
    # Same region, different runtime in it -- still one client.
    other_west = AWS_ARN_WEST.replace("funny_analyst-WchV3q89Mv", "something_else-AbCdEf1234")
    assert aws_deployer._data_client(AWS_ARN_WEST) is aws_deployer._data_client(other_west)
    assert aws_deployer._data_client(AWS_ARN_WEST) is not aws_deployer._data_client(AWS_ARN_EAST)
    # Two services in one region are two clients, not one.
    assert aws_deployer._data_client(AWS_ARN_WEST) is not aws_deployer._control_client(AWS_ARN_WEST)


def test_aws_control_client_region_argument_for_calls_with_no_arn():
    """create/list have no ARN to read a region off, so they say it outright.
    Without this the create path had no way to target anything but REGION."""
    assert aws_deployer._control_client(region="eu-central-1").meta.region_name == "eu-central-1"
    # An ARN still wins: it's about one existing runtime, which can only be
    # reached in its own region.
    assert aws_deployer._control_client(AWS_ARN_WEST, region="eu-central-1").meta.region_name == "us-west-2"


# --- AWS: which regions the portal offers, and what each one needs -------
# Region is a per-agent choice at create time (deployment_regions() ->
# deploy(region=...)); everything afterwards reads it back off the ARN, covered
# above. What's worth pinning here is the create side: the offered list, the
# per-region staging bucket, and that every regional call in a deploy really is
# pointed at the target region rather than the module-level default.


@pytest.mark.parametrize(
    "configured,expected,why",
    [
        (None, ["us-east-1"], "unset means single-region, still offering the default"),
        ("", ["us-east-1"], "empty means the same as unset"),
        ("us-west-2", ["us-east-1", "us-west-2"], "the default is always offered, and offered first"),
        (" us-west-2 , eu-central-1 ", ["us-east-1", "us-west-2", "eu-central-1"], "whitespace is tolerated"),
        ("us-east-1,us-west-2", ["us-east-1", "us-west-2"], "naming the default again doesn't duplicate it"),
        ("us-west-2,us-west-2", ["us-east-1", "us-west-2"], "a repeated region appears once"),
        ("us-west-2,,eu-central-1", ["us-east-1", "us-west-2", "eu-central-1"], "an empty entry is dropped"),
    ],
)
def test_aws_deployment_regions(monkeypatch, configured, expected, why):
    """First entry is the default -- server.py records it as the agent's region
    when a request doesn't name one, and the form shows it first, so the order
    is load-bearing rather than cosmetic."""
    monkeypatch.setattr(aws_deployer, "REGION", "us-east-1")
    monkeypatch.delenv("AGENTCORE_REGIONS", raising=False)
    if configured is not None:
        monkeypatch.setenv("AGENTCORE_REGIONS", configured)

    assert aws_deployer.deployment_regions() == expected, why


def test_aws_staging_bucket_defaults_to_the_single_one(monkeypatch):
    monkeypatch.setattr(aws_deployer, "STAGING_BUCKET", "one-bucket")
    monkeypatch.delenv("AGENTCORE_STAGING_BUCKET_US_WEST_2", raising=False)
    assert aws_deployer._staging_bucket_for("us-west-2") == "one-bucket"


def test_aws_staging_bucket_per_region_override(monkeypatch):
    """A second region needs a second bucket -- AgentCore reads the zip from its
    own region and answers 301 for a bucket elsewhere (see
    _check_staging_bucket). The variable's name carries the region with
    underscores, which is the only spelling an environment variable allows."""
    monkeypatch.setattr(aws_deployer, "STAGING_BUCKET", "east-bucket")
    monkeypatch.setenv("AGENTCORE_STAGING_BUCKET_US_WEST_2", "west-bucket")
    assert aws_deployer._staging_bucket_for("us-west-2") == "west-bucket"
    assert aws_deployer._staging_bucket_for("us-east-1") == "east-bucket"


def _fake_s3_in(location):
    class FakeS3:
        def get_bucket_location(self, Bucket):
            return {"LocationConstraint": location}

    return FakeS3()


def test_aws_staging_bucket_check_rejects_a_bucket_in_another_region(monkeypatch):
    """The failure this replaces: the deploy builds the zip, uploads it fine
    (S3 accepts a cross-region write), and CreateAgentRuntime then says only
    "S3 operation failed: Moved Permanently" -- naming neither the bucket, the
    region, nor the fix. Confirmed against the real service before being
    turned into this check."""
    monkeypatch.setattr(aws_deployer, "_client", lambda service, region: _fake_s3_in("us-east-1"))
    with pytest.raises(RuntimeError) as failure:
        aws_deployer._check_staging_bucket("east-bucket", "us-west-2")
    message = str(failure.value)
    assert "east-bucket" in message and "us-east-1" in message and "us-west-2" in message
    # Names the variable to set and the command to create the bucket, since
    # that's the entire remedy.
    assert "AGENTCORE_STAGING_BUCKET_US_WEST_2" in message
    assert "create-bucket" in message


def test_aws_staging_bucket_check_accepts_us_east_1s_empty_location(monkeypatch):
    """GetBucketLocation answers None for us-east-1 -- an artifact of the API
    predating regions, not a missing value. Reading it as "unknown" would
    reject the single-region default setup this project ships with."""
    monkeypatch.setattr(aws_deployer, "_client", lambda service, region: _fake_s3_in(None))
    aws_deployer._check_staging_bucket("east-bucket", "us-east-1")


def test_aws_staging_bucket_check_tolerates_not_being_able_to_look(monkeypatch):
    """A GetBucketLocation the caller isn't allowed to make says nothing about
    whether the bucket works -- refusing the deploy on it would invent a
    permission requirement that deploying itself doesn't have."""

    def cannot_look(service, region):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(aws_deployer, "_client", cannot_look)
    aws_deployer._check_staging_bucket("east-bucket", "us-west-2")


class _FakeExistenceClient:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, status=None):
        self.status = status
        self.asked = []

    def get_agent_runtime(self, agentRuntimeId):
        self.asked.append(agentRuntimeId)
        if self.status is None:
            raise self.exceptions.ResourceNotFoundException("no such runtime")
        return {"status": self.status}


@pytest.mark.parametrize(
    "status,expected,why",
    [
        ("READY", True, "a live runtime exists"),
        ("CREATE_FAILED", True, "still there, still holding its name"),
        ("DELETING", True, "the name stays reserved for the whole teardown -- see the docstring"),
        (None, False, "ResourceNotFound is the honest answer for a deleted runtime"),
    ],
)
def test_aws_resource_exists(monkeypatch, status, expected, why):
    """Asked by ARN, in the runtime's own region -- which is the whole point.
    server.py's delete confirmation used to scan list_deployed(), and that is
    per-region: for an agent outside the default region it can't contain the
    resource, so the check read as "already gone" immediately.

    DELETING is not gone here -- that's the key difference from list_deployed's
    _GONE_STATUSES. AgentCore holds the name reserved until the delete finishes,
    so reading DELETING as gone let the recreate fire before the name was free."""
    client = _FakeExistenceClient(status)
    seen = {}

    def fake_control_client(resource_id=None, region=None):
        seen["resource_id"] = resource_id
        return client

    monkeypatch.setattr(aws_deployer, "_control_client", fake_control_client)
    assert aws_deployer.resource_exists(AWS_ARN_WEST) is expected, why
    assert seen["resource_id"] == AWS_ARN_WEST
    # The id, not the whole ARN -- what GetAgentRuntime actually takes.
    assert client.asked == ["funny_analyst-WchV3q89Mv"]


class _FakeListingClient:
    """A control client whose ListAgentRuntimes returns one runtime."""

    class exceptions:
        class ConflictException(Exception):
            pass

    def __init__(self, runtimes):
        self._runtimes = runtimes

    def get_paginator(self, name):
        if self._runtimes is None:
            raise RuntimeError("ListAgentRuntimes is not available to this caller")
        return SimpleNamespace(paginate=lambda: [{"agentRuntimes": self._runtimes}])


def test_aws_name_conflict_says_to_wait_when_the_old_runtime_is_still_deleting():
    client = _FakeListingClient([{"agentRuntimeName": "a1", "agentRuntimeId": "a1-XYZ", "status": "DELETING"}])
    detail = aws_deployer._name_conflict_detail(client, "a1", "us-west-2")
    assert "still being deleted" in detail
    assert "a1-XYZ" in detail
    assert "retry" in detail


def test_aws_name_conflict_names_the_leftover_from_a_failed_deploy():
    """CREATE_FAILED holds the name indefinitely; the portal has no row for it,
    so the message has to carry the id and the delete command."""
    client = _FakeListingClient(
        [{"agentRuntimeName": "a1", "agentRuntimeId": "a1-DEAD", "status": "CREATE_FAILED"}]
    )
    detail = aws_deployer._name_conflict_detail(client, "a1", "us-east-1")
    assert "leftover from a failed deploy" in detail
    assert "a1-DEAD" in detail
    assert "--region us-east-1 " in detail


def test_aws_name_conflict_still_reports_the_conflict_when_the_lookup_fails():
    detail = aws_deployer._name_conflict_detail(_FakeListingClient(None), "a1", "us-west-2")
    assert "already exists in us-west-2" in detail


class _FakeDeletingClient:
    def __init__(self, fails=False):
        self.deleted = []
        self._fails = fails

    def delete_agent_runtime(self, agentRuntimeId):
        if self._fails:
            raise RuntimeError("AccessDenied")
        self.deleted.append(agentRuntimeId)


def test_aws_failed_create_is_cleaned_up_so_its_name_is_reusable():
    """A CREATE_FAILED runtime holds its name forever and the portal has no row
    for it (the deploy raised before storing one), so without cleanup the name
    is unusable from the UI with no way to see why."""
    client = _FakeDeletingClient()
    note = aws_deployer._clean_up_failed_create(client, "a1-DEAD")
    assert client.deleted == ["a1-DEAD"]
    assert "free to reuse" in note


def test_aws_failed_create_cleanup_never_hides_the_deploy_error():
    note = aws_deployer._clean_up_failed_create(_FakeDeletingClient(fails=True), "a1-DEAD")
    assert "could not be cleaned up" in note
    assert "AccessDenied" in note


def test_aws_only_a_failed_create_gets_the_deletable_error_type(monkeypatch):
    """UPDATE_FAILED must not be cleaned up: that runtime existed and was
    serving before the update."""

    def client_for(status):
        return SimpleNamespace(
            get_agent_runtime=lambda agentRuntimeId: {"status": status, "failureReason": "boom"}
        )

    monkeypatch.setattr(aws_deployer.time, "sleep", lambda _: None)

    monkeypatch.setattr(aws_deployer, "_control_client", lambda resource_id=None, region=None: client_for("CREATE_FAILED"))
    with pytest.raises(aws_deployer._RuntimeCreateFailed):
        aws_deployer._wait_for_ready("a1-DEAD", "us-west-2")

    monkeypatch.setattr(aws_deployer, "_control_client", lambda resource_id=None, region=None: client_for("UPDATE_FAILED"))
    with pytest.raises(RuntimeError) as exc_info:
        aws_deployer._wait_for_ready("a1-LIVE", "us-west-2")
    assert not isinstance(exc_info.value, aws_deployer._RuntimeCreateFailed)


# --- AWS: code-zip vs container deployment modes -------------------------
# CreateAgentRuntime takes either artifact and the portal offers both (see
# deployers/aws.py's DEPLOYMENT_MODES), so what's worth pinning is that each
# mode sends the artifact it claims to, that the container path really builds
# for Graviton and authenticates without putting a password in argv, and that
# the two modes are otherwise identical -- an environment or entry point that
# differed between them would quietly invalidate every mode-vs-mode latency
# comparison this feature exists to make.

_FAKE_REPOSITORY_URI = "355151823911.dkr.ecr.us-east-1.amazonaws.com/agent-portal-agents"


class _FakeEcrClient:
    class exceptions:
        class RepositoryNotFoundException(Exception):
            pass

    def __init__(self, repository_missing=False):
        self.repository_missing = repository_missing

    def describe_repositories(self, repositoryNames):
        if self.repository_missing:
            raise self.exceptions.RepositoryNotFoundException("does not exist")
        return {"repositories": [{"repositoryUri": _FAKE_REPOSITORY_URI}]}

    def get_authorization_token(self):
        return {"authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:s3cret").decode()}]}


class _FakeControlClient:
    def __init__(self):
        self.created = []

    def create_agent_runtime(self, **kwargs):
        self.created.append(kwargs)
        return {"agentRuntimeId": "fake-id", "agentRuntimeArn": AWS_ARN_EAST}


@pytest.fixture
def aws_deploy_harness(monkeypatch, tmp_path):
    """Fakes everything deploy() reaches out to -- the control plane, ECR, S3,
    and the container builder subprocess -- and records what each was asked
    to do. Nothing here makes a network call or runs docker."""
    control = _FakeControlClient()
    ecr = _FakeEcrClient()
    builder_calls = []
    build_contexts = {}
    staged_configs = []
    # Which region each part of the deploy was pointed at. Recorded because
    # region is threaded through the create path by hand (see deploy()) and the
    # failure mode of getting one of them wrong is silent: an image pushed to
    # one region's ECR while the runtime is created in another builds and pushes
    # cleanly, then fails at CREATE with a pull error.
    regions = {"control": [], "ecr": [], "s3": [], "s3_check": [], "wait": []}

    # The staging bucket reports itself as being in whichever region it was
    # asked about, so the pairing check passes by default -- a test about the
    # mispaired case (see below) makes it answer a different one.
    bucket_locations = {}

    def fake_client(service, region):
        if service == "s3":
            regions["s3_check"].append(region)
            location = bucket_locations.get("location", region)
            return type("S3", (), {"get_bucket_location": lambda _self, Bucket: {"LocationConstraint": location}})()
        assert service == "ecr", f"unexpected client for {service}"
        regions["ecr"].append(region)
        return ecr

    def fake_run(cmd, input=None, capture_output=False, text=False):
        builder_calls.append({"cmd": cmd, "input": input})
        if cmd[1] == "build":
            # The build context is a temp staging directory deleted right
            # after, so what it contained is captured here rather than
            # inspected afterward.
            context = Path(cmd[-1])
            build_contexts[cmd[-1]] = sorted(p.name for p in context.iterdir())
            staged_configs.append(json.loads((context / aws_deployer.AGENT_CONFIG_FILENAME).read_text()))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    zip_path = tmp_path / "package.zip"
    zip_path.write_bytes(b"not really a zip")
    uploads = []

    def fake_boto3_client(service, region_name=None):
        assert service == "s3"
        regions["s3"].append(region_name)
        return type("S3", (), {"upload_file": lambda _self, path, bucket, key: uploads.append((bucket, key))})()

    def fake_control_client(resource_id=None, region=None):
        regions["control"].append(region)
        return control

    def fake_wait_for_ready(agent_runtime_id, region, deployment_mode=None):
        regions["wait"].append(region)

    monkeypatch.setattr(aws_deployer, "_control_client", fake_control_client)
    monkeypatch.setattr(aws_deployer, "_client", fake_client)
    monkeypatch.setattr(aws_deployer, "_wait_for_ready", fake_wait_for_ready)
    # Stubbed rather than run for real (pip cross-targeting wheels at Graviton
    # is a network build), but the config it was handed is recorded: that's the
    # only place the code path's per-agent configuration can be observed now
    # that it travels inside the artifact instead of in environmentVariables.
    def fake_build_package(agent_config):
        staged_configs.append(agent_config)
        return zip_path

    monkeypatch.setattr(aws_deployer, "_build_deployment_package", fake_build_package)
    monkeypatch.setattr(aws_deployer.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(aws_deployer.subprocess, "run", fake_run)
    return {
        "control": control,
        "ecr": ecr,
        "builder_calls": builder_calls,
        "build_contexts": build_contexts,
        "staged_configs": staged_configs,
        "uploads": uploads,
        "regions": regions,
        "bucket_locations": bucket_locations,
    }


def _deploy(**kwargs):
    return aws_deployer.deploy(
        "mode test", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "desc", "be brief", ["stock_data"], **kwargs
    )


def test_aws_deploy_defaults_to_a_code_zip_artifact(aws_deploy_harness):
    assert _deploy() == AWS_ARN_EAST
    artifact = aws_deploy_harness["control"].created[0]["agentRuntimeArtifact"]
    assert "containerConfiguration" not in artifact
    assert artifact["codeConfiguration"]["runtime"] == aws_deployer._RUNTIME_ENUM
    assert artifact["codeConfiguration"]["entryPoint"] == ["opentelemetry-instrument", "main.py"]
    assert len(aws_deploy_harness["uploads"]) == 1
    # No image build for a code deploy: docker isn't even required to be
    # installed for the mode that doesn't use it.
    assert aws_deploy_harness["builder_calls"] == []


def test_aws_deploy_container_mode_builds_pushes_and_sends_a_container_artifact(aws_deploy_harness):
    assert _deploy(deployment_mode="container") == AWS_ARN_EAST
    artifact = aws_deploy_harness["control"].created[0]["agentRuntimeArtifact"]
    assert "codeConfiguration" not in artifact
    image_uri = artifact["containerConfiguration"]["containerUri"]
    assert image_uri.startswith(f"{_FAKE_REPOSITORY_URI}:mode_test-")
    # Nothing staged to S3 -- the image *is* the artifact.
    assert aws_deploy_harness["uploads"] == []

    build, login, push = aws_deploy_harness["builder_calls"]
    assert build["cmd"][:2] == [aws_deployer.CONTAINER_BUILDER, "build"]
    # Graviton, same architecture the code-zip path cross-targets its wheels
    # at -- an x86_64 image is rejected by AgentCore at deploy time.
    assert "--platform" in build["cmd"] and "linux/arm64" in build["cmd"]
    assert image_uri in build["cmd"]
    # The image is built from exactly the files it needs, so both modes
    # provably ship the same agent source.
    assert aws_deploy_harness["build_contexts"][build["cmd"][-1]] == [
        "Dockerfile",
        "agent_config.json",
        "main.py",
        "requirements.txt",
        "tools.py",
    ]
    assert login["cmd"] == [
        aws_deployer.CONTAINER_BUILDER,
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        _FAKE_REPOSITORY_URI.split("/")[0],
    ]
    # Over stdin, never in argv where `ps` would show it.
    assert login["input"] == "s3cret"
    assert push["cmd"] == [aws_deployer.CONTAINER_BUILDER, "push", image_uri]


def test_aws_container_image_tags_are_unique_per_deploy(aws_deploy_harness):
    """A mutable tag (:latest) would let one agent's redeploy silently change
    what another agent is running, which is exactly what a mode-vs-mode
    comparison can't tolerate."""
    _deploy(deployment_mode="container")
    _deploy(deployment_mode="container")
    tags = {
        c["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        for c in aws_deploy_harness["control"].created
    }
    assert len(tags) == 2


def test_aws_both_deployment_modes_ship_the_same_configuration(aws_deploy_harness):
    _deploy()
    _deploy(deployment_mode="container")
    code_call, container_call = aws_deploy_harness["control"].created
    for field in ("agentRuntimeName", "roleArn", "environmentVariables", "networkConfiguration",
                  "protocolConfiguration", "description"):
        assert code_call[field] == container_call[field], f"{field} differs between deployment modes"
    # The agent's own model/instructions/tools no longer travel in
    # environmentVariables, so comparing the CreateAgentRuntime calls alone
    # would no longer notice the two modes configuring an agent differently.
    # One of these came from the zip builder and the other was read back out of
    # the image's build context, which is the point.
    code_config, container_config = aws_deploy_harness["staged_configs"]
    assert code_config == container_config


def test_aws_deploy_keeps_the_agents_configuration_out_of_environment_variables(aws_deploy_harness):
    """What the move to agent_config.json bought, and the regression most worth
    guarding: Runtime V2 caps the whole environmentVariables payload at 1024
    bytes, which a real system prompt blows through on its own. Only the region
    belongs here now -- anything per-agent creeping back in reintroduces both
    that ceiling and AgentCore's rejection of control characters in values."""
    _deploy()
    environment = aws_deploy_harness["control"].created[0]["environmentVariables"]
    assert environment == {"AWS_REGION": aws_deployer.REGION}
    aws_deployer._check_v2_env_payload(environment)  # would raise if V2 couldn't take it


def test_aws_deploy_rejects_an_unknown_deployment_mode(aws_deploy_harness):
    with pytest.raises(RuntimeError, match="deployment_mode must be one of"):
        _deploy(deployment_mode="containr")
    # Rejected before anything was built, uploaded, or created.
    assert aws_deploy_harness["builder_calls"] == []
    assert aws_deploy_harness["uploads"] == []
    assert aws_deploy_harness["control"].created == []


def test_aws_container_mode_explains_a_missing_ecr_repository(aws_deploy_harness):
    """The repository is one-time setup, like the S3 staging bucket -- so the
    error has to name what fixes it, and must land before a full container
    build has been paid for. It names the setup script rather than the bare
    create-repository command because creating the repository alone leaves the
    execution role unable to pull (see the pull-hint tests below)."""
    aws_deploy_harness["ecr"].repository_missing = True
    with pytest.raises(RuntimeError, match=r"scripts/setup_aws_container\.sh"):
        _deploy(deployment_mode="container")
    assert aws_deploy_harness["builder_calls"] == []
    assert aws_deploy_harness["control"].created == []


# The pull-permission failure is the one container-mode setup mistake that
# can't be caught before the build: the push succeeds and the *runtime* fails
# minutes later, so the only place left to explain it is the CREATE_FAILED
# message the agent's card ends up showing. Real reasons seen from the service
# are terse and never mention IAM.
_PULL_FAILURE_REASONS = [
    "Failed to pull image from ECR",
    "AccessDeniedException: not authorized to perform: ecr:BatchGetImage",
    "CannotPullContainerError: pull access denied",
]


@pytest.mark.parametrize("failure_reason", _PULL_FAILURE_REASONS)
def test_aws_container_create_failure_points_at_the_setup_script(failure_reason, monkeypatch):
    control = _FakeControlClient()
    monkeypatch.setattr(aws_deployer, "_control_client", lambda resource_id=None, region=None: control)
    monkeypatch.setattr(aws_deployer.time, "sleep", lambda seconds: None)
    control.get_agent_runtime = lambda agentRuntimeId: {
        "status": "CREATE_FAILED",
        "failureReason": failure_reason,
    }
    with pytest.raises(RuntimeError) as failure:
        aws_deployer._wait_for_ready("fake-id", "us-east-1", deployment_mode="container")
    # The service's own reason is kept, not replaced -- the hint is additive.
    assert failure_reason in str(failure.value)
    assert "scripts/setup_aws_container.sh" in str(failure.value)


def test_aws_code_mode_create_failure_gets_no_container_hint(monkeypatch):
    """A code-zip deploy has no image to pull, so ECR advice there would just
    be a wrong lead on an unrelated failure."""
    control = _FakeControlClient()
    monkeypatch.setattr(aws_deployer, "_control_client", lambda resource_id=None, region=None: control)
    monkeypatch.setattr(aws_deployer.time, "sleep", lambda seconds: None)
    # Deliberately a reason containing a marker word ("image"), so this pins
    # the mode check rather than only the reason matching.
    control.get_agent_runtime = lambda agentRuntimeId: {
        "status": "CREATE_FAILED",
        "failureReason": "Invalid image entrypoint",
    }
    with pytest.raises(RuntimeError) as failure:
        aws_deployer._wait_for_ready("fake-id", "us-east-1", deployment_mode="code")
    assert "setup_aws_container.sh" not in str(failure.value)


def test_aws_unrelated_container_failure_keeps_its_own_reason(monkeypatch):
    """A container failure that clearly isn't about pulling shouldn't be
    dressed up as an IAM problem."""
    control = _FakeControlClient()
    monkeypatch.setattr(aws_deployer, "_control_client", lambda resource_id=None, region=None: control)
    monkeypatch.setattr(aws_deployer.time, "sleep", lambda seconds: None)
    control.get_agent_runtime = lambda agentRuntimeId: {
        "status": "CREATE_FAILED",
        "failureReason": "Health check on port 8080 never succeeded",
    }
    with pytest.raises(RuntimeError) as failure:
        aws_deployer._wait_for_ready("fake-id", "us-east-1", deployment_mode="container")
    assert "Health check on port 8080" in str(failure.value)
    assert "setup_aws_container.sh" not in str(failure.value)


def test_aws_container_mode_explains_a_missing_container_builder(aws_deploy_harness, monkeypatch):
    """Docker not installed (or not running) is the most likely first failure
    on this path, and a bare FileNotFoundError says nothing about which of the
    two deployment modes needs it or that the other one doesn't."""

    def no_docker(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'docker'")

    monkeypatch.setattr(aws_deployer.subprocess, "run", no_docker)
    with pytest.raises(RuntimeError, match="code-zip mode"):
        _deploy(deployment_mode="container")
    assert aws_deploy_harness["control"].created == []


def test_aws_deploy_targets_the_requested_region(aws_deploy_harness, monkeypatch):
    """Every regional part of a code-zip deploy has to point at the *target*
    region, not the portal's default: the create itself, the readiness poll that
    only finds the runtime in its own region, the staging-bucket check, and the
    AWS_REGION the hosted agent then signs its Bedrock calls with."""
    monkeypatch.setenv("AGENTCORE_STAGING_BUCKET_US_WEST_2", "west-bucket")
    _deploy(region="us-west-2")

    created = aws_deploy_harness["control"].created[0]
    assert created["environmentVariables"] == {"AWS_REGION": "us-west-2"}
    assert created["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"] == "west-bucket"
    assert aws_deploy_harness["uploads"][0][0] == "west-bucket"
    regions = aws_deploy_harness["regions"]
    assert regions["control"] == ["us-west-2"]
    assert regions["wait"] == ["us-west-2"]
    assert regions["s3"] == ["us-west-2"]
    assert regions["s3_check"] == ["us-west-2"]


def test_aws_deploy_container_mode_targets_the_requested_regions_ecr(aws_deploy_harness):
    """An image pushed to one region's ECR while the runtime is created in
    another builds and pushes cleanly, then leaves the runtime in CREATE_FAILED
    with a pull error -- a registry is only pullable from its own region."""
    _deploy(deployment_mode="container", region="us-west-2")
    # Twice: the repository lookup, then the login token, both regional.
    assert aws_deploy_harness["regions"]["ecr"] == ["us-west-2", "us-west-2"]
    assert aws_deploy_harness["regions"]["control"] == ["us-west-2"]
    # No zip, so no bucket to check.
    assert aws_deploy_harness["regions"]["s3_check"] == []


def test_aws_deploy_without_a_region_uses_the_default(aws_deploy_harness):
    """The single-region install has to behave exactly as it did before region
    became a parameter -- REGION, everywhere, with nothing new required."""
    _deploy()
    created = aws_deploy_harness["control"].created[0]
    assert created["environmentVariables"] == {"AWS_REGION": aws_deployer.REGION}
    assert aws_deploy_harness["regions"]["control"] == [aws_deployer.REGION]
    assert aws_deploy_harness["regions"]["wait"] == [aws_deployer.REGION]


def test_aws_missing_ecr_repository_names_the_region_to_set_it_up_in(aws_deploy_harness):
    """Container setup is per-region, so the remedy has to say which region --
    "run the setup script" is wrong advice for someone who already ran it in the
    region they usually deploy to."""
    aws_deploy_harness["ecr"].repository_missing = True
    with pytest.raises(RuntimeError) as failure:
        _deploy(deployment_mode="container", region="us-west-2")
    assert "AGENTCORE_REGION=us-west-2 ./scripts/setup_aws_container.sh" in str(failure.value)


def test_aws_get_trace_queries_the_runtimes_own_region(monkeypatch):
    """A cross-region agent's telemetry is in that region's CloudWatch Logs.
    Querying the configured region instead reports the log group as missing,
    which the trace panel would show as a permanent "not ready"."""
    seen = {}

    def fake_query(log_group, region, query_string, minutes_back=15):
        seen["log_group"] = log_group
        seen["region"] = region
        return None

    monkeypatch.setattr(aws_deployer, "_run_logs_insights_query", fake_query)
    assert aws_deployer.get_trace(AWS_ARN_WEST, "abc123") is None
    assert seen["region"] == "us-west-2"
    assert seen["log_group"] == "/aws/bedrock-agentcore/runtimes/funny_analyst-WchV3q89Mv-DEFAULT"


# --- the platform-startup split (see wait_for_ready / _agent_init_ms) -------
# The subtraction that produces platform_startup_ms is only honest if the
# agent's half is right, and its one judgment call -- whether the container's
# module-import cost belongs to *this* session -- is invisible in the output:
# get it wrong and the numbers still look plausible, just wrong in the
# platform's favor. Hence pinning it directly.


def test_aws_agent_init_counts_module_imports_only_for_a_container_born_for_this_session():
    """A container started *by* this ping paid its imports inside the ping, so
    they're part of what this session waited for. Same numbers with an older
    container mean it was already up and had paid them long before -- charging
    them here would inflate the agent's half and shrink the platform's."""
    cold = {"session_init_ms": 300, "module_init_ms": 2000, "container_age_ms": 2400}
    assert aws_deployer._agent_init_ms(cold, cold_start_ms=2500) == 2300

    prewarmed = {"session_init_ms": 300, "module_init_ms": 2000, "container_age_ms": 90000}
    assert aws_deployer._agent_init_ms(prewarmed, cold_start_ms=2500) == 300


def test_aws_agent_init_is_none_when_the_container_reported_nothing():
    """An older container (deployed before this instrumentation) reports no
    figures at all. None, not 0 -- a zero would read as "the agent's own init
    was free", making platform startup absorb the whole cold start."""
    assert aws_deployer._agent_init_ms({}, cold_start_ms=2500) is None
    # No cold_start_ms to compare the container's age against: the module
    # half can't be attributed, so only the part that's unambiguous counts.
    assert aws_deployer._agent_init_ms(
        {"session_init_ms": 300, "module_init_ms": 2000, "container_age_ms": 100}, cold_start_ms=None
    ) == 300


def test_aws_warmup_report_reads_the_containers_own_startup_figures():
    """The three fields the container yields on the sentinel's stream, picked
    out of a stream that also carries ordinary text lines and a keepalive."""
    lines = [
        b"",
        b": keepalive",
        b'data: "warm"',
        b'data: {"session_init_ms": 340, "module_init_ms": 2100, "container_age_ms": 2450}',
    ]
    assert aws_deployer._startup_report(lines) == {
        "session_init_ms": 340,
        "module_init_ms": 2100,
        "container_age_ms": 2450,
    }


def test_aws_warmup_report_reads_a_legacy_containers_app_init_ms_as_session_init():
    """Containers deployed before this instrumentation emit app_init_ms only,
    which meant exactly what session_init_ms means now. Reading it keeps an
    un-redeployed agent reporting a split at all -- one that, as before, leaves
    that container's import cost inside the platform's half."""
    lines = [b'data: {"app_init_ms": 300}']
    assert aws_deployer._startup_report(lines) == {"session_init_ms": 300}
    assert aws_deployer._agent_init_ms(aws_deployer._startup_report(lines), cold_start_ms=2500) == 300


def test_aws_warmup_report_prefers_the_new_field_over_the_legacy_one():
    """A container that sends both must not have session_init_ms clobbered by
    app_init_ms, in either arrival order."""
    both = b'data: {"session_init_ms": 340, "app_init_ms": 300}'
    assert aws_deployer._startup_report([both])["session_init_ms"] == 340
    assert aws_deployer._startup_report([b'data: {"app_init_ms": 300}', both])["session_init_ms"] == 340


# --- where the stopwatch starts (see _warm_up / latest_client_queue_ms) ------
# This boundary is the whole reason cold_start_ms can be quoted as a platform
# number, and getting it wrong is invisible in the output: every figure stays
# plausible, just inflated by however busy this process happened to be. A real
# 20-concurrency burst from a stalled laptop reported a 30s p75 "platform
# startup" for invokes CloudWatch had served in ~2s. Hence pinning the split
# directly rather than trusting the code to keep it.

# Getting the call out costs 300ms here (what first touch of a region really
# does: build the client, resolve credentials, load the service model) and the
# invoke itself 150ms, so every assertion below can name which of the two a
# figure should have measured rather than settling for "small".
_CLIENT_DELAY_S = 0.3
_INVOKE_DELAY_S = 0.15


class _FakeInvokeResponse:
    def __init__(self, report):
        self._report = report

    def iter_lines(self):
        return [b"data: " + json.dumps(self._report).encode("utf-8")]


class _FakeDataClient:
    def __init__(self, report):
        self._report = report

    def invoke_agent_runtime(self, **kwargs):
        time.sleep(_INVOKE_DELAY_S)
        return {"response": _FakeInvokeResponse(self._report)}


def _patch_slow_client(monkeypatch, report):
    monkeypatch.setattr(
        aws_deployer,
        "_data_client",
        lambda resource_id: (time.sleep(_CLIENT_DELAY_S), _FakeDataClient(report))[1],
    )


def test_aws_cold_start_times_the_invoke_not_the_wait_to_get_there(monkeypatch):
    """The invoke took 150ms and getting to it took 300ms, so cold_start_ms is
    the 150 and client_queue_ms the 300. Timing the whole thing instead -- what
    this did originally -- reports 450ms of "cold start" for a platform that
    spent 150, and the error grows without bound with how busy this process
    is."""
    _patch_slow_client(monkeypatch, {"session_init_ms": 340, "module_init_ms": 2100, "container_age_ms": 90000})
    session_state = {}
    report = asyncio.run(aws_deployer._timed_warm_up("arn:aws:x", "session-1", session_state))

    assert report["session_init_ms"] == 340
    # Generous margins: what's pinned is which side of the boundary each delay
    # lands on, not the clock's precision. The upper bound on cold_start_ms is
    # what fails if the client's wait leaks back into it.
    assert _INVOKE_DELAY_S * 1000 * 0.8 <= session_state["cold_start_ms"] < (_CLIENT_DELAY_S + _INVOKE_DELAY_S) * 1000 * 0.5
    assert session_state["client_queue_ms"] >= _CLIENT_DELAY_S * 1000 * 0.8


def test_aws_agent_init_is_not_fooled_by_a_slow_client(monkeypatch):
    """The knock-on effect, and why this matters beyond one field being wrong:
    whether the container's imports count is decided by comparing its age
    against cold_start_ms (see _agent_init_ms). A container 250ms old is older
    than this 150ms invoke, so it was already up and had paid its 2100ms of
    imports long before -- but with the client's 300ms wrongly inside
    cold_start_ms the window widens to 450ms, swallows the age, and charges
    those imports to a session that never paid them. That both inflates the
    agent's half and shrinks the platform's, from one clock started too
    early."""
    _patch_slow_client(monkeypatch, {"session_init_ms": 340, "module_init_ms": 2100, "container_age_ms": 250})
    session_state = {}
    report = asyncio.run(aws_deployer._timed_warm_up("arn:aws:x", "session-1", session_state))

    assert aws_deployer._agent_init_ms(report, session_state["cold_start_ms"]) == 340


def test_aws_client_queue_is_none_when_the_call_never_went_out(monkeypatch):
    """A warmup that fails before issuing the invoke has no split to report,
    and says so with None rather than a 0 that would read as "the client was
    instant". cold_start_ms still gets the wall-clock fallback, so a failed
    warmup doesn't silently report nothing at all."""

    def broken_client(resource_id):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(aws_deployer, "_data_client", broken_client)
    session_state = {}
    assert asyncio.run(aws_deployer._timed_warm_up("arn:aws:x", "session-1", session_state)) == {}
    assert session_state["client_queue_ms"] is None
    assert session_state["cold_start_ms"] is not None


def test_aws_a_failed_warmup_is_reported_not_just_timed(monkeypatch):
    """The other half of the test above: a warmup that failed has to *say* it
    failed. It still doesn't raise -- a chat turn survives a bad ping -- so
    wait_for_ready returns an ordinary-looking duration, and the only thing
    separating that from a real session start is the exception kept here.
    Without it a load test with expired credentials scores every session as a
    fast success (found live, at a p50 of 218ms)."""

    def broken_client(resource_id):
        raise RuntimeError("The security token included in the request is expired")

    monkeypatch.setattr(aws_deployer, "_data_client", broken_client)
    session_state = {}

    async def run():
        session_state["_warmup_task"] = asyncio.create_task(
            aws_deployer._timed_warm_up("arn:aws:x", "session-1", session_state)
        )
        warmup_ms = await aws_deployer.wait_for_ready(session_state)
        return warmup_ms, await aws_deployer.latest_warmup_error(session_state)

    warmup_ms, error = asyncio.run(run())
    assert warmup_ms is not None
    assert isinstance(error, RuntimeError)
    assert "expired" in str(error)


def test_aws_a_successful_warmup_reports_no_error(monkeypatch):
    """The same field on the path that worked, so a stale error can't make a
    good session look bad: wait_for_ready clears it before awaiting, and a
    warmup that returned a report leaves it None."""
    _patch_slow_client(monkeypatch, {"session_init_ms": 120})
    session_state = {}

    async def run():
        session_state["_warmup_task"] = asyncio.create_task(
            aws_deployer._timed_warm_up("arn:aws:x", "session-1", session_state)
        )
        await aws_deployer.wait_for_ready(session_state)
        return await aws_deployer.latest_warmup_error(session_state)

    assert asyncio.run(run()) is None


def test_every_deployer_reports_the_same_session_start_signals():
    """server.py reads these off whichever deployer a given agent happens to
    use, without asking which platform it is -- so one module missing an
    accessor is an AttributeError on a live chat turn, not at import. A
    platform with nothing to report returns None from its own accessor (see
    deployers/__init__.py); what's required is that the name exists and is
    awaitable everywhere."""
    signals = (
        "latest_usage",
        "latest_trace_id",
        "latest_warmup_ms",
        "latest_agent_init_ms",
        "latest_platform_startup_ms",
        "latest_cold_start_ms",
        "latest_client_queue_ms",
        "latest_warmup_error",
        "latest_retries",
    )
    for module in (aws_deployer,):
        for name in signals:
            fn = getattr(module, name, None)
            assert fn is not None, f"{module.__name__} is missing {name}"
            assert inspect.iscoroutinefunction(fn), f"{module.__name__}.{name} is not awaitable"
            # Called with a session_state that never went through
            # wait_for_ready, which is what happens on any agent chatted with
            # before a warmup finished: must answer None, not raise.
            assert asyncio.run(fn({})) is None


def test_aws_v2_env_payload_guard():
    """The V2-only env-var ceiling, checked locally because the preview service
    model doesn't express it (see deployers/aws.py's _check_v2_env_payload).
    Sized from the real payload that hit it: v1-analyst's own env vars, which
    the service rejected at 1528 bytes."""
    from deployers.aws import _check_v2_env_payload

    _check_v2_env_payload({"AGENT_INSTRUCTIONS": "x" * 500})  # comfortably under, no raise
    with pytest.raises(RuntimeError, match="caps the environment variable payload"):
        _check_v2_env_payload({"AGENT_INSTRUCTIONS": "x" * 1024})  # key pushes it over


def test_aws_v2_env_payload_guard_counts_utf8_bytes():
    """The limit the service states is in bytes, not characters -- a prompt of
    non-ASCII text is over the line well before 1024 characters."""
    from deployers.aws import _check_v2_env_payload

    with pytest.raises(RuntimeError, match="caps the environment variable payload"):
        _check_v2_env_payload({"P": "é" * 512})  # 512 chars, 1024 bytes
