"""
RLM Trajectory — Phase 3 audit log.

Records every event in a recursive RLM run (root prompt, REPL exec,
sub-LLM request/response, law load, budget consumption, final answer).

Reference: arXiv 2512.24601v2 (Recursive Language Models).
Counsely Track C audits trajectories via persisted JSON.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

EventKind = Literal[
    "root_prompt",
    "root_completion",
    "repl_exec",
    "repl_exec_error",
    "sub_llm_request",
    "sub_llm_response",
    "sub_llm_error",
    "law_load",
    "budget_consume",
    "final_answer",
]


@dataclass
class TrajectoryEvent:
    event_id: str
    parent_event_id: str | None
    kind: EventKind
    depth: int
    ts_ms: float
    payload: dict
    tokens_in: int = 0
    tokens_out: int = 0

    @classmethod
    def new(
        cls,
        kind: EventKind,
        depth: int,
        payload: dict,
        parent_event_id: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> TrajectoryEvent:
        return cls(
            event_id=str(uuid.uuid4()),
            parent_event_id=parent_event_id,
            kind=kind,
            depth=depth,
            ts_ms=time.time() * 1000.0,
            payload=payload,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TrajectoryEvent:
        return cls(
            event_id=data["event_id"],
            parent_event_id=data.get("parent_event_id"),
            kind=data["kind"],
            depth=int(data["depth"]),
            ts_ms=float(data["ts_ms"]),
            payload=dict(data.get("payload", {})),
            tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
        )


@dataclass
class Trajectory:
    trajectory_id: str
    query: str
    started_at: float
    events: list[TrajectoryEvent] = field(default_factory=list)
    final_answer: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0

    @classmethod
    def new(cls, query: str) -> Trajectory:
        return cls(
            trajectory_id=str(uuid.uuid4()),
            query=query,
            started_at=time.time(),
        )

    def append(self, event: TrajectoryEvent) -> None:
        self.events.append(event)

    def to_dict(self) -> dict:
        return {
            "trajectory_id": self.trajectory_id,
            "query": self.query,
            "started_at": self.started_at,
            "events": [e.to_dict() for e in self.events],
            "final_answer": self.final_answer,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Trajectory:
        return cls(
            trajectory_id=data["trajectory_id"],
            query=data["query"],
            started_at=float(data["started_at"]),
            events=[TrajectoryEvent.from_dict(e) for e in data.get("events", [])],
            final_answer=data.get("final_answer"),
            error=data.get("error"),
            elapsed_ms=float(data.get("elapsed_ms", 0.0)),
        )

    def persist(self, dir: Path | None = None) -> Path:
        """
        Persist this trajectory as JSON.

        Default directory: ~/.kolaw/trajectories/
        Filename: {trajectory_id}.json
        """
        target_dir = (
            Path(dir)
            if dir is not None
            else Path.home() / ".kolaw" / "trajectories"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{self.trajectory_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> Trajectory:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
