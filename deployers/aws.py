"""AWS deployer: packages a static, env-var-parameterized entry point
(aws_hosted/main.py) plus tools.py and its dependencies into a self-
contained zip, uploads it to S3, and creates a Bedrock AgentCore Runtime
directly via boto3's bedrock-agentcore-control control-plane API -- no
Node CLI, no CDK, no per-agent IAM role, unlike ~/Dev/AWS's own deploy
(which uses the `agentcore` CLI). All portal-created AWS agents share one
IAM execution role and one S3 staging bucket, set up once (see README).

AgentCore Runtime's CodeZip build has no server-side dependency
installation step (confirmed: unlike Azure's `REMOTE_BUILD`, there's no
equivalent option in CreateAgentRuntime's shape) -- so unlike Gemini/Azure,
dependencies are pip-installed into the zip itself before upload, cross-
targeted at the container's actual platform/Python version since this
runs from a developer's machine (may not match).

CreateAgentRuntime's artifact is a union of exactly two alternatives (read
off the real service model, not assumed): a `codeConfiguration` pointing at
that zip, or a `containerConfiguration` pointing at an ECR image. Both are
offered -- see DEPLOYMENT_MODES and _build_container_image -- because they
are genuinely different products from the same source files
(aws_hosted/main.py + tools.py), with different cold-start behavior, and
comparing them is a first-class use of this portal (see docs/latency.md).

Chat replicates ~/Dev/AWS/demo_web.py's InvokeAgentRuntime streaming
exactly: `accept="text/event-stream"`, each `data: {...}` line's
`event.contentBlockDelta.delta.text` is one token delta. The underlying
call is a blocking StreamingBody read, bridged via bridge_sync_iterable
the same way deployers/gemini.py bridges vertexai's sync stream_query.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

from deployers import bridge_sync_iterable, mcp_urls_for

load_dotenv()

ROOT = Path(__file__).parent.parent

# AGENTCORE_REGION is checked first, and exists because AWS_REGION turned out
# not to be the portal's to read. It's a standard AWS SDK variable, so anything
# on the machine may already export it for its own unrelated purpose -- here it
# was exported as us-west-2 by an unrelated tool's config, and since
# load_dotenv() uses override=False, that silently beat .env's us-east-1 and
# quietly split this portal's agents across two regions over several deploys.
# Nobody chose that, and nothing surfaced it: each deploy succeeded.
#
# So: a project-owned name nothing else in the ecosystem sets, which therefore
# actually takes effect when .env sets it. AWS_REGION is still honoured as the
# fallback (an install that only sets it keeps working, and it's what a reader
# expects to reach for), but it can no longer override a deliberate choice.
# Regional resources -- the staging bucket, the ECR repository, the web search
# Gateway -- all have to agree with whatever this resolves to.
#
# A function only so the precedence is testable: as a bare module-level
# expression it's evaluated once at import, before any test can vary the
# environment it reads.
def _resolve_region():
    # `or`, not .get(key, default): an exported-but-empty AWS_REGION="" is a
    # real state (some tooling clears it that way) and must fall through to the
    # default rather than configuring the portal with the empty string.
    return os.environ.get("AGENTCORE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


REGION = _resolve_region()


# Which regions the portal offers when creating an agent. REGION above is only
# the *default* -- the region a request that doesn't name one deploys into, and
# the one every non-create code path still falls back to.
#
# Every other region an agent can live in is already handled: _region_of below
# reads a runtime's region off its own ARN, so chat/delete/trace/load-test
# follow an existing agent wherever it is. This list exists purely because a
# create has no ARN to read yet, so the target has to come from the request.
#
# An allowlist rather than a free-text field because the wrong value is
# expensive: a typo'd region is a well-formed create that fails only after a
# full container build and push, and a region where this account's `us.`-prefixed
# inference profiles don't exist deploys clean and then fails every invoke.
def deployment_regions():
    """REGION first (it's the default, so it should be the default option in
    the form), then any others AGENTCORE_REGIONS names, de-duplicated with
    order preserved. Unset means single-region, which is why REGION is
    unconditionally included rather than requiring the variable to repeat it.

    A function rather than a module-level constant like REGION above so that a
    caller who sets AGENTCORE_REGIONS -- a test, or a REPL -- is seen without
    re-importing the module. Every deployer exposes one (() where the platform
    doesn't offer the choice), same declare-it-explicitly convention as
    DEPLOYMENT_MODES."""
    configured = [r.strip() for r in os.environ.get("AGENTCORE_REGIONS", "").split(",")]
    return list(dict.fromkeys([REGION] + [r for r in configured if r]))


# .get(), not os.environ[...]: an unset variable makes this platform report
# itself unconfigured (see REQUIRED_CONFIG below) rather than taking the whole
# portal down at import time.
EXECUTION_ROLE_ARN = os.environ.get("AGENTCORE_EXECUTION_ROLE_ARN", "")
STAGING_BUCKET = os.environ.get("AGENTCORE_STAGING_BUCKET", "")


def _staging_bucket_for(region):
    """Which bucket a code-zip deploy into `region` stages through.

    A bucket has to be in the same region as the runtime being created. Tested
    directly rather than assumed: creating a us-west-2 runtime from the
    us-east-1 staging bucket fails with "ValidationException: S3 operation
    failed: Moved Permanently (Service: S3, Status Code: 301)" -- AgentCore
    reads the object with a client bound to its own region, and S3 answers 301
    for a bucket that lives elsewhere.

    So deploying into a second region needs a second bucket, named here.
    STAGING_BUCKET stays the default for the default region, so a single-region
    install needs nothing new. Underscores because that's what an environment
    variable name can carry: AGENTCORE_STAGING_BUCKET_US_WEST_2.

    Nothing here validates the pairing -- _check_staging_bucket does, before a
    deploy pays for a zip build only to hit that 301."""
    return os.environ.get(f"AGENTCORE_STAGING_BUCKET_{region.upper().replace('-', '_')}") or STAGING_BUCKET


def _check_staging_bucket(bucket, region):
    """Rejects a staging bucket that isn't in `region`, before the deploy builds
    anything.

    Worth its own call because of how the failure reads otherwise: the deploy
    spends a minute or two vendoring wheels, uploads the zip successfully (S3
    accepts a cross-region write through any endpoint), and only then does
    CreateAgentRuntime answer "S3 operation failed: Moved Permanently" -- a
    message that names neither the bucket, nor the region, nor the variable that
    fixes it. Same shape and reason as _ecr_repository_uri's missing-repository
    check: one cheap call up front instead of a confusing failure at the end.

    get_bucket_location reports None/"" for us-east-1 -- an artifact of the API
    predating regions, not a missing value -- so that's normalized rather than
    treated as unknown. A bucket this account can't inspect is left alone: it
    might still be perfectly usable, and refusing the deploy on a
    GetBucketLocation permission gap would be inventing a requirement."""
    try:
        location = _client("s3", region).get_bucket_location(Bucket=bucket)["LocationConstraint"]
    except Exception:
        return
    bucket_region = location or "us-east-1"
    if bucket_region != region:
        variable = f"AGENTCORE_STAGING_BUCKET_{region.upper().replace('-', '_')}"
        raise RuntimeError(
            f"Staging bucket {bucket!r} is in {bucket_region}, but this agent is being deployed to "
            f"{region}. AgentCore reads the code zip from its own region and answers "
            f"'S3 operation failed: Moved Permanently' otherwise, so {region} needs its own bucket:\n"
            f"  aws s3api create-bucket --bucket <bucket-name> --region {region} "
            f"--create-bucket-configuration LocationConstraint={region}\n"
            f"then set {variable}=<bucket-name> in .env. See docs/aws.md's 'Regions'."
        )


# What an AWS deploy cannot proceed without. AWS_REGION is deliberately absent:
# it has a working default above, unlike these two, which name account-specific
# resources nothing can guess (see README's one-time setup). See
# deployers/__init__.py's note on REQUIRED_CONFIG.
REQUIRED_CONFIG = ("AGENTCORE_EXECUTION_ROLE_ARN", "AGENTCORE_STAGING_BUCKET")

# Optional, unlike the two above -- most installs won't have run through the
# one-time AgentCore Gateway + Web Search Tool connector setup (docs/aws.md's "AWS
# web search via AgentCore Gateway"), so this is allowed to be unset. deploy()
# only requires it when the "web_search_aws" tool is actually selected (see
# WEB_SEARCH_TOOL_ID below), rather than failing every AWS deploy on an empty
# value the way EXECUTION_ROLE_ARN/STAGING_BUCKET do.
WEB_SEARCH_GATEWAY_URL = os.environ.get("AGENTCORE_WEB_SEARCH_GATEWAY_URL", "")

# Which ECR repository container-mode deploys push their image to. One
# shared repository for every portal-created AWS agent (one image tag per
# deploy, see _image_tag), architecturally the same choice as the single
# shared S3 staging bucket above rather than a repository per agent. Also
# one-time manual setup, for the same reason: the portal creating registries
# on the fly would be doing account-level provisioning nobody asked it to
# (see _ecr_repository_uri, which fails with the exact command to run).
ECR_REPOSITORY = os.environ.get("AGENTCORE_ECR_REPOSITORY", "agent-portal-agents")

# `docker` by default; overridable because any Docker-CLI-compatible builder
# (finch, podman, nerdctl) drives an identical `build`/`login`/`push` for
# what this uses them for -- no Docker-specific flags below.
CONTAINER_BUILDER = os.environ.get("AGENTCORE_CONTAINER_BUILDER", "docker")

# Matches the id in deployers/__init__.py's AVAILABLE_TOOLS and
# aws_hosted/main.py's own copy of this same string (no shared import --
# that file is zipped and deployed standalone, same reason _WARMUP_SENTINEL
# below is duplicated rather than imported).
WEB_SEARCH_TOOL_ID = "web_search_aws"

# The per-agent config file both artifact builders write and aws_hosted/main.py
# reads (see _agent_config). Duplicated as a literal there for the same reason
# as WEB_SEARCH_TOOL_ID above -- that file ships standalone and can't import
# from here. Safe as a pair regardless: main.py is copied out of this repo at
# deploy time, so a runtime can never read a filename older than the one that
# wrote it.
AGENT_CONFIG_FILENAME = "agent_config.json"

SUPPORTS_TRACING = True

# Cross-targeted at the AgentCore Runtime container's actual platform,
# which doesn't match the machine building this zip (e.g. a developer's
# Apple Silicon Mac) -- see _build_deployment_package(). The architecture
# was confirmed directly via a real failed deploy: AgentCore Runtime
# rejected an x86_64 build with "Your artifact contains binary files that
# are incompatible with Linux ARM64" -- it runs on Graviton, not x86_64.
_TARGET_PYTHON_VERSION = "3.12"
_TARGET_PYTHON_PLATFORM = "aarch64-unknown-linux-gnu"
_RUNTIME_ENUM = "PYTHON_3_12"

# The same architecture as _TARGET_PYTHON_PLATFORM above, spelled the way
# `docker build --platform` wants it -- container mode builds for the same
# Graviton hosts CodeZip mode cross-targets its wheels at, so the two modes
# can't disagree about what they're building for. On an Apple Silicon
# machine this is a native build (no emulation); on an x86_64 one Docker
# will emulate, which is slow but correct.
_TARGET_DOCKER_PLATFORM = "linux/arm64"

# AgentCore Runtime always calls a container on 8080 (/invocations and
# /ping), which is exactly what aws_hosted/main.py's BedrockAgentCoreApp
# already serves -- the same file, unchanged, is what both deployment modes
# ship. Only documented here (and in the Dockerfile's EXPOSE) since nothing
# in this module gets to choose it.
_CONTAINER_PORT = 8080

# The two artifact alternatives CreateAgentRuntime's own shape offers (see
# this module's docstring); server.py validates a requested mode against
# this tuple, and every deployer module declares one so that a platform
# without the concept says so explicitly rather than by omission -- same
# convention as SUPPORTS_TRACING above.
DEPLOYMENT_MODES = ("code", "container")
# First listed wins when a caller doesn't say -- the convention
# deployers/__init__.py documents, so server.py can record the mode a deploy
# will actually use without a second constant to keep in sync.
DEFAULT_DEPLOYMENT_MODE = DEPLOYMENT_MODES[0]

_clients = {}

# botocore's own default Config().max_pool_connections is 10 -- confirmed
# directly, not assumed -- and _data_client() below hands out one shared
# client per region, used for every invoke_agent_runtime call this whole
# process makes to that region (both real chat turns and the load test's
# simulated ones). Found live: at
# the load test's own concurrency levels (up to 20 per agent, and every
# concurrently-compared AWS agent shares this one client/pool), sessions
# beyond the 10th queue for a free pooled connection before their real
# network call even starts -- that queueing time then gets measured as if
# it were AgentCore's own cold-start latency, when it's actually this
# client's connection pool running out. Raised well above the load test's
# own per-agent cap so the pool itself is never the bottleneck being
# measured. Gemini/Azure aren't subject to the same issue: Gemini's client
# is a fresh gRPC channel per session (HTTP/2 multiplexed, no small fixed
# pool), and Azure's is a fresh async client stack per session (no shared
# pool at all) -- confirmed by reading each, not assumed to match.
_MAX_POOL_CONNECTIONS = 50


def _region_of(resource_id):
    """The region an existing runtime actually lives in, read off its own
    ARN rather than assumed to be REGION.

    This is a real bug that was reported from the portal, not a
    hypothetical: an agent created while the portal was pointed at
    us-west-2 stays in the DB with a us-west-2 ARN, and every invoke of it
    afterward went to whatever region REGION happened to be at the time.
    AgentCore answers that with a misleading error --
    "ResourceNotFoundException: No endpoint or agent found with qualifier
    'DEFAULT' for agent 'arn:aws:bedrock-agentcore:us-west-2:...'" -- which
    reads like a broken endpoint on a runtime that is in fact perfectly
    healthy, just somewhere else. The ARN is right there in the argument,
    so nothing has to be guessed or stored alongside it.

    Falls back to REGION for anything that isn't a parseable ARN, so a
    malformed id fails in the same place it did before (the API call)
    rather than here on an IndexError."""
    parts = str(resource_id).split(":")
    if len(parts) > 4 and parts[0] == "arn" and parts[3]:
        return parts[3]
    return REGION


def _client(service, region):
    """One shared client per (service, region) -- the sharing is what the
    _MAX_POOL_CONNECTIONS note above is about, and it survives being keyed
    by region: a single-region install (the normal case) still gets exactly
    one client per service, as it did when these were two module globals."""
    key = (service, region)
    if key not in _clients:
        _clients[key] = boto3.client(
            service, region_name=region, config=Config(max_pool_connections=_MAX_POOL_CONNECTIONS)
        )
    return _clients[key]


def _control_client(resource_id=None, region=None):
    """Two ways to say which region, because the callers genuinely differ.

    resource_id: for calls about one existing runtime -- the region comes off
    its own ARN, so the call follows the agent wherever it lives.

    region: for create_agent_runtime and list_agent_runtimes, which aren't
    about an existing runtime and so have no ARN to read. Passing neither
    means REGION, the region this portal deploys into by default."""
    if resource_id:
        return _client("bedrock-agentcore-control", _region_of(resource_id))
    return _client("bedrock-agentcore-control", region or REGION)


def _data_client(resource_id):
    return _client("bedrock-agentcore", _region_of(resource_id))


def _slugify(name):
    """AgentRuntimeName must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ (confirmed
    via the CreateAgentRuntime shape's validation pattern) -- underscores
    only, no hyphens (unlike Azure's Foundry agent names), must start with
    a letter, max 48 chars."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "agent"
    if not slug[0].isalpha():
        slug = "a_" + slug
    return slug[:48]


def _agent_config(model, agent_instructions, tool_ids, mcp_server_ids):
    """The per-agent configuration that ships *inside* the deployment artifact,
    as AGENT_CONFIG_FILENAME, rather than as environmentVariables.

    It used to be env vars (BEDROCK_MODEL_ID/AGENT_INSTRUCTIONS/AGENT_TOOLS/
    AGENT_MCP_SERVERS/AGENT_WEB_SEARCH_GATEWAY_URL), which is the natural
    reading of "one static main.py, parameterized per deploy" and is still what
    azure_hosted/main.py does. Two real failures say the artifact is the right
    place for AWS:

    1. A V2 runtime caps the whole environmentVariables payload at 1024 bytes
       (see _check_v2_env_payload). Agent instructions are a system prompt typed
       into a textarea and routinely exceed that alone -- one agent's payload
       came to 1713 bytes -- so V2 was simply unavailable to any agent with
       normal-length instructions. Shortening the prompt only moves the wall,
       because every other field counts against the same 1024.
    2. AgentCore rejects control characters in env var values outright:
       "Environment variable value contains invalid control characters
       (0x00-0x1F, 0x7F). Key: 'AGENT_INSTRUCTIONS'", from a real failed deploy.
       A prompt from a textarea nearly always contains newlines, so essentially
       every deploy from the create-agent form failed, with the newline named
       nowhere in the portal's own error surface. That needed a matched
       escape/unescape pair on both sides; JSON carries newlines natively, so
       the whole problem and both halves of that code are gone.

    Nothing agent-specific is left in the environment -- only AWS_REGION, which
    boto3 itself reads. The tradeoff, and it's real: env vars can be inspected
    with DescribeAgentRuntime and changed with UpdateAgentRuntime, while this
    file requires rebuilding the artifact. Costs nothing here, because editing
    an agent in the portal already recreates its runtime from a fresh artifact,
    and the portal's own DB is the source of truth for what an agent is
    configured with."""
    return {
        "model": model,
        "instructions": agent_instructions,
        "tools": list(tool_ids),
        "mcp_servers": mcp_urls_for(mcp_server_ids),
        # Only when the tool is actually selected, so a Gateway URL configured
        # in .env doesn't travel inside every agent's artifact.
        "web_search_gateway_url": WEB_SEARCH_GATEWAY_URL if WEB_SEARCH_TOOL_ID in tool_ids else "",
    }


def _write_agent_config(staging_dir, agent_config):
    """Written by both artifact builders, so the two deployment modes stay
    provably identical in what the agent actually reads."""
    (staging_dir / AGENT_CONFIG_FILENAME).write_text(json.dumps(agent_config, indent=2))


def _build_deployment_package(agent_config):
    """Installs aws_hosted/requirements.txt into a staging directory
    alongside main.py/tools.py, then zips the whole thing -- a self-
    contained package, since AgentCore's CodeZip build doesn't install
    dependencies server-side."""
    staging_dir = Path(tempfile.mkdtemp(prefix="agent-portal-aws-"))
    try:
        # This project's venvs are uv-managed and don't have a `pip`
        # module installed inside them at all (confirmed directly: `python
        # -m pip install` fails with "No module named pip") -- `uv pip
        # install` replicates pip's CLI/target-directory semantics without
        # needing pip present, and uv's own flag names differ slightly
        # (--python-platform instead of --platform, --python instead of
        # --python-version referring to an interpreter/version spec).
        result = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(staging_dir),
                "--python-platform",
                _TARGET_PYTHON_PLATFORM,
                "--python-version",
                _TARGET_PYTHON_VERSION,
                "--only-binary=:all:",
                "-r",
                str(ROOT / "aws_hosted" / "requirements.txt"),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"uv pip install failed:\n{result.stderr}")
        shutil.copy(ROOT / "aws_hosted" / "main.py", staging_dir / "main.py")
        shutil.copy(ROOT / "tools.py", staging_dir / "tools.py")
        _write_agent_config(staging_dir, agent_config)

        zip_path = Path(tempfile.gettempdir()) / f"agent-portal-aws-{os.getpid()}-{time.time_ns()}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(staging_dir))
        return zip_path
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _builder_output(result):
    """docker writes essentially everything -- build progress and real
    errors alike -- to stderr, but a failure whose message is only on stdout
    (a builder that isn't docker, a plugin) must not come back blank, so both
    are considered. Tail-truncated because a failed build's log can run to
    hundreds of lines and this ends up in a DB error_message the UI shows on
    the agent's card: the last lines are the ones that say what broke."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text[-2000:] if len(text) > 2000 else text


def _run_builder(args, what, stdin=None):
    try:
        result = subprocess.run([CONTAINER_BUILDER, *args], input=stdin, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"Container-mode deploys need {CONTAINER_BUILDER!r} on PATH ({what}). Install Docker "
            "(or set AGENTCORE_CONTAINER_BUILDER to another Docker-CLI-compatible builder), or "
            "deploy this agent in code-zip mode instead."
        ) from None
    if result.returncode != 0:
        raise RuntimeError(f"{CONTAINER_BUILDER} {args[0]} failed ({what}):\n{_builder_output(result)}")
    return result


def _ecr_repository_uri(region):
    """The full registry/repository URI to tag images into, read back from
    ECR rather than assembled from an account id -- describe_repositories
    already reports it, so nothing has to be guessed about registry
    hostnames (which differ in China/GovCloud partitions).

    Doubles as the "is container mode set up at all" check, deliberately
    before any image build: a missing repository is knowable in one cheap
    call, and finding out afterward would cost a full container build
    first. That check is per-region and has to be -- ECR repositories are
    regional, and a runtime can only pull from a registry in its own region,
    so deploying into a new region means running the setup script again
    there."""
    ecr = _client("ecr", region)
    try:
        repositories = ecr.describe_repositories(repositoryNames=[ECR_REPOSITORY])["repositories"]
    except ecr.exceptions.RepositoryNotFoundException:
        # Points at the script rather than printing the create-repository
        # command it used to: creating the repository alone gets you a
        # successful build and push followed by a CREATE_FAILED runtime,
        # because the execution role still can't pull (see
        # _container_pull_hint). The script does both halves.
        raise RuntimeError(
            f"Container-mode deploys need one-time setup ({ECR_REPOSITORY!r} in {region} doesn't "
            f"exist yet) -- an ECR repository plus pull permissions on the execution role:\n"
            f"  AGENTCORE_REGION={region} ./scripts/setup_aws_container.sh\n"
            f"See docs/aws.md's 'Container deploys'. Deploying in code-zip mode needs none of this."
        ) from None
    return repositories[0]["repositoryUri"]


def _builder_login(registry, region):
    """ECR's authorization token is a base64 "user:password" pair (confirmed
    against the real GetAuthorizationToken response, not assumed), fed to the
    builder over stdin so the password never appears in an argv anyone can
    read out of `ps`.

    The token is regional and only authenticates against its own region's
    registry, so `region` must be the region the image is being pushed to --
    which is why it's passed in rather than read off the global."""
    token = _client("ecr", region).get_authorization_token()["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode("utf-8").split(":", 1)
    _run_builder(
        ["login", "--username", username, "--password-stdin", registry],
        "authenticating to ECR",
        stdin=password,
    )


def _build_container_image(runtime_name, agent_config, region):
    """Builds aws_hosted/main.py + tools.py into a linux/arm64 image and
    pushes it to ECR in `region`, returning the image URI to hand to
    containerConfiguration.

    Builds from a staging directory holding exactly the files the image
    needs, rather than using the repo root as the build context: the root
    also carries .venv, logs/, and the portal's own SQLite DB, which would
    make the context enormous and put a .dockerignore in the way of
    understanding what actually ships. Same reasoning (and the same shape) as
    _build_deployment_package above -- and it keeps both deployment modes
    provably built from the same source files.

    One image tag per deploy (never :latest): an AgentCore runtime resolves
    its image at create time, so a mutable tag would leave two agents
    supposedly running "the same" image while one has silently moved
    underneath the other -- fatal for the mode-vs-mode latency comparison
    this exists to support."""
    repository_uri = _ecr_repository_uri(region)
    image_uri = f"{repository_uri}:{runtime_name}-{time.time_ns()}"
    staging_dir = Path(tempfile.mkdtemp(prefix="agent-portal-aws-container-"))
    try:
        for source in (
            ROOT / "aws_hosted" / "Dockerfile",
            ROOT / "aws_hosted" / "requirements.txt",
            ROOT / "aws_hosted" / "main.py",
            ROOT / "tools.py",
        ):
            shutil.copy(source, staging_dir / source.name)
        # Last, so it's the layer that changes per agent (the Dockerfile copies
        # it after requirements.txt): two agents differing only in their
        # instructions still share every dependency layer.
        _write_agent_config(staging_dir, agent_config)
        _run_builder(
            ["build", "--platform", _TARGET_DOCKER_PLATFORM, "--tag", image_uri, str(staging_dir)],
            "building the agent image",
        )
        # After the build, not before: an image that never built is not worth
        # having authenticated for, and ECR tokens are short-lived (12h).
        _builder_login(repository_uri.split("/")[0], region)
        _run_builder(["push", image_uri], "pushing the agent image to ECR")
        return image_uri
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _check_web_search_gateway_url():
    """Rejects a gateway URL that can't work, before a deploy commits to it.

    Unset is the obvious case. The one worth the extra code is a URL still
    holding .env.example's placeholder host, which found a real agent the hard
    way: the value looks configured, so this guard passed, the deploy
    succeeded, the runtime reached READY -- and then every invoke died. The
    agent POSTs to gateway-xxxxxxxxxx..., gets a 404, the MCP session
    terminates, and aws_hosted/main.py turns any tool-load failure into a
    fatal ValueError, so a wrong URL doesn't cost you one tool, it costs the
    whole agent. The service-side symptom is an ExceptionGroup about "unhandled
    errors in a TaskGroup" that names neither the URL nor the tool.

    Deliberately a shape/placeholder check and not a live GetGateway call: the
    portal's own credentials would then need gateway read permissions it
    otherwise never uses, which is new setup friction to catch a
    misconfiguration this catches for free."""
    if not WEB_SEARCH_GATEWAY_URL:
        raise RuntimeError(
            "The 'web_search_aws' tool needs AGENTCORE_WEB_SEARCH_GATEWAY_URL set in .env -- "
            "see docs/aws.md's 'AWS web search via AgentCore Gateway' section for the one-time setup."
        )
    host = urllib.parse.urlparse(WEB_SEARCH_GATEWAY_URL).hostname or ""
    # A run of x's is what every placeholder in .env.example uses for an
    # identifier only the account owner can know; no real gateway id has one.
    if "xxxx" in host:
        raise RuntimeError(
            f"AGENTCORE_WEB_SEARCH_GATEWAY_URL is still .env.example's placeholder "
            f"({WEB_SEARCH_GATEWAY_URL}), so this agent would deploy fine and then fail on every "
            f"invoke -- a bad gateway URL breaks the whole agent, not just its web search tool. "
            f"Either set it to a real gateway's MCP endpoint (docs/aws.md's 'AWS web search via "
            f"AgentCore Gateway' has the one-time setup) or create this agent without the "
            f"'Web search (AWS AgentCore Gateway)' tool selected."
        )


_PULL_FAILURE_MARKERS = ("ecr", "image", "pull", "denied", "authoriz", "accessdenied")


def _container_pull_hint(failure_reason):
    """Extra remediation appended when a container-mode runtime fails in a way
    that looks like it couldn't pull its image.

    Worth special-casing because this is the one setup mistake the portal
    cannot catch up front: a missing ECR repository fails before any build (see
    _ecr_repository_uri), but a repository the *execution role* can't pull from
    lets the build and push both succeed, so the first sign of trouble is a
    CREATE_FAILED several minutes later whose failureReason says nothing about
    IAM. Matched loosely on purpose -- the exact wording is the service's to
    change, and an occasional extra hint on an unrelated container failure
    costs nothing next to sending someone hunting through CloudWatch."""
    reason = (failure_reason or "").lower()
    if not any(marker in reason for marker in _PULL_FAILURE_MARKERS):
        return ""
    return (
        f"\n\nIf this is an image-pull failure, the execution role likely lacks pull access to "
        f"{ECR_REPOSITORY!r}. That is one-time setup, and it's idempotent -- safe to just run:\n"
        f"  ./scripts/setup_aws_container.sh\n"
        f"then create the agent again (see docs/aws.md's 'Container deploys')."
    )


DEFAULT_RUNTIME_VERSION = "V2"
RUNTIME_VERSIONS = ("V1", "V2")


def supports_platform_version():
    """Whether the boto3/botocore actually loaded here models
    CreateAgentRuntime's platformVersion -- i.e. whether this process is
    running against the AgentCore Runtime V2 private-preview SDK.

    Asked of the live service model rather than a version check, because
    version comparison has already been wrong once here: the first preview
    build was 1.43.81, *older* than the then-current public 1.43.86 that
    doesn't model the field. Public botocore raises ParamValidationError
    ("Unknown parameter") on the field, so it can't just be passed
    unconditionally.

    Note the preview API changed shape between builds: it was
    managedComputeConfiguration={"version": "V2"} in 1.43.81 and is a plain
    platformVersion="V2" string as of 1.43.87. Asking the model which name
    exists is also what keeps that from being a silent wire-format error."""
    request_shape = _control_client().meta.service_model.operation_model("CreateAgentRuntime").input_shape
    return "platformVersion" in request_shape.members


def _platform_version_kwargs(runtime_version, supported=None):
    """The platformVersion kwargs (if any) for a requested runtime version, as
    a dict to spread into CreateAgentRuntime.

    V1 sends nothing at all rather than an explicit platformVersion="V1": V1
    is the platform's own default, so the two are equivalent on create, and
    omitting keeps a V1 create working on both the public SDK (which can't
    express the field) and an account that hasn't been allowlisted for the
    private preview (which rejects the field outright, whatever its value --
    confirmed live against the earlier build: "managedComputeConfiguration is
    not enabled for this account."). V2 has no such fallback, so it raises
    instead: a silent downgrade would leave a runtime the portal calls V2
    while it turns in V1 warmup numbers, which is exactly the measurement
    this exists to make.

    RUNTIME_VERSIONS is checked here rather than left to the service because
    the preview model declares platformVersion as a free-form string (min 1,
    max 128, no enum), so a typo like "v2" is a well-formed request that only
    the service can reject -- after the zip build and S3 upload."""
    if runtime_version is None or runtime_version == "V1":
        return {}
    if runtime_version not in RUNTIME_VERSIONS:
        raise RuntimeError(f"runtime_version must be one of {RUNTIME_VERSIONS}, got {runtime_version!r}")
    if not (supports_platform_version() if supported is None else supported):
        raise RuntimeError(
            f"This boto3/botocore ({boto3.__version__}) doesn't model platformVersion, so "
            f"Runtime version {runtime_version} can't be requested -- it needs the AgentCore Runtime V2 "
            "private-preview SDK on PYTHONPATH."
        )
    return {"platformVersion": runtime_version}


# The env-var payload ceiling V2 runtimes impose, and V1 runtimes don't. Found
# the expensive way, on a real V2 create that was accepted, provisioned for ~a
# minute, then failed: the service reported the environment variable payload as
# 1528 bytes against a 1024-byte maximum for V2 agents. Nothing in the preview
# service model expresses the limit -- environmentVariables' own shape is
# identical for V1 and V2 -- so it can only be checked by hand, here.
_V2_MAX_ENV_PAYLOAD_BYTES = 1024


def _check_v2_env_payload(environment_variables):
    """Raises if this agent's environmentVariables are too large for a V2
    runtime, before the zip build and S3 upload rather than a minute into
    provisioning.

    Now effectively unreachable, and kept deliberately. The portal used to be
    badly exposed to this limit -- the agent's whole system prompt rode in
    AGENT_INSTRUCTIONS, so V2 was unavailable to any agent with normal-length
    instructions (one real payload: 1713 bytes) -- which is exactly why agent
    configuration moved into the deployment artifact instead (see
    _agent_config). What's left in the environment is AWS_REGION, so this is a
    guard against a future variable quietly reintroducing the problem rather
    than something a user should ever hit.

    Deliberately conservative: keys+values, UTF-8, no attempt to match the
    service's exact accounting. The one payload measured against the real
    service came back as 1528 bytes where this sum gives 1537, so this
    over-counts slightly (by 9 bytes there) and can only reject a payload the
    service would have accepted, never the reverse."""
    size = sum(len(key.encode()) + len(value.encode()) for key, value in environment_variables.items())
    if size > _V2_MAX_ENV_PAYLOAD_BYTES:
        raise RuntimeError(
            f"Runtime version V2 caps the environment variable payload at "
            f"{_V2_MAX_ENV_PAYLOAD_BYTES} bytes; this agent's is {size}. The agent instructions are "
            f"almost always what pushes it over -- shorten them by ~{size - _V2_MAX_ENV_PAYLOAD_BYTES} "
            "bytes, or deploy this agent as V1, which has no such limit."
        )


class _RuntimeCreateFailed(RuntimeError):
    """A create that the service itself declared dead (CREATE_FAILED), as
    opposed to any other reason a deploy can fail. Separate type because it's
    the one case where the runtime that was just created is both useless and in
    the way -- see _create_runtime's cleanup."""


def _wait_for_ready(agent_runtime_id, region, deployment_mode=None):
    # region, not the global: a runtime id is only meaningful in the region
    # that issued it, so polling REGION for a runtime created elsewhere would
    # report ResourceNotFoundException on a create that is in fact fine.
    for _ in range(60):
        time.sleep(10)
        details = _control_client(region=region).get_agent_runtime(agentRuntimeId=agent_runtime_id)
        status = details["status"]
        if status == "READY":
            return
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            failure_reason = details.get("failureReason")
            hint = _container_pull_hint(failure_reason) if deployment_mode == "container" else ""
            message = f"AgentCore Runtime failed: {failure_reason}{hint}"
            if status == "CREATE_FAILED":
                raise _RuntimeCreateFailed(message)
            raise RuntimeError(message)
    raise RuntimeError("Timed out waiting for the AgentCore Runtime to become READY.")


def _create_runtime(runtime_name, artifact, description, environment_variables, region, platform_version,
                    deployment_mode=None):
    """Everything about a CreateAgentRuntime call that is identical for both
    deployment modes -- which is everything except the artifact itself (and
    the platform version, which is orthogonal to it). The two modes ship the
    same aws_hosted/main.py reading the same agent_config.json, so a
    mode-vs-mode comparison is comparing the platform's own packaging, not two
    differently-configured agents.

    environment_variables arrives already built rather than being assembled
    here because deploy() has to size-check it for V2 (see
    _check_v2_env_payload) *before* paying for a zip build or a container
    build and push."""
    client = _control_client(region=region)
    try:
        created = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact=artifact,
            roleArn=EXECUTION_ROLE_ARN,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "HTTP"},
            description=description,
            environmentVariables=environment_variables,
            # Spread rather than passed inline because the public botocore doesn't
            # model this parameter at all (see supports_platform_version) --
            # passing it as None/absent would still be an unknown-parameter error
            # there.
            **platform_version,
        )
    except client.exceptions.ConflictException:
        raise RuntimeError(_name_conflict_detail(client, runtime_name, region)) from None
    # deployment_mode is passed only so a failure can be explained in terms
    # of the mode that produced it (see _container_pull_hint) -- it has no
    # effect on what gets created, which is the point of this function.
    try:
        _wait_for_ready(created["agentRuntimeId"], region, deployment_mode=deployment_mode)
    except _RuntimeCreateFailed as exc:
        raise RuntimeError(f"{exc}{_clean_up_failed_create(client, created['agentRuntimeId'])}") from None
    return created["agentRuntimeArn"]


def _name_conflict_detail(client, runtime_name, region):
    """Turns CreateAgentRuntime's bare "use a different name" into something
    actionable: DELETING means retry shortly, CREATE_FAILED means delete the
    orphan (with the exact command), other statuses report the id."""
    detail = f"An agent named {runtime_name!r} already exists in {region}."
    try:
        existing = _runtime_by_name(client, runtime_name)
    except Exception:
        return detail
    if existing is None:
        return detail
    status = existing["status"]
    runtime_id = existing["agentRuntimeId"]
    if status == "DELETING":
        return (
            f"{detail} It's still being deleted ({runtime_id}), and AgentCore keeps the name "
            "reserved until that finishes -- retry in a minute."
        )
    if status in ("CREATE_FAILED", "UPDATE_FAILED"):
        return (
            f"{detail} It's a leftover from a failed deploy ({runtime_id}, status {status}) and will "
            "hold the name until it's deleted:\n"
            f"  aws bedrock-agentcore-control delete-agent-runtime --region {region} "
            f"--agent-runtime-id {runtime_id}"
        )
    return f"{detail} It's in status {status} ({runtime_id}) -- rename this agent, or delete that runtime first."


def _runtime_by_name(client, runtime_name):
    """The first runtime with this name in any status, or None."""
    for page in client.get_paginator("list_agent_runtimes").paginate():
        for runtime in page["agentRuntimes"]:
            if runtime["agentRuntimeName"] == runtime_name:
                return runtime
    return None


def _clean_up_failed_create(client, agent_runtime_id):
    """Deletes a runtime the service just declared CREATE_FAILED so its name
    doesn't block the next deploy. Best-effort -- never replaces the error."""
    try:
        client.delete_agent_runtime(agentRuntimeId=agent_runtime_id)
    except Exception as exc:
        return (
            f"\n\nThe failed runtime {agent_runtime_id} could not be cleaned up ({exc}); it will hold "
            "its name until deleted."
        )
    return f"\n\nThe failed runtime {agent_runtime_id} was deleted, so its name is free to reuse."


def _code_artifact(runtime_name, agent_config, region):
    """Builds the zip, uploads it, and returns the codeConfiguration artifact
    pointing at it. The zip is deleted once uploaded; the S3 object itself
    stays, since AgentCore re-reads it (a runtime references the object, it
    doesn't copy it)."""
    bucket = _staging_bucket_for(region)
    # Before the zip is built, not after: building it is the expensive part (pip
    # vendoring wheels for Graviton), and a mispaired bucket makes the deploy
    # fail regardless.
    _check_staging_bucket(bucket, region)
    zip_path = _build_deployment_package(agent_config)
    try:
        s3_key = f"agent-portal/{runtime_name}-{time.time_ns()}.zip"
        # region_name is where the *client* talks, not where the bucket is --
        # a bucket is reachable from any region's endpoint, so this only has to
        # be a region this account can sign for.
        boto3.client("s3", region_name=region).upload_file(str(zip_path), bucket, s3_key)
    finally:
        zip_path.unlink(missing_ok=True)
    return {
        "codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": s3_key}},
            "runtime": _RUNTIME_ENUM,
            # opentelemetry-instrument wraps main.py to auto-instrument
            # botocore/Bedrock + Strands' own tracer -- confirmed
            # directly (via a real agent deployed with this wrapper)
            # this is what actually produces gen_ai.* spans in
            # CloudWatch Logs; without it there's no trace data at
            # all, even with Transaction Search enabled and the
            # right IAM permissions in place. See docs/aws.md's
            # "How AWS traces work" section. Container mode gets the
            # same wrapper from the Dockerfile's own CMD.
            "entryPoint": ["opentelemetry-instrument", "main.py"],
        }
    }


def _container_artifact(runtime_name, agent_config, region):
    return {
        "containerConfiguration": {"containerUri": _build_container_image(runtime_name, agent_config, region)}
    }


def deploy(
    name, model, description, agent_instructions, tool_ids, mcp_server_ids=(),
    deployment_mode=None, region=None, runtime_version=None,
):
    """Three independent choices, deliberately not one combined option.

    deployment_mode picks which of CreateAgentRuntime's two artifact
    alternatives to deploy: "code" (the default -- a zip on S3 run by
    AgentCore's managed Python runtime) or "container" (an image built here
    and pushed to ECR). Both are built from the same aws_hosted/main.py and the
    same agent_config.json, so the difference is purely how AgentCore packages
    and starts them -- see docs/aws.md's 'Container deploys'.

    region is where the runtime gets created; None means REGION, so a
    single-region install behaves exactly as it did before this parameter
    existed. It's resolved once here and then passed down explicitly rather
    than read off the global by each helper, because a create is the one
    operation with no ARN to recover a region from -- every later operation on
    this agent reads it back off the ARN this call returns (see _region_of).

    runtime_version selects AgentCore Runtime's platform version: "V2"
    (snapshot resume -- resumes a paused, pre-initialized snapshot; private
    preview) or "V1"/None (the platform's own default: a warm pool of
    instances, full cold start when it misses). See _platform_version_kwargs
    for what each one actually sends and why V2 is the only value that can
    fail here. Note the preview isn't necessarily enabled in every region --
    that check is the service's, not this module's.

    Any pairing of the three is valid, which is the point: a V1 container
    against a V2 code zip, in one region or two, is the comparison worth
    running."""
    region = region or REGION
    deployment_mode = deployment_mode or DEFAULT_DEPLOYMENT_MODE
    if deployment_mode not in DEPLOYMENT_MODES:
        # Checked here as well as in server.py's own request validation:
        # deploy() is also called directly by smoke/aws_smoke_test.py and
        # from a REPL, and a typo'd mode silently deploying the default one
        # would produce an agent labelled as something it isn't.
        raise RuntimeError(f"deployment_mode must be one of {DEPLOYMENT_MODES}, got {deployment_mode!r}")
    # Deliberately before the expensive work below (zip build, or a whole
    # container build and push): an unavailable V2 is knowable up front, so it
    # shouldn't cost a package build and an orphaned S3 object or ECR image to
    # find out. Same reasoning as the gateway-URL check right after it.
    platform_version = _platform_version_kwargs(runtime_version)
    if WEB_SEARCH_TOOL_ID in tool_ids:
        # Fails loudly at deploy time rather than silently shipping an agent
        # missing the tool it was configured with (the config's "tools" would
        # still list "web_search_aws", but aws_hosted/main.py would have no
        # gateway URL to connect to) -- same "raise, don't silently no-op" as
        # this module's other error handling (see _stream_events).
        _check_web_search_gateway_url()
    # Everything about *this agent* travels inside the artifact instead (see
    # _agent_config). The environment carries only what the AWS SDK itself
    # reads, which is why the V2 payload check below can no longer be the thing
    # that blocks a deploy.
    #
    # The runtime's own region, not the portal's: this is what the hosted agent
    # signs its Bedrock calls with (see aws_hosted/main.py), and a us-west-2
    # agent calling us-east-1 Bedrock would both cross a region needlessly and
    # fail wherever the model isn't available.
    environment_variables = {"AWS_REGION": region}
    if platform_version:
        _check_v2_env_payload(environment_variables)
    agent_config = _agent_config(model, agent_instructions, tool_ids, mcp_server_ids)
    runtime_name = _slugify(name)
    artifact = (
        _container_artifact(runtime_name, agent_config, region)
        if deployment_mode == "container"
        else _code_artifact(runtime_name, agent_config, region)
    )
    return _create_runtime(
        runtime_name, artifact, description, environment_variables, region, platform_version,
        deployment_mode=deployment_mode,
    )


def undeploy(resource_id):
    """Idempotent: a runtime already on its way out is a successful delete,
    not a failure. DeleteAgentRuntime is asynchronous and a runtime sits in
    DELETING for well over a minute, so a second delete for the same agent
    (a double-click, or a retry after this process restarted mid-delete) used
    to raise straight out of here -- and server.py's _run_undeploy turns any
    exception into "Delete failed" plus a flip back to STATUS_ACTIVE, so the
    row reappeared as live even though the delete was in fact proceeding
    normally. Worse, once the runtime finished deleting the retry became
    ResourceNotFound, which failed the same way, leaving a row that could
    never be deleted from the UI at all.

    Both of those states mean "this runtime is gone or going", so they're
    swallowed. Anything else still raises -- a runtime busy with an
    UpdateAgentRuntime really is "try again later", and a genuine
    permissions problem must not be silently reported as a clean delete.

    The failure code is deliberately not trusted to tell those apart.
    DeleteAgentRuntime on a runtime that no longer exists answers
    AccessDeniedException, not ResourceNotFoundException -- confirmed
    directly against this account with `bedrock-agentcore:*` on `*`
    granted, so it is the service masking existence rather than a real
    authorization gap. GetAgentRuntime does report ResourceNotFound
    truthfully for the same id, so the runtime's own status is used as the
    authority and the original error is re-raised whenever that status says
    the runtime is still there."""
    client = _control_client(resource_id)
    agent_runtime_id = resource_id.split("/")[-1]
    try:
        client.delete_agent_runtime(agentRuntimeId=agent_runtime_id)
    except (
        client.exceptions.ResourceNotFoundException,
        client.exceptions.ConflictException,
        client.exceptions.AccessDeniedException,
    ):
        try:
            status = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)["status"]
        except client.exceptions.ResourceNotFoundException:
            return
        if status != "DELETING":
            raise


# A runtime in DELETING is still returned by ListAgentRuntimes -- confirmed
# directly against a real account mid-delete, not assumed. Excluding it here
# fixes two things that both read this list as "what exists right now":
# server.py's _wait_until_actually_gone (which polls for the resource to
# disappear and, without this, could never succeed for AWS -- it burned its
# full 60s budget on every single delete), and startup seeding (which
# otherwise imports agents that are in the middle of being torn down).
#
# resource_exists deliberately does NOT use this set -- see its docstring. The
# two callers are asking different questions, and conflating them is what broke
# Edit -> Recreate: a name stays reserved for the whole of DELETING, so "don't
# list it" and "safe to reuse its name" are not the same fact.
_GONE_STATUSES = frozenset({"DELETING"})


def list_deployed(region=None):
    """ListAgentRuntimes is per-region and there is no cross-region variant, so
    this reports one region at a time. None means REGION, which is what startup
    seeding wants: it imports the default region only, deliberately, because
    scanning every configured region would pull in whatever unrelated runtimes
    the account happens to own elsewhere."""
    paginator = _control_client(region=region).get_paginator("list_agent_runtimes")
    return [
        {"name": r["agentRuntimeName"], "resource_id": r["agentRuntimeArn"]}
        for page in paginator.paginate()
        for r in page["agentRuntimes"]
        if r["status"] not in _GONE_STATUSES
    ]


def resource_exists(resource_id):
    """Whether one specific runtime is still there, asked in its own region.

    Exists because server.py's _wait_until_actually_gone used to answer this
    by scanning list_deployed(), which only ever covers one region: for an
    agent deployed outside the default region that scan can't contain it, so
    the wait would conclude "already gone" immediately and stop protecting a
    recreate from colliding with the name it is about to reuse.

    GetAgentRuntime instead -- addressed by ARN, so it follows the agent to
    whatever region it's in, it's one call rather than a full paginated list,
    and it's already the call undeploy() above trusts as the authority on
    whether a runtime exists.

    A runtime in DELETING counts as *still there*, unlike list_deployed's view
    of the same status (see _GONE_STATUSES). The only caller is the wait that
    guards Edit -> Recreate, and AgentCore holds a name reserved until the
    delete finishes, so reading DELETING as gone let the recreate fire into a
    name still in use -- reproduced live in us-west-2 as "An agent with the
    specified name already exists"."""
    client = _control_client(resource_id)
    try:
        client.get_agent_runtime(agentRuntimeId=resource_id.split("/")[-1])
    except client.exceptions.ResourceNotFoundException:
        return False
    return True


# Sent as the "prompt" of a throwaway InvokeAgentRuntime call fired the
# moment a chat session opens (create_session), so the hosted container
# builds and caches this session's Strands Agent -- including its MCP
# client connections, the slowest part -- before the user's first real
# Send instead of during it. A plain sentinel string, not a structured
# field, keeps the wire payload unchanged for containers that don't know
# about it yet: aws_hosted/main.py carries its own copy of this exact
# string (no shared import -- it's zipped and deployed standalone) and
# must be redeployed to actually recognize it; until then it just answers
# the sentinel like a real (harmless, low-cost) message instead of no-oping,
# which is fine as a transitional state but means the eager-build benefit
# only applies to agents redeployed after this change.
_WARMUP_SENTINEL = "--WARMUP--"


async def _warm_up(resource_id, session_id, timings=None, fired=None):
    """Best-effort: any failure here (older container, transient network
    issue) just means the first real Send pays the same cold-start cost it
    always did -- never surfaced as an error since the caller never awaits
    this except to avoid racing the real call (see stream_chat).

    Returns the container's own account of what it spent starting the session
    -- session_init_ms (get_or_create_agent), module_init_ms (its imports and
    module-level setup) and container_age_ms (how old the process was when the
    ping reached it) -- as yielded by aws_hosted/main.py for the warmup
    sentinel. Returns {} on any failure, and on an older container that
    predates these fields, so wait_for_ready can tell "not measured" apart
    from a real zero (same convention as latest_retries elsewhere in this
    module). Anything else on the line is still just drained.

    A legacy container yielding only the older app_init_ms is read as
    session_init_ms, its exact former meaning: an agent deployed before this
    change still reports a platform number, just one that (as before) leaves
    that container's import cost inside it.

    timings and fired are where the measurement is split, and the split is
    the whole point. This coroutine runs as a background task whose actual
    HTTP call happens in a worker thread, so an arbitrary amount of time can
    pass between the task being created and the request leaving this
    process: waiting for a free thread in asyncio's default executor,
    building the boto3 client on first touch of a region (credential
    resolution, service-model load, TLS), or simply not being scheduled
    while the machine is busy. None of that is the platform's cost. So the
    clock for cold_start_ms starts once the client is in hand and the
    request is about to go out, and everything before it is reported
    separately as client_queue_ms.

    Timing the whole thing from `fired` instead -- which is what this did
    originally -- charges a busy client's own delay to the platform, and by
    enough to invalidate a run: a real 20-concurrency burst from a loaded
    laptop reported a 30s p75 "platform startup" for invokes CloudWatch
    shows AgentCore served in ~2s, because those invokes sat in the client
    for up to 20s before being issued. Both numbers are worth having --
    client_queue_ms is what makes that failure visible as a client problem
    instead of a platform one."""

    def call():
        # Fetched before the clock starts: first use in a region builds the
        # client, resolves credentials and loads the service model, all of
        # which is this process's cost rather than the platform's.
        client = _data_client(resource_id)
        call_started = time.monotonic()
        if timings is not None and fired is not None:
            timings["client_queue_ms"] = round((call_started - fired) * 1000)
        try:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=resource_id,
                runtimeSessionId=session_id,
                payload=json.dumps({"prompt": _WARMUP_SENTINEL}).encode("utf-8"),
                contentType="application/json",
                accept="text/event-stream",
            )
            return _startup_report(response["response"].iter_lines())
        finally:
            # In the finally, so an invoke that raised still reports how long
            # it took to fail rather than leaving _timed_warm_up to fall back
            # to the whole wall time -- which would smuggle back in exactly
            # the queueing this boundary exists to keep out.
            if timings is not None:
                timings["cold_start_ms"] = round((time.monotonic() - call_started) * 1000)

    try:
        return await asyncio.to_thread(call)
    except Exception as exc:
        # Recorded rather than dropped. Still not raised -- the chat path's
        # tolerance of a failed ping is deliberate (see above) -- but a caller
        # whose whole purpose is measuring the warmup has to be able to tell a
        # measurement apart from a failure, and returning {} alone doesn't say
        # which. Found live: a run with expired credentials reported 40 clean
        # ~200ms "successes", because every invoke failed here in the time it
        # takes to reject a signature and the timings above still stamped that
        # as a session start. See latest_warmup_error.
        if timings is not None:
            timings["error"] = exc
        return {}


def _startup_report(lines):
    """The parse half of _warm_up, split out so it's testable without a real
    invoke -- see that docstring for what the fields mean and why a legacy
    app_init_ms is read as session_init_ms."""
    report = {}
    for line in lines:
        if not line or not line.startswith(b"data: "):
            continue
        payload = json.loads(line[len(b"data: ") :])
        if not isinstance(payload, dict):
            continue
        for field in ("session_init_ms", "module_init_ms", "container_age_ms"):
            if payload.get(field) is not None:
                report[field] = payload[field]
        if payload.get("app_init_ms") is not None:
            report.setdefault("session_init_ms", payload["app_init_ms"])
    return report


async def _timed_warm_up(resource_id, session_id, session_state):
    """Fires _warm_up and stamps what it cost where wait_for_ready can read
    it back later, as two numbers rather than one: cold_start_ms, the
    invoke's own duration, and client_queue_ms, the delay on this side before
    the invoke went out. _warm_up has the argument for where that boundary
    belongs.

    This has to be recorded by the task itself rather than computed at await
    time. wait_for_ready's own clock starts when a caller begins awaiting,
    which for a real chat turn is when the user presses Send -- long after
    the ping went out -- so it measures only the part of the warmup that
    turn actually waited through, not what the warmup cost. Measuring
    instead from create_session to the moment of the await would be worse in
    the other direction: it would swallow the entire idle gap while the user
    typed, reporting a 30s "warmup" for an 8s one. Only the task knows when
    it really finished, so it says so here."""
    fired = time.monotonic()
    timings = {}
    try:
        return await _warm_up(resource_id, session_id, timings, fired)
    finally:
        # The fallback covers the call never reaching its own clock at all --
        # a client that failed to build, a task cancelled before its thread
        # ran. The whole wall time is then the only figure there is, and
        # overstating one invoke beats reporting nothing for a session that
        # did happen. client_queue_ms stays None there for the same reason
        # the other unmeasured fields do: no honest value to give.
        session_state["cold_start_ms"] = timings.get(
            "cold_start_ms", round((time.monotonic() - fired) * 1000)
        )
        session_state["client_queue_ms"] = timings.get("client_queue_ms")
        session_state["warmup_error"] = timings.get("error")


async def create_session(resource_id, user_id):
    session_id = f"portal-session-{secrets.token_hex(16)}"
    session_state = {"session_id": session_id}
    session_state["_warmup_task"] = asyncio.create_task(
        _timed_warm_up(resource_id, session_id, session_state)
    )
    return session_state


async def close_session(session_state):
    pass  # AgentCore has no explicit session-teardown call


def _new_trace_parent():
    """A W3C traceparent we generate ourselves -- AgentCore/X-Ray honor a
    caller-supplied trace ID (confirmed via InvokeAgentRuntime's real
    `traceParent` header parameter), the same trick ~/Dev/AWS/demo_web.py
    already uses. This means a turn's trace can be looked up directly by
    ID afterward instead of searching by time window/session id."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return trace_id, f"00-{trace_id}-{span_id}-01"


def _stream_events(resource_id, message, session_id, trace_id, trace_parent):
    response = _data_client(resource_id).invoke_agent_runtime(
        agentRuntimeArn=resource_id,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": message}).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
        traceParent=trace_parent,
    )
    yield {"trace_id": trace_id}
    # botocore retries a throttled/5xx call internally (its own backoff,
    # invisible otherwise) and reports how many attempts it took via
    # ResponseMetadata -- confirmed directly against a real invoke_agent_
    # runtime response, not assumed from docs. Not a precise "this call was
    # throttled" signal (botocore also retries on some transient network/5xx
    # errors), but a real, honest one: >0 here means something made this
    # call slower than a single clean round trip, which is exactly the
    # "throttled LLM" story this is meant to surface.
    retries = response.get("ResponseMetadata", {}).get("RetryAttempts")
    if retries is not None:
        yield {"retries": retries}
    for line in response["response"].iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        payload = json.loads(line[len(b"data: ") :])
        if "event" not in payload:
            # bedrock_agentcore's own runtime wrapper (aws_hosted/main.py's
            # entrypoint, via BedrockAgentCoreApp) sends this shape instead
            # of a normal Bedrock event when the entrypoint itself raised
            # mid-stream. Confirmed directly against its source
            # (_sync_stream_with_error_handling/_stream_with_error_handling
            # in bedrock_agentcore/runtime/app.py): "error" is str(exception)
            # -- the actually useful text (e.g. a real ParamValidationError
            # message) -- while "message" is always the same static "An
            # error occurred during streaming", regardless of cause.
            detail = payload.get("error") or payload.get("message") or f"agent runtime error: {payload}"
            error_type = payload.get("error_type")
            raise RuntimeError(f"{error_type}: {detail}" if error_type else detail)
        event = payload["event"]
        # A tool call is identified in contentBlockStart -- name and
        # toolUseId land there before any argument JSON streams in via
        # contentBlockDelta -- so this fires before the network wait on the
        # tool's own result, making it the earliest real "the agent is
        # doing something" signal available, well ahead of any answer text.
        start = event.get("contentBlockStart", {}).get("start", {})
        if "toolUse" in start:
            yield {"tool_call": {"name": start["toolUse"].get("name")}}
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            yield {"text": delta["text"]}
        reasoning = delta.get("reasoningContent", {}).get("text")
        if reasoning:
            yield {"reasoning": reasoning}
        # The stream's final event is a distinct "metadata" event (not a
        # contentBlockDelta) carrying real token usage -- confirmed
        # directly against a live agent, not guessed from docs.
        usage = event.get("metadata", {}).get("usage")
        if usage:
            yield {
                "usage": {
                    "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens"),
                }
            }


async def wait_for_ready(session_state):
    """Waits out the warmup ping fired at session-open (create_session) --
    pure session-start cost, no LLM call involved at all -- instead of
    racing it with a second concurrent invoke_agent_runtime call against
    the same session_id. Usually already done by the time a user finishes
    typing and hits Send, so this is normally a no-op wait; _warm_up itself
    swallows its own errors, so this never raises -- a caller that needs to
    know the ping failed reads latest_warmup_error. Timed (not just
    awaited) so a slow/cold-start turn can be told apart from a slow LLM
    call -- see latest_warmup_ms. Also what the load test's session-
    start-only mode calls directly (chat_ws's "warmup_only" message),
    which never calls stream_chat/the LLM at all. The task is never
    cleared from session_state, so calling this more than once per session
    (or after stream_chat already has) is safe -- re-awaiting an
    already-done asyncio.Task returns instantly.

    Also splits the session start into two numbers, which is the point of the
    whole sentinel mechanism:

      * platform_startup_ms -- everything AgentCore Runtime itself did before
        any of this agent's code ran: routing the invoke, and provisioning or
        reusing an instance. This is the number that can be compared across
        agent platforms, because nothing about *this* agent's dependencies or
        tool set moves it.
      * agent_init_ms -- everything our own code spent, folded into one
        figure: the container's module import/setup plus get_or_create_agent()
        building this session's Agent and opening its MCP client connections.

    Derived by subtraction rather than measured directly, since the platform
    won't say when it handed over: agent_init_ms is timed *inside* the
    container (aws_hosted/main.py) and carried back on the warmup response,
    and what's left of the round trip is the platform's.

    Two subtleties in that arithmetic:

    Subtracted from cold_start_ms, not from the warmup_ms returned below.
    cold_start_ms is the invoke's own duration, so the result describes the
    session start itself and doesn't change with when a caller happened to
    start waiting -- a chat turn that waits out only the tail of a warmup must
    not be reported as a faster platform than a load test that waits out all
    of it. Nor does it move with how busy this process was when the ping was
    fired: that delay is reported on its own as client_queue_ms (see
    _warm_up), which keeps it out of a number labelled as the platform's.

    module_init_ms only counts when the container was in fact started for this
    session: the container reports its age at the moment the ping arrived, and
    an age older than the round trip means its imports were already paid for,
    before this measurement began, so subtracting them would flatter the
    platform. (A pre-warmed instance is exactly the case AgentCore's warm pool
    is meant to produce.)

    platform_startup_ms is None (not clamped to 0) when the container reported
    nothing to subtract -- an agent deployed before the sentinel existed, or a
    warmup call that failed outright. There's no honest subtraction to do
    without it, and a zero would read as an impossibly fast platform.

    The returned warmup_ms is the wait this caller experienced, which is not
    what the warmup cost: the ping is fired at session-open and runs while
    the user types, so a real chat turn typically waits out only its tail
    (sometimes none of it). latest_cold_start_ms reports the other number --
    the invoke's own duration, as timed by _timed_warm_up. Keeping both
    is the point: warmup_ms explains this turn, cold_start_ms explains the
    session start, and the difference is what pre-warming actually bought."""
    warmup_task = session_state.get("_warmup_task")
    session_state["last_warmup_ms"] = None
    session_state["last_agent_init_ms"] = None
    session_state["last_platform_startup_ms"] = None
    session_state["last_cold_start_ms"] = None
    session_state["last_client_queue_ms"] = None
    session_state["last_warmup_error"] = None
    if warmup_task is None:
        return 0
    warmup_started = time.monotonic()
    report = await warmup_task
    warmup_ms = round((time.monotonic() - warmup_started) * 1000)
    session_state["last_warmup_ms"] = warmup_ms
    # Set by the task itself before this await returned (its finally block),
    # so it is always populated here -- .get() only guards a session_state
    # built by something other than create_session, e.g. a test double.
    cold_start_ms = session_state.get("cold_start_ms")
    session_state["last_cold_start_ms"] = cold_start_ms
    session_state["last_client_queue_ms"] = session_state.get("client_queue_ms")
    session_state["last_warmup_error"] = session_state.get("warmup_error")
    session_state["last_agent_init_ms"] = _agent_init_ms(report, cold_start_ms)
    if session_state["last_agent_init_ms"] is not None and cold_start_ms is not None:
        session_state["last_platform_startup_ms"] = max(0, cold_start_ms - session_state["last_agent_init_ms"])
    return warmup_ms


def _agent_init_ms(report, cold_start_ms):
    """Folds the container's own startup figures (see _warm_up) into the one
    number that belongs on our side of the split, or None if it reported
    nothing to work with.

    The module_init_ms half is included only when the container's age at
    invoke time is inside the round trip that measured it -- see
    wait_for_ready's docstring for why, and note the comparison is against
    cold_start_ms (the whole ping) for the same reason the subtraction is."""
    session_init_ms = report.get("session_init_ms")
    if session_init_ms is None:
        return None
    module_init_ms = report.get("module_init_ms")
    container_age_ms = report.get("container_age_ms")
    started_for_this_session = (
        module_init_ms is not None
        and container_age_ms is not None
        and cold_start_ms is not None
        and container_age_ms <= cold_start_ms
    )
    return session_init_ms + module_init_ms if started_for_this_session else session_init_ms


async def stream_chat(resource_id, message, user_id, session_state):
    session_id = session_state["session_id"]
    await wait_for_ready(session_state)
    trace_id, trace_parent = _new_trace_parent()

    def make_iter():
        return _stream_events(resource_id, message, session_id, trace_id, trace_parent)

    async for item in bridge_sync_iterable(make_iter):
        if "usage" in item:
            session_state["last_usage"] = item["usage"]
        elif "trace_id" in item:
            session_state["last_trace_id"] = item["trace_id"]
        elif "retries" in item:
            session_state["last_retries"] = item["retries"]
        else:
            yield item


async def latest_usage(session_state):
    return session_state.get("last_usage")


async def latest_warmup_ms(session_state):
    return session_state.get("last_warmup_ms")


async def latest_agent_init_ms(session_state):
    return session_state.get("last_agent_init_ms")


async def latest_platform_startup_ms(session_state):
    return session_state.get("last_platform_startup_ms")


async def latest_cold_start_ms(session_state):
    return session_state.get("last_cold_start_ms")


async def latest_client_queue_ms(session_state):
    return session_state.get("last_client_queue_ms")


async def latest_warmup_error(session_state):
    return session_state.get("last_warmup_error")


async def latest_retries(session_state):
    return session_state.get("last_retries")


async def latest_trace_id(session_state):
    return session_state.get("last_trace_id")


def _log_group_for(resource_id):
    runtime_id = resource_id.split("/")[-1]
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def _run_logs_insights_query(log_group, region, query_string, minutes_back=15):
    """Polls to completion rather than a fixed sleep-then-fetch-once --
    confirmed directly that a fixed short wait isn't reliable (event
    records can take under a minute to index, but the exact time varies).

    region is the runtime's own, not REGION: the log group belongs to the
    runtime, so a cross-region agent's telemetry is in that region's Logs
    and querying REGION's would report ResourceNotFoundException on a log
    group that exists perfectly well elsewhere."""
    from datetime import datetime, timedelta, timezone

    logs = _client("logs", region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)
    query_id = logs.start_query(
        logGroupName=log_group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query_string,
        limit=200,
    )["queryId"]
    for _ in range(30):
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return result if result["status"] == "Complete" else None
        time.sleep(2)
    return None


def _truncate(text, limit=600):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text) - limit} more chars)"


def _format_content(content):
    """content: a list of {"text": "..."} / {"toolUse": {...}} /
    {"toolResult": {...}} parts -- Strands/Bedrock's native shape,
    confirmed directly against real captured events (distinct from
    Azure's/OpenAI's differently-shaped "parts" format)."""
    lines = []
    for part in content or []:
        if "text" in part:
            lines.append(_truncate(part["text"]))
        elif "toolUse" in part:
            tool = part["toolUse"]
            args = ", ".join(f"{k}={v!r}" for k, v in (tool.get("input") or {}).items())
            lines.append(f"-> call {tool.get('name')}({args})")
        elif "toolResult" in part:
            for inner in part["toolResult"].get("content") or []:
                if "text" in inner:
                    lines.append(f"<- {_truncate(inner['text'])}")
    return lines


def get_trace(resource_id, trace_id):
    """Reconstructs a turn's conversation from the gen_ai.* OTEL *event*
    records that Strands' auto-instrumentation (via the
    `opentelemetry-instrument` entrypoint wrapper) writes straight into
    the runtime's own CloudWatch log group -- no X-Ray API, no `agentcore`
    CLI. Confirmed AgentCore Runtime does *not* emit named/timed span
    records here (only these content-bearing events), unlike Gemini/Azure
    -- verified directly on both PYTHON_3_12 and PYTHON_3_14 runtimes, so
    this renders per-turn conversation content without per-call duration,
    a real platform limitation rather than a bug (see docs/aws.md's "How
    AWS traces work").

    Returns {"trace_id": ..., "spans": [{"name", "duration_ms", "lines"}]}
    -- same shape as Gemini's span_to_dict, for a consistent frontend
    across all three platforms, though here it's always a single span:
    confirmed directly that `gen_ai.choice`'s event for a tool-use
    decision fires *before* the corresponding tool-call/result events for
    that same round, not after -- splitting into one card per round on
    that boundary produced an awkward empty-looking first card. A single
    chronological transcript (the same approach ~/Dev/AWS/show_traces.py
    already uses, rather than a novel one) reads correctly regardless of
    that ordering quirk. Returns None until the whole turn is queryable --
    measured directly, that's 60-90 seconds after the answer, not the "under
    a minute" this used to claim (server.py's _TRACE_RETRY_DELAYS is sized
    for the real number). Callers should treat None as "not ready", not as a
    hard failure."""
    log_group = _log_group_for(resource_id)
    result = _run_logs_insights_query(
        log_group,
        _region_of(resource_id),
        # traceId must be listed in `fields`, not just `filter` -- confirmed
        # live: the identical query without it silently returned zero rows
        # even though the same records matched fine once traceId was
        # explicitly selected. CloudWatch Logs Insights auto-discovers JSON
        # fields from a sample of recent events, and filtering on a
        # discovered field that isn't also selected isn't reliable.
        f'fields @timestamp, @message, traceId | filter traceId = "{trace_id}" | filter isPresent(eventName) '
        "| sort @timestamp asc",
    )
    if not result or not result["results"]:
        return None

    lines = []
    seen_lines = set()
    turn_complete = False

    def emit(prefix, line):
        # Confirmed live on a real multi-round tool-use turn: at every
        # completion round, the instrumentation re-logs the *entire*
        # conversation-so-far as a fresh batch of gen_ai.*.message events
        # (same toolUseId, byte-identical content) rather than just the
        # new delta -- e.g. a 2nd-turn "and AMZN?" question replayed the
        # 1st turn's GOOG tool-call/result/answer as if they'd just
        # happened again. No tool was actually re-invoked (same toolUseId
        # both times, confirmed by inspecting the raw CloudWatch events),
        # so a global "already rendered this exact line" dedup -- rather
        # than the narrower user-message-only one this used to be -- is
        # what makes the transcript accurate, not just less noisy.
        full = f"{prefix}: {line}"
        if full not in seen_lines:
            seen_lines.add(full)
            lines.append(full)

    for row in result["results"]:
        msg = next(f["value"] for f in row if f["field"] == "@message")
        event = json.loads(msg)
        name = event.get("eventName")
        body = event.get("body")
        if not isinstance(body, dict):
            continue

        if name == "gen_ai.user.message":
            # A tool result also arrives wrapped as a "user" message
            # (Bedrock's conversation format) -- skip it here, since it's
            # a verbatim duplicate of the gen_ai.tool.message event
            # already rendered on its own (same redundancy
            # ~/Dev/AWS/show_traces.py's own docstring already documents).
            text_only = [p for p in (body.get("content") or []) if "text" in p]
            for line in _format_content(text_only):
                emit("You", line)

        elif name == "gen_ai.assistant.message":
            for line in _format_content(body.get("content")):
                emit("Agent", line)

        elif name == "gen_ai.tool.message":
            for line in _format_content(body.get("content")):
                emit("tool", line)

        elif name == "gen_ai.choice":
            message = body.get("message", {})
            for line in _format_content(message.get("content")):
                emit("Agent", line)
            # A "tool_use" choice means the model wants another tool round,
            # not that the turn is done -- only a non-tool_use finish_reason
            # (e.g. "end_turn") means the transcript is complete. Without
            # this check, get_trace can return a truthy-but-partial result
            # (missing the final answer) if it's queried in the narrow
            # window after the tool-use round's events have indexed but
            # before the final round's have -- confirmed live: a real query
            # returned only the tool-call/tool-result lines, with the
            # final "Agent: ..." answer landing in CloudWatch moments later.
            if body.get("finish_reason") != "tool_use":
                turn_complete = True

    # turn_complete alone isn't enough: these records index independently and
    # out of order, so the window where the final "end_turn" choice is
    # queryable but the user's own question isn't is real -- reproduced live,
    # a query at t+79s returned exactly one line (the answer) for a turn whose
    # full four-line transcript was there moments later. A transcript missing
    # the question it answers isn't ready, it's just truncated, so require the
    # "You:" line too and let the caller keep waiting.
    if not lines or not turn_complete or not any(line.startswith("You: ") for line in lines):
        return None
    return {"trace_id": trace_id, "spans": [{"name": "conversation", "duration_ms": None, "lines": lines}]}
