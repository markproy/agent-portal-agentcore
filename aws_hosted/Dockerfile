# The image behind a container-mode AWS deploy (deployers/aws.py's
# _build_container_image) -- the *same* main.py/tools.py/requirements.txt a
# code-zip deploy ships, just packaged as an image instead of a zip on S3.
# Not built with this directory as its context: deployers/aws.py copies these
# files into a staging directory (along with the generated agent_config.json)
# and builds that, so the build context is exactly what the image needs (see
# that function's docstring).
#
# Built for linux/arm64 -- AgentCore Runtime runs on Graviton, confirmed the
# expensive way by a real failed deploy on the code-zip path ("Your artifact
# contains binary files that are incompatible with Linux ARM64").
#
# Base image comes from AWS's own public ECR mirror of the official Python
# image rather than Docker Hub: same image, no anonymous-pull rate limit, and
# the pull stays inside AWS. 3.12 to match the code-zip path's managed
# runtime (deployers/aws.py's _RUNTIME_ENUM), so the two modes differ in
# packaging only, not in Python version.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies before the source, so editing an agent's code doesn't
# re-resolve and re-download every wheel on the next build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# agent_config.json last: it's the only file that differs between two agents
# built from the same checkout, so keeping it in its own final layer lets every
# dependency and source layer stay shared in the cache (see deployers/aws.py's
# _agent_config for why per-agent config ships in the image at all).
COPY main.py tools.py ./
COPY agent_config.json ./

# AgentCore Runtime calls a container on 8080 (POST /invocations, GET /ping),
# which is what aws_hosted/main.py's BedrockAgentCoreApp already serves.
EXPOSE 8080

# opentelemetry-instrument, for the same reason the code-zip path uses it as
# its entryPoint: without the wrapper, Strands' tracer and the botocore/
# Bedrock auto-instrumentation never activate and the portal's trace panel
# has nothing to read (see docs/aws.md's "How AWS traces work").
CMD ["opentelemetry-instrument", "python", "main.py"]
