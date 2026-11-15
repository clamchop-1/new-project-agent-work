"""Interactive, read-only terminal for LocalCodingAgent."""

import json
import argparse
import re
import subprocess
import sys
from pathlib import Path

from conversation_session import load_session, save_session, clear_session
from agent_config import config_path, default_projects, load_config, legacy_name_map
from platform_capabilities import capability_report
from assistant_files import AssistantFiles
from agent_core import AgentError, Project

ROOT = Path(__file__).resolve().parent

CONFIG_PATH = config_path(ROOT)
CONFIG = load_config(CONFIG_PATH, default_projects(ROOT))
PROJECTS = tuple(legacy_name_map(CONFIG))


def _memory_bullets(text, heading):
    match = re.search(
        rf'^### {re.escape(heading)}\s*\n(.*?)(?=^### |^## |\Z)',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    return [line[2:].strip() for line in match.group(1).splitlines()
            if line.startswith('- ')]


def local_policy_answer(task, project_name, memory_sections):
    """Answer narrow memory/teaching questions from local policy, without Qwen."""
    lowered = task.casefold()
    asks_about_memory = (
        any(word in lowered for word in ('memory', 'handover', 'hand-off', 'handoff', 'project context'))
        and any(word in lowered for word in ('load', 'check', 'show', 'inspect', 'look', 'what', 'read'))
    )
    if asks_about_memory:
        source_rows = []
        memory_text = ''
        for section in memory_sections:
            memory_text = memory_text or section.get('text', '')
            source_rows.extend(section.get('sources', [section.get('path', 'unknown source')]))
        unique_sources = list(dict.fromkeys(source_rows))
        lines = [f'Project memory for {project_name}:']
        if unique_sources:
            lines.append('Loaded authoritative sources:')
            lines.extend(f'  - {Path(source).name}' for source in unique_sources)
        else:
            lines.append('No authoritative memory source is loaded.')
        handoff_id = re.search(r'^Handoff-ID:\s*(.+)$', memory_text, re.MULTILINE)
        objective = re.search(r'^Objective:\s*(.+)$', memory_text, re.MULTILINE)
        status = re.search(r'^Status:\s*(.+)$', memory_text, re.MULTILINE)
        if handoff_id:
            lines.append(f'Newest handoff: {handoff_id.group(1).strip()}')
        if objective:
            lines.append(f'Objective: {objective.group(1).strip()}')
        if status:
            lines.append(f'Current status: {status.group(1).strip()}')
        next_actions = _memory_bullets(memory_text, 'Next Actions')
        if next_actions:
            lines.append('Next actions:')
            lines.extend(f'  - {action}' for action in next_actions[:3])
        lines.append('Historical handoff sections are retained but are not loaded automatically.')
        return '\n'.join(lines)

    asks_to_learn = (
        any(word in lowered for word in ('train', 'training', 'teach', 'teaching', 'upload', 'fine-tune', 'fine tune'))
        and any(word in lowered for word in ('manual', 'material', 'pdf', 'guide', 'instruction', 'document'))
    )
    if asks_to_learn:
        return (
            'You can give Qwen more reference material, but this terminal cannot accept file attachments or '
            'fine-tune model weights. Place a text file or PDF under the current assistant folder, or use /root PATH '
            'to switch to its drive, then ask for specific pages or sections. That material can guide the current task. '
            'For lasting guidance, distill the useful principles and save them through the reviewed project-memory '
            'adapter with explicit authorization. This interactive terminal does not currently have a direct memory-save command.'
        )
    return None


def inside_git_worktree(path):
    return any((parent / '.git').exists() for parent in Path(path).parents)


def run_task(project, model, task, assistant_root, assistant_write_paths=(), verbose=False):
    memory_sections = Project(legacy_name_map(CONFIG)[project]).memory()
    local_answer = local_policy_answer(task, project, memory_sections)
    if local_answer is not None and not assistant_write_paths:
        print('\nStatus: COMPLETE\n')
        print(local_answer)
        print('\nSource: Local project memory/configuration; no model inference.\n')
        try:
            turns = load_session(project)
            turns.append({'user': task[:3000], 'assistant': local_answer[:3000]})
            save_session(project, turns)
        except (OSError, ValueError) as exc:
            print(f'Conversation history not saved: {exc}')
        return

    command = [
        sys.executable,
        str(ROOT / "agent.py"),
        "run",
        "--project", project,
        "--task", task,
        "--assistant-root", str(assistant_root),
        "--conversation",
    ]
    for path in assistant_write_paths:
        command.extend(["--assistant-write-path", path])
    if model:
        command.extend(["--model", model])

    print("\nRunning local agent...\n", flush=True)

    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )

    output = completed.stdout.strip()

    if verbose:
        print(output)
        if completed.stderr:
            print(completed.stderr)

    if completed.returncode != 0:
        print("Task failed.")
        print(completed.stderr or output)
        return

    # The existing CLI prints routing information followed by JSON.
    lines = output.splitlines()

    routing = None
    json_start = None

    for index, line in enumerate(lines):
        if line.startswith("LOCAL MODEL ROUTING:"):
            try:
                routing = json.loads(
                    line.split("LOCAL MODEL ROUTING:", 1)[1].strip()
                )
            except json.JSONDecodeError:
                pass

        if line.strip().startswith("{"):
            json_start = index
            break

    if json_start is None:
        print("Could not parse agent response:")
        print(output)
        return

    try:
        result = json.loads("\n".join(lines[json_start:]))
    except json.JSONDecodeError:
        print("Could not parse agent response:")
        print(output)
        return

    if routing and not verbose:
        print(f"Model: {routing.get('model', 'unknown')} (local)")

    status = result.get("status", "unknown")
    summary = result.get("summary") or {}

    if (
        status == "complete"
        and isinstance(summary, dict)
        and isinstance(summary.get("summary"), str)
    ):
        try:
            turns = load_session(project)
            turns.append({
                "user": task[:3000],
                "assistant": summary["summary"][:3000],
            })
            save_session(project, turns)
        except (OSError, ValueError) as exc:
            print(f"Conversation history not saved: {exc}")

    if verbose:
        return

    print(f"\nStatus: {status.upper()}\n")

    if isinstance(summary, dict):
        print(summary.get("summary", "No summary provided."))

        unfinished = summary.get("unfinished", [])
        if unfinished:
            print("\nUnfinished:")
            for item in unfinished:
                print(f"  - {item}")
    else:
        print(summary)

    run_directory = result.get("run")

    if run_directory:
        result_path = Path(run_directory) / "execution" / "result.json"

        if result_path.is_file():
            try:
                details = json.loads(
                    result_path.read_text(encoding="utf-8")
                )

                print("\n--- Execution ---")
                print(f"Model: {details.get('model', 'unknown')}")
                print(f"Time: {details.get('seconds', 0):.2f} seconds")
                print(f"Steps: {details.get('steps', 0)}")
                local_writes = len(details.get('writes', []))
                assistant_writes = details.get('assistant_file_writes', [])
                print(f"Files written: {local_writes + len(assistant_writes)}")
                for receipt in assistant_writes:
                    print(f"  Assistant file: {receipt.get('path')} ({receipt.get('after_sha256', 'unknown')})")
                print(f"Cloud used: {details.get('cloud_fallback', False)}")
                print(f"Tests: {details.get('verified_test_status', 'unknown')}")

            except (OSError, ValueError, TypeError) as exc:
                print(f"\nExecution details unavailable: {exc}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Interactive local assistant")
    parser.add_argument('--project', choices=PROJECTS)
    parser.add_argument('--model')
    parser.add_argument('--assistant-root', type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    project = arguments.project or (
        "LocalCodingAgent" if "LocalCodingAgent" in PROJECTS
        else "LocalAgentSandbox" if "LocalAgentSandbox" in PROJECTS
        else PROJECTS[0]
    )
    assistant_root = AssistantFiles(arguments.assistant_root).root
    model = arguments.model or ""
    verbose = False
    pending_write_paths = []

    print("\nLocalCodingAgent — Interactive Terminal")
    print("Local inference | Broad file reads | One-task exact-path edits | Cloud disabled")
    print(f"Assistant folder: {assistant_root}")
    print("Type /help for commands.\n")

    while True:
        try:
            line = input(f"[{project} | {model}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not line:
            continue

        if line in ("/exit", "/quit"):
            print("Goodbye.")
            break

        if line == "/help":
            print("/projects       List available projects")
            print("/capabilities   Show detected host tools")
            print("/project NAME   Select a project")
            print("/root PATH      Set the file root (local absolute path)")
            print("/allow-edit PATH  Allow editing this existing text file in the next task")
            print("/allow-create PATH Allow creating this text file in the next task")
            print("/cancel-write   Withdraw pending file-write paths")
            print("/model NAME     Select a model or auto")
            print("/status         Show current settings")
            print("/history        Show recent conversation for this project")
            print("/clear          Clear conversation for this project")
            print("/verbose        Toggle raw JSON output")
            print("/exit           Quit")
            continue

        if line == "/projects":
            for name in PROJECTS:
                print((" * " if name == project else "   ") + name)
            continue

        if line == "/capabilities":
            print(json.dumps(capability_report(), indent=2))
            continue

        if line.startswith("/project "):
            candidate = line.split(" ", 1)[1].strip()
            if candidate in PROJECTS:
                project = candidate
                pending_write_paths.clear()
                print(f"Selected project: {project}")
            else:
                print("Unknown project. Use /projects.")
            continue

        if line.startswith("/root "):
            requested_root = line.split(" ", 1)[1].strip().strip('"')
            try:
                assistant_root = AssistantFiles(requested_root).root
                pending_write_paths.clear()
                print(f"Assistant file root: {assistant_root}")
            except (OSError, ValueError, AgentError) as exc:
                print(f"Could not select assistant file root: {exc}")
            continue

        if line.startswith("/allow-edit ") or line.startswith("/allow-create "):
            allow_create = line.startswith("/allow-create ")
            requested_path = line.split(" ", 1)[1].strip().strip('"')
            try:
                scope = AssistantFiles(assistant_root)
                canonical = scope.canonical_path(requested_path)
                target = assistant_root / Path(canonical)
                if inside_git_worktree(target):
                    print("Git worktree files use the selected project's reviewed write-permit workflow.")
                    continue
                if allow_create:
                    if target.exists():
                        print("That file already exists; use /allow-edit PATH instead.")
                        continue
                    if not target.parent.is_dir():
                        print("The parent folder must already exist; the assistant will not create directory trees.")
                        continue
                else:
                    scope.read_text(canonical, 1, 1)
                if len(pending_write_paths) >= 5:
                    print("At most five exact file paths can be authorized for one task.")
                    continue
                if canonical in pending_write_paths:
                    print("That exact path is already authorized for the next task.")
                    continue
                pending_write_paths.append(canonical)
                operation = "create" if allow_create else "edit"
                print(f"Next-task {operation} allowed for: {assistant_root / Path(canonical)}")
                print("No other files can be edited or created; no delete tool is available.")
            except (OSError, ValueError, AgentError) as exc:
                print(f"Could not authorize that file path: {exc}")
            continue

        if line == "/cancel-write":
            pending_write_paths.clear()
            print("Pending file-write paths cleared.")
            continue

        if line.startswith("/model "):
            candidate = line.split(" ", 1)[1].strip()
            if candidate:
                model = candidate
                print(f"Selected model: {model}")
            else:
                print("Specify a model or auto.")
            continue

        if line == "/verbose":
            verbose = not verbose
            print(f"Verbose mode: {'ON' if verbose else 'OFF'}")
            continue

        if line == "/history":
            try:
                turns = load_session(project)
                if not turns:
                    print("No saved conversation.")
                for index, turn in enumerate(turns, 1):
                    print(f"\n{index}. You: {turn['user']}")
                    print(f"   Agent: {turn['assistant']}")
            except (OSError, ValueError) as exc:
                print(f"Could not load history: {exc}")
            continue

        if line == "/clear":
            try:
                clear_session(project)
                print("Conversation cleared.")
            except (OSError, ValueError) as exc:
                print(f"Could not clear history: {exc}")
            continue

        if line == "/status":
            print(f"Project: {project}")
            print(f"Assistant files: {assistant_root}")
            print(f"Next-task write paths: {pending_write_paths or 'none'}")
            print(f"Model: {model or CONFIG['inference']['model']}")
            print(f"Endpoint: {CONFIG['inference']['endpoint']}")
            logical_id = next((key for key, row in CONFIG['projects'].items()
                               if row['name'] == project), 'unknown')
            print(f"Logical project ID: {logical_id}")
            print("Mode: Broad local file reads; edits require one-task exact-path authorization")
            print("Cloud: Disabled")
            print(f"Verbose: {verbose}")
            continue

        if line.startswith("/"):
            print("Unknown command. Type /help.")
            continue

        write_paths = tuple(pending_write_paths)
        pending_write_paths.clear()
        run_task(project, model, line, assistant_root, write_paths, verbose)

if __name__ == "__main__":
    main()
