# PipeForge -- BlackboardClient
# Atomic Redis operations via Lua scripts.
# Improvements from production critique:
#   - Atomic pre-call budget reservation (prevents overshoot)
#   - Dead-letter queue for tasks exceeding max retries
#   - Idempotency key support
#   - Per-tenant key namespacing

import redis
import json
import time
from typing import Optional

# -- Lua: atomic check-then-push (no double-enqueue) ----------------------
LUA_SAFE_PUSH = """
local already = redis.call('LPOS', KEYS[1], ARGV[1])
if already == false then
    redis.call('LPUSH', KEYS[1], ARGV[1])
    return 1
end
return 0
"""

# -- Lua: atomic Sentinel requeue (prevents double-recovery) --------------
LUA_SAFE_REQUEUE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
if state['next_step'] ~= ARGV[1] then return 0 end
if state['status'] == 'KILLED_BY_BUDGET' then return 0 end
if state['status'] == 'BLOCKED_SECURITY' then return 0 end
if state['status'] == 'COMPLETED' then return 0 end
state['last_heartbeat'] = tonumber(ARGV[2])
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', 86400)
redis.call('LPUSH', KEYS[2], ARGV[3])
return 1
"""

# -- Lua: atomic pre-call budget reservation -------------------------------
# Reserves estimated token cost BEFORE the LLM call.
# Returns 1 if reservation succeeded, 0 if it would exceed budget.
# Keys[1] = session_id
# ARGV[1] = estimated_cost (float as string)
# ARGV[2] = max_budget (float as string)
LUA_RESERVE_BUDGET = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
local current = tonumber(state['current_spend'] or 0)
local reserved = tonumber(state['reserved_spend'] or 0)
local estimate = tonumber(ARGV[1])
local max_budget = tonumber(ARGV[2])

-- Check if reservation would exceed budget
if (current + reserved + estimate) > max_budget then
    return 0  -- Reject: would exceed budget
end

-- Reserve the estimated spend
state['reserved_spend'] = reserved + estimate
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', 86400)
return 1
"""

# -- Lua: commit actual spend, release reservation -------------------------
# Called AFTER the LLM call with real token counts.
# Keys[1] = session_id
# ARGV[1] = actual_cost (float as string)
# ARGV[2] = estimated_cost that was reserved (to release)
# ARGV[3] = input_tokens (int)
# ARGV[4] = output_tokens (int)
LUA_COMMIT_SPEND = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
local actual   = tonumber(ARGV[1])
local reserved = tonumber(ARGV[2])
local inp_tok  = tonumber(ARGV[3])
local out_tok  = tonumber(ARGV[4])

state['current_spend']   = (tonumber(state['current_spend'] or 0)) + actual
state['reserved_spend']  = math.max(0, (tonumber(state['reserved_spend'] or 0)) - reserved)
state['current_tokens']  = (tonumber(state['current_tokens'] or 0)) + inp_tok + out_tok
state['last_heartbeat']  = tonumber(ARGV[5] or redis.call('TIME')[1])

redis.call('SET', KEYS[1], cjson.encode(state), 'EX', 86400)
return 1
"""

# -- Lua: idempotent step completion ---------------------------------------
# Only advances next_step if current step matches expected (prevents replays)
LUA_ADVANCE_STEP = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
if state['next_step'] ~= ARGV[1] then return 0 end  -- Already advanced
state['next_step'] = ARGV[2]
state['last_heartbeat'] = tonumber(ARGV[3])
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', 86400)
return 1
"""


class BlackboardClient:
    """
    Thread-safe, atomic wrapper around Redis for PipeForge.
    All session mutations go through this class.
    """

    def __init__(self, host: str = "localhost", port: int = 6379,
                 tenant: str = "default"):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)
        self.tenant = tenant
        # Register Lua scripts
        self._safe_push       = self.r.register_script(LUA_SAFE_PUSH)
        self._safe_requeue    = self.r.register_script(LUA_SAFE_REQUEUE)
        self._reserve_budget  = self.r.register_script(LUA_RESERVE_BUDGET)
        self._commit_spend    = self.r.register_script(LUA_COMMIT_SPEND)
        self._advance_step    = self.r.register_script(LUA_ADVANCE_STEP)

    def _key(self, sid: str) -> str:
        """Tenant-namespaced session key."""
        return f"pf:{self.tenant}:{sid}"

    # -- Basic ops --------------------------------------------------------

    def get_state(self, sid: str) -> Optional[dict]:
        raw = self.r.get(self._key(sid)) or self.r.get(sid)  # backward compat
        return json.loads(raw) if raw else None

    def set_state(self, sid: str, state: dict, ttl_hours: int = 24):
        """Write with 24h safety TTL. Keyed per tenant so two tenants that happen to
        share a session id do not clobber each other."""
        self.r.set(self._key(sid), json.dumps(state), ex=ttl_hours * 3600)

    def delete(self, sid: str):
        self.r.delete(sid)
        self.r.delete(self._key(sid))

    def all_session_ids(self) -> list[str]:
        prefix = self._key("")  # "pf:{tenant}:"
        sids = []
        for key in self.r.scan_iter(match=f"{prefix}*", count=200):
            sids.append(key[len(prefix):])
        return sids

    # -- Queue ops --------------------------------------------------------

    def safe_push(self, queue: str, sid: str) -> bool:
        result = self._safe_push(keys=[queue], args=[sid])
        return bool(result)

    def blocking_pop(self, queue: str, timeout: int = 5) -> Optional[str]:
        result = self.r.brpop(queue, timeout=timeout)
        return result[1] if result else None

    def queue_length(self, queue: str) -> int:
        return self.r.llen(queue)

    def remove_from_queue(self, queue: str, sid: str):
        self.r.lrem(queue, 0, sid)

    def purge_from_all_queues(self, sid: str):
        queues = [
            "queue_collector", "queue_collector_priority",
            "queue_processor", "queue_processor_retry",
            "queue_validator"
        ]
        for q in queues:
            self.remove_from_queue(q, sid)

    def send_to_dead_letter(self, sid: str, reason: str):
        """Move a session to the dead-letter queue with reason recorded."""
        state = self.get_state(sid)
        if state:
            state["dlq_reason"] = reason
            state["dlq_at"]     = time.strftime("%Y-%m-%d %H:%M:%S")
            state["status"]     = "DEAD_LETTER"
            state["next_step"]  = "FINISH"
            self.set_state(sid, state)
        self.r.lpush("queue_dead_letter", json.dumps({"sid": sid, "reason": reason,
                                                       "ts": time.time()}))
        self.purge_from_all_queues(sid)

    # -- Budget reservation (pre-call atomic check) ------------------------

    def reserve_budget(self, sid: str, estimated_cost: float,
                       max_budget: float) -> bool:
        """
        Atomically reserve estimated_cost before making an LLM call.
        Returns True if reservation succeeded (safe to proceed).
        Returns False if it would exceed budget (abort the call).
        """
        result = self._reserve_budget(
            keys=[self._key(sid)],
            args=[str(estimated_cost), str(max_budget)]
        )
        return bool(result)

    def commit_spend(self, sid: str, actual_cost: float,
                     estimated_cost: float, input_tokens: int,
                     output_tokens: int,
                     node: str = "", model: str = "",
                     call_purpose: str = "", latency_ms: float = 0.0,
                     error: str = "") -> bool:
        """
        Commit actual spend after LLM call, release reservation.
        ALSO writes a structured entry to the per-session cost ledger
        (Redis Stream) so every individual call is auditable, not just
        the running total.
        Call this in the finally block of every LLM invocation.
        """
        result = self._commit_spend(
            keys=[self._key(sid)],
            args=[str(actual_cost), str(estimated_cost),
                  str(input_tokens), str(output_tokens),
                  str(time.time())]
        )

        # Append to the cost ledger regardless of reservation outcome --
        # we want a record even of failed/aborted calls for audit purposes.
        self.log_cost_event(
            sid=sid, node=node, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=actual_cost, call_purpose=call_purpose,
            latency_ms=latency_ms, error=error
        )

        return bool(result)

    # -- Cost ledger (Redis Stream -- append-only audit trail) ------------

    def _ledger_key(self, sid: str) -> str:
        return f"pf:ledger:{sid}"

    def log_cost_event(self, sid: str, node: str, model: str,
                       input_tokens: int, output_tokens: int,
                       cost: float, call_purpose: str = "",
                       latency_ms: float = 0.0, error: str = "") -> str:
        """
        Append one structured entry to the session's cost ledger.
        Uses a Redis Stream (XADD) -- purpose-built for ordered,
        append-only event logs with each entry getting a unique,
        time-ordered ID automatically.
        Returns the entry ID.
        """
        entry = {
            "node":           node or "unknown",
            "model":          model or "unknown",
            "input_tokens":   str(input_tokens),
            "output_tokens":  str(output_tokens),
            "total_tokens":   str(input_tokens + output_tokens),
            "cost_usd":       f"{cost:.8f}",
            "call_purpose":   call_purpose or "",
            "latency_ms":     f"{latency_ms:.1f}",
            "error":          error or "",
            "wall_time":      time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        entry_id = self.r.xadd(self._ledger_key(sid), entry, maxlen=500)

        # 24h TTL on the ledger stream itself (mirrors session TTL)
        self.r.expire(self._ledger_key(sid), 24 * 3600)
        return entry_id

    def get_cost_ledger(self, sid: str, count: int = 100) -> list[dict]:
        """
        Retrieve the full ordered cost ledger for a session.
        Returns list of dicts, oldest first.
        """
        try:
            raw_entries = self.r.xrange(self._ledger_key(sid), count=count)
        except Exception:
            return []

        ledger = []
        for entry_id, fields in raw_entries:
            ledger.append({
                "entry_id":      entry_id,
                "node":          fields.get("node", ""),
                "model":         fields.get("model", ""),
                "input_tokens":  int(fields.get("input_tokens", 0)),
                "output_tokens": int(fields.get("output_tokens", 0)),
                "total_tokens":  int(fields.get("total_tokens", 0)),
                "cost_usd":      float(fields.get("cost_usd", 0.0)),
                "call_purpose":  fields.get("call_purpose", ""),
                "latency_ms":    float(fields.get("latency_ms", 0.0)),
                "error":         fields.get("error", ""),
                "wall_time":     fields.get("wall_time", ""),
            })
        return ledger

    def ledger_summary(self, sid: str) -> dict:
        """
        Aggregate the cost ledger into a per-node, per-model breakdown.
        Answers: "which node/call actually drove the spend?"
        """
        ledger = self.get_cost_ledger(sid)
        if not ledger:
            return {"total_calls": 0, "total_cost": 0.0, "by_node": {}, "by_model": {}}

        by_node  = {}
        by_model = {}
        total_cost = 0.0

        for entry in ledger:
            node  = entry["node"]
            model = entry["model"]
            cost  = entry["cost_usd"]
            total_cost += cost

            if node not in by_node:
                by_node[node] = {"calls": 0, "cost": 0.0, "tokens": 0}
            by_node[node]["calls"]  += 1
            by_node[node]["cost"]   += cost
            by_node[node]["tokens"] += entry["total_tokens"]

            if model not in by_model:
                by_model[model] = {"calls": 0, "cost": 0.0, "tokens": 0}
            by_model[model]["calls"]  += 1
            by_model[model]["cost"]   += cost
            by_model[model]["tokens"] += entry["total_tokens"]

        return {
            "total_calls": len(ledger),
            "total_cost":  round(total_cost, 6),
            "by_node":     by_node,
            "by_model":    by_model,
            "first_call":  ledger[0]["wall_time"] if ledger else None,
            "last_call":   ledger[-1]["wall_time"] if ledger else None,
        }

    # -- Idempotent step advancement ---------------------------------------

    def advance_step(self, sid: str, from_step: str, to_step: str) -> bool:
        """
        Atomically advance next_step from from_step to to_step.
        No-op if step has already been advanced (idempotent replay safety).
        """
        result = self._advance_step(
            keys=[self._key(sid)],
            args=[from_step, to_step, str(time.time())]
        )
        return bool(result)

    # -- Sentinel requeue -------------------------------------------------

    def safe_requeue(self, sid: str, expected_step: str) -> bool:
        queue_key = f"queue_{expected_step}"
        result = self._safe_requeue(
            keys=[self._key(sid), queue_key],
            args=[expected_step, str(time.time()), sid]
        )
        return bool(result)

    # -- Idempotency keys -------------------------------------------------

    def set_idempotency_key(self, key: str, value: str, ttl_sec: int = 86400):
        """Mark an operation as completed. Returns False if already done."""
        return bool(self.r.set(f"idem:{key}", value, nx=True, ex=ttl_sec))

    def check_idempotency_key(self, key: str) -> Optional[str]:
        return self.r.get(f"idem:{key}")

    # -- Append log -------------------------------------------------------

    def append_log(self, sid: str, message: str) -> bool:
        state = self.get_state(sid)
        if not state:
            return False
        state["memory"].append(message)
        state["last_heartbeat"] = time.time()
        self.set_state(sid, state)
        return True

    def raw(self) -> redis.Redis:
        return self.r