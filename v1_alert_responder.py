#!/usr/bin/env python3
"""
V1 Alert Responder
------------------
Takes an alert (fake for now), asks Claude Code to investigate the live
cluster using the Kubernetes + Prometheus MCP tools, and posts the
enriched, human-readable summary to Slack.

Flow:  alert  ->  build prompt  ->  `claude` investigates via MCP tools  ->  Slack

Run:   python3 v1_alert_responder.py
"""

import json
import os
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------------------
# 1. The alert (hardcoded fake for now — shaped like a real Pyrra/Alertmanager
#    ErrorBudgetBurn alert). Later this comes from Alertmanager instead.
# ---------------------------------------------------------------------------
FAKE_ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "ErrorBudgetBurn",
        "severity": "critical",
        "slo": "system-availability-istio",
        "namespace": "monitoring",
    },
    "annotations": {
        "summary": "High error budget burn for system-availability-istio",
        "description": "The system-availability SLO is burning error budget fast.",
    },
}


# ---------------------------------------------------------------------------
# 2. Build the investigation prompt from the alert.
# ---------------------------------------------------------------------------
def build_prompt(alert: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    return f"""You are an on-call SRE assistant. An alert just fired. Investigate the
LIVE cluster using your Kubernetes and Prometheus tools, then write a SHORT
Slack-ready summary.

ALERT:
- name: {labels.get('alertname')}
- severity: {labels.get('severity')}
- SLO: {labels.get('slo')}
- namespace: {labels.get('namespace')}
- description: {annotations.get('description')}

Do this:
1. Check pod health in the relevant namespaces.
2. Query Prometheus for anything relevant to this alert.
3. State the most likely cause in plain English.
4. Suggest ONE next step a human could take.

Keep it under 120 words. Do not invent data — only report what the tools return.
If the data needed isn't present in this cluster, say so honestly."""


# ---------------------------------------------------------------------------
# 3. Ask Claude Code to investigate (uses the `claude` CLI already working,
#    with the MCP tools you already registered).
# ---------------------------------------------------------------------------
def investigate(prompt: str) -> str:
    # `claude -p` runs a one-shot prompt and prints the result.
    result = subprocess.run(
        ["claude", "-p", prompt, "--allowedTools", "mcp__kubernetes__*", "mcp__prometheus__*",],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        return f"[investigation failed]\n{result.stderr.strip()}"
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 4. Post to Slack. Webhook is read from a LOCAL file, never hardcoded.
# ---------------------------------------------------------------------------
def load_webhook() -> str | None:
    path = os.path.join(os.path.dirname(__file__), "slack_webhook.txt")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read().strip()


def post_to_slack(text: str, webhook: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            print(f"Slack returned status {resp.status}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 5. Wire it together.
# ---------------------------------------------------------------------------
def main() -> None:
    alert = FAKE_ALERT
    print(f"[*] Alert received: {alert['labels']['alertname']} "
          f"({alert['labels']['severity']})")

    print("[*] Asking Claude to investigate the live cluster...")
    prompt = build_prompt(alert)
    summary = investigate(prompt)

    message = (
        f":rotating_light: *AI Triage — {alert['labels']['alertname']}* "
        f"({alert['labels']['severity']})\n\n{summary}"
    )

    print("\n----- AI SUMMARY -----\n")
    print(summary)
    print("\n----------------------\n")

    webhook = load_webhook()
    if webhook:
        print("[*] Posting to Slack...")
        post_to_slack(message, webhook)
        print("[*] Posted to Slack.")
    else:
        print("[!] No slack_webhook.txt found — skipping Slack post.")
        print("    (Create slack_webhook.txt with your webhook URL to enable it.)")


if __name__ == "__main__":
    main()
