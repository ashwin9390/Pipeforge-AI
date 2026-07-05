# Why PipeForge-AI Exist?

=======================================================
THE THREE REAL PROBLEMS THIS SOLVES
=======================================================

--------------------------------------
PROBLEM 1: "Denial of Wallet" Attacks
--------------------------------------

WHAT HAPPENS:
  You run an AI agent to do a task. The agent gets stuck in a loop.
  It calls GPT-4o 200 times before you notice.
  You wake up to a $400 OpenAI bill.
  This is called a "Denial of Wallet" attack.
  It happens by accident more often than by malice.

WHO IT AFFECTS:
  - Any team running LLM agents in production
  - Developers testing complex multi-step workflows
  - Companies building AI automation products

HOW OTHER FRAMEWORKS HANDLE IT:
  LangChain: No built-in cost control
  CrewAI:    No built-in cost control
  AutoGen:   No built-in cost control
  Everyone:  "Just set a max_tokens limit and hope for the best"

HOW PIPEFORGE HANDLES IT:
  - Uses tiktoken for REAL token counts (not a character/4 guess)
  - Reserves budget BEFORE each LLM call (atomic Lua script in Redis)
  - If a call would exceed budget, it is BLOCKED before it fires
  - Warns the user at 80% of budget
  - Hard kills the session at 100%
  - Reconciles token counts vs spend every 5 minutes to catch anomalies

REAL EXAMPLE:
  Budget set to $0.50 per session.
  Session spends $0.39 on collector + processor calls.
  Processor tries to make another call -- estimates $0.15.
  $0.39 + $0.00 reserved + $0.15 estimate = $0.54 > $0.50.
  The call is BLOCKED before it reaches OpenAI.
  No overshoot. No bill shock.


----------------------------------------
PROBLEM 2: Silent Agent Death
----------------------------------------

WHAT HAPPENS:
  An agent starts a task. The container crashes. The API times out.
  The task disappears into the void. Nobody knows it failed.
  The user waits forever. No error. No result. No log.

WHO IT AFFECTS:
  - Any team running AI agents in containers or cloud functions
  - Workflows that take minutes to complete (research, writing, analysis)
  - Systems where uptime matters

HOW OTHER FRAMEWORKS HANDLE IT:
  Most frameworks: If the process dies, the task dies.
  You have to build your own retry logic from scratch.
  Most teams never do. They just restart manually.

HOW PIPEFORGE HANDLES IT:
  - State lives in REDIS, not inside the agent process
  - If a container crashes, the state survives
  - The Sentinel node checks heartbeats every 15 seconds
  - If a session goes silent for 60+ seconds, Sentinel requeues it
  - The requeue is ATOMIC (Lua script) -- no double processing
  - Jitter prevents 100 sessions from re-queueing at the same millisecond
  - After 5 failed requeues, session goes to the Dead Letter Queue for review

REAL EXAMPLE:
  Processor is running a long LLM call on session pf_abc123.
  AWS spot instance gets reclaimed. Container dies.
  30 seconds later, Sentinel sees no heartbeat for pf_abc123.
  60 seconds after death, Sentinel requeues pf_abc123 to queue_processor.
  A new processor container picks it up.
  Task completes. User never knew anything went wrong.


----------------------------------------------
PROBLEM 3: Black Box -- No Observability
----------------------------------------------

WHAT HAPPENS:
  Your agent fails. You have no idea why.
  Was it the LLM response? The tool call? The routing logic?
  You dig through container logs for 2 hours.
  You find a JSON decode error buried in line 40,000.

WHO IT AFFECTS:
  - Every team running agents in production
  - Debugging is the #1 time sink in AI agent development

HOW OTHER FRAMEWORKS HANDLE IT:
  LangGraph: Requires LangSmith (paid, proprietary)
  CrewAI:    No built-in distributed tracing
  AutoGen:   No built-in distributed tracing
  Everyone:  "Add print statements and grep through logs"

HOW PIPEFORGE HANDLES IT:
  - Every node emits OpenTelemetry spans
  - Jaeger UI is bundled (free, open source, port 16686)
  - Each span includes: session ID, node name, tokens used, cost, MCP tools called
  - You can see the full waterfall: collector -> processor -> validator
  - Switch to any backend (Datadog, Grafana, Honeycomb) with ONE env var
  - Security events logged to Redis audit log (last 500 entries)
  - Reconciliation anomalies logged and queryable

REAL EXAMPLE:
  Task fails. Open Jaeger at http://localhost:16686.
  Search for session pf_abc123.
  See: collector took 0.8s, processor took 12.3s, then nothing.
  Click the processor span.
  See: llm.cost_usd=0.0042, mcp.tools_used=brave_search, then ERROR.
  Find: brave_search returned 404. Processor crashed on JSON decode.
  Fix takes 5 minutes instead of 2 hours.


=======================================================
WHO SHOULD USE PIPEFORGE
=======================================================

USE PIPEFORGE IF YOU ARE:
  - A developer who wants to build agent workflows WITHOUT framework lock-in
  - A team that has been burned by unexpected OpenAI bills
  - Building a product that runs many AI tasks in parallel
  - Deploying agents to a Linux server or cloud VPS
  - Someone who prefers code they can read and modify over black-box SDKs

DO NOT USE PIPEFORGE IF YOU:
  - Need a no-code/low-code solution (use Zapier, Make.com, or Coze)
  - Are just experimenting with one-off LLM calls (use the OpenAI SDK directly)
  - Need Microsoft ecosystem integration (use AutoGen or Copilot Studio)
  - Need a fully managed cloud service (use LangSmith or Vertex AI)


=======================================================
HOW IT COMPARES TO WHAT YOU MAY ALREADY KNOW
=======================================================

VS LangChain / LangGraph:
  LangGraph is the closest competitor in architecture (state machine).
  PipeForge differences:
  - No LangChain dependency (lighter, faster to start)
  - Built-in cost circuit breaker (LangGraph has none)
  - Sentinel self-healing (LangGraph has none)
  - Any OTel backend vs LangSmith-only

VS CrewAI:
  CrewAI is great for role-based agent teams.
  PipeForge differences:
  - PipeForge is infrastructure; CrewAI is a higher abstraction
  - CrewAI has no financial guardrails
  - PipeForge exposes Redis state directly (debuggable)
  - You can USE CrewAI agents as workers INSIDE PipeForge

VS AutoGen (Microsoft):
  AutoGen is excellent for conversational multi-agent systems.
  PipeForge differences:
  - PipeForge focuses on pipeline tasks (not conversation)
  - No Microsoft dependency
  - Built-in Dockerized deployment (AutoGen needs your own infra)

VS Building From Scratch:
  PipeForge IS "from scratch" -- it is just pre-assembled.
  You get: Redis queue, heartbeat monitor, cost circuit breaker,
  OTel tracing, MCP tools, security scanner -- without writing
  each one yourself.
  Saves approximately 3-4 weeks of boilerplate engineering.


=======================================================
REAL WORLD USE CASES
=======================================================

1. CONTENT FACTORY
   Input:  A list of 500 blog post topics
   Agents: Collector (web research) -> Processor (draft) -> Validator (quality check)
   Output: 500 reviewed drafts, cost tracked per article
   Value:  Know exactly what each article cost. Auto-retry failed ones.

2. DEVOPS AUTOMATION
   Input:  GitHub PR opened
   Agents: Collector (read PR diff) -> Processor (security scan + review) -> Validator (approve/reject)
   Output: Automated PR review comment
   Value:  Never lose a review to a crashed container.

3. DATA ENRICHMENT PIPELINE
   Input:  10,000 company names
   Agents: Collector (search each company) -> Processor (extract info) -> Validator (verify)
   Output: Structured JSON for each company
   Value:  Budget cap prevents runaway spend on bad data.

4. AI CUSTOMER SUPPORT TRIAGE
   Input:  Support ticket text
   Agents: Collector (look up customer history via MCP) -> Processor (draft response) -> Validator (check tone/policy)
   Output: Draft response for human agent
   Value:  Traces show exactly why each ticket was handled a certain way.

5. RESEARCH ASSISTANT
   Input:  A research question
   Agents: Collector (MCP web search) -> Processor (synthesise) -> Validator (fact check)
   Output: Cited research summary
   Value:  Cost per research query is visible and capped.
