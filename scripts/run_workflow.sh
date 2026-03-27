#!/bin/bash
# Daily Linear Task Digest Workflow — single executable script
# Runs all 4 steps: fetch → analyze → digest → send
set -euo pipefail

# ── Resolve env vars from openclaw.json if not already in shell ──
OC=/root/.openclaw/openclaw.json
export LINEAR_API_KEY="${LINEAR_API_KEY:-$(jq -r '.env.LINEAR_API_KEY // empty' $OC 2>/dev/null)}"
export SUPABASE_URL="${SUPABASE_URL:-$(jq -r '.env.SUPABASE_URL // empty' $OC 2>/dev/null)}"
export SUPABASE_KEY="${SUPABASE_KEY:-$(jq -r '.env.SUPABASE_KEY // empty' $OC 2>/dev/null)}"
export PG_CONNECTION_STRING="${PG_CONNECTION_STRING:-$(jq -r '.env.PG_CONNECTION_STRING // empty' $OC 2>/dev/null)}"
export ORG_ID="${ORG_ID:-$(jq -r '.env.ORG_ID // empty' $OC 2>/dev/null)}"
export AGENT_ID="${AGENT_ID:-$(jq -r '.env.AGENT_ID // empty' $OC 2>/dev/null)}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(jq -r '.env.TELEGRAM_CHAT_ID // empty' $OC 2>/dev/null)}"
export RUN_ID="${RUN_ID:-$(uuidgen 2>/dev/null || date +%s)}"

AGENT_DIR=/root/agents/linear-task-manager-v2

echo "=== Daily Linear Task Digest Workflow ==="
echo "RUN_ID: ${RUN_ID}"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── Step 0: Provision database tables ──
echo ""
echo "--- Step 0: Provision database ---"
cd "$AGENT_DIR"
python3 scripts/data_writer.py provision 2>&1

# ── Step 1: Fetch Linear tasks via GraphQL API ──
echo ""
echo "--- Step 1: Fetch Linear tasks ---"
if [ -z "$LINEAR_API_KEY" ]; then
  echo "ERROR: LINEAR_API_KEY not set" >&2
  exit 1
fi

curl -sf -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: ${LINEAR_API_KEY}" \
  -d '{"query": "{ viewer { assignedIssues(filter: { state: { type: { nin: [\"completed\", \"canceled\"] } } }, first: 100) { nodes { id identifier title description priority dueDate estimate url state { name } project { name } labels { nodes { name } } } } } }"}' \
  | jq '.data.viewer.assignedIssues.nodes' \
  > /tmp/linear_raw_${RUN_ID}.json

TASK_COUNT=$(jq '. | length' /tmp/linear_raw_${RUN_ID}.json)
echo "Fetched ${TASK_COUNT} tasks from Linear"

if [ "$TASK_COUNT" -eq 0 ]; then
  echo "No tasks found. Creating empty digest."
  echo "[]" > /tmp/tasks_${RUN_ID}.json
else
  # Transform to standard format
  python3 -c '
import json, os
from datetime import datetime, timezone

RUN_ID = os.environ["RUN_ID"]

with open(f"/tmp/linear_raw_{RUN_ID}.json") as f:
    raw_tasks = json.load(f)

tasks = []
priority_map = {0: "None", 1: "P1", 2: "P2", 3: "P3", 4: "P4"}

for t in raw_tasks:
    tasks.append({
        "task_id": t.get("identifier", t.get("id", "")),
        "title": t.get("title", ""),
        "description": t.get("description", ""),
        "status": t.get("state", {}).get("name", "Unknown") if isinstance(t.get("state"), dict) else t.get("status", "Unknown"),
        "priority": priority_map.get(t.get("priority", 0), "None"),
        "due_date": t.get("dueDate", ""),
        "estimate_hours": t.get("estimate", 0),
        "project_name": t.get("project", {}).get("name", "") if isinstance(t.get("project"), dict) else "",
        "labels": [l.get("name","") for l in t.get("labels", {}).get("nodes", [])] if isinstance(t.get("labels"), dict) else [],
        "url": t.get("url", "")
    })

with open(f"/tmp/tasks_{RUN_ID}.json", "w") as f:
    json.dump(tasks, f, indent=2)

print(json.dumps({"total_tasks": len(tasks), "run_id": RUN_ID}))
'
fi

# ── Step 2: Analyze criticality ──
echo ""
echo "--- Step 2: Analyze criticality ---"
python3 -c '
import json, os
from datetime import datetime, timezone

RUN_ID = os.environ["RUN_ID"]

with open(f"/tmp/tasks_{RUN_ID}.json") as f:
    tasks = json.load(f)

records = []

for task in tasks:
    impact = 0
    title = task.get("title", "")
    desc = task.get("description", "")
    text = f"{title} {desc}".lower()
    if any(kw in text for kw in ["urgent","immediate","critical","blocker","asap"]):
        impact += 20

    priority_scores = {"P1":30,"P2":20,"P3":10,"P4":5,"None":0}
    impact += priority_scores.get(task.get("priority","None"), 0)

    due_date_str = task.get("due_date")
    if due_date_str:
        try:
            due_date = datetime.fromisoformat(due_date_str.replace("Z",""))
            days_until = (due_date - datetime.now()).days
            if days_until < 0:
                impact += 25
            elif days_until == 0:
                impact += 15
            elif days_until <= 7:
                impact += 10
        except:
            pass

    complexity = 0
    estimate = task.get("estimate_hours") or 0
    if estimate == 0:
        complexity += 10
    elif estimate <= 3:
        complexity += 15
    elif estimate <= 8:
        complexity += 25
    else:
        complexity += 40

    desc_len = len(task.get("description",""))
    if desc_len < 100:
        complexity += 5
    elif desc_len < 500:
        complexity += 15
    else:
        complexity += 30

    criticality = min(impact + complexity, 100)

    records.append({
        "task_id": task.get("task_id", ""),
        "title": task.get("title", ""),
        "priority": task.get("priority", "None"),
        "due_date": task.get("due_date", ""),
        "status": task.get("status", ""),
        "project_name": task.get("project_name", ""),
        "impact_score": float(impact),
        "complexity_score": float(complexity),
        "criticality_score": float(criticality),
        "scoring_details": {
            "impact_breakdown": {"urgency_keywords": impact >= 20, "priority": task.get("priority"), "due_date_proximity": due_date_str},
            "complexity_breakdown": {"estimate_hours": estimate, "description_length": desc_len}
        }
    })

with open(f"/tmp/scored_tasks_{RUN_ID}.json", "w") as f:
    json.dump(records, f, indent=2)

print(json.dumps({"total_scored": len(records), "run_id": RUN_ID}))
'

# Write scores to database
cd "$AGENT_DIR"
python3 scripts/data_writer.py write \
  --table result_task_scores \
  --records "$(cat /tmp/scored_tasks_${RUN_ID}.json)" \
  --conflict task_id,run_id \
  --run-id "${RUN_ID}"

# ── Step 3: Build digest ──
echo ""
echo "--- Step 3: Build digest ---"
cd "$AGENT_DIR"
python3 scripts/data_writer.py query \
  --table result_task_scores \
  --where '{"run_id": "'${RUN_ID}'"}' \
  --order-by "criticality_score DESC" \
  --limit 100 \
  > /tmp/scored_query_${RUN_ID}.json

python3 -c '
import json, os
from datetime import datetime, timezone

RUN_ID = os.environ["RUN_ID"]

with open(f"/tmp/scored_query_{RUN_ID}.json") as f:
    data = json.load(f)
    tasks = data.get("records", [])

def sort_key(t):
    due = t.get("due_date", "")
    if due:
        try:
            dt = datetime.fromisoformat(due.replace("Z",""))
            date_key = dt.timestamp()
        except:
            date_key = float("inf")
    else:
        date_key = float("inf")
    return (date_key, -t.get("criticality_score", 0))

tasks.sort(key=sort_key)

overdue_count = 0
for t in tasks:
    due = t.get("due_date", "")
    if due:
        try:
            dt = datetime.fromisoformat(due.replace("Z",""))
            if dt < datetime.now():
                overdue_count += 1
        except:
            pass

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
lines = [f"📋 Your Top 10 Linear Tasks ({today})", ""]

for i, task in enumerate(tasks[:10], 1):
    crit = task.get("criticality_score", 0)
    if crit >= 80:
        label = "🔴 CRITICAL"
    elif crit >= 60:
        label = "🟠 HIGH"
    elif crit >= 40:
        label = "🟡 MEDIUM"
    else:
        label = "🟢 LOW"

    due = task.get("due_date", "")
    due_display = due[:10] if due else "No date"
    project = task.get("project_name") or "No project"

    task_title = task.get("title", "Untitled")
    lines.append(f"{i}. {label} {task_title}")
    lines.append(f"   📁 {project} | 📅 {due_display} | Score: {crit:.0f}/100")
    lines.append("")

if not tasks:
    lines.append("✅ No pending tasks found. Enjoy your day!")

digest = "\n".join(lines)

with open(f"/tmp/digest_{RUN_ID}.txt", "w") as f:
    f.write(digest)

avg_crit = sum(t.get("criticality_score", 0) for t in tasks) / len(tasks) if tasks else 0
top_task = tasks[0] if tasks else {}

digest_record = [{
    "digest_date": today,
    "total_tasks": len(tasks),
    "tasks_in_digest": min(len(tasks), 10),
    "top_task_id": top_task.get("task_id", ""),
    "top_task_score": top_task.get("criticality_score", 0),
    "avg_criticality": round(avg_crit, 2),
    "overdue_count": overdue_count,
    "digest_json": {"tasks": [{"task_id": t.get("task_id"), "title": t.get("title"), "score": t.get("criticality_score")} for t in tasks[:10]]},
    "delivery_status": "pending"
}]

with open(f"/tmp/digest_record_{RUN_ID}.json", "w") as f:
    json.dump(digest_record, f)

print(digest)
'

# Write digest to database
cd "$AGENT_DIR"
python3 scripts/data_writer.py write \
  --table result_daily_digests \
  --records "$(cat /tmp/digest_record_${RUN_ID}.json)" \
  --conflict none \
  --run-id "${RUN_ID}"

# ── Step 4: Send to Telegram ──
echo ""
echo "--- Step 4: Send to Telegram ---"
if [ -n "$TELEGRAM_CHAT_ID" ]; then
  openclaw message send \
    --channel telegram \
    --target "${TELEGRAM_CHAT_ID}" \
    --message "$(cat /tmp/digest_${RUN_ID}.txt)" \
    --json 2>&1 || echo "Telegram send failed, digest available at /tmp/digest_${RUN_ID}.txt"
else
  echo "TELEGRAM_CHAT_ID not set, skipping Telegram delivery"
  cat /tmp/digest_${RUN_ID}.txt
fi

# ── Cleanup ──
echo ""
echo "--- Workflow complete ---"
echo "RUN_ID: ${RUN_ID}"
echo "Digest: /tmp/digest_${RUN_ID}.txt"
rm -f /tmp/linear_raw_${RUN_ID}.json
