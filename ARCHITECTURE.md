# Reference architecture — TokenIQ

A single governed gateway sits in front of every LLM call. It is the one place that can
**see, cut, cap, and price** token spend across your organization's agents and engineers.

> **Control plane, not compressor.** Compression is one pluggable stage. The defensible
> layer is routing + governance + outcome pricing tuned on proprietary telemetry.

![TokenIQ architecture](docs/ai-token-control-plane.png)

---

## 1. Layered view

```mermaid
graph TD
  subgraph PROD[Producers]
    A1[Coding agents<br/>Claude Code · Cursor · Codex]
    A2[Agentic frameworks<br/>LangChain · internal]
    A3[CI / batch jobs]
  end

  subgraph CP[Control plane — single governed gateway]
    direction LR
    G[Meter + tag<br/>team · run · corr-id] --> C[Semantic cache]
    C --> GV[Budget governor<br/>+ kill switch]
    GV --> R[Router<br/>cheapest tier passing bar]
    R --> CO[Compression adapter<br/>Headroom-style · pluggable]
    CO --> M{{Model dispatch}}
    M --> QG[Quality gate<br/>+ escalation]
  end

  subgraph PROV[Model providers]
    P1[small]
    P2[mid]
    P3[frontier]
    P4[internal LLM provider / Bedrock]
  end

  subgraph DATA[Data & state]
    S1[(Vector store<br/>pgvector / FAISS)]
    S2[(Redis<br/>run + governor state)]
    S3[(Postgres<br/>telemetry + cost ledger)]
  end

  subgraph LOOP[Learning & economics loop — the moat]
    L1[Router trainer<br/>learns model-per-fingerprint]
    L2[Outcome linker<br/>Git / Jira / CI]
    L3[FinOps<br/>chargeback · budgets · dashboards]
  end

  PROD --> G
  M --> PROV
  C -.cache index.-> S1
  GV -.run state.-> S2
  QG -.telemetry.-> S3
  S3 --> L1
  S3 --> L2
  L1 -.updates policy.-> R
  L2 --> L3
```

The hot path is `meter → cache → govern → route → compress → model → quality gate`.
Everything to the right of the gateway (stores, trainer, linker, FinOps) is asynchronous.

---

## 2. Request lifecycle

```mermaid
sequenceDiagram
  participant A as Agent
  participant G as Gateway
  participant C as Cache
  participant GV as Governor
  participant R as Router
  participant CO as Compressor
  participant M as Model
  participant QG as Quality gate
  participant T as Telemetry
  A->>G: request (task, prompt, run_id)
  G->>C: lookup (exact + semantic)
  alt cache hit
    C-->>A: cached answer (0 tokens)
  else miss
    G->>GV: allowed? (budget + loop check)
    alt over budget or loop detected
      GV-->>A: KILLED (blast-radius cap)
    else allowed
      GV->>R: route(task)
      R->>CO: compress payload
      CO->>M: call cheapest tier that clears the bar
      M->>QG: output + quality
      opt fails quality bar
        QG->>M: escalate one tier, re-run
      end
      QG-->>A: response
      QG->>T: log tokens · cost · quality · run_id
    end
  end
```

---

## 3. Component responsibilities

| Component | Responsibility | Demo file | Production swap |
|---|---|---|---|
| Gateway | OpenAI-compatible ingress; meter + tag every call (`team`, `agent_run_id`, `correlation_id`) | `gateway.py` | LiteLLM / Envoy + auth |
| Semantic cache | Return prior answer on exact or semantic match | `control_plane.py` (`SemanticCache`) | pgvector / FAISS, cosine ≥ 0.95 |
| Budget governor | Per-run token cap + runaway-loop kill switch | `control_plane.py` (`BudgetGovernor`) | Redis-backed counters + circuit breaker |
| Router | Pick cheapest tier whose capability clears the task bar | `control_plane.py` (`route`) | Learned policy (contextual bandit) |
| Compression adapter | Shrink input context before the model | `control_plane.py` (`compress_tokens`) | **Headroom** proxy/library |
| Quality gate | Validate output; escalate a tier on failure | `control_plane.py` (`run_model` + escalation) | Tests / regex / LLM-judge |
| Telemetry | Log tokens, cost, quality, IDs | `gateway.py` (`/metrics`) | Postgres + stream |
| Router trainer | Learn cheapest model-per-fingerprint from quality outcomes | (loop) | Offline job over telemetry — **the moat** |
| Outcome linker | Join `correlation_id` → SDLC events for cost-per-outcome | (loop) | Git / Jira / CI webhooks |

---

## 4. Data & state

| Store | Holds | Why |
|---|---|---|
| Redis | Per-run token counters, governor loop state | Low-latency, hot-path reads/writes |
| Vector store (pgvector / FAISS) | Cache embeddings, router task fingerprints | Semantic lookup + routing memory |
| Postgres | Telemetry, cost ledger, chargeback | System of record, audit, FinOps |

---

## 5. Deployment topology (enterprise fit)

- **Centrally operated, not per-developer.** The gateway runs as a governed service; agents
  point their OpenAI-compatible base URL at it. This fits a regulated, audited, sandboxed
  enterprise runtime better than a local per-developer process.
- **Sidecar option** for latency-sensitive teams: cache + governor as a local sidecar, telemetry
  shipped centrally.
- **Stateless gateway, externalized state** (Redis + Postgres) so it scales horizontally behind a load balancer.

---

## 6. Cross-cutting concerns

| Concern | How it's handled |
|---|---|
| Audit | Every call logged with team/run/correlation IDs in the cost ledger |
| RBAC & quotas | Per-team budgets enforced at the governor |
| Data residency | Compression + cache run inside the trust boundary; no payloads leave |
| Safety | Kill switch caps runaway agent spend (blast radius) |
| Cost attribution | Chargeback/showback by team and by outcome |

---

## 7. The four levers

| Lever | Cuts | Mechanism |
|---|---|---|
| Semantic cache | Tokens (skips calls) | Reuse prior answers |
| Router | Cost (cheaper tier) | Cheapest model that clears the quality bar |
| Compression | Tokens (smaller payload) | Content-aware context shrink (pluggable) |
| Governor | **Risk** (blast radius) | Per-run cap + runaway kill switch |

Three levers cut the bill; one caps the risk. Verified: cost per resolved ticket
**$4.38 → $0.31 (−93%)**, quality held at 0.89.
