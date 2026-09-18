"""Standalone verification of deployers/aws.py's mechanism before trusting
it live in the portal UI -- same rationale as smoke/smoke_test.py/
smoke/azure_smoke_test.py. This path is the most novel of the three: it's the
first time this whole project deploys to Bedrock AgentCore Runtime via
direct boto3 control-plane calls rather than the `agentcore` CLI + CDK
(see deployers/aws.py's docstring), and the dependency-vendoring step
(cross-targeted pip install) has never been exercised for real.

Run: ./aws_smoke_test.sh
"""

import asyncio
import time

from deployers import aws as aws_deployer

DISPLAY_NAME = "agent_portal_smoke_test"


async def chat(resource_id, question):
    session_state = await aws_deployer.create_session(resource_id, "smoke-test-user")
    try:
        answer = ""
        async for event in aws_deployer.stream_chat(resource_id, question, "smoke-test-user", session_state):
            if event.get("text"):
                answer += event["text"]
        return answer
    finally:
        await aws_deployer.close_session(session_state)


def main():
    print(f"Deploying {DISPLAY_NAME!r} (model=claude-haiku-4.5, tools=stock_data)...")
    started = time.monotonic()
    resource_id = aws_deployer.deploy(
        name=DISPLAY_NAME,
        model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="Smoke test agent for agent-portal's AWS deploy path.",
        agent_instructions="You are a terse assistant. Use get_stock_price when asked about a stock price.",
        tool_ids=["stock_data"],
    )
    elapsed = time.monotonic() - started
    print(f"Deployed in {elapsed:.1f}s: {resource_id}")

    print("\nConfirming it shows up via list_deployed()...")
    found = any(e["resource_id"] == resource_id for e in aws_deployer.list_deployed())
    print(f"  found in list_deployed(): {found}")
    assert found, "Newly created agent not found in list_deployed()"

    print("\nAsking a real question...")
    answer = asyncio.run(chat(resource_id, "What's the latest price of AAPL?"))
    print(f"  Agent answered: {answer!r}")
    assert answer.strip(), "Agent returned an empty answer"
    assert "AAPL" in answer.upper() or "$" in answer, "Answer doesn't look like it used the tool"

    print("\nDeleting the smoke test agent...")
    aws_deployer.undeploy(resource_id)
    # DeleteAgentRuntime is asynchronous -- confirmed directly: the
    # resource briefly still appeared in list_deployed() right after a
    # successful delete call, then a follow-up GetAgentRuntime raised
    # ResourceNotFoundException moments later. Poll rather than check once.
    still_there = True
    for _ in range(12):
        still_there = any(e["resource_id"] == resource_id for e in aws_deployer.list_deployed())
        if not still_there:
            break
        time.sleep(5)
    print(f"  still in list_deployed() after delete: {still_there}")
    assert not still_there, "Agent still present after undeploy() (waited 60s)"

    print("\nSMOKE TEST PASSED: deploy -> list -> chat -> delete all work as expected.")


if __name__ == "__main__":
    main()
