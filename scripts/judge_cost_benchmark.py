"""Token and dollar cost of the v2 judge per clip, per night and per month.

    python scripts/judge_cost_benchmark.py BENCH [--outputs model=file.json ...] [--exact] [--pass-rate 0.4]

BENCH_DIR: one folder per case with payload.json and frames.json ([[t, jpg], ...]). --outputs gives the
JSON answers per model (case -> output object) to price output tokens; without it ~200 output tokens are
assumed. Offline, text tokens are estimated (chars/3.5, bytes/4.5 for non-ASCII-heavy text) and images use
w*h/750. With --exact and ANTHROPIC_API_KEY set, input tokens come from messages.count_tokens instead.
Prices: judge.prices in config/global.yaml (USD per 1M tokens).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from clipwatcher import config  # noqa: E402
from clipwatcher.judge import compact  # noqa: E402

LABEL_TOKENS = 6  # "[t=12.5s]" text block
SCREEN_OUT = 60  # tokens of a short screen answer


def est_tokens(text: str) -> int:
    return math.ceil(max(len(text) / 3.5, len(text.encode()) / 4.5))


def load_case(d: Path) -> dict:
    payload = json.loads((d / "payload.json").read_text())
    frames = [(t, Path(p)) for t, p in json.loads((d / "frames.json").read_text())]
    img = 0
    for _, p in frames:
        with Image.open(p) as im:
            img += round(im.width * im.height / 750)
    return {
        "name": d.name,
        "payload": payload,
        "frames": frames,
        "img": img,
        "labels": LABEL_TOKENS * len(frames),
    }


def exact_tokens(model: str, case: dict) -> tuple[int, int]:
    """(system tokens, rest tokens) via count_tokens."""
    import anthropic

    client = anthropic.Anthropic()
    req = compact.build_request(model, case["frames"], case["payload"])
    full = client.messages.count_tokens(model=model, system=req["system"], messages=req["messages"])
    bare = client.messages.count_tokens(
        model=model, system=req["system"], messages=[{"role": "user", "content": "x"}]
    )
    return bare.input_tokens - 1, full.input_tokens - bare.input_tokens + 1


def price(model: str) -> dict:
    return config.global_config()["judge"]["prices"][model]


def cost(model: str, sys_t: int, rest_t: int, out_t: int, *, cached: bool, batch: bool = False) -> float:
    p = price(model)
    sys_cost = sys_t * (p["cache_read"] if cached else p["cache_write"])
    usd = (sys_cost + rest_t * p["input"] + out_t * p["output"]) / 1e6
    return usd * (0.5 if batch else 1.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bench")
    ap.add_argument("--outputs", nargs="*", default=[], help="model=answers.json")
    ap.add_argument("--exact", action="store_true")
    ap.add_argument(
        "--pass-rate", type=float, default=0.4, help="share of clips the screen passes to the judge"
    )
    ap.add_argument("--credits", type=float, default=600.0)
    a = ap.parse_args(argv)

    jcfg = config.global_config()["judge"]
    models = [jcfg["screen_model"], jcfg["judge_model"]]
    answers = {}
    for spec in a.outputs:
        m, f = spec.split("=", 1)
        answers[m] = json.loads(Path(f).read_text())
    cases = [load_case(d) for d in sorted(Path(a.bench).iterdir()) if (d / "payload.json").exists()]
    sys_est = est_tokens(compact.system_prompt())

    print(f"System prompt: ~{sys_est} tokens (cached after the first clip)\n")
    print("| case | frames | image tok | payload tok | input total | out tok (haiku/sonnet) |")
    print("|---|---|---|---|---|---|")
    rows = []
    for c in cases:
        pay = est_tokens(compact.payload_json(c["payload"]))
        sys_t, rest = sys_est, c["img"] + c["labels"] + pay
        if a.exact:
            sys_t, rest = exact_tokens(models[1], c)
        outs = []
        for m in models:
            ans = answers.get(m, {}).get(c["name"])
            outs.append(
                est_tokens(json.dumps(ans, ensure_ascii=False, separators=(",", ":"))) if ans else 200
            )
        rows.append({"sys": sys_t, "rest": rest, "pay": pay, "out": dict(zip(models, outs, strict=True))})
        print(
            f"| {c['name']} | {len(c['frames'])} | {c['img']} | {pay} | {sys_t + rest} | "
            f"{outs[0]} / {outs[1]} |"
        )

    n = len(rows)
    avg = {k: sum(r[k] for r in rows) / n for k in ("sys", "rest", "pay")}
    out_avg = {m: sum(r["out"][m] for r in rows) / n for m in models}
    hk, sn = models

    def per_clip(kind: str, cached: bool, batch: bool = False) -> float:
        if kind == "two_stage":
            screen = cost(hk, avg["sys"], avg["pay"], SCREEN_OUT, cached=cached, batch=batch)
            return screen + a.pass_rate * cost(
                sn, avg["sys"], avg["rest"], out_avg[sn], cached=cached, batch=batch
            )
        m = hk if kind == "haiku" else sn
        return cost(m, avg["sys"], avg["rest"], out_avg[m], cached=cached, batch=batch)

    print(f"\nAverages: system {avg['sys']:.0f}, frames+labels+payload {avg['rest']:.0f}, "
          f"output haiku {out_avg[hk]:.0f} / sonnet {out_avg[sn]:.0f} tokens\n")  # fmt: skip
    print("| setup | 1st clip (miss) | next clips (hit) | batch, hit | 50 clips | 1,500/mo | "
          f"clips on ${a.credits:.0f} |")  # fmt: skip
    print("|---|---|---|---|---|---|---|")
    # v1 (before this change), Sonnet: 6 frames at 853x480, ~450-token prompt below the cache minimum (never
    # cached), per-word "[12.3]word" transcript, 60 raw "user: text" chat lines, verbose output plus thinking.
    v1_prompt = est_tokens((config.ROOT / "prompts" / "judge_rubric.md").read_text())
    v1_rest = []
    for c in cases:
        words = sum(len(t.split()) for _, _, t in c["payload"]["tr"])
        chat = min(c["payload"]["ch"]["n"], 60) * 9  # ~9 tokens per "user: message" line
        v1_rest.append(6 * round(853 * 480 / 750) + words * 4 + chat + 60)
    v1_in = v1_prompt + sum(v1_rest) / n
    v1_out = 350 + 300  # verbose JSON + adaptive thinking at the default effort (assumption)
    v1 = (v1_in * price(sn)["input"] + v1_out * price(sn)["output"]) / 1e6
    print(f"| v1 before (Sonnet, est.) | ${v1:.5f} | ${v1:.5f} | ${v1 / 2:.5f} | ${50 * v1:.3f} | "
          f"${1500 * v1:.2f} | {int(a.credits / v1):,} |")  # fmt: skip
    labels = {"haiku": "Haiku 5.5 single call", "sonnet": "Sonnet 5.5 single call",
              "two_stage": f"Haiku screen -> Sonnet judge ({a.pass_rate:.0%} pass)"}  # fmt: skip
    for kind, label in labels.items():
        miss, hit, bh = per_clip(kind, False), per_clip(kind, True), per_clip(kind, True, True)
        night = miss + 49 * hit
        month = 1500 * hit
        print(f"| {label} | ${miss:.5f} | ${hit:.5f} | ${bh:.5f} | ${night:.3f} | ${month:.2f} | "
              f"{int(a.credits / hit):,} |")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
