# Backlog

Known gaps worth fixing, with the evidence that justifies each one. Not a
wishlist: everything here has already cost something real.

## Make Edit -> Recreate survive a failed create

`Recreate` is how you change anything a deployed agent can't be updated in
place with -- its model, its instructions, its region, its deployment mode.
Today it is a client-side delete-then-create: `static/app.js`'s
`waitForDeleted()` polls `DELETE /api/agents/{id}` until the row is gone, and
only then fires `POST /api/agents`. There is no rollback, and the definition
lives nowhere but the row that was just deleted.

So a create that fails after the delete succeeded doesn't leave the old agent
in place, or a failed row explaining itself. It leaves nothing at all: the
platform resource is gone, the portal row is gone, and the model,
instructions, tool selection, and deployment mode are gone with it.

That is not hypothetical. Recreating `v1-container-based` from us-west-2 into
us-east-1 deleted `v1_container_based-fkJA7Y6Y5R`, failed the create, and lost
the agent outright. It was only recoverable because the deleted row still
happened to sit in a freed page of `agent_portal.db` and SQLite hadn't reused
it yet -- `strings agent_portal.db | grep container` is genuinely how the
model and system prompt came back. That is luck, not a recovery path.

Either ordering fixes it:

- **Create first, delete after.** Strictly better when the platform allows two
  agents to coexist, since the old one keeps serving until the new one is
  READY. The blocker is name collision: AgentCore derives the runtime name
  from the agent name, so this needs a distinct temporary name, or the rename
  step it already avoids. Note `_wait_until_actually_gone` in `server.py`
  exists precisely because the current ordering can collide on the freed name
  -- flipping the order retires that concern rather than adding to it.
- **Persist the definition before deleting.** Cheaper, and enough on its own:
  keep the row (a `recreating` status, or a saved copy) so a failed create
  ends at a row that says what it was trying to be and offers a retry,
  instead of at nothing.

Worth checking whether Recreate belongs on the server at all. As a single
endpoint it could own the whole sequence transactionally; as two client calls,
any interruption between them -- a failed create, a closed tab, a dropped
connection -- loses the agent the same way.

## Log background deploy failures somewhere durable

`_run_deploy` records a failure on the agent's row via
`db.set_status(..., STATUS_FAILED, error_message=...)`, which is the right
thing and is what the UI shows. But when the row itself is gone -- the
recreate case above -- the error goes with it, and `run.sh` doesn't redirect
the portal's output anywhere, so the traceback only ever existed in whatever
terminal launched it. (`logs/portal.log` is from a manual redirect in an
earlier session, not something `run.sh` maintains.)

After the incident above, there was no way to answer "why did that create
fail". Appending deploy exceptions to a file under `logs/` would have answered
it immediately, and costs a few lines.

## Azure's unpinned yfinance goes stale on a long-lived agent

`azure_hosted/requirements.txt` lists `yfinance` with no version pin, so a
code-based Azure deploy's server-side pip install grabs whatever's latest at
that moment and bakes it into the container. Yahoo Finance changes its
API/scraping surface often enough that yfinance ships frequent patch releases
just to keep working against it -- so a container built once and left running
can go stale on its own, with nothing in this repo having changed at all.

Not hypothetical either: `stock-analysis-agent`, deployed 11 days earlier and
never redeployed since, started failing `get_stock_price`/`get_price_history`
on every call -- the model reported it couldn't retrieve a price rather than
erroring outright, so nothing surfaced as a portal bug. The same `tools.py`
function called directly, in the same checkout, worked fine (local `.venv`
has yfinance 1.7.0). Confirmed via dependency comparison, not the trace panel
-- `get_trace` for this exact turn returned a stale, mismatched trace from an
unrelated earlier conversation (Azure's known indexing-lag issue), so it was
useless for this diagnosis. Fixed by a plain Recreate, which rebuilds the
container and picks up current yfinance -- verified live afterward.

Left unpinned deliberately for now rather than fixed here: pinning trades this
failure mode for a different one (staying pinned to a version that's already
broken against Yahoo's current API, indefinitely, until someone notices and
bumps it by hand). Floating is probably still the right default for a
scraping library chasing a moving target, but it means any long-lived Azure
agent using `stock_data` can silently go stale between redeploys, with no
alert -- worth either a periodic redeploy habit, or a scheduled health check
that actually calls the tool and expects real data back, not just an active
container.
