---
name: linear-to-ingestion-wrapper
version: 2.0.0
description: "Fetches assigned Linear tasks via linear-cli and saves to temp file for processing"
user-invocable: false
metadata:
  openclaw:
    requires:
      bins: [npx, python3]
      env: [LINEAR_API_KEY, RUN_ID]
    primaryEnv: LINEAR_API_KEY
---

# Linear Task Fetcher

Fetches all assigned Linear tasks (excluding "Done") and saves them as JSON for downstream skills.

## Usage

### Step 1: Fetch Tasks via linear-cli

```bash
# Resolve env: use shell env if set, otherwise pull from openclaw config
export LINEAR_API_KEY="${LINEAR_API_KEY:-$(openclaw config get env.LINEAR_API_KEY 2>/dev/null)}"
export LINEAR_TOKEN="${LINEAR_TOKEN:-$(openclaw config get env.LINEAR_TOKEN 2>/dev/null)}"
export RUN_ID="${RUN_ID:-$(openclaw config get env.RUN_ID 2>/dev/null)}"
export RUN_ID="${RUN_ID:-$(uuidgen 2>/dev/null || date +%s)}"

npx linear-cli issues list \
  --assignee me \
  --status "!Done" \
  --format json \
  > /tmp/linear_raw_${RUN_ID}.json 2>/dev/null

echo "Fetched $(jq '. | length' /tmp/linear_raw_${RUN_ID}.json) tasks"
```

### Step 2: Transform to Standard Format

```bash
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
```

## Expected Output

`/tmp/tasks_${RUN_ID}.json` containing:

```json
[
  {
    "task_id": "ENG-123",
    "title": "Fix auth timeout bug",
    "description": "Users report session expiring...",
    "status": "In Progress",
    "priority": "P1",
    "due_date": "2024-03-28",
    "estimate_hours": 3,
    "project_name": "Backend",
    "labels": ["bug", "auth"],
    "url": "https://linear.app/team/issue/ENG-123"
  }
]
```

## Error Handling

- If linear-cli is not installed → `npx` will auto-install it
- If LINEAR_API_KEY is invalid → linear-cli exits with auth error
- If no tasks found → saves empty array `[]` (not an error)
