#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"


# Keep these in sync with CLAUDE.md (Devlog section) and .claude/skills/doc/SKILL.md (Event types).
ALLOWED_TASK_STATUSES = {"todo", "doing", "done", "blocked", "skipped"}
ALLOWED_DECISION_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
ALLOWED_QUESTION_STATUSES = {"open", "answered", "dropped"}
ALLOWED_DEVLOG_EVENTS = {
    "feature", "bugfix", "refactor", "kb_update", "decision",
    "handoff", "verification", "human_review", "blueprint", "dj_entry",
}
TERMINAL_TASK_STATUSES = {"done", "skipped"}
_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Missing file: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _priority_key(priority: Any) -> int:
    """
    Lower is higher priority.
    Accepts "P0".."P9" or integers.
    Unknown values are treated as lowest priority.
    """
    if isinstance(priority, int):
        return priority
    if isinstance(priority, str):
        text = priority.strip().upper()
        if text.startswith("P") and text[1:].isdigit():
            return int(text[1:])
        if text.isdigit():
            return int(text)
    return 999


@dataclass(frozen=True)
class ValidationIssue:
    level: str  # "error" | "warning"
    message: str


def _expect_type(obj: Any, expected: type, path: str, issues: list[ValidationIssue]) -> bool:
    if isinstance(obj, expected):
        return True
    issues.append(ValidationIssue("error", f"{path}: expected {expected.__name__}"))
    return False


def validate_charter(charter: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not _expect_type(charter, dict, "charter", issues):
        return issues

    for key in ("project", "working_agreement", "assistant_persona"):
        if key not in charter:
            issues.append(ValidationIssue("error", f"charter: missing key `{key}`"))
        elif not isinstance(charter[key], dict):
            issues.append(ValidationIssue("error", f"charter.{key}: expected dict"))

    project = charter.get("project")
    if isinstance(project, dict):
        for key in ("name", "one_liner", "type", "why", "success_criteria", "constraints"):
            if key not in project:
                issues.append(ValidationIssue("error", f"charter.project: missing key `{key}`"))
        constraints = project.get("constraints")
        if constraints is not None and not isinstance(constraints, dict):
            issues.append(ValidationIssue("error", "charter.project.constraints: expected dict"))

        if _is_nonempty_str(project.get("one_liner")) is False:
            issues.append(ValidationIssue("warning", "charter.project.one_liner: empty (planning may be ambiguous)"))
        if _is_nonempty_str(project.get("why")) is False:
            issues.append(ValidationIssue("warning", "charter.project.why: empty (hard to prioritize tradeoffs)"))
        success_criteria = project.get("success_criteria")
        if isinstance(success_criteria, list) and len(success_criteria) == 0:
            issues.append(ValidationIssue("warning", "charter.project.success_criteria: empty (no objective 'done')"))

        tag_taxonomy = project.get("tag_taxonomy")
        if tag_taxonomy is None:
            issues.append(ValidationIssue("warning", "charter.project.tag_taxonomy: missing (tag routing disabled)"))
        elif not isinstance(tag_taxonomy, list):
            issues.append(ValidationIssue("error", "charter.project.tag_taxonomy: expected list"))
        elif len(tag_taxonomy) == 0:
            issues.append(ValidationIssue("warning", "charter.project.tag_taxonomy: empty"))
        else:
            for i, tag in enumerate(tag_taxonomy):
                if not _is_nonempty_str(tag):
                    issues.append(ValidationIssue("error", f"charter.project.tag_taxonomy[{i}]: must be non-empty string"))

    return issues


def validate_devlog(devlog_path: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not devlog_path.exists():
        return issues

    text = devlog_path.read_text(encoding="utf-8")
    if not text.strip():
        return issues

    for line_num, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            issues.append(ValidationIssue("error", f"devlog line {line_num}: invalid JSON"))
            continue
        if not isinstance(entry, dict):
            issues.append(ValidationIssue("error", f"devlog line {line_num}: expected object"))
            continue
        for key in ("ts", "event", "summary"):
            if key not in entry:
                issues.append(ValidationIssue("error", f"devlog line {line_num}: missing `{key}`"))
        ts = entry.get("ts")
        if isinstance(ts, str) and not _ISO8601_RE.match(ts):
            issues.append(ValidationIssue("warning", f"devlog line {line_num}: `ts` is not ISO 8601 format"))
        event = entry.get("event")
        if isinstance(event, str) and event not in ALLOWED_DEVLOG_EVENTS:
            issues.append(ValidationIssue("warning", f"devlog line {line_num}: unknown event `{event}`"))

    return issues


def _validate_question(question: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not _expect_type(question, dict, path, issues):
        return
    for key in ("id", "question", "blocking", "status"):
        if key not in question:
            issues.append(ValidationIssue("error", f"{path}: missing key `{key}`"))
    if "status" in question and question.get("status") not in ALLOWED_QUESTION_STATUSES:
        issues.append(
            ValidationIssue(
                "error",
                f"{path}.status: invalid `{question.get('status')}` (allowed: {sorted(ALLOWED_QUESTION_STATUSES)})",
            )
        )
    if "blocking" in question and not isinstance(question.get("blocking"), list):
        issues.append(ValidationIssue("error", f"{path}.blocking: expected list"))


def _validate_decision(decision: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not _expect_type(decision, dict, path, issues):
        return
    for key in ("id", "summary", "status"):
        if key not in decision:
            issues.append(ValidationIssue("error", f"{path}: missing key `{key}`"))
    if "status" in decision and decision.get("status") not in ALLOWED_DECISION_STATUSES:
        issues.append(
            ValidationIssue(
                "error",
                f"{path}.status: invalid `{decision.get('status')}` (allowed: {sorted(ALLOWED_DECISION_STATUSES)})",
            )
        )


def _validate_task(task: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not _expect_type(task, dict, path, issues):
        return
    required = (
        "id",
        "title",
        "intent",
        "depends_on",
        "priority",
        "status",
        "owner",
        "deliverable",
        "acceptance_criteria",
        "verification",
    )
    for key in required:
        if key not in task:
            issues.append(ValidationIssue("error", f"{path}: missing key `{key}`"))

    if "status" in task and task.get("status") not in ALLOWED_TASK_STATUSES:
        issues.append(
            ValidationIssue(
                "error",
                f"{path}.status: invalid `{task.get('status')}` (allowed: {sorted(ALLOWED_TASK_STATUSES)})",
            )
        )
    if "depends_on" in task and not isinstance(task.get("depends_on"), list):
        issues.append(ValidationIssue("error", f"{path}.depends_on: expected list"))
    if "acceptance_criteria" in task and not isinstance(task.get("acceptance_criteria"), list):
        issues.append(ValidationIssue("error", f"{path}.acceptance_criteria: expected list"))
    if "verification" in task and not isinstance(task.get("verification"), list):
        issues.append(ValidationIssue("error", f"{path}.verification: expected list"))

    if _is_nonempty_str(task.get("deliverable")) is False:
        issues.append(ValidationIssue("error", f"{path}.deliverable: must be a non-empty string"))
    if isinstance(task.get("acceptance_criteria"), list) and len(task.get("acceptance_criteria")) == 0:
        issues.append(ValidationIssue("error", f"{path}.acceptance_criteria: must have at least 1 item"))
    if isinstance(task.get("verification"), list) and len(task.get("verification")) == 0:
        issues.append(ValidationIssue("error", f"{path}.verification: must have at least 1 item"))


def _toposort_tasks(tasks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """
    Returns (sorted_ids, remaining_ids). remaining_ids is non-empty if there is a cycle.
    """
    ids = [t.get("id") for t in tasks]
    id_set = {i for i in ids if isinstance(i, str)}
    depends: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {}
    indegree: dict[str, int] = {task_id: 0 for task_id in id_set}

    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or task_id not in id_set:
            continue
        deps = task.get("depends_on", [])
        if not isinstance(deps, list):
            deps = []
        deps_set = {d for d in deps if isinstance(d, str) and d in id_set and d != task_id}
        depends[task_id] = deps_set
        for dep in deps_set:
            dependents.setdefault(dep, set()).add(task_id)
        indegree[task_id] = len(deps_set)

    ready = [task_id for task_id, deg in indegree.items() if deg == 0]
    ready.sort()
    ordered: list[str] = []

    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for nxt in sorted(dependents.get(node, set())):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()

    remaining = [task_id for task_id, deg in indegree.items() if deg > 0]
    return ordered, remaining


def validate_roadmap(roadmap: Any) -> tuple[list[ValidationIssue], list[dict[str, Any]]]:
    issues: list[ValidationIssue] = []
    if not _expect_type(roadmap, dict, "roadmap", issues):
        return issues, []

    for key in ("meta", "open_questions", "decisions", "tasks"):
        if key not in roadmap:
            issues.append(ValidationIssue("error", f"roadmap: missing key `{key}`"))

    open_questions = roadmap.get("open_questions")
    if isinstance(open_questions, list):
        for idx, question in enumerate(open_questions):
            _validate_question(question, f"roadmap.open_questions[{idx}]", issues)
    elif open_questions is not None:
        issues.append(ValidationIssue("error", "roadmap.open_questions: expected list"))

    decisions = roadmap.get("decisions")
    if isinstance(decisions, list):
        for idx, decision in enumerate(decisions):
            _validate_decision(decision, f"roadmap.decisions[{idx}]", issues)
    elif decisions is not None:
        issues.append(ValidationIssue("error", "roadmap.decisions: expected list"))

    tasks = roadmap.get("tasks")
    if not isinstance(tasks, list):
        if tasks is not None:
            issues.append(ValidationIssue("error", "roadmap.tasks: expected list"))
        return issues, []

    task_by_id: dict[str, dict[str, Any]] = {}
    for idx, task in enumerate(tasks):
        _validate_task(task, f"roadmap.tasks[{idx}]", issues)
        task_id = task.get("id")
        if isinstance(task_id, str):
            if task_id in task_by_id:
                issues.append(ValidationIssue("error", f"roadmap.tasks[{idx}].id: duplicate id `{task_id}`"))
            else:
                task_by_id[task_id] = task

    # Dependency sanity
    for task_id, task in task_by_id.items():
        deps = task.get("depends_on", [])
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if dep == task_id:
                issues.append(ValidationIssue("error", f"task `{task_id}` depends on itself"))
            elif isinstance(dep, str) and dep not in task_by_id:
                issues.append(ValidationIssue("error", f"task `{task_id}` depends on missing task `{dep}`"))

    ordered, remaining = _toposort_tasks(list(task_by_id.values()))
    if remaining:
        issues.append(ValidationIssue("error", f"dependency cycle detected among: {', '.join(sorted(remaining))}"))

    return issues, [task_by_id[task_id] for task_id in ordered]


def _format_issues(issues: Iterable[ValidationIssue]) -> str:
    lines = []
    for issue in issues:
        prefix = "ERROR" if issue.level == "error" else "WARN"
        lines.append(f"{prefix}: {issue.message}")
    return "\n".join(lines)


def cmd_validate(_: argparse.Namespace) -> int:
    charter = _read_json(STATE_DIR / "charter.json")
    roadmap = _read_json(STATE_DIR / "roadmap.json")

    issues = []
    issues.extend(validate_charter(charter))
    roadmap_issues, _ = validate_roadmap(roadmap)
    issues.extend(roadmap_issues)
    issues.extend(validate_devlog(STATE_DIR / "devlog.ndjson"))

    errors = [i for i in issues if i.level == "error"]
    if issues:
        print(_format_issues(issues))
    if errors:
        return 1
    if not issues:
        print("Validation passed.")
    return 0


def cmd_order(_: argparse.Namespace) -> int:
    roadmap = _read_json(STATE_DIR / "roadmap.json")
    issues, ordered_tasks = validate_roadmap(roadmap)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        print(_format_issues(issues))
        return 1

    for task in ordered_tasks:
        print(task["id"])
    return 0


def cmd_ready(_: argparse.Namespace) -> int:
    roadmap = _read_json(STATE_DIR / "roadmap.json")
    issues, ordered_tasks = validate_roadmap(roadmap)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        print(_format_issues(issues))
        return 1

    task_by_id = {t["id"]: t for t in ordered_tasks}
    ready: list[dict[str, Any]] = []

    for task in ordered_tasks:
        if task.get("status") != "todo":
            continue
        deps = task.get("depends_on", [])
        if not isinstance(deps, list):
            continue
        if all(task_by_id[dep].get("status") in TERMINAL_TASK_STATUSES for dep in deps):
            ready.append(task)

    ready.sort(key=lambda t: (_priority_key(t.get("priority")), t["id"]))
    if not ready:
        print("No tasks ready.")
    for task in ready:
        print(f'{task["id"]}\t{task.get("priority")}\t{task.get("title")}')
    return 0


def cmd_steps(args: argparse.Namespace) -> int:
    """Show decomposition steps for a complex task."""
    roadmap = _read_json(STATE_DIR / "roadmap.json")
    issues, ordered_tasks = validate_roadmap(roadmap)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        print(_format_issues(issues))
        return 1

    task_id = args.task_id
    task_by_id = {t["id"]: t for t in ordered_tasks}

    if task_id not in task_by_id:
        print(f"ERROR: Task '{task_id}' not found")
        return 1

    task = task_by_id[task_id]
    steps = task.get("steps", [])

    if not steps:
        print(f"Task '{task_id}' has no decomposition steps.")
        print(f"Complexity: {task.get('complexity', 'simple')}")
        print(f"Deliverable: {task.get('deliverable', 'N/A')}")
        return 0

    print(f"Task: {task_id} - {task.get('title', 'N/A')}")
    print(f"Complexity: {task.get('complexity', 'complex')}")
    print(f"Steps ({len(steps)}):")
    print("-" * 60)

    for step in steps:
        step_num = step.get("step", "?")
        status = step.get("status", "todo")
        critical = " [CRITICAL]" if step.get("critical") else ""
        status_icon = "\u2713" if status == "done" else "\u25cb"

        print(f"{status_icon} Step {step_num}{critical}: {step.get('title', 'N/A')}")
        print(f"   Deliverable: {step.get('deliverable', 'N/A')}")
        print(f"   Verify: {step.get('verify', 'N/A')}")
        if step.get("rollback"):
            print(f"   Rollback: {step.get('rollback')}")
        print()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taskmaster",
        description="Taskmaster: lightweight roadmap + dependency validation for multi-agent planning.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate", help="Validate Taskmaster state files.")
    validate.set_defaults(func=cmd_validate)

    order = sub.add_parser("order", help="Print tasks in dependency order.")
    order.set_defaults(func=cmd_order)

    ready = sub.add_parser("ready", help="List tasks ready to start (todo + deps done).")
    ready.set_defaults(func=cmd_ready)

    steps = sub.add_parser("steps", help="Show decomposition steps for a task.")
    steps.add_argument("task_id", help="Task ID to show steps for (e.g., T-001)")
    steps.set_defaults(func=cmd_steps)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
