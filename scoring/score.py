#!/usr/bin/env python3
"""Scorer for one ATC-Bench episode. Exact-match checks are scored here. The written reasons in the audit are
graded by judge.py when ANTHROPIC_API_KEY is set; without it the audit is scored on whether each failure was found.

Usage: python score.py key.json answers.json out_dir [outside_files.txt audit_marker]

Each probe in the key holds a list of checks. A check has a weight and may be must-pass.
  probe score = 0 if any must-pass check fails, else (weight of passed checks) / (total weight)
  all-pass    = 1 only if every check passes
Level score = mean probe score at that level. picture = mean of the level scores.
audit = (share of the real failures found with every essential criterion met) x (share of look-alikes left alone).
It is the headline (reward) when an episode has audit cases: every model tested answers the recall questions
correctly, so they would hide it. audit_partial is the same with partial credit per case.
Two scores are reported for every episode: the Picture score (partial credit) and episode_pass (strict: 1 only
if every check on every item passed).
Writes out_dir/reward.json (numbers only) and out_dir/detail.json (per probe and per check).
"""
import json
import os
import re
import sys
from pathlib import Path


def norm(v):
    s = str(v).strip().upper()
    s = s.replace(",", "").replace("FEET", "").replace("FT", "").replace("RUNWAY", "").replace("HEADING", "").replace("NM", "")
    s = re.sub(r"[^A-Z0-9.]", "", s)
    if s in ("NONE", "NONEOFTHESE", "NA", "NULL", ""):
        return "NONE"
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return s


def pairs(v):
    """A list of pairs as a set of frozensets of normalised callsigns."""
    out = set()
    if v is None or isinstance(v, (int, float)):
        return out
    if isinstance(v, str):
        v = [] if norm(v) == "NONE" else re.split(r"[;,]", v)
    for p in v:
        parts = p if isinstance(p, (list, tuple)) else re.split(r"[/&+]| and | AND |-(?=[A-Za-z])", str(p))
        cs = frozenset(norm(x) for x in parts if norm(x) != "NONE")
        if len(cs) == 2:
            out.add(cs)
    return out


def field(answer, name):
    if name is None:
        return answer
    return answer.get(name) if isinstance(answer, dict) else None


PARA = re.compile(r"\d{1,2}\s*-\s*\d{1,2}\s*-\s*\d{1,2}")


def findings(v):
    """The audit answer as a list of (position, set of aircraft, set of handbook paragraphs). At most 12 count."""
    out = []
    if isinstance(v, dict):
        v = v.get("findings", [])
    if not isinstance(v, list):
        return out
    for f in v[:12]:
        if not isinstance(f, dict):
            continue
        ac = f.get("aircraft", [])
        ac = re.split(r"[,;/&\s]+", ac) if isinstance(ac, str) else ac if isinstance(ac, list) else []
        paras = {re.sub(r"\s", "", p) for p in PARA.findall(str(f.get("paragraph", "")))}
        out.append((norm(f.get("position", "")), {norm(a) for a in ac if norm(a) != "NONE"}, paras, str(f.get("reason", ""))[:2000]))
    return out


def matches(f, spec):
    """A finding matches a scripted case if it names the right position, at least one of the case's aircraft, and
    no aircraft from outside the case."""
    pos, ac = f[0], f[1]
    core, allowed = {norm(a) for a in spec["core"]}, {norm(a) for a in spec["allowed"]}
    return norm(spec["position"]) in pos and bool(ac & core) and ac <= allowed


def run_check(c, answer):
    got = field(answer, c.get("field"))
    kind = c.get("kind", "eq")
    if kind in ("finding", "finding_rule", "no_finding"):
        if not isinstance(got, (list, dict)):
            return False
        hits = [f for f in findings(got) if matches(f, c["expect"])]
        if kind == "finding":
            return bool(hits)
        if kind == "no_finding":
            return not hits
        return any(f[2] & set(c["expect"]["rules"]) for f in hits)
    if kind == "eq":
        return got is not None and not isinstance(got, (dict, list)) and norm(got) == norm(c["expect"])
    if kind in ("has_pair", "no_extra_pairs", "pair") and got is None:
        return False
    if kind == "has_pair":
        return next(iter(pairs([c["expect"]])), None) in pairs(got)
    if kind == "no_extra_pairs":
        return pairs(got) <= pairs(c["expect"])
    if kind == "pair":
        want = pairs([c["expect"]]) if norm(c["expect"]) != "NONE" else set()
        return pairs(got if isinstance(got, list) else [got]) == want
    raise ValueError(kind)


def score(key, answers, judge=None):
    """judge: a function that grades written reasons (see judge.py), or None. Checks of kind "judge" are graded by
    it. With no judge, or when the judge fails, those checks are left out of the score and the rest is unchanged."""
    graded, pending, decoys = [], [], []
    for pid, k in key.items():
        ans = answers.get(k.get("answer_id") or pid) if isinstance(answers, dict) else None   # audit cases share one answer
        results = []
        for c in k["checks"]:
            r = dict(name=c["name"], weight=c["weight"], must=bool(c.get("must")), expect=c["expect"], passed=False)
            if c.get("kind") == "judge":
                # the judge reads the reason written in the finding that matched this case, if there is one
                hits = [f for f in findings(ans) if matches(f, c["expect"])][:3] if isinstance(ans, (list, dict)) else []
                r.update(judged=True, expect=c["expect"]["criterion"], evidence="no matching finding")
                for h, f in enumerate(hits):
                    pending.append((r, dict(id=f"{pid}#{len(results)}@{h}", case=k["family"], facts=c["expect"]["facts"],
                                            reason=f[3], criterion=c["expect"]["criterion"])))
            elif c.get("kind") == "no_finding" and isinstance(ans, (list, dict)) and c["expect"].get("topic"):
                # A look-alike is only tripped by a finding that is about it. A finding that names the same position
                # and aircraft for some other reason is left alone. The judge decides; without it, key words do.
                hits = [f for f in findings(ans) if matches(f, c["expect"])]
                r["passed"] = not hits
                if hits:
                    items = [dict(id=f"{pid}#d{n}", case=k["family"], facts=c["expect"].get("facts", ""), reason=f[3],
                                  criterion="Topic check: the finding is about " + c["expect"]["topic"]) for n, f in enumerate(hits)]
                    words = [w.lower() for w in c["expect"].get("keywords", [])]
                    by_words = any(any(w in f[3].lower() for w in words) or (f[2] & set(c["expect"].get("rules", []))) for f in hits)
                    decoys.append((r, items, by_words))
            else:
                r["passed"] = bool(ans is not None and run_check(c, ans))
            results.append(r)
        graded.append((pid, k, ans, results))
    verdicts, judge_error = None, 0
    if judge is not None:
        try:
            asked = [item for _, item in pending] + [it for _, items, _ in decoys for it in items]
            verdicts = judge(asked) if asked else {}
        except Exception as exc:                 # the exact-match score must survive a judge failure
            judge_error = getattr(exc, "code", 2)
            print(f"judge failed: {type(exc).__name__}: {str(exc)[:300]}")
    for r, items, by_words in decoys:
        if verdicts is not None:
            r["passed"] = not any(bool(verdicts.get(it["id"], {}).get("passed")) for it in items)
        else:
            r["passed"] = not by_words
    if verdicts is not None:
        for r, item in pending:
            v = verdicts.get(item["id"], {})
            if v.get("passed") or not r["passed"]:
                r["passed"], r["evidence"] = bool(v.get("passed")) or r["passed"], v.get("evidence", "")
    detail, by_level = [], {}
    for pid, k, ans, results in graded:
        if verdicts is None:
            results = [r for r in results if not r.get("judged")]
        total = sum(r["weight"] for r in results)
        gate = all(r["passed"] for r in results if r["must"])
        s = sum(r["weight"] for r in results if r["passed"]) / total if gate else 0.0
        allp = all(r["passed"] for r in results)
        is_rubric = k["rubric"] if k.get("rubric") is not None else len(results) > 1
        detail.append(dict(id=pid, level=k["level"], family=k["family"], needs=k["needs"], rubric=is_rubric,
                           score=round(s, 4), all_pass=allp, returned=ans, checks=results))
        by_level.setdefault(k["level"], []).append((s, allp))
    reward = {}
    lv_scores, lv_all = [], []
    for lv in ("perception", "comprehension", "projection"):
        xs = by_level.get(lv, [])
        if not xs:
            continue
        reward[lv] = round(sum(s for s, _ in xs) / len(xs), 4)
        reward[f"{lv}_all_pass"] = round(sum(a for _, a in xs) / len(xs), 4)
        reward[f"{lv}_n"] = len(xs)
        lv_scores.append(reward[lv])
        lv_all.append(reward[f"{lv}_all_pass"])
    reward["picture"] = round(sum(lv_scores) / len(lv_scores), 4) if lv_scores else 0.0
    cases = [d for d in detail if d["family"].startswith("audit:")]
    real = [d for d in cases if "(handled correctly)" not in d["family"]]
    alike = [d for d in cases if "(handled correctly)" in d["family"]]
    if cases:
        # A real failure counts when it was found and every essential criterion of its reason was met (the must-pass
        # checks): then its score is above 0. The headline counts cases, pass or fail. audit_partial is the same
        # with each case's partial credit in place of the pass.
        found = sum(d["score"] for d in real) / len(real) if real else 1.0
        passed = sum(1 for d in real if d["score"] > 0)
        kept = sum(1 for d in alike if d["all_pass"]) / len(alike) if alike else 1.0
        reward["audit"] = round((passed / len(real) if real else 1.0) * kept, 4)
        reward["audit_partial"] = round(found * kept, 4)
        reward["audit_failures_passed"] = passed
        reward["audit_failures"], reward["audit_failures_credit"] = len(real), round(sum(d["score"] for d in real), 3)
        reward["audit_lookalikes"], reward["audit_lookalikes_left_alone"] = len(alike), sum(1 for d in alike if d["all_pass"])
    reward["reward"] = reward["audit"] if cases else reward["picture"]
    reward["all_pass"] = round(sum(lv_all) / len(lv_all), 4) if lv_all else 0.0
    rub = [d for d in detail if d["rubric"]]
    fact = [d for d in detail if not d["rubric"]]
    if rub:
        reward["process_probes"] = round(sum(d["score"] for d in rub) / len(rub), 4)
        reward["process_probes_all_pass"] = round(sum(d["all_pass"] for d in rub) / len(rub), 4)
        reward["process_probes_n"] = len(rub)
    if fact:
        reward["fact_probes"] = round(sum(d["score"] for d in fact) / len(fact), 4)
        reward["fact_probes_n"] = len(fact)
    reward["n_probes"] = len(detail)
    reward["n_checks"] = sum(len(d["checks"]) for d in detail)
    reward["n_checks_passed"] = sum(c["passed"] for d in detail for c in d["checks"])
    # the strict score: the episode passes only if every check on every item passed (judge criteria included
    # when the judge ran). Reported beside the partial-credit Picture score, never in place of it.
    reward["items_passed"] = sum(1 for d in detail if d["all_pass"])
    reward["episode_pass"] = 1 if detail and all(d["all_pass"] for d in detail) else 0
    # the judged part, reported on its own: the share of reasoning criteria met across the real failures
    reward["judge_ok"] = 1 if verdicts is not None else 0
    reward["judge_error"] = judge_error
    if verdicts is not None:
        jc = [c for d in detail for c in d["checks"] if c.get("judged")]
        reward["judge_criteria"] = len(jc)
        reward["judge_criteria_passed"] = sum(c["passed"] for c in jc)
        reward["method"] = round(sum(c["passed"] for c in jc) / len(jc), 4) if jc else 0.0
        # one number per criterion, in answer-key order, so each verdict can be read back from the run's results
        for n, c in enumerate(jc, 1):
            reward[f"judge_c{n:02d}"] = 1 if c["passed"] else 0
    return reward, detail


def main():
    key = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    try:
        answers = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    except Exception as exc:  # missing or malformed answers score zero
        answers = {}
        (out / "answers_error.txt").write_text(str(exc), encoding="utf-8")
    # The judge runs only when its key and package are there; ATC_JUDGE=off turns it off. Nothing here prints the key.
    judge, why = None, 0
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if os.environ.get("ATC_JUDGE", "") != "off" and has_key:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import judge as judge_module
            judge = judge_module.judge
        except Exception as exc:
            why = 1
            print(f"judge not loaded: {type(exc).__name__}: {str(exc)[:200]}")
    reward, detail = score(key, answers, judge)
    reward["judge_key_present"] = 1 if has_key else 0
    if why:
        reward["judge_error"] = why
    if len(sys.argv) > 5:
        # files written outside the working directory between steps: -1 if the record was never started
        listed = Path(sys.argv[4])
        started = Path(sys.argv[5]).exists()
        lines = [x for x in listed.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()] if listed.exists() else []
        reward["outside_files"] = len(lines) if started else -1
    (out / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
    (out / "detail.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
    print(json.dumps(reward))
    # a readable report for the run log: every question, and each check that failed
    print("--- report")
    for d in detail:
        bad = [c for c in d["checks"] if not c["passed"]]
        print(f"{d['id']} | {d['level']} | {d['family']} | score {d['score']} | {len(d['checks']) - len(bad)}/{len(d['checks'])} checks")
        for c in bad:
            if c.get("judged"):
                print(f"    JUDGE FAILED {c['expect'][:160]} | evidence: {c.get('evidence', '')[:160]}")
                continue
            got = d["returned"].get(c["name"]) if isinstance(d["returned"], dict) and c["name"] in d["returned"] else d["returned"]
            print(f"    FAILED {c['name']}{' (must-pass)' if c['must'] else ''}: expected {json.dumps(c['expect'])[:200]} | answered {json.dumps(got)[:300]}")
    print("--- end report")


if __name__ == "__main__":
    main()
