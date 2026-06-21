# retail-store-ai

This is an AI agent that helps with on-call for my retail-store platform (EKS, Istio, Prometheus, Pyrra SLOs). When an alert fires, it goes and looks at the live cluster, works out what's probably wrong, and posts a writeup to Slack. It never changes anything itself. It tells you what it found and what it'd run, and you make the call.

> 📸 You can see it working in [chapter 5 of the platform walkthrough](https://github.com/erysimum/retail-store-gitops/tree/main/docs/walkthrough/05-ai-agent): the subagents firing off in parallel, then the Slack card with the root cause, the evidence, and the remediation commands to fix it.

## What it does

When an alert comes in (a normal Alertmanager payload to `POST /alert`), the agent kicks off two investigators run at the same time in parallel. Each one is its own `claude -p` process that's only allowed to touch one thing:

- a metrics agent that can only query Prometheus
- a kubernetes agent that can only query Kubernetes
- plus a quick git-log check on the side

Once they're done, a third step with no tools at all pulls their findings together into clean JSON (I validate it with Pydantic), and turns that into a Slack card: what broke, who's affected, the evidence from each source, and commands you can copy and paste.

## Why it's built this way

- **Read-only, human in the loop.** No part of it can write to the cluster or to Git. It looks and suggests, that's it.
- **Least privilege.** Each `claude -p` call is scoped with `--allowedTools` to exactly the one tool its job needs, nothing more.
- **Actually parallel.** The investigators run as separate OS processes (`asyncio.create_subprocess_exec`, awaited together), not async pretending to be parallel.
- **Structured output.** The compiler returns JSON checked against a Pydantic model, with a fallback parser if the output comes back malformed.
- **Guardrails.** Timeouts on each investigator, a dedup window so repeat alerts don't pile up, and a guarded Slack post.

## What happened when I tested it on EKS

## What happened when I tested it on EKS

- On a cluster with no relevant data, the metrics agent correctly said the data wasn't there instead of making numbers up.
- When I pointed it at a system that had already recovered (after I deleted the fault injection), it correctly spotted that the alert was stale instead of calling it a live incident. A relief for the on-call engineer.

Short version: it reasons well from what it can see. But when I tested it against the cluster after injecting a 3% fault on catalog, it blamed a catalog pod restart, which was wrong. Catalog never actually sees the 500s (Istio aborts the requests before they reach the pod), and the agent has no way of knowing a human injected the fault. It only reasons from the evidence in front of it, so it filled the gap with the most plausible thing it could see. 

## Files

- `main.py` — the FastAPI agent (orchestrator, subagents, compiler, Slack renderer)
- `sample_alert.json` — an Alertmanager-shaped payload for testing
- `requirements.txt` — Python dependencies
- `v1_alert_responder.py`, `v1_alert_server.py` — earlier single-shot versions

## Running it locally

You'll need a cluster reachable by `kubectl`, Prometheus on `localhost:9090`, and the Claude Code CLI with the kubernetes and prometheus MCP servers registered.

    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    echo "https://hooks.slack.com/services/..." > slack_webhook.txt   # never commit this
    python main.py
    # in another terminal:
    curl -X POST http://localhost:8000/alert -H "Content-Type: application/json" -d @sample_alert.json

If there's no webhook set up, it just prints the card to your terminal instead of posting to Slack.
