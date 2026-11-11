# cKunAgentSSH

[English](README.en.md) · [简体中文](README.md)

An AI-driven SSH operations terminal that brings connection management, remote / local terminals, SFTP, Docker, Git, real-time monitoring, scheduled tasks, SSH tunnels, operations audit, and an AI Agent into a single IDE-style desktop workspace. The AI executes operations over the same host connections, constrained by approvals and auditing. A built-in cKunBot personal bot (persona-driven chat) shares the model configuration and memory foundation with the ops Agent but stays isolated from it.

**Frontend**: Electron + Vue 3 + TypeScript　**AI backend**: Python FastAPI　**Release target**: Windows x64

[Features](#features) · [Tech stack](#tech-stack) · [Architecture](#architecture) · [Project structure and engineering conventions](#project-structure-and-engineering-conventions) · [Quick start](#quick-start) · [Packaging and release](#packaging-and-release) · [Logs and troubleshooting](#logs-and-troubleshooting) · [Access gateway](#access-gateway)

## Features

The title bar provides 11 feature entries plus a Settings page, serving as the single entry point for all application capabilities.

| Entry | Page | Shortcut | Description |
| --- | --- | --- | --- |
| AI | Task console (home) | `Ctrl+Shift+B` | Agent runs, approvals, and traces; badge shows pending approvals / running / unread |
| Docker | Remote Docker | `Ctrl+Shift+6` | Containers, images, volumes, networks |
| K8s | K8s clusters | `Ctrl+Shift+0` | Pods / workloads / services / nodes / events and scaling via kubectl; attached to a remote SSH session |
| Git | Git tools | `Ctrl+Shift+7` | GitHub acceleration and repository management |
| SFTP | File transfer | `Ctrl+Shift+2` | Dual-pane file management with resumable transfers |
| Terminal | Remote management | `Ctrl+Shift+1` | Remote / local multi-tab terminals |
| Monitoring | Monitoring dashboard | `Ctrl+Shift+3` | Host and GPU metrics |
| Schedule | Scheduled tasks | `Ctrl+Shift+4` | Ops / daily scheduled tasks and failure diagnosis |
| Tunnel | SSH tunnels | `Ctrl+Shift+5` | TCP / SOCKS5 / Dynamic forwarding |
| Traces | Ops traces | `Ctrl+Shift+8` | Change and Agent run records |
| Bot | cKunBot personal bot | `Ctrl+Shift+9` | Persona chat, persona editing, multi-channel access |
| Settings | Settings | `Ctrl+,` | Models, chat, appearance, skills, agent, memory, ops |

### AI capabilities

The app has no separate "chat mode". Conversations and executions in the task console all enter the Agent run endpoint (`POST /agent/run`, SSE); the backend handles intent detection, context assembly, tool calls, approvals, and auditing. Auxiliary capabilities such as structuring, commit messages, and command prediction use their own lightweight stateless endpoints.

**Task console and run flow**

- The task console is the AI's only conversation surface: the input area carries the target host, target / plan posture, skills, text attachments, and model selection; while running, the send button becomes a stop button and the draft is preserved.
- The AI icon in the title bar and `Ctrl+Shift+B` switch between the task console and any page; the badge prioritizes "pending approval > running > unread".
- Execution does not flood the screen: approvals and clarifications are collected above the input box; routine run status and tool-call details are not expanded in the message stream and are replayed by `run_id` on the ops traces page. At the end of the message stream, the run notice area keeps only task plans, warnings, human-help requests, and errors.
- Supports chain-of-thought display, streaming Markdown rendering, copy, and conversation history; long conversations render in batches to avoid mounting all messages at once.
- SSE heartbeat and stall detection run in parallel: the backend sends a heartbeat every 15 seconds, and if no bytes arrive for 90 consecutive seconds the connection is considered stalled, so a partial response is never treated as complete.

**Harness and execution loop**

- The ReAct decision loop is driven by the Harness; the interceptor chain covers skill injection, filesystem protection, loop detection, permission gates, auditing, dynamic step budgets, cost and wall-clock circuit breakers, confidence, reflection and review, clarification, and plan discipline.
- Step budgets are allocated dynamically by task complexity; as the limit approaches, the model is prompted to converge, and on the final iteration tools are physically disabled to avoid "fake completion"; completion verification and goal self-check run before finishing.
- Before a run, read-only probing detects the target machine's distro, init, package manager, and login identity and writes them into the host profile; task nature (read-only / potential change) is injected as universal metadata up front, while ops methodology rides the Ops Expert identity section in full.
- Reflection is triggered after a run finishes or fails; the policy engine evolves iteration limits, temperature, and thinking switches based on recent completion, efficiency, robustness, and cost, with bounds to prevent oscillation. A background heartbeat reconciles memory and trims decay every 5 minutes, and distills repeated failure patterns into experience candidates.

**Tools and remote execution**

- 35 built-in tools covering shell execution, web search, to-dos, skill loading, sub-agents, host and process observation, multi-session inspection, service management, scheduled tasks, log reading and cleanup, remote file read / write / edit / search, image viewing, background jobs, memory self-editing, the Runbook experience library, rollback snapshot restore, and persisting Spec / Plan / Goal documents.
- Tools are labeled by risk level (safe / medium / dangerous) and can be enabled or disabled by risk group in Settings.
- The Agent's remote operations return to the Electron main process through the WebSocket reverse channel `/ws/bridge`, then execute via ssh2; when the channel is down the system fails closed and in-flight requests are not retried.
- Permission and delivery are separated: permission cards decide "may this execute", delivery cards decide "should this be written to a visible terminal"; both appear only in the approval dock.

**Memory system**

- Hybrid retrieval: BM25 from SQLite FTS5 and vector KNN are fused via RRF; writes are deduplicated by semantic similarity, and recall is bounded by an injection budget.
- A resident profile block stores user preferences, Agent rules, and per-host profiles; host profiles come from read-only probing and are refreshed periodically.
- Memory writes go through a background task queue, and a Reconciler decides whether to add, update, delete, or ignore; it supports auditing, history rollback, and decay for long-unused entries.
- In Settings you can browse, search, pin, edit, and invalidate memories, and view reconciliation tasks and profile history; troubleshooting experience can be distilled and retrieved as Runbooks.

**Permissions and approvals**

- Approval modes are "confirm every dangerous operation" and "model decides, still asks when unsure"; the approval dock queues clarifications, permissions, and terminal delivery in that order. Low-risk permission batches outside production can be approved in one click; dangerous operations, production environments, and unknown-risk items must be confirmed one by one.
- Approval cards show the risk level, target host, environment, full command, a Chinese consequence description (marked as reference only when model-generated), and the write diff; actions are allow once, allow for this session, always allow, and deny. Dangerous operations do not offer "allow for this session" — only one-time authorization or permanent registration; an explicit denial takes precedence over any cache during that run.
- Authorization is scoped by host: a command registered on a test machine is not automatically exempt on a production machine. Permanent authorizations support TTL; "allow once" is strictly bound to the current `run_id` and never leaks to later runs.
- Dangerous operations in production still pop cards for confirmation one by one in interactive sessions, but only offer "allow once" and "always allow"; clicking "always allow" writes to the allowlist, and model adjudication does not apply. Unattended runs (scheduled tasks, headless execution, runbooks) cannot show cards and only honor pre-registered allowlists. After an approval times out, the pending item is retained and never silently allowed; after approval the AI must still re-issue the same command.
- When write steps are added to a structured task, auto-approval falls back to per-step confirmation.

**Posture, clarification, and structuring**

- The target and plan postures are shown as chips. The plan posture sends `workflow_mode=plan` and `permission_mode=plan` together, and write operations are hard-blocked as read-only before approval.
- When intent is insufficient, a multi-step clarification card appears with options, custom input, and skip, avoiding execution on wrong assumptions; the clarification wizard is not triggered in production or non-interactive runs.
- Long instructions can be structured once before sending: without tools, the original text is decomposed into explicit steps with ops fields filled in, marking original text, inferences, completions, and additions; the draft is bound to the current target host, and the plan posture strips write steps. Adoption and fallback are decided explicitly by the user, and a structuring failure does not affect sending the original draft.

**Model management**

- Supports local vLLM and OpenAI-compatible endpoints: DeepSeek, OpenAI, Qwen, Kimi, GLM, Xiaomi, Anthropic.
- Connectivity and latency can be tested; global defaults and SSH-scenario defaults are set separately; models can be switched temporarily within a conversation. If no default model exists, the backend explicitly refuses to run instead of silently switching to another model.
- Models can configure context and output limits and thinking budgets, and be marked with thinking, vision, and tool-calling capabilities; when an endpoint rejects tool calls, the system degrades to tool-free mode automatically.

**Post-processing and skills**

- After a single conversation turn ends, the system can automatically generate a session title, extract memories, and distill skills; skill generation is triggered every 3 turns with a count cap, all of which can be disabled in Settings.
- Enabled skills can be matched automatically by trigger words; three ops skills are built in: `linux-ops-triage`, `nginx-service-diagnose`, and `ops-change-safety`.

**Experts and general identity**

- The default identity is a general-purpose AI assistant; ops, research, and copywriting are optional expert packs — domains are not hard-coded and can be added or removed.
- Four built-in experts: general (inherits all defaults), Ops Expert (general_ops: ops role + change/read-only methodology + ops tool discipline), research, and copywriting. Research and copywriting are restricted to no execution tools, no plugins, and global-only memory scope.
- Experts can only tighten, never loosen: tools/skills intersect, permissions take the stricter side, iterations take min, memory scope only shrinks; corrupted config fails closed.
- When no expert is selected and an ops signal is detected (change verb or ops noun), a non-blocking suggestion chip appears above the input area with "Switch & re-run" or "Dismiss"; no silent switching.
- Unattended runs: runbooks always use the Ops Expert; scheduled tasks can bind an expert — legacy tasks are migrated to Ops Expert, new tasks default to general.

**Cross-page AI entry points**

- **Terminal selection**: select content and click the floating button to prefill the task console with the selection, working directory, and recent commands.
- **Batch commands**: a safety classification runs before multi-machine execution, and classification failures are treated as dangerous; results are compared side by side and can be sent to the task console for analysis.
- **Alert triggers**: when monitoring metrics exceed limits consecutively (threshold rules configurable, per-host toggles), a fully governed diagnostic run starts automatically — approvals, auditing, and cost breakers still apply, and results land in the task console conversation; the same anomaly has a cooldown period to prevent storms.
- **Scheduled tasks**: failed records can prefill AI diagnosis in one click; inspection tasks output `[巡检结论]` by convention and, on anomalies, write to memory and push to a webhook per configuration.
- **Git**: generate commit messages from the diff of selected files, falling back to a rule-based draft on failure.
- **Command prediction**: Tab completion can use model prediction, off by default, enabled in appearance settings.

**Audit and traces**

- Every Agent tool call, approval, and authorization is written to a SHA-256 hash chain, with integrity verification, CSV export, and archiving.
- Ops traces are grouped by `run_id` and include host, command, exit code, conclusion, and the full execution chain; the "trace" action on a message jumps directly to the corresponding record.
- Runs that left a rollback snapshot can be rolled back in one click from the traces page: a new governed run performs the reverse operation (approvals and auditing as usual), and the run list marks rollback-capable items with a "rollback" column; production rollbacks still pop cards for confirmation in interactive sessions.

### cKunBot personal bot

The virtual companion persona subsystem opened by the "Bot" entry (`Ctrl+Shift+9`) in the title bar, fully isolated from the ops Agent: it does not execute commands or call tools; it is only a "person" with personality, memory, and boundaries in chat. Terminology is pinned in [CONTEXT.md](CONTEXT.md); the implementation lives in `backend/bot/` and `backend/routes/bot*.py`.

**Persona and expression**

- **Persona editor**: six free-form sections for personality / background / likes and red lines / memory settings / interaction examples / advanced play, plus avatar, opening line, and bound model; slider values come from a server-side dictionary, so the frontend never copies the text by hand.
- **Default persona**: the built-in persona is pinned to the top of the list, is the fallback owner of new channels, and cannot be deleted; it is marked "default" in the UI.
- **Reply style and thinking switch**: persona-level settings for concise / moderate / long-form and whether to show the thought process; the style tier automatically lowers with the relationship state axis. Local sessions show a collapsible thinking block above the bubble, while the QQ channel only takes the final plain text.
- **Image input**: local sessions can send images with a message (up to 4 per message), recognized directly by multimodal models; history messages show thumbnails that can be enlarged, while the QQ channel stays plain text.
- **Avatar system**: server-side avatar gallery and unified picker, supporting custom user avatars and local display names / avatars for QQ sessions; when unset, a faint initial placeholder is shown.
- **Expression DNA**: six discrete tiers for sentence length, interjections, punctuation habits, emoji, kaomoji, and stickers, plus catchphrases and custom forbidden words, compiling "how to speak" from adjectives into executable constraints; the output is then cleaned once to remove AI-sounding text.
- **Stickers**: the model uses a standalone line marker `[表情:开心]` to mean "send another image"; two built-in "whale girl" sets — 8 text-free images (happy / shy / speechless / cheer / aggrieved / sleepy / angry / love) and 8 meme short-phrase images (好耶 / 诶嘿 / 服了 / 冲鸭 / 呜呜 / 困困 / 哼 / 贴贴) — previewed by set in tabs in the persona editor. Local bubbles render the image directly; QQ uploads rich media before sending; WeChat sends an image URL only when a public callback address is configured, falling back to a synonymous kaomoji when it cannot send.
- **Honesty charter**: 7 immutable bottom lines (no sycophancy, no fabricated memories, no pretending to have a body, no executing commands, etc.) always sit at the end of the prompt with declared top priority; persona text that tries to override them is rejected at save time.

**Relationship and state (0 extra model calls)**

- **Relationship stages**: stranger → familiar → close → in sync, derived from a monotonically increasing score scaled by a warming pace (slow / normal / fast) threshold, decaying by half-life at read time; the UI progress bar and the prompt share one source, and there is only one stage definition across the project.
- **State axes**: seven axes — trust / intimacy / tension / novelty / valence / energy / patience — derived deterministically from "event → delta" (replayable, unit-testable), with soft saturation, event cooldowns, and regression to baseline (energy follows a circadian rhythm); only tier words are fed to the model, never raw numbers.
- **Sense of presence**: three pacing modes — hesitate / relief / eager — derived by the state layer; the frontend performs the matching typing animation and bubble delivery rhythm.

**Memory (conservative decay + self-evolution)**

- 11 categories of fact extraction by regex (name / residence / birthday / dietary restrictions / aversions / preferences / occupation / pets / family / habits / plans); sensitive information is never extracted as a whole, and users can say "don't remember / forget X"; override injections cannot become facts through extraction.
- Recall = FTS + character overlap + vector fusion, with three-level injection gating (hard hit / context gate / old-memory hook) + 7-day anti-repeat; **being injected warms it up**, and memories not recalled for a long time are downweighted by half-life (floor 0.1, never deleted), with a hard cap of 200 per persona; only on overflow are the "least hot + oldest" evicted with an audit row — memory maintenance runs in the background heartbeat, one persona per round, so the chat hot path never blocks. **Group conversations recall only what was said inside that group** (the candidate pool is filtered by source conversation), so a birthday told in a private chat never surfaces from her group replies; local and one-to-one chats are not filtered — in personal use "she remembers you" is a feature, not a leak.

**Proactivity and multi-channel**

- **Proactivity**: desire is derived from state (don't disturb when not familiar, strongest mid-conversation, wanting to share something new), with four suppression gates (minimum interval / quiet window / lull-index cooldown / daily quota); what to say comes from a deterministic topic picker — if the user said they were tired last turn, ask whether they have recovered; share something newly remembered in the last 3 days; with low probability, bring up an old memory.
- **Multi-channel**: each bot has an independent thread and long connection (up to 8); QQ is implemented, and platform differences converge in a 5-member provider protocol; credentials are encrypted at rest and never echoed by the API; connections are restored on startup, and one failure does not affect the others. Replies are delivered in blank-line-separated parts, and when parts run out the last part is appended to without swallowing text; multi-part messages carry an increasing `msg_seq` so the platform does not deduplicate them away. Proactive "thoughts" that cannot be sent during QQ's passive-reply window are held for 48h and attached with "对了，" when the other party messages. A bot is bound to one persona at a time, but the persona is resolved **when each inbound message arrives**, so switching personas in the config takes effect immediately without disconnecting and reconnecting; afterwards she automatically starts a new conversation in each group, the old ones stay readable, and the previous persona no longer gets to speak through this account on its own.

### Terminals and files

- **SSH connection management**: password, private key, and SSH Agent auth; credentials are persisted encrypted via the system safeStorage, and configurations can be imported and exported.
- **Remote terminal**: ssh2 + xterm.js, with OSC 7 working-directory tracking and LS_COLORS.
- **Local terminal**: node-pty drives PowerShell, CMD, Git Bash, and WSL.
- **SFTP**: resumable transfers, chunked concurrency, heartbeat detection, and error classification; a transfer center shows progress and history, and frequently used remote directories can be saved as bookmarks.
- **Remote file editing**: built-in Monaco Editor for editing remote files directly.
- **Command assistance**: Tab completion over SSH, fuzzy history suggestions with execution-count badges, and a custom quick-command bar.
- **Multi-node batch dispatch**: run commands in parallel across multiple active sessions, compare results side by side with line-level diffs highlighted; production environments or dangerous commands require a forced second confirmation before execution.
- **Secure access**: besides direct connections, traffic can be relayed through a self-hosted TLS gateway with SPKI certificate pinning, password auth, and rate-limited lockout on failures; the gateway is a single-file, zero-dependency implementation.

### Observability and automation

- **Monitoring dashboard**: collects CPU, memory, load, disk, top processes, listening ports, and GPU every 5 seconds, with trend charts. After consecutive failures it retries in the background and resumes when the connection recovers.
- **GPU panel**: nvitop-style per-card view showing compute, VRAM, temperature, power, and processes, flagging anomalies such as "occupying the card without working", IO wait, and SWAP full.
- **Scheduled tasks**: ops and daily tasks are managed in separate sections; daily tasks include built-in templates for daily summaries, weekly report material, and file archiving; countdown, historical duration, and failure records, with execution history rendered as Markdown and collapsed by default, failure lines expanded; inspections can be run by the Agent.
- **Ops runbooks**: structured steps (command + description, ≤20 steps) executed in one click; each step is a headless run judged deterministically by marker lines, and on interception or failure it stops and hands over to AI diagnosis, leaving execution history and audit records.
- **SSH tunnels**: TCP port forwarding, SOCKS5 proxy, and Dynamic forwarding; rules are persisted, state-machine managed, and port conflicts pre-checked; tunnels use independent connections and do not depend on terminal sessions.
- **Docker management**: manage containers, images, volumes, and networks over remote SSH sessions; supports container details, live stats, logs, image export and load, unused image cleanup, and image pulls, with second confirmation and auditing for dangerous operations.
- **GitHub acceleration**: mirror latency testing and convergence, precise hosts writing and restoration, self-checks, and failure inspection.
- **Git repository management**: dual-pane workbench with status grouping on the left and changes, branches, and a commit graph on the right; supports local and remote repository overview, batch fetch, `pull --ff-only`, branch switching, per-file commit selection, commit and push, push and open terminal; write operations are audited after confirmation.
- **Git three-remote sync**: consistency inspection across the primary remote and the read-only mirrors (GitCode/Gitee/GitHub); per-remote `ls-remote` compares SHAs without downloading objects, and each remote reports one of four states — in sync / behind / diverged / probe failed; a behind remote can be fast-forwarded (`push --follow-tags`, reported per remote and non-atomic), while a diverged one only offers `--ff-only` merge and never a force push; the primary remote can be switched and persisted to the registry (version 2).

### Experience and engineering

- **Keyboard-first**: command palette (`Ctrl+Shift+P`), direct page access (`Ctrl+Shift+1..9`), and terminal tab shortcuts.
- **Appearance system**: 9 themes × 10 accent colors, with gradient mode and direction configuration.
- **Markdown rendering pipeline**: messages and execution history render uniformly through marked + highlight.js + DOMPurify; highlight.js imports selected languages on demand to control bundle size, languages marked but not registered render as plain text, and render failures fall back to sanitized plain text.
- **CI gates**: backend pytest, backend lint (ruff `F,E9`), frontend type checking, frontend unit tests, electron-vite build, and gateway byte-drift self-check; see "Common commands" below for the full order.

## Tech stack

### Desktop

| Layer | Technology | Purpose |
| --- | --- | --- |
| Desktop framework | Electron 35 | Desktop app shell |
| Frontend | Vue 3 + Vite 6 | Renderer SPA |
| State management | Pinia | Global state and conversation management |
| Code editor | Monaco Editor | Remote file editing |
| Terminal | xterm.js + node-pty | Local terminal |
| Markdown rendering | marked + highlight.js + DOMPurify | Message stream and execution history rendering |
| SSH | ssh2 | Remote terminal, SFTP, tunnels |
| Build | electron-vite | Main / preload / renderer builds |
| Packaging | electron-builder | Windows dir output and tar.gz distribution |

### Backend

| Layer | Technology | Purpose |
| --- | --- | --- |
| Framework | Python 3.11+ / FastAPI | Agent runs, memory, and model management |
| ASGI | uvicorn | Runs FastAPI |
| Agent orchestration | In-house Harness | ReAct loop, interceptor chain, approvals, and tool execution |
| LLM client | openai SDK | OpenAI-compatible protocol (vLLM / Ollama / OpenAI, etc.) |
| Storage | SQLite | Conversations, memory (FTS5 + vectors), skills, and audit |

### Known issues

- `backend/requirements.txt` pins `openai` to `>=1.30.0,<2.0`: CI and the embedded runtime install 1.x while a dev machine may run 2.x. `backend/tests/test_ai_loop_core_regression.py` replaces `AsyncOpenAI` with a fake client, so the real 2.x interface is not covered. Decide whether to widen the ceiling or pin the dev environment only after validating against a live LLM endpoint.
- npm audits need the official registry explicitly: there is no `.npmrc` in the repository, and a user-level registry that does not implement `/-/npm/v1/security/*` makes `npm audit` return 404. Reproduce with `npm audit --registry=https://registry.npmjs.org`. The shipped surface is only `node-pty` and `ssh2`; dev-toolchain upgrades are scheduled separately because any `npm install` triggers the native rebuild in `scripts/rebuild-native.cjs`.

## Architecture

```
┌──────────────────────────── Electron ────────────────────────────┐
│  Renderer (Vue 3)                  Main (Node.js)                │
│  Task console / Terminal           node-pty / ssh2 /             │
│  Editor / xterm.js / Pinia         BackendManager                │
│         │ HTTP / SSE                      ▲                      │
│         │                                 │ WS /ws/bridge        │
└──────────┼─────────────────────────────────┼─────────────────────┘
           ▼                                 │
           Python FastAPI backend ──────────┘
           • Agent runs (SSE, /agent/run)
           • Harness / tools / approvals
           • Memory and audit (SQLite)
           • Model configuration and routing
           │
           ▼
           OpenAI-compatible LLM service
```

**Three data flows:**

- **AI run**: Renderer → HTTP / SSE → Python FastAPI; conversations and executions all go through `/agent/run`, while structuring, commit messages, command prediction, and other auxiliary capabilities use dedicated endpoints.
- **Terminal / SSH / SFTP / tunnels**: Renderer → Electron IPC → node-pty / ssh2, bypassing Python.
- **Agent remote execution**: Python backend → WebSocket reverse channel `/ws/bridge` → Electron main process → ssh2. When the channel is down the system fails closed and in-flight requests are not retried.

### Startup critical path

Both the backend and the main process defer heavy dependencies until first use: `openai` / `botpy` / `cryptography` are absent from the `import main` graph, and the main process loads node-pty's `.node` binary and ssh2 on demand via `loadPty()` / `loadSsh2()`. Moving them back to module-level imports turns neither unit tests nor typecheck red — only `backend/tests/test_startup_critical_path.py` catches it, so that guard alone protects this invariant.

## Project structure and engineering conventions

```
cKunAgentSSH/
├── electron/              # Main process: windows, IPC, SSH/SFTP/PTY/tunnels, backend and gateway management
├── src/                   # Vue 3 renderer: pages, components, composables, Pinia stores, API
├── shared/                # Pure logic and protocols shared by main / renderer / gateway
├── backend/               # Python FastAPI: Agent, memory, tools, routes, tests
│   ├── agent/             # Harness, memory, tools, intent detection, reflection evolution, bridge relay
│   ├── routes/            # HTTP routes (agent / chat / settings / skills / ... / bot family)
│   └── tests/             # pytest tests
├── gateway/               # TLS access gateway (single file, zero deps, deployed independently)
├── scripts/               # dev / package / clean / ci-check / rebuild-native / prepare-runtime / deploy
├── resources/             # App and tray icons
├── docs/                  # Design/implementation specs (structure refactor plan; not shipped)
├── designs/               # UI concept mockups (local artifacts, ignored by .gitignore)
├── runtime/               # Embedded Python runtime (prepared locally, ignored by .gitignore)
├── electron.vite.config.ts
├── electron-builder.yml
└── package.json
```

**Directory boundaries**: `electron/`, `src/`, `shared/`, `backend/`, `gateway/`, `scripts/` and `resources/` are part of the build and runtime chain; their names are hardcoded in configs, CI and runtime paths (`shared/` alone has nearly 100 cross-layer imports), so do not rename them casually. `docs/` and `designs/` are documentation and mockups and are not packaged. `runtime/`, `out/`, `dist/` and `node_modules/` are reproducible local artifacts. Tests and guard scripts deliberately sit next to the code under test and are collected by the globs in `vitest.config.ts`; the `.ts` files in `src/styles/` are CSS guard utilities and tests, not stylesheets. Frontend visual and component rules live in [DESIGN.md](DESIGN.md); terminology is pinned in [CONTEXT.md](CONTEXT.md). The structure refactor plan lives in [docs/structure-refactor-plan.md](docs/structure-refactor-plan.md).

**Naming and formatting conventions** (rules already in effect; existing inconsistencies are not bulk-renamed, new code follows these):

| Object | Convention | Example |
| --- | --- | --- |
| Directories / TS files | kebab-case | `electron/backend-manager.ts`, `src/utils/transfer-conflict.ts` |
| Vue component files | PascalCase | `ConnectionModal.vue`, `K8sView.vue` |
| composable | `useXxx.ts` / `useXxx()` (established exception in that directory) | `useSshNavigation.ts` |
| `src/stores/` and store ids | kebab-case (new code) | `server-monitor`, `ssh-ai` |
| Python modules / functions | snake_case | `bridge_relay.py`, `init_db()` |
| Python classes / constants | PascalCase / UPPER_SNAKE | `ChannelRuntime`, `ALLOWLIST_PATH` |
| Frontend tests | next to the code under test, `*.test.ts` | `src/utils/think-scrubber.test.ts` |
| Backend tests | `test_<domain>_<what it guards>.py`; no round or ticket numbers in names | `test_startup_critical_path.py` |
| Encoding / indentation | UTF-8; TS / Vue / JSON 2 spaces, Python 4 spaces | see [.editorconfig](.editorconfig) |
| Line endings | LF for new files and new content; existing CRLF / LF mix is not bulk-converted | see [.editorconfig](.editorconfig) |

**Verification**: `npm run ci` is the full local gate — backend pytest, ruff `F,E9`, vue-tsc, vitest, electron-vite build and the gateway byte-drift self-check, in that order; `scripts/ci-check.sh` and `.github/workflows/ci.yml` share one definition. When features, commands or architecture change, both READMEs must be updated section by section (see [CONTEXT.md](CONTEXT.md)).

## Quick start

### Requirements

- Node.js 20+
- Python 3.11+
- A reachable OpenAI-compatible LLM service (vLLM / Ollama / OpenAI, etc.)

### Install and run

```bash
npm install
pip install -r backend/requirements.txt
npm run dev
```

Backend test dependencies are installed separately:

```bash
pip install -r backend/requirements-dev.txt
```

### Model configuration

No model is preconfigured on first launch. Add an endpoint and specify the default model in **Settings → Models**; without one, Agent runs are explicitly refused.

Developers or demo environments can enable first-launch auto-detection in `backend/.env`:

```bash
# Required: first-launch detection (replace with your LLM service address; never commit a real internal address here)
VLLM_BASE_URL=http://your-llm-host:9030/v1
# Optional: thinking model
VLLM_THINKING_URL=http://your-llm-host:9031/v1
# Optional: API key
VLLM_API_KEY=EMPTY
```

### Common commands

| Command | Description |
| --- | --- |
| `npm run dev` | Development mode (Vite HMR + Electron) |
| `npm run dev:web` | Development mode listening on `0.0.0.0` (POSIX: `dev:web:unix`) |
| `npm run dev:backend` | Start the backend alone, listening on `0.0.0.0` by default; append `-- --host 127.0.0.1` for local-only use |
| `npm run prepare:runtime` | Prepare the embedded Python runtime (download CPython + install backend deps into `runtime/`) |
| `npm run build` | Build the production version |
| `npm run typecheck` | Frontend type checking |
| `npm test` | Frontend unit tests (vitest) |
| `npm run test:backend` | Backend unit tests (pytest) |
| `npm run lint:backend` | Backend lint gate (ruff `F,E9`: unused imports / undefined names / syntax errors; the wider ruleset's pre-existing findings are not gated, see `backend/ruff.toml`) |
| `npm run ci` | Full local gate matching CI checks |
| `npm run package` | Package the Windows build and tar.gz |
| `npm run clean` | Clean packaging output, intermediate config, and test/lint caches |
| `npm run preview` | Preview the build result |

## Packaging and release

```bash
npm run package
```

Packaging flow:

1. `electron-vite build` produces `out/`.
2. electron-builder produces the unpacked directory `dist/win-unpacked/`.
3. `scripts/package.js` compresses it into `dist/ckunagentssh-<version>-win32-x64.tar.gz`.

The release form is **tar.gz unpacked distribution** with no installer. Desktop and Start menu shortcuts are created on first launch; after the unpacked directory is moved, the app rebuilds stale shortcuts automatically based on the exe fingerprint.

**Embedded Python runtime**: `npm run package` requires `runtime/` in the repo root by default (Windows: `runtime/python.exe`, POSIX: `runtime/bin/python3`) and bundles it into `resources/python-runtime/`. Packaging fails without the runtime; use `npm run package -- --allow-missing-runtime` only to validate the pipeline. `runtime/` is ignored by `.gitignore` and must be prepared locally or in CI.

Local preparation takes one command:

```bash
npm run prepare:runtime
```

It fetches the relocatable `install_only_stripped` CPython from python-build-standalone, unpacks it into `runtime/`, installs dependencies from `backend/requirements.lock` (falling back to the range-based `backend/requirements.txt` when the lock file is absent), and precompiles site-packages bytecode as `checked-hash` (otherwise electron-builder copying and rewriting mtimes would invalidate all `.pyc` files and force recompilation on the target machine at first launch). The CI step "Prepare embedded Python runtime" calls the same script, so local and pipeline runs share one definition. Re-running is idempotent: if the interpreter exists, only dependencies and bytecode are rerun; `-- -Force` redownloads and re-unpacks.

When GitHub direct access is unstable (a typical error is `curl: (56) schannel: server closed abruptly`), there are two options: `npm run prepare:runtime -- -AssetUrl <mirror or proxy URL>`, or obtain the asset yourself and run `npm run prepare:runtime -- -Archive <local tar.gz>`. Downloads use `curl -C -`, so rerunning after a failure resumes. For dependencies installed in mainland China, point `PIP_INDEX_URL` at a mirror.

Other notes:

- Icons: the app icon is `resources/icon.png`; the tray prefers `resources/icon-tray.png` and falls back to the full icon when missing.
- Code signing: no certificate is configured currently.

## Logs and troubleshooting

Logs live in the Electron `userData` directory:

| File | Content | Format and rotation |
| --- | --- | --- |
| `main.log` | Main process and forwarded renderer console | `[HH:MM:SS.mmm] [LEVEL] message`; 5 MB per file, keeping `.1` / `.2` for 3 files total |
| `electron-bridge.log` | Bridge channel and remote command execution errors | ISO timestamps; 1 MB per file, rotated to `.old` when exceeded |

- The Python backend log format is `%(asctime)s %(levelname)-7s %(name)s: %(message)s`, with time format `%H:%M:%S` and default level `warning`; uvicorn access logs are disabled to avoid health-check spam.
- When running the backend directly in development (`cd backend && python main.py`), `qq-botpy` writes `botpy.log` under `backend/` and rotates it daily as `botpy.log.<YYYY-MM-DD>` according to its own configuration. These are local run artifacts covered by the `*.log` and `*.log.[0-9-]*` rules in `.gitignore`, and never enter version control.
- The Python backend log output redacts sensitive fields such as passwords. If `userData` is not writable, the main process silently disables file logging without affecting the app.

## Access gateway

The gateway is for self-hosted intranet relaying and is independent of the Electron packaging chain:

```bash
node gateway/hillgw.mjs --config gw.json    # start
node gateway/hillgw.mjs --hash <password>   # generate passwordSha256 in config
node gateway/hillgw.mjs --selftest          # frame codec and policy matching self-check
```

Server certificates and private keys are provided by operations; clients pin the server identity via SPKI fingerprints.

## License

[MIT](LICENSE)
