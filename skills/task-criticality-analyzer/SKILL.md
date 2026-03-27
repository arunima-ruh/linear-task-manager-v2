---
name: task-criticality-analyzer
version: 2.0.0
description: "Analyzes Linear task impact and complexity. Writes criticality scores to result_task_scores via data_writer.py."
user-invocable: false
metadata:
  openclaw:
    requires:
      bins: [python3]
      env: [PG_CONNECTION_STRING, ORG_ID, AGENT_ID, RUN_ID]
---

# Task Criticality Analyzer

Scores tasks on impact (urgency keywords + priority + due date) and complexity (subtasks + estimates).

## Usage

### Step 1: Calculate Criticality Scores

```bash
# Resolve env: use shell env if set, otherwise pull from openclaw config
export RUN_ID="${RUN_ID:-$(openclaw config get env.RUN_ID 2>/dev/null)}"
export RUN_ID="${RUN_ID:-$(uuidgen 2>/dev/null || date +%s)}"
export SUPABASE_URL="${SUPABASE_URL:-$(openclaw config get env.SUPABASE_URL 2>/dev/null)}"
export SUPABASE_KEY="${SUPABASE_KEY:-$(openclaw config get env.SUPABASE_KEY 2>/dev/null)}"
export PG_CONNECTION_STRING="${PG_CONNECTION_STRING:-$(openclaw config get env.PG_CONNECTION_STRING 2>/dev/null)}"
export ORG_ID="${ORG_ID:-$(openclaw config get env.ORG_ID 2>/dev/null)}"
export AGENT_ID="${AGENT_ID:-$(openclaw config get env.AGENT_ID 2>/dev/null)}"

python3 -c '
import json, os
from datetime import datetime, timezone

RUN_ID = os.environ["RUN_ID"]

with open(f"/tmp/tasks_{RUN_ID}.json") as f:
    tasks = json.load(f)

records = []

for task in tasks:
    # Impact score (0-50)
    impact = 0
    text = f"{task.get(\"title\",\"\")} {task.get(\"description\",\"\")}".lower()
    if any(kw in text for kw in ["urgent","immediate","critical","blocker","asap"]):
        impact += 20

    priority_scores = {"P1":30,"P2":20,"P3":10,"P4":5,"None":0}
    impact += priority_scores.get(task.get("priority","None"), 0)

    # Due date proximity
    due_date_str = task.get("due_date")
    if due_date_str:
        try:
            due_date = datetime.fromisoformat(due_date_str.replace("Z",""))
            days_until = (due_date - datetime.now()).days
            if days_until < 0:
                impact += 25  # Overdue
            elif days_until == 0:
                impact += 15  # Due today
            elif days_until <= 7:
                impact += 10  # Due this week
        except:
            pass

    # Complexity score (0-50)
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
```

### Step 2: Write Scores to Database

```bash
# Resolve env for data_writer.py (uses SUPABASE_URL/KEY or PG_CONNECTION_STRING)
export SUPABASE_URL="${SUPABASE_URL:-$(openclaw config get env.SUPABASE_URL 2>/dev/null)}"
export SUPABASE_KEY="${SUPABASE_KEY:-$(openclaw config get env.SUPABASE_KEY 2>/dev/null)}"
export PG_CONNECTION_STRING="${PG_CONNECTION_STRING:-$(openclaw config get env.PG_CONNECTION_STRING 2>/dev/null)}"
export ORG_ID="${ORG_ID:-$(openclaw config get env.ORG_ID 2>/dev/null)}"
export AGENT_ID="${AGENT_ID:-$(openclaw config get env.AGENT_ID 2>/dev/null)}"

python3 scripts/data_writer.py write \
  --table result_task_scores \
  --records "$(cat /tmp/scored_tasks_${RUN_ID}.json)" \
  --conflict task_id,run_id \
  --run-id "${RUN_ID}"
```

## Scoring Logic

**Impact (0-50):**
- Urgent keywords (+20)
- Priority P1 (+30), P2 (+20), P3 (+10), P4 (+5)
- Due date: Overdue (+25), Today (+15), This week (+10)

**Complexity (0-50):**
- Estimate: None (+10), 1-3h (+15), 4-8h (+25), 9+h (+40)
- Description length: Short (+5), Medium (+15), Long (+30)

## Error Handling

- If tasks file is empty → writes empty array (not an error)
- If data_writer.py fails → check PG_CONNECTION_STRING
