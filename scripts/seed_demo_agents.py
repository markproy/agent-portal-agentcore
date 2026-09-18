#!/usr/bin/env python3
"""Create a small set of demo agents in a running portal.

Usage:
    python scripts/seed_demo_agents.py [--host 127.0.0.1:8910] [--region us-east-1]

The portal must already be running (./run.sh) and your .env must have
AGENTCORE_EXECUTION_ROLE_ARN and AGENTCORE_STAGING_BUCKET configured.
Each agent takes 3-4 minutes to deploy on first run.
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

DEMO_AGENTS = [
    {
        "name": "stock-analyst",
        "description": "Concise stock analyst with real-time prices, price history, and web search for market news.",
        "agent_instructions": (
            "You are a sharp, no-nonsense stock analyst. "
            "When asked about a stock or market topic, use your tools to get current prices, "
            "price history, and recent news, then give a concise 2-3 sentence analysis backed by numbers. "
            "Cite specific figures. When data supports a chart, embed one using QuickChart: "
            "build a Chart.js config JSON, URL-encode it, and embed as "
            "![description](https://quickchart.io/chart?c=URLENCODED_CONFIG)."
        ),
        "tools": ["web_search", "stock_data"],
    },
    {
        "name": "trip-planner",
        "description": "Enthusiastic trip planner that builds day-by-day itineraries with food, activities, and local tips.",
        "agent_instructions": (
            "You are an enthusiastic trip planner who builds detailed, personalized itineraries. "
            "When someone tells you where they're going and when, search the web for current conditions, "
            "top restaurants, must-see attractions, hidden gems, and practical tips "
            "(weather, transport, reservations to book ahead). "
            "Structure your response as a day-by-day itinerary with morning / afternoon / evening sections. "
            "Include at least one splurge and one budget option per day. "
            "Be specific with names and neighborhoods, not generic advice."
        ),
        "tools": ["web_search"],
    },
    {
        "name": "research-assistant",
        "description": "General-purpose research assistant that searches the web and summarizes findings clearly.",
        "agent_instructions": (
            "You are a thorough research assistant. "
            "When asked a question, search the web for current, authoritative sources, "
            "then synthesize the findings into a clear, well-structured answer. "
            "Distinguish between established facts and recent/uncertain information. "
            "Cite your sources with brief descriptions of what each one contributed."
        ),
        "tools": ["web_search"],
    },
]


def api(host, method, path, body=None):
    url = f"http://{host}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def wait_active(host, agent_id, name, timeout=300):
    deadline = time.monotonic() + timeout
    dots = 0
    while time.monotonic() < deadline:
        agent = api(host, "GET", f"/api/agents/{agent_id}")
        status = agent.get("status", "unknown")
        if status == "active":
            print(f"\r  {name}: active ✓                    ")
            return True
        if status == "failed":
            print(f"\r  {name}: FAILED — {agent.get('error_message','')[:80]}")
            return False
        dots = (dots + 1) % 4
        print(f"\r  {name}: creating{'.' * dots}{'  ' * (3 - dots)}", end="", flush=True)
        time.sleep(5)
    print(f"\r  {name}: timed out waiting for active status")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1:8910", help="Portal host:port (default: 127.0.0.1:8910)")
    parser.add_argument("--region", default="us-east-1", help="AWS region to deploy into (default: us-east-1)")
    args = parser.parse_args()

    # Verify portal is reachable
    try:
        config = api(args.host, "GET", "/api/config")
    except Exception as e:
        print(f"Cannot reach portal at {args.host}: {e}")
        print("Make sure the portal is running (./run.sh) before seeding agents.")
        sys.exit(1)

    if not config.get("platforms", {}).get("aws", {}).get("available"):
        print("AWS platform is not configured. Set AGENTCORE_EXECUTION_ROLE_ARN and")
        print("AGENTCORE_STAGING_BUCKET in .env, then restart the portal.")
        sys.exit(1)

    # Skip agents that already exist
    existing = {a["name"] for a in api(args.host, "GET", "/api/agents")}
    to_create = [a for a in DEMO_AGENTS if a["name"] not in existing]

    if not to_create:
        print("All demo agents already exist. Nothing to do.")
        return

    if existing & {a["name"] for a in DEMO_AGENTS}:
        already = existing & {a["name"] for a in DEMO_AGENTS}
        print(f"Skipping {', '.join(sorted(already))} (already exist).")

    print(f"Creating {len(to_create)} demo agent(s) in {args.region}...")
    print("Each deploy takes 3-4 minutes. Starting them all in parallel.\n")

    created = []
    for agent in to_create:
        result = api(args.host, "POST", "/api/agents", {
            "name": agent["name"],
            "platform": "aws",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "description": agent["description"],
            "agent_instructions": agent["agent_instructions"],
            "tools": agent["tools"],
            "mcp_servers": [],
            "deployment_mode": "code",
            "region": args.region,
            "runtime_version": "V2",
        })
        if "id" not in result:
            print(f"  {agent['name']}: create failed — {result}")
            continue
        created.append((result["id"], agent["name"]))
        print(f"  {agent['name']}: deploy started ({result['id'][:8]}...)")

    if not created:
        print("\nNo agents were created.")
        sys.exit(1)

    print()
    ok = 0
    for agent_id, name in created:
        if wait_active(args.host, agent_id, name):
            ok += 1

    print(f"\n{ok}/{len(created)} agents ready.")
    if ok == len(created):
        print("Open http://127.0.0.1:8910 and start chatting.")
    else:
        print("Some agents failed to deploy. Check the portal for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
