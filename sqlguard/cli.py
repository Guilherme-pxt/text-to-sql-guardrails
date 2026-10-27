"""python -m sqlguard <synth | ask | eval | run-all | report>"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from .generate import generate
from .guards import run_guarded
from .jev import JevClient, cost_usd, estimate_tokens
from .judges import Routing, jev_pre_request, name_matches
from .llm import build_provider
from .pipeline import collect, read_jsonl, synth, write_json, write_jsonl


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def ollama_ps(base_url: str) -> list[dict]:
    """Loaded models as Ollama reports them (GET /api/ps, read-only)."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/ps", timeout=10) as r:
            return [{"name": m.get("name"), "context_length": m.get("context_length"), "expires_at": m.get("expires_at")}
                    for m in json.loads(r.read()).get("models", [])]
    except OSError as exc:
        return [{"error": str(exc)}]


def make_jev(cfg: dict, offline: bool = False) -> JevClient:
    j = cfg.get("jev", {})
    return JevClient(cfg["run"]["cache"], key_root=".", timeout=float(j.get("timeout_s", 60)),
                     price_per_mtok=float(j.get("price_usd_per_mtok", 0.042)),
                     max_usd=0.0 if offline else float(j.get("max_usd", 1.0)))


def cmd_synth(cfg, args) -> int:
    print(json.dumps(synth(cfg), indent=2))
    return 0


def cmd_ask(cfg, args) -> int:
    """One question through the full stack (L2 with Jev), printing each decision."""
    db = Path(cfg["run"]["data_dir"]) / "quorvia.sqlite"
    provider = build_provider(cfg["llm"], cfg["run"]["cache"])
    routing = Routing.from_cfg(cfg["routing"])
    from .judges import jev_post_request, jev_pre_probs, preview, route_post, route_pre
    jev = make_jev(cfg)
    m = name_matches(db, args.question)
    pre = jev_pre_probs(jev.ask(jev_pre_request(cfg["jev"]["model"], args.question, m)))
    print(f"jev before: kind={pre['kind']} P(read)={pre['read_data']:.2f} answerable={pre['answerable']:.2f} "
          f"clarify={pre['clarify']:.2f} injection={pre['injection']:.2f}")
    r = route_pre(pre, routing)
    if r:
        print(f"-> {r[0].upper()}: {r[1]}")
        return 0
    g = generate(provider, args.question, int(cfg["llm"].get("max_tokens_sql", 400)))
    print(f"llm ({g.latency_s:.1f} s): {g.intent} {g.sql or g.message}")
    if g.intent != "sql":
        print(f"-> {'CONFIRM' if g.intent == 'clarify' else 'REFUSE'}")
        return 0
    res = run_guarded(db, g.sql)
    if res.status != "ok":
        print(f"-> REFUSE: {res.status} {res.guard or ''} {res.reason}")
        return 0
    post = jev.ask(jev_post_request(cfg["jev"]["model"], args.question, g.sql,
                                    preview(res.columns, res.rows, res.truncated)))
    p = post.noul("answers_question")
    action, why = route_post(p, routing)
    print(f"jev after: P(answers the question)={p:.2f} -> {action.upper()} {why}")
    print(" | ".join(res.columns))
    for row in res.rows[:20]:
        print(" | ".join(str(v) for v in row))
    return 0


def _evaluate_and_report(cfg, items, recs, evalgen_path, meta) -> dict:
    from .evaluate import evaluate
    from .report import write_report
    res_dir = Path(cfg["run"]["results_dir"])
    routing = Routing.from_cfg(cfg["routing"])
    ev = evaluate(recs, items, routing, float(cfg["jev"]["price_usd_per_mtok"]))
    for v, dec in ev["decisions"].items():
        write_jsonl(res_dir / "decisions" / f"{v}.jsonl", dec)
    write_json(res_dir / "metrics.json", {k: ev[k] for k in ("summary", "comparisons", "calibration")})
    write_json(res_dir / "sweeps.json", {"clarify_confirm": ev["sweep_clarify"], "post_confirm_below": ev["sweep_post"]})
    from .attacks import run_suite
    from .pipeline import _audit_kwargs, guard_cfg
    ev["attacks"] = run_suite(Path(cfg["run"]["data_dir"]) / "quorvia.sqlite", guard_cfg(cfg), _audit_kwargs(cfg))
    write_json(res_dir / "attack_suite.json", ev["attacks"])
    eg = None
    if evalgen_path:
        from .evalgen_bridge import run_evalgen
        eg = run_evalgen(cfg, evalgen_path, items, ev["decisions"], recs)
    write_report(cfg, items, recs, ev, eg, meta, res_dir / "report.md")
    return ev


def cmd_eval(cfg, args) -> int:
    res_dir = Path(cfg["run"]["results_dir"])
    items = read_jsonl(Path(cfg["run"]["data_dir"]) / "questions.jsonl")
    if args.limit:
        items = items[: args.limit]
    provider = build_provider(cfg["llm"], cfg["run"]["cache"])
    jev = None if args.no_jev else make_jev(cfg, offline=args.offline)
    if args.offline:
        provider.allow_miss = False
    if jev is not None and not args.offline:
        est = sum(estimate_tokens(jev_pre_request(cfg["jev"]["model"], it["question"], {})) for it in items) * 2.2
        print(f"jev pre-run estimate: about {int(est):,} input tokens, US${cost_usd(int(est), jev.price):.4f} "
              f"(cap US${jev.max_usd:.2f})")
        if cost_usd(int(est), jev.price) > jev.max_usd:
            print("estimate is over the cap; not starting", file=sys.stderr)
            return 2
    ps_before = ollama_ps(cfg["llm"]["base_url"])
    t0 = time.perf_counter()
    recs = collect(cfg, items, provider, jev)
    wall = time.perf_counter() - t0
    ps_after = ollama_ps(cfg["llm"]["base_url"])
    write_jsonl(res_dir / "records.jsonl", recs)
    meta = {"date": time.strftime("%Y-%m-%d"), "wall_time_s": round(wall, 1), "items": len(items),
            "llm_model": cfg["llm"]["model"], "num_ctx": cfg["llm"]["num_ctx"], "keep_alive": cfg["llm"]["keep_alive"],
            "llm_calls": {"cache_hits": provider.hits, "cache_misses": provider.misses},
            "llm_time_of_cache_misses_s": round(provider.miss_time_s, 1),
            "jev": None if jev is None else {"model_requested": cfg["jev"]["model"],
                                             "models_seen": sorted(jev.models_seen), "requests_sent": jev.misses,
                                             "cache_hits": jev.hits, "retries": jev.retries,
                                             "input_tokens_all": jev.input_tokens,
                                             "input_tokens_billed_this_run": jev.billed_input_tokens,
                                             "output_tokens_all": jev.output_tokens,
                                             "usd_all": cost_usd(jev.input_tokens, jev.price),
                                             "usd_billed_this_run": cost_usd(jev.billed_input_tokens, jev.price)},
            "ollama_ps_before": ps_before, "ollama_ps_after": ps_after,
            "python": platform.python_version(), "platform": platform.platform()}
    prev = res_dir / "run_meta.json"
    if prev.exists() and args.keep_meta:
        old = json.loads(prev.read_text())
        meta["reference_run"] = old.get("reference_run", old)
    write_json(prev, meta)
    print(f"collected {len(recs)} items in {wall:.0f} s (llm hits {provider.hits}, misses {provider.misses}"
          + (f"; jev sent {jev.misses}, hits {jev.hits}, billed US${cost_usd(jev.billed_input_tokens, jev.price):.4f}"
             if jev else "") + ")")
    evalgen_path = args.evalgen or os.environ.get("SQLGUARD_EVALGEN_PATH") or cfg.get("evalgen", {}).get("path")
    _evaluate_and_report(cfg, items, recs, evalgen_path, meta)
    print(f"wrote {res_dir}/report.md")
    return 0


def cmd_report(cfg, args) -> int:
    """Rebuild metrics and report from results/records.jsonl, no model calls."""
    res_dir = Path(cfg["run"]["results_dir"])
    items = read_jsonl(Path(cfg["run"]["data_dir"]) / "questions.jsonl")
    recs = read_jsonl(res_dir / "records.jsonl")
    ids = {r["qid"] for r in recs}
    items = [it for it in items if it["qid"] in ids]
    meta = json.loads((res_dir / "run_meta.json").read_text()) if (res_dir / "run_meta.json").exists() else {}
    evalgen_path = args.evalgen or os.environ.get("SQLGUARD_EVALGEN_PATH") or cfg.get("evalgen", {}).get("path")
    _evaluate_and_report(cfg, items, recs, evalgen_path, meta)
    print(f"wrote {res_dir}/report.md")
    return 0


def cmd_run_all(cfg, args) -> int:
    print(json.dumps(synth(cfg)))
    return cmd_eval(cfg, args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sqlguard", description=__doc__)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--results-dir", help="override [run].results_dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("synth", help="build data/quorvia.sqlite and data/questions.jsonl with gold results")
    p = sub.add_parser("ask", help="one question through the full stack")
    p.add_argument("question")
    for name in ("eval", "run-all"):
        p = sub.add_parser(name, help="collect every stage, evaluate every layer, write results/")
        p.add_argument("--limit", type=int, default=0, help="first N questions only (smoke test)")
        p.add_argument("--no-jev", action="store_true", help="skip the Jev variants")
        p.add_argument("--offline", action="store_true", help="replay the cache only; a miss is an error")
        p.add_argument("--evalgen", default="", help="path to an eval-dataset-generator checkout")
        p.add_argument("--keep-meta", action="store_true", help="keep the reference run's timing in run_meta.json")
    p = sub.add_parser("report", help="rebuild metrics and report.md from results/records.jsonl")
    p.add_argument("--evalgen", default="")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.results_dir:
        cfg["run"]["results_dir"] = args.results_dir
    return {"synth": cmd_synth, "ask": cmd_ask, "eval": cmd_eval, "run-all": cmd_run_all,
            "report": cmd_report}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
