import argparse
import json
import urllib.request
from urllib.parse import urlparse

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

def get_models(endpoint):
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Only local Ollama endpoints are enabled in this initial version.")

    with urllib.request.urlopen(endpoint.rstrip("/") + "/api/tags", timeout=10) as response:
        data = json.load(response)

    return [item["name"] for item in data.get("models", [])]

def choose_model(models, task):
    task = task.lower()

    if any(word in task for word in ("architecture", "complex", "debug", "reason")):
        preferences = ["qwen3.8:27b", "deepseek-r1:8b", "qwen3:8b"]
        category = "complex reasoning"
    elif any(word in task for word in ("code", "python", "function", "refactor", "test")):
        preferences = ["qwen2.5-coder:7b-instruct", "qwen3.5:4b", "qwen3:8b"]
        category = "coding"
    else:
        preferences = ["qwen3.5:4b", "qwen3:4b", "llama3.2:3b"]
        category = "general assistance"

    selected = next((model for model in preferences if model in models), None)

    if selected is None:
        raise RuntimeError("No suitable installed model found.")

    return {
        "task_category": category,
        "provider": "local_ollama",
        "model": selected,
        "reason": "Selected from available local models using task-category preferences.",
        "cloud_used": False,
    }

def main():
    parser = argparse.ArgumentParser(description="LocalCodingAgent model router")
    parser.add_argument("--task", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()

    models = get_models(args.endpoint)
    print(json.dumps(choose_model(models, args.task), indent=2))

if __name__ == "__main__":
    main()
