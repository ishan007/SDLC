#!/usr/bin/env python3
"""Claude Code hook: write one AI log entry per turn (Stop) or subagent run (SubagentStop).

Reads the hook payload from stdin, parses the session transcript and writes
submission/ai-logs/<id>.json in the assignment's log format. Fields a hook
cannot know (human_decision, limitation) stay null; the ai-log-curator
subagent fills them in later.

Environment overrides:
  AI_LOG_DIR      output folder (default: <project>/submission/ai-logs)
  AI_LOG_CONTEXT  value for "context" (default: "Assignment for AI in SDLC")
  AI_LOG_STAGE    force a value for "stage" instead of guessing from the prompt
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SUBAGENT_TOOLS = {"Agent", "Task"}
BUILT_IN_SUBAGENTS = {
    "general-purpose", "Explore", "Plan", "claude", "claude-code-guide",
    "statusline-setup", "web-fetch", "fork",
}
BUILT_IN_SKILLS = {
    "update-config", "keybindings-help", "code-review", "simplify", "loop",
    "fewer-permission-prompts", "claude-api", "init", "security-review", "run",
    "workflow-authoring", "plugin-authoring", "dataviz",
}
CLI_COMMANDS = {
    "clear", "compact", "config", "help", "model", "cost", "status", "memory",
    "mcp", "permissions", "resume", "login", "logout", "doctor", "agents", "hooks",
}
VERIFY_PATTERN =re.compile(
    r"dotnet (test|build)|npm (run )?test|npx playwright|pytest|jest|vitest|eslint|tsc\b"
)
STAGE_KEYWORDS = [
    ("requirement", ["requirement", "assignment", "analyse", "analyze", "user story", "brief"]),
    ("design", ["design", "diagram", "architecture", "mermaid", "c4"]),
    ("testing", ["test", "playwright", "coverage", "xunit", "e2e"]),
    ("review", ["review", "eval", "audit", "risk"]),
    ("deployment", ["deploy", "pipeline", "ci", "release", "incident"]),
    ("setup", ["hook", "subagent", "config", "setup", "install", "settings"]),
]
MAX_TEXT = 600


def read_transcript(path):
    entries = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_human_prompt(entry):
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return False
    text = text_of(content).strip()
    return bool(text) and not text.startswith("<local-command-stdout>")


def clean_prompt(text):
    """Strip IDE/system wrappers, keep slash-command names, drop pasted-content ids."""
    command = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    text = re.sub(r"<(system-reminder|ide_selection|ide_opened_file)>.*?</\1>", "", text, flags=re.S)
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", "[pasted content]", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text).strip()
    if command and not text:
        text = f"{command.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return text


def last_turn(entries):
    """Return (prompt_entry, entries_after_prompt) for the most recent human prompt."""
    for index in range(len(entries) - 1, -1, -1):
        if is_human_prompt(entries[index]):
            return entries[index], entries[index + 1:]
    return None, entries


def slugify(text, words=4):
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(tokens[:words]) or "turn"


def truncate(text, limit=MAX_TEXT):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def guess_stage(prompt):
    forced = os.environ.get("AI_LOG_STAGE")
    if forced:
        return forced
    lowered = prompt.lower()
    for stage, keywords in STAGE_KEYWORDS:
        if any(re.search(rf"\b{re.escape(word)}", lowered) for word in keywords):
            return stage
    return "development"


def map_mode(permission_mode, wrote_files):
    if permission_mode == "plan":
        return "plan"
    if permission_mode == "acceptEdits":
        return "edit"
    return "agent" if wrote_files else "ask"


def tool_results_by_id(turn_entries):
    results = {}
    for entry in turn_entries:
        content = entry.get("message", {}).get("content")
        if entry.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = block
    return results


def subagent_status(result_block):
    if result_block is None:
        return "partial"
    if result_block.get("is_error"):
        return "failed"
    text = text_of(result_block.get("content")).lower()
    if "interrupted" in text or "still running" in text or "in the background" in text:
        return "partial"
    return "completed"


def analyse(turn_entries, project_dir):
    tools, skills, subagents, mcp_tools, artifacts, verifications = [], [], [], [], [], []
    model, last_text = None, ""
    results = tool_results_by_id(turn_entries)

    for entry in turn_entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message", {})
        if message.get("model") and not message["model"].startswith("<"):
            model = message["model"]
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                last_text = block["text"]
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            tool_input = block.get("input") or {}
            if name not in tools:
                tools.append(name)

            if name in WRITE_TOOLS:
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if path:
                    try:
                        path = str(Path(path).resolve().relative_to(project_dir))
                    except ValueError:
                        pass
                    if path not in artifacts:
                        artifacts.append(path)
            elif name == "Bash":
                command = tool_input.get("command", "")
                if VERIFY_PATTERN.search(command):
                    verifications.append(truncate(command, 120))
            elif name == "Skill":
                skill = tool_input.get("skill", "")
                skills.append({
                    "name": skill,
                    "type": "built-in" if skill in BUILT_IN_SKILLS or ":" in skill else "custom",
                    "purpose": truncate(tool_input.get("args") or "", 200) or None,
                    "invocation": "automatic",
                })
            elif name in SUBAGENT_TOOLS:
                agent_kind = tool_input.get("subagent_type") or "general-purpose"
                subagents.append({
                    "name": agent_kind,
                    "type": "built-in" if agent_kind in BUILT_IN_SUBAGENTS else "custom",
                    "task": truncate(tool_input.get("description") or tool_input.get("prompt") or "", 200),
                    "status": subagent_status(results.get(block.get("id"))),
                })
            elif name.startswith("mcp__"):
                parts = name.split("__", 2)
                mcp_tools.append({
                    "server": parts[1] if len(parts) > 1 else "",
                    "tool": parts[2] if len(parts) > 2 else name,
                    "purpose": truncate(json.dumps(tool_input), 160),
                })

    return {
        "tools": tools, "skills": skills, "subagents": subagents, "mcp_tools": mcp_tools,
        "artifacts": artifacts, "verifications": verifications,
        "model": model, "last_text": last_text,
    }


def session_index(log_dir, session_id):
    index_dir = log_dir / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / f"{session_id}.json"
    try:
        data = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"turns": [], "pending_subagents": []}
    return index_path, data


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("stop_hook_active"):
        return 0

    event = payload.get("hook_event_name", "Stop")
    session_id = payload.get("session_id", "unknown")
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    log_dir = Path(os.environ.get("AI_LOG_DIR") or project_dir / "submission" / "ai-logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    is_subagent = event == "SubagentStop"
    transcript_path = payload.get("transcript_path", "")
    if is_subagent:
        if not payload.get("agent_transcript_path"):
            return 0  # no separate transcript; the parent turn's log already lists the subagent
        transcript_path = payload["agent_transcript_path"]

    entries = read_transcript(transcript_path)
    if is_subagent and payload.get("agent_transcript_path"):
        prompt_entry = next((e for e in entries if e.get("type") == "user"), None)
        turn_entries = entries
    else:
        prompt_entry, turn_entries = last_turn(entries)
    if prompt_entry is None:
        return 0

    request = clean_prompt(text_of(prompt_entry.get("message", {}).get("content")))
    if not request or request.startswith("/clear"):
        return 0
    found = analyse(turn_entries, project_dir)
    slash = re.search(r"<command-name>/?(.*?)</command-name>",
                      text_of(prompt_entry.get("message", {}).get("content")))
    if slash and slash.group(1).strip() not in CLI_COMMANDS:
        skill = slash.group(1).strip()
        found["skills"] = [s for s in found["skills"] if s["name"] != skill]
        found["skills"].insert(0, {
            "name": skill,
            "type": "built-in" if skill in BUILT_IN_SKILLS or ":" in skill else "custom",
            "purpose": truncate(request, 200),
            "invocation": "explicit",
        })
    if is_subagent and not found["tools"] and not found["last_text"]:
        return 0

    now = datetime.now(timezone.utc)
    agent_name = payload.get("agent_type") or ("subagent" if is_subagent else "claude-code")
    log_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{'subagent' if is_subagent else 'agent'}-{slugify(request)}"

    index_path, index = session_index(log_dir, session_id)
    parent_task = index["turns"][0] if index["turns"] else None
    related_ids = list(index["turns"][-3:])
    if not is_subagent:
        related_ids += index.get("pending_subagents", [])

    if found["verifications"]:
        verification = "Verified by running: " + "; ".join(dict.fromkeys(found["verifications"]))
    elif found["artifacts"]:
        verification = "Changes not verified by an automated check in this turn; manual review required."
    else:
        verification = "No files changed; response only."

    log = {
        "id": log_id,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage": guess_stage(request),
        "agent": "sub-agent" if is_subagent else "agent",
        "agent_name": agent_name,
        "agent_type": "sub-agent" if is_subagent else "built-in",
        "ai_tool": "Claude Code",
        "model": found["model"] or os.environ.get("ANTHROPIC_MODEL") or "unknown",
        "mode": map_mode(prompt_entry.get("permissionMode") or payload.get("permission_mode"),
                         bool(found["artifacts"])),
        "related_ids": related_ids,
        "parent_task": parent_task,
        "context": os.environ.get("AI_LOG_CONTEXT", "Assignment for AI in SDLC"),
        "request": truncate(request),
        "skills_used": found["skills"],
        "subagents_used": found["subagents"],
        "mcp_tools_used": found["mcp_tools"],
        "tools_used": found["tools"],
        "suggestion": truncate(found["last_text"]) or None,
        "human_decision": None,
        "action_verification": verification,
        "limitation": None,
        "status": "applied" if found["artifacts"] else "suggested",
        "artifacts": found["artifacts"],
    }

    (log_dir / f"{log_id}.json").write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")

    if is_subagent:
        index.setdefault("pending_subagents", []).append(log_id)
    else:
        index["turns"].append(log_id)
        index["pending_subagents"] = []
    index_path.write_text(json.dumps(index, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # never block Claude because logging failed
        print(f"capture_ai_log: {error}", file=sys.stderr)
        sys.exit(0)
