"""Interactive, read-only terminal for LocalCodingAgent."""

import json
import subprocess
import sys
from pathlib import Path

from conversation_session import load_session, save_session, clear_session

ROOT = Path(__file__).resolve().parent

PROJECTS = (
    "LocalAgentSandbox",
    "KronosStage",
    "PhotoLibraryTools",
    "PhotoLibraryPlanning",
)


def run_task(project, model, task, verbose=False):
    command = [
        sys.executable,
        str(ROOT / "agent.py"),
        "run",
        "--project", project,
        "--model", model,
        "--task", task,
    ]

    if project == "LocalAgentSandbox":
        command.append("--conversation")

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
        project == "LocalAgentSandbox"
        and status == "complete"
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
                print(f"Files written: {len(details.get('writes', []))}")
                print(f"Cloud used: {details.get('cloud_fallback', False)}")
                print(f"Tests: {details.get('verified_test_status', 'unknown')}")

            except (OSError, ValueError, TypeError) as exc:
                print(f"\nExecution details unavailable: {exc}")

    print()


def main():
    project = "LocalAgentSandbox"
    model = "auto"
    verbose = False

    print("\nLocalCodingAgent — Interactive Terminal")
    print("Local inference | Read-only | Cloud disabled")
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
            print("/project NAME   Select a project")
            print("/model NAME     Select a model or auto")
            print("/status         Show current settings")
            print("/history        Show recent sandbox conversation")
            print("/clear          Clear sandbox conversation")
            print("/verbose        Toggle raw JSON output")
            print("/exit           Quit")
            continue

        if line == "/projects":
            for name in PROJECTS:
                print((" * " if name == project else "   ") + name)
            continue

        if line.startswith("/project "):
            candidate = line.split(" ", 1)[1].strip()
            if candidate in PROJECTS:
                project = candidate
                print(f"Selected project: {project}")
            else:
                print("Unknown project. Use /projects.")
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
            if project != "LocalAgentSandbox":
                print("Conversation history is currently sandbox-only.")
                continue
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
            if project != "LocalAgentSandbox":
                print("Conversation history is currently sandbox-only.")
                continue
            try:
                clear_session(project)
                print("Sandbox conversation cleared.")
            except (OSError, ValueError) as exc:
                print(f"Could not clear history: {exc}")
            continue

        if line == "/status":
            print(f"Project: {project}")
            print(f"Model: {model}")
            print("Endpoint: http://127.0.0.1:11434")
            print("Mode: Read-only")
            print("Cloud: Disabled")
            print(f"Verbose: {verbose}")
            continue

        if line.startswith("/"):
            print("Unknown command. Type /help.")
            continue

        run_task(project, model, line, verbose)

if __name__ == "__main__":
    main()
