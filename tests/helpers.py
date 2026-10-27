"""Shared fixtures: a freshly built database in a temp dir, a config, fake model and Jev transports."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from sqlguard.llm import ChatResult, ScriptedProvider
from sqlguard.synth import build_db

_DB_DIR = None


def shared_db() -> Path:
    """One synthetic database per test process (it is read-only for every test)."""
    global _DB_DIR
    if _DB_DIR is None:
        _DB_DIR = tempfile.TemporaryDirectory()
        build_db(Path(_DB_DIR.name) / "quorvia.sqlite", seed=42)
    return Path(_DB_DIR.name) / "quorvia.sqlite"


def config(tmp: Path, data_dir: Path) -> dict:
    return {"run": {"seed": 42, "data_dir": str(data_dir), "results_dir": str(tmp / "results"),
                    "cache": str(tmp / "cache.sqlite")},
            "llm": {"model": "scripted", "max_tokens_sql": 400, "judge_top_logprobs": 10, "num_ctx": 24576,
                    "keep_alive": -1, "base_url": "http://localhost:11434"},
            "guards": {"row_limit": 1000, "timeout_s": 2.0, "progress_every": 1000},
            "oracle": {"wall_cap_s": 5.0, "row_cap": 10001, "unbounded_rows": 1000, "unbounded_s": 2.0},
            "jev": {"model": "jev-1.14.0", "price_usd_per_mtok": 0.042, "max_usd": 1.0, "timeout_s": 60},
            "routing": {}}


def answer_with(token: str, p: float = 0.9) -> ChatResult:
    """A one-token judge reply with top log-probabilities favouring ``token``."""
    other = {"yes": "no", "no": "yes"}.get(token, "B" if token != "B" else "A")
    return ChatResult(text=token, latency_s=0.1,
                      top_logprobs=[{token: math.log(p), other: math.log(1 - p)}])


def scripted_model(sql_by_question: dict[str, str], judge=None) -> ScriptedProvider:
    """Generation returns the scripted reply for the question; judge calls go to ``judge(user_text)``."""
    def fn(messages):
        user = messages[-1]["content"]
        if user.startswith("Question: ") and "\n" not in user:
            q = user[len("Question: "):]
            return sql_by_question.get(q, "REFUSE: not scripted")
        if judge is not None:
            return judge(user)
        return answer_with("A") if "one letter" in user else answer_with("no", 0.8)
    return ScriptedProvider(fn)


class FakeJev:
    """A transport for JevClient: answers every question with fixed values and records each request."""

    def __init__(self, noul: float = 0.1, kind: str = "read_data", status: int = 200, per_question=None):
        self.noul = noul
        self.kind = kind
        self.status = status
        self.per_question = per_question or {}
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        req = json.loads(body)
        self.requests.append(req)
        self.headers.append(headers)
        if self.status != 200:
            return self.status, {}, b'{"error": "nope"}'
        answers = {}
        for qid, q in req["questions"].items():
            if q["type"] == "choice":
                probs = {k: (0.9 if k == self.kind else 0.1 / (len(q["criteria"]) - 1)) for k in q["criteria"]}
                answers[qid] = {"type": "choice", "choice": self.kind, "probabilities": probs, "confidence": 0.85}
            else:
                answers[qid] = {"type": "noul", "noul": self.per_question.get(qid, self.noul)}
        out = {"model": req["model"], "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 10}}
        return 200, {}, json.dumps(out).encode()
