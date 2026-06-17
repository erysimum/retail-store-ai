# retail-store-ai

An advisory AI SRE agent for the retail-store platform
(EKS + Istio + Prometheus + Pyrra SLOs). When an alert fires, the agent investigates
the live cluster and posts a diagnosis to Slack. It is read-only and advisory — it
suggests commands; a human runs them.

## What it does

1. Receives an Alertmanager-shaped alert via POST /alert.
2. Runs two investigation subagents in parallel, each a separate claude -p
   process scoped to one tool:
   - a metrics subagent (Prometheus MCP only)
   - a kubernetes subagent (Kubernetes MCP only)
   - plus a lightweight local git log check
3. A compiler step (no tools) synthesizes the findings into structured JSON,
   validated against a Pydantic schema.
4. Posts a Slack Block Kit card: root cause, impact, per-source evidence, and
   copy-paste remediation commands.

## Design decisions

- Read-only / human-in-the-loop. No subagent can write to the cluster or Git.
- Least privilege: each claude -p call is scoped with --allowedTools to exactly
  the one MCP tool its job needs.
- Real parallelism: subagents run as separate OS processes via
  asyncio.create_subprocess_exec, awaited together with asyncio.gather.
- Structured output: the compiler returns JSON validated by a Pydantic model,
  with a parser fallback for malformed output.
- Guardrails: per-subagent timeouts, a dedup window, and a guarded Slack POST.

## What I observed testing it on EKS

- With no relevant data (local kind cluster), the metrics subagent correctly
  reported the data was absent instead of inventing numbers.
- During a live breach, the metrics analysis was accurate, but the root cause
  sometimes attributed the failure to a coincidental recent pod restart rather
  than the injected fault — the agent has no way to know a human injected it.
- Fired at a system that had already recovered, the agent correctly identified
  that the alert was stale rather than reporting an active incident.

The agent reasons reasonably from what it can observe, but cannot know what is
not in the cluster — which is why it stays advisory, with a human in the loop.

## Files

- main.py — the FastAPI agent (orchestrator, subagents, compiler, Slack renderer)
- sample_alert.json — an Alertmanager-shaped payload for testing
- requirements.txt — Python dependencies
- v1_alert_responder.py, v1_alert_server.py — earlier single-shot versions

## Running locally

Requires a cluster reachable by kubectl, Prometheus at localhost:9090, and the
Claude Code CLI with kubernetes and prometheus MCP servers registered.

    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    echo "https://hooks.slack.com/services/..." > slack_webhook.txt   # never commit this
    python main.py
    # in another terminal:
    curl -X POST http://localhost:8000/alert -H "Content-Type: application/json" -d @sample_alert.json

If no webhook is configured, the agent prints the card to stdout instead of posting.


