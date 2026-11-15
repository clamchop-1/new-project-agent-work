"""Model-level provider contract used by the agent run loop."""
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class InferenceProvider(Protocol):
    """Minimal local inference contract; providers do not receive project tools."""

    def model(self, name: str) -> Mapping[str, Any]:
        """Verify and identify an installed model before starting a run."""
        ...

    def chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        context: int,
        options: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return one non-streaming assistant response and provider metrics."""
        ...