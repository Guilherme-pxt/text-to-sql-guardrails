"""Local LLM access: Ollama over urllib, a scripted fake for tests, a sqlite3 cache.

``chat(messages, max_tokens, top_logprobs=0) -> ChatResult`` is all the
generator and the LLM judges need.

The Ollama server on the reference machine is shared with a production system
that keeps ``qwen2.5-coder:7b`` loaded with ``keep_alive = -1`` and
``num_ctx = 24576``. Every request therefore sends exactly those two values: a
different ``num_ctx`` would make Ollama reload the model, and a finite
``keep_alive`` would eventually unload it under the other system. This module
never asks Ollama to unload, pull or load anything else.

Decoding is pinned (temperature 0, fixed seed). The cache stores the measured
latency with the response, so a cached rerun still reports what each call cost
when it ran.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ChatResult:
    text: str
    latency_s: float
    cached: bool = False
    top_logprobs: list[dict[str, float]] = field(default_factory=list)  # per generated token: token -> logprob
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMError(RuntimeError):
    pass


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434", temperature: float = 0.0,
                 seed: int = 42, num_ctx: int = 24576, keep_alive: int | str = -1, timeout: float = 300.0):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.seed = seed
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout = timeout

    def payload(self, messages: list[dict], max_tokens: int, top_logprobs: int) -> dict:
        body = {"model": self.model, "messages": messages, "stream": False, "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature, "seed": self.seed, "num_ctx": self.num_ctx,
                            "num_predict": max_tokens}}
        if top_logprobs:
            body["logprobs"] = True
            body["top_logprobs"] = top_logprobs
        return body

    def chat(self, messages: list[dict], max_tokens: int = 256, top_logprobs: int = 0) -> ChatResult:
        data = json.dumps(self.payload(messages, max_tokens, top_logprobs)).encode()
        req = urllib.request.Request(f"{self.base_url}/api/chat", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise LLMError(f"ollama /api/chat returned {exc.code}: {exc.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"ollama request failed: {exc}") from None
        lps = [{t["token"]: t["logprob"] for t in step.get("top_logprobs", [])} for step in out.get("logprobs") or []]
        return ChatResult(text=out.get("message", {}).get("content", ""), latency_s=time.perf_counter() - t0,
                          top_logprobs=lps, prompt_tokens=out.get("prompt_eval_count"),
                          completion_tokens=out.get("eval_count"))


class ScriptedProvider:
    """Calls ``fn(messages) -> str | ChatResult`` and records every call. For the offline tests."""

    name = "scripted"
    model = "scripted"

    def __init__(self, fn: Callable[[list[dict]], "str | ChatResult"]):
        self.fn = fn
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], max_tokens: int = 256, top_logprobs: int = 0) -> ChatResult:
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "top_logprobs": top_logprobs})
        out = self.fn(messages)
        return out if isinstance(out, ChatResult) else ChatResult(text=out, latency_s=0.0)


class CachedProvider:
    """sqlite3 cache keyed on the full request (provider, model, messages, decoding); keep_alive is not part of it."""

    def __init__(self, inner, path: str | Path):
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self.model = inner.model
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.hits = 0
        self.misses = 0
        self.miss_time_s = 0.0  # model time actually spent in this process
        self.allow_miss = True

    def _key(self, messages: list[dict], max_tokens: int, top_logprobs: int) -> str:
        extra = {k: getattr(self.inner, k, None) for k in ("temperature", "seed", "num_ctx")}
        raw = json.dumps(["llm", self.inner.name, self.model, messages, max_tokens, top_logprobs, extra],
                         sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def has(self, messages: list[dict], max_tokens: int = 256, top_logprobs: int = 0) -> bool:
        return self.db.execute("SELECT 1 FROM cache WHERE key = ?",
                               (self._key(messages, max_tokens, top_logprobs),)).fetchone() is not None

    def chat(self, messages: list[dict], max_tokens: int = 256, top_logprobs: int = 0) -> ChatResult:
        key = self._key(messages, max_tokens, top_logprobs)
        row = self.db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            self.hits += 1
            d = json.loads(row[0])
            return ChatResult(d["text"], d["latency_s"], True, d.get("top_logprobs", []), d.get("prompt_tokens"),
                              d.get("completion_tokens"))
        if not self.allow_miss:
            raise LLMError("cache miss with allow_miss=False")
        self.misses += 1
        res = self.inner.chat(messages, max_tokens=max_tokens, top_logprobs=top_logprobs)
        self.miss_time_s += res.latency_s
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, json.dumps({
            "text": res.text, "latency_s": res.latency_s, "top_logprobs": res.top_logprobs,
            "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens})))
        self.db.commit()
        return res

    def close(self) -> None:
        self.db.close()


def build_provider(cfg: dict, cache_path: str | Path | None = None):
    p = OllamaProvider(cfg["model"], cfg.get("base_url", "http://localhost:11434"),
                       float(cfg.get("temperature", 0.0)), int(cfg.get("seed", 42)), int(cfg.get("num_ctx", 24576)),
                       cfg.get("keep_alive", -1), float(cfg.get("timeout_s", 300)))
    return CachedProvider(p, cache_path) if cache_path else p


def prob_of(top_logprobs: list[dict[str, float]], options: dict[str, list[str]], text: str = "") -> dict[str, float]:
    """Distribution over ``options`` read from the first generated token's top log-probabilities.

    ``options`` maps each label to the token spellings that count for it (compared
    after strip() and lower()). Labels absent from the top-k get zero mass; if no
    label is present at all, the generated text decides (probability 1 for the
    label it starts with, uniform if none)."""
    mass = {k: 0.0 for k in options}
    if top_logprobs:
        for tok, lp in top_logprobs[0].items():
            t = tok.strip().lower()
            for k, spellings in options.items():
                if t in spellings:
                    mass[k] += math.exp(lp)
    total = sum(mass.values())
    if total > 0:
        return {k: v / total for k, v in mass.items()}
    t = text.strip().lower()
    for k, spellings in options.items():
        if any(t.startswith(s) for s in spellings):
            return {j: (1.0 if j == k else 0.0) for j in options}
    return {k: 1.0 / len(options) for k in options}
