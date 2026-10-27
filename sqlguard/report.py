"""results/report.md: every table of the evaluation, built from the stored records."""

from __future__ import annotations

from pathlib import Path

NAMES = {
    "L0_llm": "L0 LLM only (prompt policy)",
    "L1_guards": "L1 + deterministic guards",
    "L2_jev": "L2 + Jev decides (before and after)",
    "L2_llm": "L2 + LLM decides (same judgments)",
    "L2_jev_pre": "ablation: Jev before generation only",
    "L0_jev": "ablation: Jev without the guards",
}


def pct(x, nd=1) -> str:
    return "" if x is None else f"{100 * x:.{nd}f}%"


def num(x, nd=3) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def ci(c) -> str:
    return f"[{100 * c[0]:.1f}, {100 * c[1]:.1f}]"


def dci(d) -> str:
    if not d:
        return ""
    return f"{100 * d['delta']:+.1f} [{100 * d['ci95'][0]:+.1f}, {100 * d['ci95'][1]:+.1f}]"


def main_table(summ: dict, variants: list[str]) -> list[str]:
    out = ["| layer | correct action, all 124 (95% CI) | execution accuracy, 54 answerable (95% CI) | wrong answer shown | "
           "false refusals | asks on clear | asks on ambiguous | guesses on ambiguous | out of scope declined | "
           "adversarial refused | unsafe executions | median latency, answerable | Jev cost per 1,000 questions |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v in variants:
        s = summ.get(v)
        if not s:
            continue
        out.append(f"| {NAMES.get(v, v)} | {pct(s['correct'])} {ci(s['correct_ci95'])} | "
                   f"{pct(s['execution_accuracy'])} {ci(s['execution_accuracy_ci95'])} | "
                   f"{pct(s['wrong_answer_shown'])} | {pct(s['false_refusal'])} | {pct(s['confirm_rate_clear'])} | "
                   f"{pct(s['confirm_rate_ambiguous'])} | {pct(s['guess_rate_ambiguous'])} | "
                   f"{pct(s['out_of_scope_declined'])} | {pct(s['adversarial_refused'])} | "
                   f"**{s['unsafe_executions']}** | {s['latency_median_answerable_s']:.2f} s | "
                   f"{'' if not s['jev_tokens'] else f'US${s['jev_usd_per_1000_questions']:.3f}'} |")
    return out


def write_report(cfg, items, recs, ev, evalgen, meta, path: Path) -> None:
    summ, comps, cal = ev["summary"], ev["comparisons"], ev["calibration"]
    L = ["# Results: text-to-SQL with guardrails", ""]
    if meta:
        ref = meta.get("reference_run", meta)
        L += [f"Reference run of {ref.get('date', '')}: {ref.get('items', '')} questions, LLM `{ref.get('llm_model', '')}` "
              f"(num_ctx {ref.get('num_ctx', '')}, keep_alive {ref.get('keep_alive', '')}), uncached wall time "
              f"{ref.get('wall_time_s', '')} s, of which LLM time {ref.get('llm_time_of_cache_misses_s', '')} s; "
              f"LLM calls: {ref.get('llm_calls', {})}.", ""]
        if ref is not meta:
            L += [f"These tables were rebuilt on {meta.get('date', '')} by replaying that run's cached model calls "
                  f"(LLM calls {meta.get('llm_calls', {})}, wall time {meta.get('wall_time_s', '')} s).", ""]
        j = ref.get("jev")
        if j:
            L += [f"Jev: requested `{j['model_requested']}`, served {j['models_seen']}; {j['requests_sent']} requests "
                  f"sent, {j['input_tokens_billed_this_run']:,} input tokens billed (US${j['usd_billed_this_run']:.4f}); "
                  f"{j['cache_hits']} further requests were identical to one already sent and came from the cache.", ""]
    L += ["## Layers", ""]
    L += main_table(summ, ["L0_llm", "L1_guards", "L2_jev", "L2_llm"])
    L += ["", "Ablations:", ""]
    L += main_table(summ, ["L2_jev", "L2_jev_pre", "L0_jev"])

    L += ["", "## What got through: unsafe executions per layer", "",
          "| layer | unsafe | by kind | items |", "|---|---|---|---|"]
    for v, s in summ.items():
        L.append(f"| {NAMES.get(v, v)} | {s['unsafe_executions']} | {s['unsafe_by_kind'] or ''} | "
                 f"{', '.join(s['unsafe_items'])} |")

    L += ["", "## Adversarial refusals by attack type", "",
          "| layer | write (8) | injection (9) | pii (10) | unbounded (5) | system (3) |", "|---|---|---|---|---|---|"]
    for v, s in summ.items():
        a = s["adversarial_refused_by_subtype"]
        L.append(f"| {NAMES.get(v, v)} | " + " | ".join(pct(a[k], 0) for k in
                                                         ("write", "injection", "pii", "unbounded", "system")) + " |")

    L += ["", "## Leave-one-out: the deterministic guards without one guard", "",
          "| layer | unsafe executions | items | execution accuracy | false refusals |", "|---|---|---|---|---|"]
    for v in ["L1_guards"] + [k for k in summ if k.startswith("L1_minus_")]:
        s = summ[v]
        L.append(f"| {v} | {s['unsafe_executions']} | {', '.join(s['unsafe_items'])} | "
                 f"{pct(s['execution_accuracy'])} | {pct(s['false_refusal'])} |")

    L += ["", "## Which guard stopped what (full guard set, adversarial and answerable items whose SQL ran)", ""]
    from collections import Counter
    stops = Counter()
    for r in recs:
        if r["gen"]["intent"] == "sql":
            g = r["guarded"]["full"]
            stops[(r["category"], g["status"] if g["status"] != "blocked" else f"blocked: {g['guard']}")] += 1
    L += ["| category | outcome | items |", "|---|---|---|"]
    for (c, o), n in sorted(stops.items()):
        L.append(f"| {c} | {o} | {n} |")

    L += ["", "## Paired comparisons (correct action, paired bootstrap, 10,000 resamples)", "",
          "| comparison | all | answerable | ambiguous | out of scope | adversarial |", "|---|---|---|---|---|---|"]
    for name, c in comps.items():
        L.append(f"| {name} | {dci(c['overall'])} | {dci(c['answerable'])} | {dci(c['ambiguous'])} | "
                 f"{dci(c['out_of_scope'])} | {dci(c['adversarial'])} |")

    L += ["", "## Execution accuracy by question type", "", "| layer | easy (18) | join (18) | aggregation (18) |",
          "|---|---|---|---|"]
    for v, s in summ.items():
        e = s["execution_accuracy_by_subtype"]
        L.append(f"| {NAMES.get(v, v)} | {pct(e['easy'])} | {pct(e['join'])} | {pct(e['aggregation'])} |")

    L += ["", "## False refusals on the 54 answerable questions, by reason", "", "| layer | reasons |", "|---|---|"]
    for v, s in summ.items():
        L.append(f"| {NAMES.get(v, v)} | {s['false_refusal_reasons'] or ''} |")

    if cal:
        L += ["", "## The two judges: discrimination and calibration of each judgment", "",
              "AUROC: probability that a random positive scores above a random negative. Brier and ECE "
              "(5 bins): lower is better.", "",
              "| judgment | n (positives) | Jev AUROC | LLM AUROC | Jev Brier | LLM Brier | Jev ECE | LLM ECE |",
              "|---|---|---|---|---|---|---|---|"]
        for j in ("answerable", "needs_clarification", "injection", "not_read_data", "answers_question"):
            a, b = cal.get("jev", {}).get(j), cal.get("llm", {}).get(j)
            ref = a or b
            if not ref:
                continue
            L.append(f"| {j} | {ref['n']} ({ref['positives']}) | {num(a and a['auroc'])} | {num(b and b['auroc'])} | "
                     f"{num(a and a['brier'])} | {num(b and b['brier'])} | {num(a and a['ece'])} | "
                     f"{num(b and b['ece'])} |")

    L += ["", "## Latency per question, seconds (model times as measured when each call was made)", "",
          "Most refusals stop before generation, so the median over all 124 questions mostly measures refusals; the "
          "answerable questions are the path that runs every stage.", "",
          "| layer | median, 54 answerable | generate | execute | judge | mean, all 124 | p90, all 124 |",
          "|---|---|---|---|---|---|---|"]
    for v, s in summ.items():
        st = s["latency_answerable_by_stage_s"]
        L.append(f"| {NAMES.get(v, v)} | {s['latency_median_answerable_s']:.2f} | {st['generate']:.2f} | "
                 f"{st['execute']:.3f} | {st['judge']:.2f} | {s['latency_mean_s']:.2f} | {s['latency_p90_s']:.2f} |")

    for key, field in (("sweep_clarify", "clarify_confirm"), ("sweep_post", "post_confirm_below")):
        L += ["", f"## Threshold sensitivity: `{field}` (pre-registered value in config.toml)", "",
              f"| layer | {field} | correct | execution accuracy | asks on clear | asks on ambiguous | "
              "wrong answer shown | unsafe |", "|---|---|---|---|---|---|---|---|"]
        for r in ev[key]:
            L.append(f"| {r['variant']} | {r[field]} | {pct(r['correct'])} | {pct(r['execution_accuracy'])} | "
                     f"{pct(r['confirm_rate_clear'])} | {pct(r['confirm_rate_ambiguous'])} | "
                     f"{pct(r['wrong_answer_shown'])} | {r['unsafe']} |")

    att = ev.get("attacks")
    if att:
        L += ["", "## Hand-written attack SQL through each guard configuration (added after the reference run)", "",
              "No model involved: each statement is fed straight to the guards, and the oracle judges what ran. "
              "`no guards` runs every statement on a writable copy, like layer 0.", "",
              "| attack | kind | " + " | ".join(att["configs"]) + " |", "|---|---|" + "---|" * len(att["configs"])]
        for r in att["rows"]:
            L.append(f"| {r['attack']} | {r['kind']} | " + " | ".join(
                (f"**{o}**" if o.startswith("UNSAFE") else o) for o in r["outcome"].values()) + " |")
        L.append("| **unsafe** | | " + " | ".join(f"**{att['unsafe_totals'][c]}**" for c in att["configs"]) + " |")

    if evalgen:
        L += ["", "## Scored and gated with eval-dataset-generator", "",
              "| version | det_score | action accuracy | gate vs baseline (default thresholds) |", "|---|---|---|---|"]
        gates = {g["candidate"]: g for g in evalgen["gates"]}
        for v, s in evalgen["summaries"].items():
            g = gates.get(v)
            gate = "baseline" if g is None else (f"{g['verdict'].upper()}, exit {g['exit_code']}, delta "
                                                  f"{g['delta']:+.3f}, 95% CI [{g['delta_ci95'][0]:+.3f}, "
                                                  f"{g['delta_ci95'][1]:+.3f}]; {'; '.join(g['reasons']) or 'no rule fired'}")
            L.append(f"| {v} | {s['det_score']:.3f} | {s['action_accuracy']:.3f} | {gate} |")

    L += ["", "## Per-question decisions", "", "Every decision of every layer is in `results/decisions/<layer>.jsonl`; "
          "every stage (generated SQL, guard outcomes, oracle audit, both judges' probabilities) is in "
          "`results/records.jsonl`.", ""]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
