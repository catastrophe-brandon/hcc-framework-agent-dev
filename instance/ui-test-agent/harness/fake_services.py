"""Small, stateful in-process Jira and memory fakes for offline preflight tests."""

from __future__ import annotations

from copy import deepcopy


class FakeJira:
    """Implement only the Jira operations used by the UI-test preflight."""

    ALLOWED = {"jira_search", "jira_get_issue", "jira_add_comment"}

    def __init__(self, issues: list[dict] | None = None):
        self.issues = {issue["key"]: deepcopy(issue) for issue in issues or []}
        self.calls: list[tuple[str, dict]] = []

    def snapshot(self) -> dict:
        return {"issues": deepcopy(self.issues), "calls": deepcopy(self.calls)}

    def call(self, name: str, arguments: dict):
        if name not in self.ALLOWED:
            raise AssertionError(
                f"Unsupported Jira operation in offline harness: {name}"
            )
        self.calls.append((name, deepcopy(arguments)))

        if name == "jira_search":
            label = _label_from_jql(arguments.get("jql", ""))
            matching = [
                deepcopy(issue)
                for issue in self.issues.values()
                if label in (_fields(issue).get("labels") or [])
            ]
            return {"issues": matching[: arguments.get("limit", 20)]}

        issue_key = arguments.get("issue_key")
        issue = self.issues.get(issue_key)
        if issue is None:
            return None
        if name == "jira_get_issue":
            return deepcopy(issue)

        comment = {
            "body": arguments.get("comment", arguments.get("body", "")),
            "created": "2099-01-01T00:00:00Z",
            "author": {"displayName": "Local Harness"},
        }
        issue.setdefault("comments", []).append(comment)
        return {"key": issue_key, "comment": deepcopy(comment)}


class FakeMemory:
    """Per-test task and learning-memory store; no external persistence."""

    ALLOWED = {
        "task_list",
        "task_get",
        "task_add",
        "task_update",
        "task_check_capacity",
        "bot_status_update",
        "memory_search",
        "memory_store",
        "memory_list",
    }

    def __init__(
        self, tasks: list[dict] | None = None, memories: list[dict] | None = None
    ):
        self.tasks = deepcopy(tasks or [])
        self.memories = deepcopy(memories or [])
        self.calls: list[tuple[str, dict]] = []
        self.cycle_runs: list[dict] = []
        self.costs: list[dict] = []
        self.status_updates: list[dict] = []
        self.idle_states: dict[str, dict] = {}

    def snapshot(self) -> dict:
        return {
            "tasks": deepcopy(self.tasks),
            "memories": deepcopy(self.memories),
            "calls": deepcopy(self.calls),
            "cycle_runs": deepcopy(self.cycle_runs),
            "costs": deepcopy(self.costs),
            "status_updates": deepcopy(self.status_updates),
            "idle_states": deepcopy(self.idle_states),
        }

    def call(self, name: str, arguments: dict):
        if name not in self.ALLOWED:
            raise AssertionError(
                f"Unsupported memory operation in offline harness: {name}"
            )
        self.calls.append((name, deepcopy(arguments)))

        if name == "task_list":
            instance_id = arguments.get("instance_id")
            status = arguments.get("status")
            include_archived = arguments.get("include_archived", False)
            return deepcopy(
                [
                    task
                    for task in self.tasks
                    if (
                        not instance_id
                        or task.get("instance_id") in {None, instance_id}
                    )
                    and (not status or task.get("status") == status)
                    and (include_archived or task.get("status") != "archived")
                ]
            )
        if name == "task_get":
            key = arguments.get("external_key") or arguments.get("id")
            return deepcopy(
                next(
                    (task for task in self.tasks if task.get("external_key") == key),
                    None,
                )
            )
        if name == "task_check_capacity":
            instance_id = arguments.get("instance_id")
            active = [
                task
                for task in self.tasks
                if task.get("status") in {"in_progress", "pr_open", "pr_changes"}
                and (not instance_id or task.get("instance_id") in {None, instance_id})
            ]
            maximum = 10
            return {
                "active": len(active),
                "max": maximum,
                "has_capacity": len(active) < maximum,
            }
        if name == "bot_status_update":
            self.status_updates.append(deepcopy(arguments))
            return {"updated": True}
        if name == "task_add":
            task = deepcopy(arguments)
            task.setdefault("id", f"local-task-{len(self.tasks) + 1}")
            self.tasks.append(task)
            return deepcopy(task)
        if name == "task_update":
            key = arguments.get("external_key")
            task = next(
                (item for item in self.tasks if item.get("external_key") == key), None
            )
            if task is None:
                return None
            updates = {
                k: v
                for k, v in arguments.items()
                if k != "external_key" and v is not None
            }
            if isinstance(updates.get("metadata"), dict):
                task["metadata"] = {
                    **(task.get("metadata") or {}),
                    **updates["metadata"],
                }
                updates.pop("metadata")
            task.update(deepcopy(updates))
            return deepcopy(task)
        if name == "memory_store":
            record = deepcopy(arguments)
            record.setdefault("id", f"local-memory-{len(self.memories) + 1}")
            self.memories.append(record)
            return deepcopy(record)
        if name == "memory_list":
            return deepcopy(self.memories)
        query = str(arguments.get("query", "")).casefold()
        category = arguments.get("category")
        repo = arguments.get("repo")
        tag = arguments.get("tag")
        matches = [
            record
            for record in self.memories
            if query in str(record).casefold()
            and (not category or record.get("category") == category)
            and (not repo or record.get("repo") == repo)
            and (not tag or tag in (record.get("tags") or []))
        ]
        return deepcopy(matches[: arguments.get("limit", 5)])


def _fields(issue: dict) -> dict:
    return issue.get("fields") or issue


def _label_from_jql(jql: str) -> str:
    marker = 'labels = "'
    start = jql.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = jql.find('"', start)
    return jql[start:end] if end >= 0 else ""
