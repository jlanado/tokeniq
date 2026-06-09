"""
Runnable demo for TokenIQ.

    python demo.py

Scenario 1 - one coding agent resolving a Jira ticket, run twice:
    (a) NAIVE        : frontier-only, no cache, no governor
    (b) CONTROL PLANE: router + cache + governor
Scenario 2 - a buggy agent stuck in a retry loop, with the governor off vs on.
"""
from control_plane import execute, summarize

# --- Scenario 1: resolve JIRA-4821 (NPE in PaymentService) ---------------
TICKET_STEPS = [
    ("classify",    "Triage ticket JIRA-4821: NPE in PaymentService on null currency"),
    ("retrieve",    "Fetch code context for PaymentService.processPayment"),
    ("retrieve",    "Fetch code context for PaymentService.processPayment"),      # dup -> cache
    ("summarize",   "Summarize related incidents for PaymentService null currency"),
    ("codegen",     "Generate null-check fix for PaymentService.processPayment currency field"),
    ("retrieve",    "Fetch code context for PaymentService.processPayment"),      # dup -> cache
    ("review",      "Review proposed fix for side effects in PaymentService"),
    ("write_tests", "Write unit tests for null currency handling in PaymentService"),
    ("codegen",     "Generate null-check fix for PaymentService.processPayment currency field"),  # dup -> cache
    ("retrieve",    "Fetch CI config for payments module"),
    ("summarize",   "Summarize test results for payments module"),
    ("review",      "Review proposed fix for side effects in PaymentService"),    # dup -> cache
    ("review",      "Review PR description for JIRA-4821"),
    ("classify",    "Classify PR risk level for JIRA-4821"),
    ("summarize",   "Summarize related incidents for PaymentService null currency"),  # dup -> cache
    ("codegen",     "Generate changelog entry for JIRA-4821 fix"),
]

# --- Scenario 2: agent loops on the same failing codegen call ------------
RUNAWAY_STEPS = TICKET_STEPS[:5] + [
    ("codegen", "Retry: regenerate fix for PaymentService null currency"),
] * 6


def row(label, s):
    cpo = "killed" if s["killed"] else f"${s['qa_cpo']:.2f}"
    print(f"  {label:<16} {s['tokens']:>9,}  ${s['cost']:>7.2f}   "
          f"{s['quality']:.2f}   {s['cache_hit_rate']*100:>4.0f}%   "
          f"{s['routing_savings']*100:>4.0f}%   {cpo:>8}")


def main():
    print("=" * 78)
    print("SCENARIO 1  -  one agent resolving JIRA-4821")
    print("=" * 78)
    print(f"  {'mode':<16} {'tokens':>9}  {'cost':>8}   Q    cache   route   qaCPO")
    print("  " + "-" * 70)

    naive_logs, naive_cache, _ = execute(
        TICKET_STEPS, use_router=False, use_cache=False, use_governor=False)
    naive = summarize(naive_logs, naive_cache)
    row("naive", naive)

    ctrl_logs, ctrl_cache, _ = execute(
        TICKET_STEPS, use_router=True, use_cache=True, use_governor=True, use_compression=True)
    ctrl = summarize(ctrl_logs, ctrl_cache)
    row("control plane", ctrl)

    tok_cut = 1 - ctrl["tokens"] / naive["tokens"]
    cost_cut = 1 - ctrl["cost"] / naive["cost"]
    cpo_cut = 1 - ctrl["qa_cpo"] / naive["qa_cpo"]
    print("  " + "-" * 70)
    print(f"  RESULT: tokens -{tok_cut*100:.0f}%   cost -{cost_cut*100:.0f}%   "
          f"quality {naive['quality']:.2f} -> {ctrl['quality']:.2f}   "
          f"cost/ticket -{cpo_cut*100:.0f}%")

    print()
    print("=" * 78)
    print("LEVER STACK  -  each stage compounds (governor is a safety lever, shown below)")
    print("=" * 78)
    stack = [
        ("naive (frontier)",     dict(use_router=False, use_cache=False, use_governor=False, use_compression=False)),
        ("+ semantic cache",     dict(use_router=False, use_cache=True,  use_governor=False, use_compression=False)),
        ("+ model routing",      dict(use_router=True,  use_cache=True,  use_governor=False, use_compression=False)),
        ("+ payload compress",   dict(use_router=True,  use_cache=True,  use_governor=False, use_compression=True)),
    ]
    base = None
    print(f"  {'stage':<20} {'cost':>8}   cumulative cut")
    print("  " + "-" * 48)
    for label, kw in stack:
        logs, cache, _ = execute(TICKET_STEPS, **kw)
        s = summarize(logs, cache)
        if base is None:
            base = s["cost"]
        cut = 1 - s["cost"] / base
        print(f"  {label:<20} ${s['cost']:>7.2f}   -{cut*100:>4.0f}%")

    print()
    print("=" * 78)
    print("SCENARIO 2  -  buggy agent stuck in a retry loop (governor = blast radius cap)")
    print("=" * 78)

    off_logs, off_cache, _ = execute(
        RUNAWAY_STEPS, use_router=True, use_cache=False, use_governor=False)
    off = summarize(off_logs, off_cache)
    print(f"  governor OFF : ran all {len(off_logs)} calls, "
          f"{off['tokens']:,} tokens, ${off['cost']:.2f} burned")

    on_logs, on_cache, gov = execute(
        RUNAWAY_STEPS, use_router=True, use_cache=False, use_governor=True)
    on = summarize(on_logs, on_cache)
    print(f"  governor ON  : KILLED after {len([l for l in on_logs if not l.killed])} calls, "
          f"{on['tokens']:,} tokens, ${on['cost']:.2f} spent")
    print(f"                 reason: {on['kill_reason']}")
    saved = off["cost"] - on["cost"]
    print(f"  RESULT: kill switch saved ${saved:.2f} on a single runaway agent")
    print()


if __name__ == "__main__":
    main()
