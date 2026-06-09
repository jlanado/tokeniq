"""
TokenIQ — HTTP gateway (the single chokepoint every call routes through).

Run (simulation mode, no API keys, no network needed):
    pip install fastapi uvicorn
    uvicorn gateway:app --reload
    # then:
    curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
      -d '{"task":"codegen","agent_run_id":"run-1","prompt":"fix null currency NPE"}' | python -m json.tool
    curl -s localhost:8000/metrics | python -m json.tool

Endpoints:
    POST /v1/chat/completions  - metered, routed, cached, governed model call
    GET  /metrics              - live tokens / cost / cache rate / routing savings / qaCPO
    POST /reset                - clear telemetry + per-run governor state

Going live: replace `call_backend()` with a real provider call (LiteLLM, Bedrock,
internal LLM provider). Everything else - routing, cache, governor, telemetry - is unchanged.
The gateway is the only place that touches a model, so it is the only place to govern.
"""
from __future__ import annotations
import random
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from control_plane import (
    TIERS, FRONTIER, TASK_SPECS, make_task, route, run_model, cost,
    SemanticCache, BudgetGovernor, compress_tokens,
)

app = FastAPI(title="TokenIQ")

# Compression stage: pluggable transform (reference adapter = Headroom-style
# content-aware compressor). Set False to bypass.
COMPRESSION_ON = True

# process-wide state (use Redis/Postgres in production)
CACHE = SemanticCache()
GOVERNORS: dict[str, BudgetGovernor] = {}      # per agent_run_id
TELEMETRY: list[dict] = []
RNG = random.Random(7)


class ChatRequest(BaseModel):
    task: str                       # classify | retrieve | summarize | write_tests | review | codegen
    prompt: str
    agent_run_id: str = "default"
    team: str = "platform"


def call_backend(task, tier):
    """Stand-in for a real model call. Returns (quality, tokens)."""
    q, passed = run_model(task, tier, RNG)
    return q, passed, task.tokens


@app.get("/")
def root():
    return RedirectResponse(url="/docs")


@app.post("/v1/chat/completions")
def chat(req: ChatRequest):
    if req.task not in TASK_SPECS:
        raise HTTPException(status_code=422, detail=f"unknown task '{req.task}'; valid: {list(TASK_SPECS)}")
    task = make_task(req.task)
    gov = GOVERNORS.setdefault(req.agent_run_id, BudgetGovernor())

    # 1) loop detection — runs before cache so repeat calls are counted even when cached
    if not gov.check_loop(req.task, req.prompt):
        return {"served_from": "killed", "reason": gov.kill_reason,
                "task": req.task, "run": req.agent_run_id}

    # 2) cache — after loop check, before budget (cache hits are free)
    hit = CACHE.get(req.task, req.prompt)
    if hit is not None:
        rec = {"task": req.task, "team": req.team, "run": req.agent_run_id,
               "tier": "cache", "tokens": 0, "cost": 0.0, "quality": hit, "cached": True}
        TELEMETRY.append(rec)
        return {"served_from": "cache", **rec}

    # 3) token budget — only for cache misses (model calls consume budget)
    footprint = compress_tokens(task) if COMPRESSION_ON else task.tokens
    if not gov.check_budget(footprint):
        return {"served_from": "killed", "reason": gov.kill_reason,
                "task": req.task, "run": req.agent_run_id}

    # 4) route + 5) quality gate w/ escalation  (compression applied to footprint)
    tier = route(task)
    q, passed, _ = call_backend(task, tier)
    tokens = footprint
    escalated = False
    if not passed:
        idx = TIERS.index(tier)
        if idx < len(TIERS) - 1:
            tier = TIERS[idx + 1]
            q, passed, _ = call_backend(task, tier)
            tokens += footprint
            escalated = True

    c = cost(tokens, tier)
    gov.add(tokens)
    CACHE.put(req.task, req.prompt, q)

    raw_tokens = task.tokens * (2 if escalated else 1)
    rec = {"task": req.task, "team": req.team, "run": req.agent_run_id,
           "tier": tier.name, "tokens": tokens, "raw_tokens": raw_tokens,
           "cost": round(c, 5), "quality": q, "escalated": escalated, "cached": False}
    TELEMETRY.append(rec)
    return {"served_from": "model", **rec}


@app.get("/metrics")
def metrics():
    billed = [r for r in TELEMETRY if not r.get("cached")]
    tokens = sum(r["tokens"] for r in billed)
    raw_tokens = sum(r.get("raw_tokens", r["tokens"]) for r in billed)
    total = sum(r["cost"] for r in billed)
    quals = [r["quality"] for r in billed]
    Q = sum(quals) / len(quals) if quals else 0.0
    frontier_cost = sum(cost(r["tokens"], FRONTIER) for r in billed)
    cached = [r for r in TELEMETRY if r.get("cached")]
    hit_rate = len(cached) / len(TELEMETRY) if TELEMETRY else 0.0
    return {
        "calls": len(TELEMETRY),
        "tokens": tokens,
        "compression_savings": round(1 - tokens / raw_tokens, 3) if raw_tokens else 0.0,
        "cost_usd": round(total, 4),
        "quality": round(Q, 3),
        "cache_hit_rate": round(hit_rate, 3),
        "routing_savings": round(1 - total / frontier_cost, 3) if frontier_cost else 0.0,
        "quality_adjusted_cost_per_outcome": round(total / Q, 4) if Q else None,
    }


@app.post("/reset")
def reset():
    CACHE.__init__()
    GOVERNORS.clear()
    TELEMETRY.clear()
    return {"ok": True}
