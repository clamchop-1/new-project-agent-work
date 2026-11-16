"""Model-level provider contract used by the agent run loop."""
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from agent_core import AgentError


class ProviderFailure(AgentError):
    """Sanitized, structured inference failure with safe fallback metadata."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 security: bool = False):
        self.code = code
        self.retryable = retryable
        self.security = security
        super().__init__(message)


@runtime_checkable
class InferenceProvider(Protocol):
    """Minimal local inference contract; providers do not receive project tools."""

    def model(self, name: str) -> Mapping[str, Any]:
        """Verify and identify an installed model before starting a run."""
        ...

    def models(self) -> Sequence[Mapping[str, Any]]:
        """List models already available from this explicitly selected provider."""
        ...

    def health_check(self) -> Mapping[str, Any]:
        """Confirm provider service availability without sending task context."""
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

    def close(self) -> None:
        """Release provider-owned resources such as a managed SSH tunnel."""
        ...
