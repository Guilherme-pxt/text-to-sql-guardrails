"""Jev (TypeSafe) client: urllib, sqlite3 cache, budget cap.

Jev is a System One model: it answers typed questions about a ``state`` with
calibrated probabilities instead of generating text. A ``noul`` question
returns the probability that the answer is yes; a ``choice`` returns a
distribution over the options and a ``confidence``.

The API key is read from ``JEV_API_KEY`` or a ``JEV_API_KEY=...`` line in
``.env.jev`` at the project root, lazily, only when a request misses the
cache. It is held in memory and never logged, cached or written. Every
response is cached keyed on the request body (which never contains the key),
with its measured latency and the token usage the API reported, so a rerun
needs neither the key nor the network.

API reference: https://docs.typesafe.ai/api/system-one.md
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

URL = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUS = {429, 500, 502, 503, 504, 529}

# transport(url, headers, body, timeout) -> (status, response headers, response body)
Transport = Callable[[str, dict, bytes, float], "tuple[int, dict, bytes]"]


class JevError(RuntimeError):
    pass


class BudgetExceeded(JevError):
    pass


def load_api_key(root: str | Path = ".", env=None) -> str:
    env = os.environ if env is None else env
    key = (env.get("JEV_API_KEY") or "").strip()
    if key:
        return key
    path = Path(root) / ".env.jev"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith("JEV_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
                if key:
                    return key
    raise JevError("no Jev API key: set JEV_API_KEY or put a JEV_API_KEY=... line in .env.jev at the project root")


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError(f"jev request failed: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def estimate_tokens(body: dict) -> int:
    """Rough input-token estimate (4 characters per token), used only for the budget check before a call."""
    return len(json.dumps(body, ensure_ascii=False)) // 4 + 1


def cost_usd(input_tokens: int, price_per_mtok: float) -> float:
    return input_tokens / 1_000_000 * price_per_mtok


@dataclass
class JevResult:
    answers: dict[str, dict]   # question id -> {"noul": p} or {"choice", "probabilities", "confidence"}
    model: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    cached: bool

    def noul(self, qid: str) -> float:
        return float(self.answers[qid]["noul"])

    def probs(self, qid: str) -> dict[str, float]:
        return {k: float(v) for k, v in self.answers[qid]["probabilities"].items()}


def _slim(answer: dict) -> dict:
    keep = {k: answer[k] for k in ("type", "noul", "choice", "probabilities", "confidence", "score") if k in answer}
    return keep


class JevClient:
    def __init__(self, cache_path: str | Path | None, key_root: str | Path = ".", transport: Transport | None = None,
                 timeout: float = 60.0, max_attempts: int = 5, backoff_s: float = 1.0, sleep=time.sleep,
                 url: str = URL, env=None, price_per_mtok: float = 0.042, max_usd: float | None = None):
        self.url = url
        self.key_root = key_root
        self.env = env
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.price = price_per_mtok
        self.max_usd = max_usd
        self._key: str | None = None
        self.db = None
        if cache_path:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(cache_path))
            self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.hits = 0
        self.misses = 0
        self.retries = 0
        self.billed_input_tokens = 0   # tokens of requests actually sent by this process
        self.input_tokens = 0          # tokens of every request answered, cached or not
        self.output_tokens = 0
        self.models_seen: set[str] = set()

    def __repr__(self) -> str:  # never shows the key
        return f"JevClient(url={self.url!r}, cached={self.db is not None})"

    def _cache_key(self, body: dict) -> str:
        return hashlib.sha256(json.dumps(["jev", self.url, body], sort_keys=True).encode()).hexdigest()

    def cached(self, body: dict) -> dict | None:
        if self.db is None:
            return None
        row = self.db.execute("SELECT value FROM cache WHERE key = ?", (self._cache_key(body),)).fetchone()
        return json.loads(row[0]) if row else None

    def _post(self, body: dict) -> tuple[dict, float]:
        if self._key is None:
            self._key = load_api_key(self.key_root, self.env)
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        data = json.dumps(body).encode()
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.perf_counter()
            status, rh, raw = self.transport(self.url, headers, data, self.timeout)
            latency = time.perf_counter() - t0
            if status == 200:
                return json.loads(raw), latency
            if status in RETRY_STATUS and attempt < self.max_attempts:
                self.retries += 1
                wait = self.backoff_s * 2 ** (attempt - 1)
                ra = {k.lower(): v for k, v in (rh or {}).items()}.get("retry-after")
                try:
                    wait = max(wait, float(ra)) if ra is not None else wait
                except ValueError:
                    pass
                self.sleep(min(wait, 60.0))
                continue
            text = raw.decode("utf-8", "replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]
            raise JevError(f"jev /v1/systemone returned {status}: {text}")
        raise JevError("jev: retries exhausted")  # unreachable: the last attempt returns or raises above

    def ask(self, body: dict) -> JevResult:
        hit = self.cached(body)
        if hit is not None:
            self.hits += 1
            res = JevResult(hit["answers"], hit["model"], hit["input_tokens"], hit["output_tokens"],
                            hit["latency_s"], True)
        else:
            if self.max_usd is not None:
                spent = cost_usd(self.billed_input_tokens + estimate_tokens(body), self.price)
                if spent > self.max_usd:
                    raise BudgetExceeded(f"jev: the next request would take spend to about US${spent:.4f}, over the "
                                         f"US${self.max_usd:.2f} cap")
            self.misses += 1
            data, latency = self._post(body)
            answers = data.get("answers") or {}
            missing = [q for q in body["questions"] if q not in answers]
            if missing:
                raise JevError(f"jev response is missing answers for {missing[:5]}")
            usage = data.get("usage") or {}
            rec = {"answers": {q: _slim(answers[q]) for q in body["questions"]}, "model": data.get("model", ""),
                   "input_tokens": int(usage.get("input_tokens") or 0),
                   "output_tokens": int(usage.get("output_tokens") or 0), "latency_s": latency}
            if self.db is not None:
                self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (self._cache_key(body), json.dumps(rec)))
                self.db.commit()
            self.billed_input_tokens += rec["input_tokens"]
            res = JevResult(rec["answers"], rec["model"], rec["input_tokens"], rec["output_tokens"], latency, False)
        self.input_tokens += res.input_tokens
        self.output_tokens += res.output_tokens
        if res.model:
            self.models_seen.add(res.model)
        return res

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
