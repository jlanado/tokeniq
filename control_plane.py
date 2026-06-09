"""
TokenIQ — core control plane logic (stdlib only, no external deps).

Pieces:
  - Tier / Task models        : model tiers + task fingerprints
  - route()                   : pick cheapest tier that clears the quality bar
  - SemanticCache             : exact + Jaccard 'semantic' reuse
  - BudgetGovernor            : per-run token cap + loop kill switch
  - run_model()               : simulated model call -> (quality, passed)
  - execute()                 : run a workflow in naive or control-plane mode
  - summarize()               : tokens, cost, cache rate, routing savings, qaCPO

Prices are ILLUSTRATIVE (USD per 1M tokens). Swap for real prices / real
embeddings / real model calls in production - the control flow is unchanged.
"""
from __future__ import annotations
import hashlib
import random
import re
from dataclasses import dataclass


# --------------------------------------------------------------------------
# Model tiers
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Tier:
    name: str
    price_per_1m: float   # USD / 1M tokens (blended in+out, illustrative)
    capability: float     # 0..1 competence score

TIERS = [
    Tier("small",     0.30, 0.70),
    Tier("mid",       3.00, 0.88),
    Tier("frontier", 15.00, 0.97),
]
FRONTIER = TIERS[-1]


def cost(tokens: int, tier: Tier) -> float:
    return tokens / 1_000_000 * tier.price_per_1m


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Task:
    kind: str
    required: float   # capability needed 0..1
    tokens: int       # token footprint when executed

# kind -> (required capability, token footprint)
TASK_SPECS = {
    "classify":    (0.50,  5_000),
    "retrieve":    (0.55, 12_000),
    "summarize":   (0.60,  8_000),
    "write_tests": (0.78, 20_000),
    "review":      (0.80, 25_000),
    "codegen":     (0.85, 32_000),
}

def make_task(kind: str) -> Task:
    req, toks = TASK_SPECS[kind]
    return Task(kind, req, toks)


# --------------------------------------------------------------------------
# Compression stage (pluggable transform). Models a content-aware compressor
# such as Headroom: shrink the input context before it reaches the model.
# (context_fraction = how much of the footprint is compressible input,
#  removed_ratio    = how much of that context the compressor strips)
# Savings vary by content type, matching real compressor benchmarks.
# Swap this for a real adapter (Headroom proxy/library) in production.
# --------------------------------------------------------------------------
COMPRESS = {
    "classify":    (0.55, 0.30),
    "retrieve":    (0.90, 0.85),   # RAG / fetched code context -> highly compressible
    "summarize":   (0.70, 0.45),
    "write_tests": (0.50, 0.50),
    "review":      (0.80, 0.70),   # logs / diffs -> very compressible
    "codegen":     (0.40, 0.45),   # mostly generated output -> less compressible
}

def compress_tokens(task: Task) -> int:
    ctx_frac, removed = COMPRESS[task.kind]
    return round(task.tokens * (1 - ctx_frac * removed))


# --------------------------------------------------------------------------
# Router: cheapest tier that clears the bar (cold-start heuristic).
# In production this becomes a learned table updated by quality-gate outcomes.
# --------------------------------------------------------------------------
def route(task: Task) -> Tier:
    for tier in TIERS:                       # cheapest first
        if tier.capability >= task.required:
            return tier
    return FRONTIER


# --------------------------------------------------------------------------
# Quality gate (simulated). Real version: tests / regex / LLM-judge.
# --------------------------------------------------------------------------
def run_model(task: Task, tier: Tier, rng: random.Random) -> tuple[float, bool]:
    margin = tier.capability - task.required
    if margin >= -0.001:                     # adequately powered
        q = min(1.0, 0.86 + margin * 0.25 + rng.uniform(-0.01, 0.02))
        return round(q, 3), True
    q = max(0.30, tier.capability - 0.15 + rng.uniform(-0.05, 0.05))   # under-powered
    return round(q, 3), q >= 0.80


# --------------------------------------------------------------------------
# Semantic cache: exact hash hit + Jaccard token-overlap 'semantic' hit.
# Real version: embed prompt -> ANN search (pgvector/FAISS), cosine >= 0.95.
# --------------------------------------------------------------------------
def _tokset(prompt: str) -> set[str]:
    return set(re.findall(r"\w+", prompt.lower()))

class SemanticCache:
    def __init__(self, threshold: float = 0.85):
        self.exact: dict[str, float] = {}
        self.entries: list[tuple[str, set[str], float]] = []
        self.threshold = threshold
        self.hits = 0
        self.lookups = 0

    def _key(self, kind: str, prompt: str) -> str:
        return kind + "|" + hashlib.sha1(prompt.encode()).hexdigest()

    def get(self, kind: str, prompt: str):
        self.lookups += 1
        k = self._key(kind, prompt)
        if k in self.exact:
            self.hits += 1
            return self.exact[k]
        ts = _tokset(prompt)
        for ekind, ets, val in self.entries:
            if ekind != kind or not ts or not ets:
                continue
            jaccard = len(ts & ets) / len(ts | ets)
            if jaccard >= self.threshold:
                self.hits += 1
                return val
        return None

    def put(self, kind: str, prompt: str, val: float):
        self.exact[self._key(kind, prompt)] = val
        self.entries.append((kind, _tokset(prompt), val))


# --------------------------------------------------------------------------
# Budget governor: per-run token cap + runaway-loop kill switch.
# --------------------------------------------------------------------------
class BudgetGovernor:
    def __init__(self, token_budget: int = 800_000, loop_limit: int = 3):
        self.token_budget = token_budget
        self.loop_limit = loop_limit
        self.tokens_used = 0
        self.seen: dict[tuple[str, str], int] = {}
        self.killed = False
        self.kill_reason: str | None = None

    def check_loop(self, kind: str, prompt: str) -> bool:
        """Increment seen counter; return False if loop limit exceeded."""
        sig = (kind, prompt)
        self.seen[sig] = self.seen.get(sig, 0) + 1
        if self.seen[sig] > self.loop_limit:
            self.killed = True
            self.kill_reason = f"loop detected on '{kind}' (>{self.loop_limit}x identical calls)"
            return False
        return True

    def check_budget(self, next_tokens: int) -> bool:
        """Return False if adding next_tokens would exceed the token budget."""
        if self.tokens_used + next_tokens > self.token_budget:
            self.killed = True
            self.kill_reason = "per-run token budget exceeded"
            return False
        return True

    def allow(self, kind: str, prompt: str, next_tokens: int) -> bool:
        """Combined check used by the simulation loop in execute()."""
        return self.check_loop(kind, prompt) and self.check_budget(next_tokens)

    def add(self, tokens: int):
        self.tokens_used += tokens


# --------------------------------------------------------------------------
# Execution engine
# --------------------------------------------------------------------------
@dataclass
class StepLog:
    kind: str
    tier: str
    tokens: int
    cost: float
    quality: float
    cached: bool = False
    escalated: bool = False
    killed: bool = False
    kill_reason: str | None = None


def execute(steps, *, use_router: bool, use_cache: bool, use_governor: bool,
            use_compression: bool = False, token_budget: int = 800_000, seed: int = 7):
    rng = random.Random(seed)
    cache = SemanticCache() if use_cache else None
    gov = BudgetGovernor(token_budget=token_budget) if use_governor else None
    logs: list[StepLog] = []

    for kind, prompt in steps:
        task = make_task(kind)
        footprint = compress_tokens(task) if use_compression else task.tokens

        if cache is not None:
            hit = cache.get(kind, prompt)
            if hit is not None:
                logs.append(StepLog(kind, "cache", 0, 0.0, hit, cached=True))
                continue

        if gov is not None and not gov.allow(kind, prompt, footprint):
            logs.append(StepLog(kind, "-", 0, 0.0, 0.0,
                                killed=True, kill_reason=gov.kill_reason))
            break

        tier = route(task) if use_router else FRONTIER
        q, passed = run_model(task, tier, rng)
        tokens = footprint
        escalated = False

        if use_router and not passed:                # quality gate -> escalate one tier
            idx = TIERS.index(tier)
            if idx < len(TIERS) - 1:
                tier = TIERS[idx + 1]
                q, passed = run_model(task, tier, rng)
                tokens += footprint                  # billed for the failed try + retry
                escalated = True

        c = cost(tokens, tier)
        if gov is not None:
            gov.add(tokens)
        if cache is not None:
            cache.put(kind, prompt, q)
        logs.append(StepLog(kind, tier.name, tokens, round(c, 4), q, escalated=escalated))

    return logs, cache, gov


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def summarize(logs, cache, *, outcomes: int = 1) -> dict:
    executed = [l for l in logs if not l.killed and l.tier != "cache"]
    tokens = sum(l.tokens for l in executed)
    total_cost = sum(l.cost for l in executed)

    quals = [l.quality for l in executed if l.quality > 0]
    Q = sum(quals) / len(quals) if quals else 0.0

    # counterfactual: same billed tokens, all on frontier
    frontier_cost = sum(cost(l.tokens, FRONTIER) for l in executed)
    routing_savings = (1 - total_cost / frontier_cost) if frontier_cost else 0.0

    hit_rate = (cache.hits / cache.lookups) if cache and cache.lookups else 0.0
    qa_cpo = (total_cost / (outcomes * Q)) if Q else float("inf")

    killed = next((l for l in logs if l.killed), None)
    return {
        "tokens": tokens,
        "cost": round(total_cost, 4),
        "quality": round(Q, 3),
        "cache_hit_rate": round(hit_rate, 3),
        "routing_savings": round(routing_savings, 3),
        "qa_cpo": round(qa_cpo, 4),
        "killed": bool(killed),
        "kill_reason": killed.kill_reason if killed else None,
        "outcomes": outcomes,
    }
