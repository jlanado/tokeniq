"""
TokenIQ control plane as a LangGraph StateGraph.

The control plane IS a graph:
  - cache hit  -> early exit
  - governor kill -> early exit
  - quality gate fail -> escalate loop (bump a tier, re-call)

Reuses the exact core logic from control_plane.py so behaviour matches the
gateway, the CLI demo, and the visual simulator.

    pip install langgraph
    python langgraph_app.py          # runs a request + prints the graph as mermaid

Visualize:
  - print(app.get_graph().draw_mermaid())   # mermaid text (renders on GitHub)
  - app.get_graph().draw_mermaid_png()       # PNG (needs network to mermaid.ink)
  - LangGraph Studio                         # step through the graph live
"""
from __future__ import annotations
import random
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from control_plane import (
    TIERS, make_task, route, run_model, compress_tokens, cost,
    SemanticCache, BudgetGovernor,
)

# cross-request singletons (would be Redis/pgvector in production)
CACHE = SemanticCache()
GOVERNORS: dict[str, BudgetGovernor] = {}
RNG = random.Random(7)


class State(TypedDict, total=False):
    task: str
    prompt: str
    run_id: str
    tier_idx: int
    acc_tokens: int      # accumulates each model attempt (escalation bills twice)
    quality: float
    passed: bool
    escalated: bool
    served_from: str     # cache | killed | model
    note: str


# ---- nodes -------------------------------------------------------------
def meter(s: State) -> State:
    return {"served_from": "", "acc_tokens": 0}            # tag + init


def cache_lookup(s: State) -> State:
    hit = CACHE.get(s["task"], s["prompt"])
    if hit is not None:
        return {"served_from": "cache", "quality": hit, "acc_tokens": 0}
    return {}


def governor(s: State) -> State:
    gov = GOVERNORS.setdefault(s["run_id"], BudgetGovernor())
    foot = compress_tokens(make_task(s["task"]))
    if not gov.allow(s["task"], s["prompt"], foot):
        return {"served_from": "killed", "note": gov.kill_reason}
    return {}


def router(s: State) -> State:
    return {"tier_idx": TIERS.index(route(make_task(s["task"])))}


def compress(s: State) -> State:
    return {}                                              # footprint computed in call_model


def call_model(s: State) -> State:
    task = make_task(s["task"])
    tier = TIERS[s["tier_idx"]]
    q, passed = run_model(task, tier, RNG)
    foot = compress_tokens(task)
    return {"acc_tokens": s.get("acc_tokens", 0) + foot, "quality": q, "passed": passed}


def escalate(s: State) -> State:
    return {"tier_idx": s["tier_idx"] + 1, "escalated": True}


def finalize(s: State) -> State:
    tier = TIERS[s["tier_idx"]]
    GOVERNORS[s["run_id"]].add(s["acc_tokens"])
    CACHE.put(s["task"], s["prompt"], s["quality"])
    return {"served_from": "model"}


# ---- conditional edges -------------------------------------------------
def after_cache(s: State) -> str:
    return "hit" if s.get("served_from") == "cache" else "miss"


def after_governor(s: State) -> str:
    return "kill" if s.get("served_from") == "killed" else "ok"


def after_gate(s: State) -> str:
    if s.get("passed") or s["tier_idx"] >= len(TIERS) - 1:
        return "pass"
    return "escalate"


# ---- build graph -------------------------------------------------------
def build():
    g = StateGraph(State)
    for name, fn in [("meter", meter), ("cache", cache_lookup), ("governor", governor),
                     ("router", router), ("compress", compress), ("model", call_model),
                     ("escalate", escalate), ("finalize", finalize)]:
        g.add_node(name, fn)

    g.add_edge(START, "meter")
    g.add_edge("meter", "cache")
    g.add_conditional_edges("cache", after_cache, {"hit": END, "miss": "governor"})
    g.add_conditional_edges("governor", after_governor, {"kill": END, "ok": "router"})
    g.add_edge("router", "compress")
    g.add_edge("compress", "model")
    g.add_conditional_edges("model", after_gate, {"pass": "finalize", "escalate": "escalate"})
    g.add_edge("escalate", "model")                        # the escalation loop
    g.add_edge("finalize", END)
    return g.compile()


app = build()


if __name__ == "__main__":
    out = app.invoke({"task": "codegen", "prompt": "fix null currency NPE", "run_id": "run-1"})
    print("served_from:", out["served_from"], "| tokens:", out.get("acc_tokens"),
          "| quality:", round(out.get("quality", 0), 3))
    print("\n--- graph (mermaid) ---\n")
    print(app.get_graph().draw_mermaid())
