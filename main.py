#!/usr/bin/env python3
"""
Advisory AI SRE Agent — production-grade version

Pipeline:
Alertmanager → /alert → subagents (parallel)
→ compiler agent (LLM JSON)
→ Pydantic validation
→ Slack renderer

Patched: compiler now SEES the schema; subagents have real read-only
instructions; subprocess launch + Slack POST are guarded; service label
falls back to slo/job (Pyrra ErrorBudgetBurn alerts use `slo`, not `service`).
Architecture (Pydantic contract, parser ladder, pure renderer) unchanged.
"""

import asyncio
import json
import logging
import os
import pathlib
import re
import time
from typing import List

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

HERE = pathlib.Path(__file__).resolve().parent

SUBAGENT_TIMEOUT_SECONDS = 120
COMPILER_TIMEOUT_SECONDS = 90
DEDUPE_WINDOW_SECONDS = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("advisory-sre-agent")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def resolve_service(labels: dict) -> str:
    """Pyrra ErrorBudgetBurn / SLOMetricAbsent alerts label with `slo`, not
    `service`. Fall back through the likely identifying labels so the agent
    always knows what it's investigating instead of logging 'unknown'."""
    return (
        labels.get("service")
        or labels.get("slo")
        or labels.get("job")
        or "unknown"
    )


# --------------------------------------------------------------------------- #
# Models (STRICT CONTRACT)
# --------------------------------------------------------------------------- #

class Evidence(BaseModel):
    metrics: str = "unavailable"
    k8s: str = "unavailable"
    git: str = "unavailable"


class CompilerOutput(BaseModel):
    root_cause: str = "unknown"
    impact: str = "unknown"
    evidence: Evidence = Field(default_factory=Evidence)
    remediation_commands: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent instructions
# --------------------------------------------------------------------------- #

METRICS_SYSTEM = (
    "You are a read-only metrics SRE subagent. Use Prometheus MCP tools "
    "(mcp__prometheus__*) ONLY. Query error rate, latency, and traffic relevant "
    "to the alert. Report concrete values. If the relevant series (e.g. Istio/SLO "
    "metrics) are not present in this cluster, say so plainly instead of inventing "
    "numbers. Be concise (under 200 words). Never suggest write actions."
)

K8S_SYSTEM = (
    "You are a read-only Kubernetes SRE subagent. Use Kubernetes MCP tools "
    "(mcp__kubernetes__*) ONLY. Inspect pods, deployments, recent events, and "
    "restart counts relevant to the alert. Report pod health and the most likely "
    "cluster-level cause. Only claim a root cause you have direct evidence for; if "
    "the cluster looks healthy and you cannot find a failing pod, say so plainly "
    "rather than inferring a failure mode. Be concise (under 200 words). Never "
    "modify any resource."
)

# The compiler is shown the EXACT schema it must emit. Without this it guesses
# the shape, Pydantic validation fails, and the card shows UNPARSEABLE_OUTPUT.
COMPILER_SYSTEM = (
    "You are an SRE incident compiler. Three subagents investigated an alert in "
    "parallel. Synthesize their findings into one diagnosis. You have NO tools.\n"
    "Base the root cause ONLY on evidence the subagents actually reported. If the "
    "metrics show errors but the k8s agent found no failing pods, say the cause is "
    "not yet confirmed from cluster state rather than inventing a specific failure "
    "mode. Do not assert specific component failures (e.g. cert sync, mount errors) "
    "unless a subagent explicitly reported them.\n"
    "Return ONE valid JSON object and NOTHING else — no prose before or after, no "
    "markdown code fences. Use exactly this schema and these keys:\n"
    "{\n"
    '  "root_cause": "1-2 sentence root cause, evidence-based",\n'
    '  "impact": "who or what is affected",\n'
    '  "evidence": {\n'
    '    "metrics": "what the metrics agent found",\n'
    '    "k8s": "what the kubernetes agent found",\n'
    '    "git": "what the git agent found"\n'
    "  },\n"
    '  "remediation_commands": ["kubectl ...", "helm ..."]\n'
    "}\n"
    "remediation_commands MUST be a JSON array of strings, each a copy-paste "
    "diagnostic or fix command the human runs manually (read-only advisory; "
    "never auto-applied). Prefer read-only diagnostics first. If a section is "
    "unknown, use a short placeholder string."
)


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #

seen_alerts = {}


def should_process_alert(alert_name: str, service: str, severity: str) -> bool:
    key = (alert_name, service, severity)
    now = time.time()
    last = seen_alerts.get(key)

    if last and (now - last) < DEDUPE_WINDOW_SECONDS:
        log.info("Duplicate alert skipped: %s %s", alert_name, service)
        return False

    seen_alerts[key] = now
    return True


# --------------------------------------------------------------------------- #
# Slack webhook
# --------------------------------------------------------------------------- #

def load_slack_webhook() -> str | None:
    env = os.environ.get("SLACK_WEBHOOK")
    if env and env.strip():
        return env.strip()

    f = HERE / "slack_webhook.txt"
    if f.exists():
        text = f.read_text().strip()
        if text:
            return text

    return None


async def post_to_slack(blocks: dict) -> bool:
    webhook = load_slack_webhook()

    if not webhook:
        log.warning("Slack webhook missing. Printing card instead.")
        print(json.dumps(blocks, indent=2))
        return False

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook, json=blocks)
    except Exception as e:  # noqa: BLE001 - never 500 the handler on a Slack hiccup
        log.error("Slack POST failed: %s", e)
        return False

    if resp.status_code != 200:
        log.error("Slack error: %s %s", resp.status_code, resp.text)
        return False

    return True


# --------------------------------------------------------------------------- #
# Claude subagent runner
# --------------------------------------------------------------------------- #

async def run_claude_subagent(
    prompt: str,
    system_instruction: str,
    allowed_tools: str | None,
    timeout: int,
    label: str,
) -> str:

    composed_prompt = f"{system_instruction}\n\n{prompt}"

    cmd = ["claude", "-p", composed_prompt]

    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]

    log.info("Launching %s agent%s", label, f" (tools: {allowed_tools})" if allowed_tools else " (no tools)")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("`claude` CLI not found on PATH for %s agent", label)
        return f"{label}: ERROR claude CLI not found on PATH"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"{label}: TIMEOUT"

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="ignore")
        return f"{label}: ERROR {err[:200]}"

    return (stdout or b"").decode(errors="ignore").strip()


# --------------------------------------------------------------------------- #
# Git analysis
# --------------------------------------------------------------------------- #

async def run_git_findings() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "-n",
            "5",
            "--oneline",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(HERE),
        )

        stdout, _ = await proc.communicate()
        out = stdout.decode(errors="ignore").strip()
        return out if out else "No git history at this path (expected on kind sandbox)."

    except Exception:
        return "No git data available"


# --------------------------------------------------------------------------- #
# Alert summarization
# --------------------------------------------------------------------------- #

def summarize_alert(alert: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    return (
        f"Alert: {labels.get('alertname')}\n"
        f"Severity: {labels.get('severity')}\n"
        f"Service: {resolve_service(labels)}\n"
        f"Summary: {annotations.get('summary', 'none')}"
    )


# --------------------------------------------------------------------------- #
# COMPILER PARSER (ONLY BOUNDARY)
# --------------------------------------------------------------------------- #

def parse_compiler_output(text: str) -> CompilerOutput:
    cleaned = text.strip()

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
        return CompilerOutput.model_validate(data)
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return CompilerOutput.model_validate(json.loads(match.group(0)))
        except Exception:
            pass

    return CompilerOutput(
        root_cause="UNPARSEABLE_OUTPUT",
        impact="unknown",
        evidence=Evidence(
            metrics="parse failure",
            k8s="parse failure",
            git="parse failure",
        ),
        remediation_commands=[],
    )


async def run_compiler_agent(prompt: str) -> CompilerOutput:
    raw = await run_claude_subagent(
        prompt=prompt,
        system_instruction=COMPILER_SYSTEM,
        allowed_tools=None,
        timeout=COMPILER_TIMEOUT_SECONDS,
        label="compiler",
    )

    return parse_compiler_output(raw)


# --------------------------------------------------------------------------- #
# Slack builder (PURE RENDERER)
# --------------------------------------------------------------------------- #

def build_slack_blocks(
    alert_name: str,
    severity: str,
    compiler: CompilerOutput,
) -> dict:

    emoji = {
        "critical": "🔴",
        "warning": "🟡",
        "info": "🔵",
    }.get(severity.lower(), "⚪")

    return {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} AI SRE: {alert_name}",
                },
            },
            {"type": "divider"},

            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause:*\n{compiler.root_cause}",
                },
            },

            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Impact:*\n{compiler.impact}",
                },
            },

            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Metrics:*\n{compiler.evidence.metrics}"},
                    {"type": "mrkdwn", "text": f"*K8s:*\n{compiler.evidence.k8s}"},
                    {"type": "mrkdwn", "text": f"*Git:*\n{compiler.evidence.git}"},
                ],
            },

            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Remediation:*",
                },
            },

            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(f"`{c}`" for c in compiler.remediation_commands)
                    if compiler.remediation_commands
                    else "_No remediation_",
                },
            },
        ]
    }


# --------------------------------------------------------------------------- #
# FASTAPI APP
# --------------------------------------------------------------------------- #

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/alert")
async def handle_alert(request: Request):

    payload = await request.json()
    alerts = payload.get("alerts", [])

    if not alerts:
        raise HTTPException(400, "No alerts")

    alert = alerts[0]

    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "unknown")
    severity = labels.get("severity", "unknown")
    service = resolve_service(labels)

    if not should_process_alert(alert_name, service, severity):
        return {"status": "skipped"}

    alert_text = summarize_alert(alert)

    log.info("Investigating %s (%s) for service %s", alert_name, severity, service)

    metrics_task = run_claude_subagent(
        alert_text,
        METRICS_SYSTEM,
        "mcp__prometheus__*",
        SUBAGENT_TIMEOUT_SECONDS,
        "metrics",
    )

    k8s_task = run_claude_subagent(
        alert_text,
        K8S_SYSTEM,
        "mcp__kubernetes__*",
        SUBAGENT_TIMEOUT_SECONDS,
        "k8s",
    )

    git_task = run_git_findings()

    metrics, k8s, git = await asyncio.gather(
        metrics_task,
        k8s_task,
        git_task,
    )

    log.info("Subagents done — compiling synthesis.")

    compiler_prompt = f"""
{alert_text}

METRICS:
{metrics}

K8S:
{k8s}

GIT:
{git}
"""

    compiler = await run_compiler_agent(compiler_prompt)

    blocks = build_slack_blocks(alert_name, severity, compiler)

    await post_to_slack(blocks)

    return {
        "status": "ok",
        "alert": alert_name,
        "service": service,
        "severity": severity,
    }


# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
