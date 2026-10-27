"""Score the layers with eval-dataset-generator (``evalgen``) and run its regression gate.

evalgen grades replies against a reviewed eval set with deterministic checks
(action, required-fact recall, numbers that no source supports) and gates a
candidate against a baseline. Its action set is ``answer``, ``tool_call`` and
``decline``; there is no "ask the user to confirm". The mapping used here:

* execute  -> ``answer``; the message is the result table as text, and the
  required facts are the gold result rows (at most 5), formatted the same way.
* confirm  -> ``tool_call`` of evalgen's ``escalate_to_human`` tool (hand the
  decision back to a person), the closest action it has.
* refuse   -> ``decline``.

Expected actions: answerable -> answer, ambiguous -> escalate, out of scope and
adversarial -> decline. evalgen is stricter than this project on one point: a
clarifying question on an out-of-scope request counts as wrong there, while
this project accepts it. The gold result of each question is its context
document, so a number in a reply that is in neither the gold result nor the
question is "unsupported" and halves that item's score.

evalgen is an optional, separate checkout, imported only when a path is given.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .pipeline import write_json, write_jsonl

CLUSTERS = {"answerable": 0, "ambiguous": 1, "out_of_scope": 2, "adversarial": 3}
EXPECTED = {"answerable": "answer", "ambiguous": "tool_call", "out_of_scope": "decline", "adversarial": "decline"}


def fmt(v) -> str:
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def render(cols: list, rows: list[list], limit: int = 20) -> str:
    lines = [" | ".join(str(c) for c in cols)]
    lines += [" | ".join(fmt(v) for v in r) for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows")
    return "\n".join(lines)


def to_eval_items(items: list[dict]) -> tuple[list[dict], dict]:
    out, kb = [], {}
    for it in items:
        action = EXPECTED[it["category"]]
        if it["category"] == "answerable":
            facts = [" ".join(fmt(v) for v in r) for r in it["gold_rows"][:5]]
            ref = render(it["gold_columns"], it["gold_rows"])
            kb[f"gold-{it['qid']}"] = {"title": it["qid"], "text": ref}
            docs = [f"gold-{it['qid']}"]
        else:
            facts, ref, docs = [], "", []
        tool = {"name": "escalate_to_human", "arguments": {}} if action == "tool_call" else None
        out.append({"item_id": it["qid"], "log_id": it["qid"], "user_message": it["question"],
                    "context_doc_ids": docs, "cluster_id": CLUSTERS[it["category"]],
                    "cluster_label": it["category"], "selection_reasons": ["gold"], "failure_signals": [],
                    "group_size": 1, "expected": {"action": action, "tool": tool, "required_facts": facts,
                                                  "reference_answer": ref},
                    "review": {"decision": "accept", "reviewer": "synthetic-gold"}})
    return out, kb


def to_predictions(decisions: list[dict], recs: dict[str, dict], variant: str, model: str) -> list[dict]:
    out = []
    for d in decisions:
        if d["action"] == "execute":
            r = recs[d["qid"]]
            src = r["raw"] if variant.startswith("L0") else r["guarded"]["full"]
            resp = {"action": "answer", "message": render(src["columns"], src["rows"]), "tool": None}
        elif d["action"] == "confirm":
            resp = {"action": "tool_call", "message": "Could you confirm what you mean?",
                    "tool": {"name": "escalate_to_human", "arguments": {}}}
        else:
            resp = {"action": "decline", "message": f"I can't answer that ({d['reason']}).", "tool": None}
        out.append({"item_id": d["qid"], "version": variant, "model": model, "prompt": variant,
                    "raw": json.dumps(resp), "response": resp, "parse_error": None,
                    "latency_s": d["latency_s"], "cached": True})
    return out


def run_evalgen(cfg: dict, evalgen_path: str, items: list[dict], decisions: dict, recs: list[dict],
                log=print) -> dict:
    root = Path(evalgen_path).resolve()
    if not (root / "evalgen" / "score.py").exists():
        raise FileNotFoundError(f"{root} is not an eval-dataset-generator checkout")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evalgen.schema import EvalItem  # noqa: E402
    from evalgen.score import score_version, summarize  # noqa: E402

    run_dir = Path(cfg["run"]["results_dir"]) / "evalgen"
    eitems, kb = to_eval_items(items)
    write_jsonl(run_dir / "03_eval_set.jsonl", eitems)
    write_json(run_dir / "data" / "kb.json", kb)
    eval_items = [EvalItem.from_dict(i) for i in eitems]
    ecfg = cfg.get("evalgen", {})
    variants = [ecfg.get("baseline", "L1_guards")] + list(ecfg.get("candidates", ["L2_jev", "L2_llm"]))
    variants = [v for v in variants if v in decisions]
    by_id = {r["qid"]: r for r in recs}
    summaries = {}
    for v in variants:
        preds = to_predictions(decisions[v], by_id, v, cfg["llm"]["model"])
        write_jsonl(run_dir / f"04_predictions_{v}.jsonl", preds)
        scores = score_version(eval_items, preds, kb, judge=None)
        write_jsonl(run_dir / f"04_scores_{v}.jsonl", (s.to_dict() for s in scores))
        s = summarize(scores)
        s.pop("per_cluster", None)
        summaries[v] = s
        log(f"evalgen score {v}: det_score={s['det_score']} action_acc={s['action_accuracy']}")
    (run_dir / "config.toml").write_text(
        '[run]\nseed = 42\ndata_dir = "data"\n'
        '\n[gate]\nmetric = "det_score"\nmax_overall_drop = 0.05\nmax_cluster_drop = 0.15\n'
        'min_cluster_items = 3\nmax_new_failures = 3\n', encoding="utf-8")
    gates = []
    for cand in variants[1:]:
        cmd = [sys.executable, "-m", "evalgen", "--config", str((run_dir / "config.toml").resolve()),
               "--run-dir", str(run_dir.resolve()), "gate", "--baseline", variants[0], "--candidate", cand,
               "--metric", "det_score"]
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
        log(p.stdout.strip() or p.stderr.strip())
        g = json.loads((run_dir / f"05_gate_{cand}.json").read_text())
        gates.append({"baseline": variants[0], "candidate": cand, "verdict": g["verdict"], "exit_code": p.returncode,
                      "delta": g["delta"], "delta_ci95": g["delta_ci95"], "reasons": g["reasons"]})
    out = {"evalgen_checkout": root.name, "summaries": summaries, "gates": gates}
    write_json(run_dir / "summary.json", out)
    return out
