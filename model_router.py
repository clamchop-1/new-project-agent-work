import argparse
import json

from agent_config import DEFAULT_ENDPOINT, validate_endpoint
from local_agent import create_inference_provider


def get_models(endpoint, provider_name='ollama'):
    endpoint = validate_endpoint(endpoint)
    return [
        item['name'] for item in
        create_inference_provider(provider_name, endpoint).models()
        if not item.get('remote_host') and not item.get('remote_model')
        and 'cloud' not in item.get('name', '').casefold()
    ]

def choose_model(models, task, provider_name='ollama'):
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
        "provider": provider_name,
        "model": selected,
        "reason": "Selected from available local models using task-category preferences.",
        "cloud_used": False,
    }

def main():
    parser = argparse.ArgumentParser(description="LocalCodingAgent model router")
    parser.add_argument("--task", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--provider", choices=['ollama'], default='ollama')
    args = parser.parse_args()

    models = get_models(args.endpoint, args.provider)
    print(json.dumps(choose_model(models, args.task, args.provider), indent=2))

if __name__ == "__main__":
    main()
