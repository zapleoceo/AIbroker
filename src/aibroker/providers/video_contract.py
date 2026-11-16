"""Provider-neutral asynchronous video result, separate from chat usage metadata."""
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoResult:
    id: str
    status: str
    url: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "timeout"}
