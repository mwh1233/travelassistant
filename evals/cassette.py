"""Tool-response cassettes: the storage behind Replay mode.

Why Replay matters
------------------
D09 §3.2 calls Replay "『一次只改一个变量』最有效的落地手段". If the tool layer
is frozen to historical returns, then a metric movement can *only* be attributed
to the one thing you changed (model, prompt, graph). Without it, model + prompt +
retriever + tool implementation all move together and the comparison is noise
(D09 §7.3).

Format
------
One JSON file per case, keyed by ``tool|args-hash``::

    {
      "case_id": "traj_transport_param_fidelity_001",
      "mode": "record",
      "created_at": "...",
      "git_sha": "abc1234",
      "entries": {
        "query_transport_options|3f9a1c2d4e5f": {
          "tool": "query_transport_options",
          "args": {"origin_city": "北京", ...},
          "output": "FAKE transport ...",
          "args_hash": "3f9a1c2d4e5f"
        }
      }
    }

A **miss in Replay mode is an error, not a fallback**. Silently returning a
default would hide the fact that the recording is stale after an external
contract change — exactly the failure Replay exists to surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASSETTE_DIR = PROJECT_ROOT / "evals" / "cassettes"

CASSETTE_SCHEMA_VERSION = "1.0"


class CassetteMiss(RuntimeError):
    """Raised when Replay mode is asked for a call that was never recorded."""


def args_hash(args: dict[str, Any]) -> str:
    payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def entry_key(tool: str, args: dict[str, Any]) -> str:
    return f"{tool}|{args_hash(args)}"


@dataclass
class Cassette:
    """Read/write store for recorded tool responses.

    ``mode`` is one of:

    - ``off``    — no recording, no replay (normal deterministic/live runs)
    - ``record`` — call through and persist the observed response
    - ``replay`` — return only recorded responses; a miss raises
    """

    case_id: str
    mode: str = "off"
    directory: Path | None = None
    git_sha: str = ""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    hits: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return (self.directory or CASSETTE_DIR) / f"{self.case_id}.json"

    # ---------- loading ----------

    def load(self) -> "Cassette":
        if self.mode == "off" or not self.path.exists():
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self
        self.entries = payload.get("entries") or {}
        return self

    # ---------- read/write ----------

    def lookup(self, tool: str, args: dict[str, Any]) -> str | None:
        """Return a recorded response, or ``None`` when not recorded."""

        entry = self.entries.get(entry_key(tool, args))
        if entry is None:
            self.misses.append(entry_key(tool, args))
            return None
        self.hits += 1
        return entry.get("output")

    def record(self, tool: str, args: dict[str, Any], output: Any) -> None:
        if self.mode != "record":
            return
        self.entries[entry_key(tool, args)] = {
            "tool": tool,
            "args": args,
            "output": output,
            "args_hash": args_hash(args),
        }

    def save(self) -> Path | None:
        if self.mode != "record":
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cassette_schema_version": CASSETTE_SCHEMA_VERSION,
            "case_id": self.case_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": self.git_sha,
            "entries": self.entries,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return self.path

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "entries": len(self.entries),
            "hits": self.hits,
            "misses": len(self.misses),
            "miss_sample": self.misses[:5],
            "path": str(self.path) if self.mode != "off" else None,
        }


def load_cassette(case_id: str, *, mode: str = "off", directory: Path | None = None) -> Cassette:
    return Cassette(case_id=case_id, mode=mode, directory=directory).load()
