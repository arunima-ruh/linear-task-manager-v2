---
name: task-digest-builder
version: 2.0.0
description: "Queries scored tasks from DB, sorts by due date then criticality, formats top-10 digest, writes summary to result_daily_digests."
user-invocable: false
metadata:
  openclaw:
    requires:
      bins: [python3]
      env: [PG_CONNECTION_STRING, ORG_ID, AGENT_ID, RUN_ID]
---

# Task Digest Builder

Builds the final prioritized task list for Telegram delivery.

## Usage

### Step 1: Query Scored Tasks from Database

```bash
# Resolve env: use shell env if set, otherwise read from openclaw.json directly
OC=/root/.openclaw/openclaw.json
export RUN_ID="${RUN_ID:-$(jq -r '.env.RUN_ID // empty' $OC 2>/dev/null)}"
export SUPABASE_URL="${SUPABASE_URL:-$(jq -r '.env.SUPABASE_URL // empty' $OC 2>/dev/null)}"
export SUPABASE_KEY="${SUPABASE_KEY:-$(jq -r '.env.SUPABASE_KEY // empty' $OC 2>/dev/null)}"
export PG_CONNECTION_STRING="${PG_CONNECTION_STRING:-$(jq -r '.env.PG_CONNECTION_STRING // empty' $OC 2>/dev/null)}"
export ORG_ID="${ORG_ID:-$(jq -r '.env.ORG_ID // empty' $OC 2>/dev/null)}"
export AGENT_ID="${AGENT_ID:-$(jq -r '.env.AGENT_ID // empty' $OC 2>/dev/null)}"

python3 scripts/data_writer.py query \
  --table result_task_scores \
  --where '{"run_id": "'${RUN_ID}'"}' \
  --order-by "criticality_score DESC" \
  --limit 100 \
  > /tmp/scored_query_${RUN_ID}.json
```

### Step 2: Build and Format Digest

```bash
python3 -c '
import json, os, subprocess
from datetime import datetime, timezone

RUN_ID = os.environ["RUN_ID"]

with open(f"/tmp/scored_query_{RUN_ID}.json") as f:
    data = json.load(f)
    tasks = data.get("records", [])

# Sort by due date (asc), then criticality (desc)
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

# Count overdue
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

# Format digest
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

    lines.append(f"{i}. {label} {task.get(\"title\", \"Untitled\")}")
    lines.append(f"   📁 {project} | 📅 {due_display} | Score: {crit:.0f}/100")
    lines.append("")

if not tasks:
    lines.append("✅ No pending tasks found. Enjoy your day!")

digest = "\n".join(lines)

with open(f"/tmp/digest_{RUN_ID}.txt", "w") as f:
    f.write(digest)

# Calculate stats for result_daily_digests
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
```

### Step 3: Write Digest Summary to Database

```bash
# Resolve env for data_writer.py
OC=/root/.openclaw/openclaw.json
export SUPABASE_URL="${SUPABASE_URL:-$(jq -r '.env.SUPABASE_URL // empty' $OC 2>/dev/null)}"
export SUPABASE_KEY="${SUPABASE_KEY:-$(jq -r '.env.SUPABASE_KEY // empty' $OC 2>/dev/null)}"
export PG_CONNECTION_STRING="${PG_CONNECTION_STRING:-$(jq -r '.env.PG_CONNECTION_STRING // empty' $OC 2>/dev/null)}"
export ORG_ID="${ORG_ID:-$(jq -r '.env.ORG_ID // empty' $OC 2>/dev/null)}"
export AGENT_ID="${AGENT_ID:-$(jq -r '.env.AGENT_ID // empty' $OC 2>/dev/null)}"

python3 scripts/data_writer.py write \
  --table result_daily_digests \
  --records "$(cat /tmp/digest_record_${RUN_ID}.json)" \
  --conflict none \
  --run-id "${RUN_ID}"
```

## Expected Output

Markdown-formatted digest in `/tmp/digest_${RUN_ID}.txt`:

```
📋 Your Top 10 Linear Tasks (2024-03-18)

1. 🔴 CRITICAL Fix authentication bug in prod
   📁 Backend | 📅 2024-03-18 | Score: 95/100

2. 🟠 HIGH Implement rate limiting
   📁 Backend | 📅 2024-03-19 | Score: 72/100
```

## Error Handling

- If no scored tasks found → outputs "No pending tasks" message
- If database query fails → check PG_CONNECTION_STRING
