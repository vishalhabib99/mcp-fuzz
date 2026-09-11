# mcp-fuzz

Runtime behavioral testing for [MCP](https://modelcontextprotocol.io) servers.

![mcp-fuzz run against the official @modelcontextprotocol/server-memory reference server: Crash resilience 100% (A), 9/9 tools tested, all ok](docs/demo.png)

*Real output from a live `--include-destructive` run against the official [`@modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory) reference server — not a synthetic example.*

[`mcp-doctor`](https://github.com/vishalhabib99/mcp-doctor) reads an MCP server's *source code* and checks whether its tools are well-documented. `mcp-fuzz` does the opposite: it actually **launches the server and calls its tools**, with inputs derived from each tool's own declared JSON schema, and checks whether the server behaves the way that schema and its description claim — does a missing required field get a structured error back, or does the server crash? Does a wrong-typed field get rejected cleanly, or does it hang?

Static analysis can't see any of that. Only running the code can.

It still deliberately stops short of judging whether a *successful* call's response is actually correct — a schema-only placeholder value usually isn't realistic enough to fairly judge that. [`mcp-reality-check`](https://github.com/vishalhabib99/mcp-reality-check) is the third tool in the family that picks up exactly that: does it fail safely, and does it actually work.

## Install

```bash
pip install mcp-runtime-check
```

(The PyPI *distribution* name is `mcp-runtime-check` — `mcp-fuzz` and close variants were blocked by PyPI's anti-typosquat check as too similar to existing packages, same naming friction mcp-doctor hit. The installed CLI command is still `mcp-fuzz`, and the repo/import package are unchanged.)

## Use

```bash
mcp-fuzz -- python server.py
mcp-fuzz -- npx -y some-mcp-server
```

`mcp-fuzz` launches the command you give it as an MCP server over stdio, lists its tools, and for each one runs three kinds of calls built purely from that tool's own `inputSchema` — no LLM, no network calls of its own:

- **valid** — one plausible value per property (respecting `type`, `enum`, `minimum`/`maximum`, `format`, ...) — should succeed.
- **missing required** — the valid call, with one required property removed at a time — should come back as a structured MCP error, not a crash.
- **wrong type** — the valid call, with one property swapped to a value of a different JSON type — same expectation.

Any call that crashes the server, hangs past `--timeout` (default 15s), or leaves the connection unusable triggers a full reconnect before the next case runs, so one bad tool doesn't invalidate the rest of the report.

By default, only a minimal, safe set of environment variables (`PATH`, `HOME`, ...) is passed to the target server — never your full shell environment. Many real servers need an API key to start at all; pass one through explicitly rather than relying on the ambient environment:

```bash
mcp-fuzz --env BRAVE_API_KEY=... -- npx -y @brave/brave-search-mcp-server
```

## Safety — read this before pointing it at anything real

`mcp-fuzz` actually **executes** tool calls. Unlike mcp-doctor, it has real side effects if a tool does. By default, **only tools annotated `readOnlyHint: true` are tested** — everything else is skipped and listed as such in the report. Pass `--include-destructive` to test everything, but only against a server you're confident is safe to call blindly (a local sandbox, a test/staging backend) — never a server wired to production data, a real inbox, a real payment system, etc. Many real-world servers don't set `readOnlyHint` accurately or at all, in which case those tools are conservatively skipped rather than assumed safe.

## What the score means

The reported "crash resilience" percentage covers only the **missing-required** and **wrong-type** cases — the fraction that came back as a structured error instead of a crash or hang. It does **not** grade whether the tool's "valid" call produced a *correct* result: a synthetic, schema-only-derived value (a placeholder string where the field really expects a real arXiv ID, or a URL that has to actually resolve) often isn't realistic enough for a failure there to be a fair judgment. A failed "valid" call is reported separately, flagged explicitly as **"may be a synthetic-input false positive, not a confirmed bug"** — worth a manual look, not proof of a bug.

## JSON output / CI

```bash
mcp-fuzz --json -- python server.py
mcp-fuzz --fail-under 90 -- python server.py   # non-zero exit if crash resilience < 90%
```

## Full-fidelity trace export

`--json`'s report is deliberately narrow — it exists to answer "what's the crash-resilience score," so it drops every "ok" outcome, the call arguments, and any timing. That's the right shape for the score, wrong shape for reconstructing what a run actually did call by call — useful if you want to feed a real session into an external evidence or trajectory-debugging tool.

```bash
mcp-fuzz --full-trace session.jsonl -- python server.py
```

Writes one JSON line per tool call (`tool`, `case`, `property`, `outcome`, `detail`, `arguments`, `startedAt`, `durationMs`), plus one line per skipped tool — independent of, and in addition to, whatever `--json`/text report you also asked for.

Added after an actual external ask, not speculatively: [`agent-inspect`](https://github.com/rajudandigam/agent-inspect) (a TypeScript agent-trajectory/evidence-debugging tool) asked what it would take to feed a real mcp-fuzz session into its evidence model. Before this flag existed, the only way to do that was a one-off script that duplicated mcp-fuzz's own input generators just to recover the arguments and timing `--json` throws away — this flag makes that a first-class, supported path instead. Feeding a session through confirmed a real, useful finding along the way: mcp-fuzz's `graceful_error`/`valid_call_errored` outcomes (the *good*, correctly-handled case) map naturally onto a generic trajectory tool's `status:error`/"failed" vocabulary, which reads a healthy 100%/A crash-resilience session as mostly broken unless the consumer knows to treat those two outcomes as distinct from `crash`/`timeout`. Worth remembering for anyone else piping this JSONL into a similar tool.

## Real-world spot check

| Repo | Stars | Lang | What mcp-fuzz found |
|---|---|---|---|
| [`modelcontextprotocol/server-everything`](https://github.com/modelcontextprotocol/servers/tree/main/src/everything) | — | TS | Official reference server, run cross-language via `npx`. Clean pass — 9/9 read-only tools handled every bad-input case cleanly (100%/A). `trigger-long-running-operation`'s valid call correctly timed out — it's deliberately a long-running operation, exactly the kind of result the report's own "may be a false positive" framing exists for. |
| [`blazickjp/arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server) | 3.1k | Python | Found a real bug in mcp-fuzz itself, not the target: installing this repo (which pins `mcp<2.0`) into the same environment downgraded the shared `mcp` package from 2.1.1 to 1.29.1. mcp<2.0 exposes several fields under their raw camelCase wire name (`isError`, `inputSchema`, `readOnlyHint`); mcp>=2.0 renamed them to snake_case. Every hardcoded snake_case attribute access broke with an `AttributeError` the moment an older `mcp` happened to be installed. Fixed with a small compatibility helper that tries the current name first, falls back to the old one. 11 tools tested cleanly afterward (100%/A) — the several "valid call errored" flags are exactly the documented synthetic-input false-positive case (a placeholder `"paper_id": "test"` isn't a real arXiv ID). |
| [`punitarani/fli`](https://github.com/punitarani/fli) | — | Python | Clean pass — all 4 read-only tools (Google Flights MCP) handled every bad-input case cleanly, and even the synthetic "valid" inputs succeeded without error (100%/A, no "worth investigating" flags at all). |
| [`modelcontextprotocol/server-sequential-thinking`](https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking) | — | TS | Official reference server. Clean pass, tested with `--include-destructive` (its one tool isn't read-only-annotated but has no real side effects) — 100%/A. |
| [`modelcontextprotocol/server-filesystem`](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) | — | TS | Official reference server, run against a throwaway sandbox directory. Clean pass — 6 of 10 read-only tools "valid call errored" on `ENOENT: no such file`, exactly the documented synthetic-input false positive: a generic placeholder string isn't a real path that exists in the sandbox. The 4 write/edit/move/create tools were correctly skipped as not read-only. |
| [`antvis/mcp-server-chart`](https://github.com/antvis/mcp-server-chart) | 4.3k | TS | The strongest real finding yet — **37.85%/F**. All 27 read-only chart/diagram tools crash (not a graceful structured error) on realistic bad input: 133 of 214 missing-required/wrong-type calls raised a raw internal exception through as a protocol-level error instead. `generate_bar_chart` alone: omitting the required `data` array → `Cannot read properties of null (reading 'map')`; passing the wrong type for `data` → `e.map is not a function`; a wrong-typed `title` even crashed a downstream call to a remote rendering API with an HTTP 500. Every one of these is a completely realistic mistake a real LLM agent could make (a hallucinated missing or wrong-typed argument), not an artifact of unrealistic synthetic data — the strongest, most legitimate signal this tool has produced. [Filed upstream](https://github.com/antvis/mcp-server-chart/issues/323) — traced to an already-merged-but-unreleased fix (`main`'s `9fd0bb4`/#292), [confirmed and commented](https://github.com/antvis/mcp-server-chart/issues/323#issuecomment-5555745287). |
| [`haris-musa/excel-mcp-server`](https://github.com/haris-musa/excel-mcp-server) | 4.1k | Python | Clean pass — 6 read-only tools handled all 31 bad-input cases cleanly (100%/A). 19 write tools correctly skipped as not read-only. |
| [`czlonkowski/n8n-mcp`](https://github.com/czlonkowski/n8n-mcp) | 23k | TS | Clean pass, run via `npx` — 7/7 tools, 44 bad-input cases, 100%/A. Several "valid call errored" flags are the documented synthetic-input false positive (a placeholder node/workflow name that doesn't exist). |
| [`mendableai/firecrawl-mcp-server`](https://github.com/mendableai/firecrawl-mcp-server) | 7k+ | TS | **Found a real bug in mcp-fuzz itself, not the target.** Run keyless (no `FIRECRAWL_API_KEY`, its two free tools hit the real Firecrawl cloud). Every one of 93 bad-input calls came back a false **0%/F** — firecrawl validates arguments with zod and has the SDK raise a well-formed JSON-RPC `-32602 INVALID_PARAMS` error for a bad call, rather than a `CallToolResult` with `isError=true` content; mcp-fuzz's blanket `except Exception` treated that identically to a real crash. Fixed by classifying `MCPError` on its actual code: `-32602` (real schema validation) is graceful, everything else stays a crash — see "A bug in mcp-fuzz itself" below. Re-verified: 93/93 handled cleanly, **100%/A**. |
| official reference servers: [`fetch`](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch), [`time`](https://github.com/modelcontextprotocol/servers/tree/main/src/time), [`git`](https://github.com/modelcontextprotocol/servers/tree/main/src/git) | — | Python/TS | All clean passes (100%/A). `fetch`'s one tool isn't annotated `readOnlyHint` despite genuinely being read-only GET-style — re-tested with `--include-destructive`, still clean; a real but minor annotation gap, not worth filing upstream on Anthropic's own reference implementation. |
| [`modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory) | — | TS | Official reference server (in-memory knowledge-graph store). Clean pass — **100%/A**, 0 crashes across 4 bad-input calls. Only 3 of 9 tools are annotated `readOnlyHint`/tested by default (`read_graph`, `search_nodes`, `open_nodes`); the 6 mutating tools (`create_entities`, `add_observations`, etc.) are correctly skipped without `--include-destructive`, exactly as documented. |
| [`qdrant/mcp-server-qdrant`](https://github.com/qdrant/mcp-server-qdrant) | — | Python | Clean pass — **100%/A**, 0 crashes across 8 bad-input calls on both tools, tested with `--include-destructive` against a fully local embedded store (`QDRANT_LOCAL_PATH`, no external service). Both "valid" calls errored with "All connection attempts failed" — likely `qdrant-client`'s `AsyncQdrantClient` not fully supporting its own embedded local-storage mode, not confirmed as an mcp-server-qdrant or mcp-fuzz bug, and not chased further (out of scope for today's pass; the bad-input crash-resilience score itself is unaffected). |
| [`upstash/context7`](https://github.com/upstash/context7) | 61k | TS | Widely-used documentation-lookup server (`@upstash/context7-mcp`). Clean pass — **100%/A**, 0 crashes across 8 bad-input calls on both of its tools (`resolve-library-id`, `query-docs`), both annotated read-only and tested. Largest-star repo checked so far. |
| [`wonderwhy-er/DesktopCommanderMCP`](https://github.com/wonderwhy-er/DesktopCommanderMCP) | 9k | TS | Clean pass — **100%/A**, 0 crashes across 49 bad-input calls on 14 of 26 tools (the other 12 — `write_file`, `start_process`, `kill_process`, etc. — correctly skipped as not read-only). 5 "valid call errored" flags (`read_file`, `get_file_info`, `get_more_search_results`, `read_process_output`, `get_prompts`) are all the documented synthetic-input false positive: a placeholder path/PID/session ID that genuinely doesn't exist on the runner, not a real bug. |
| [`modelcontextprotocol/server-sqlite`](https://pypi.org/project/mcp-server-sqlite/) | — | Python | Clean pass — **100%/A**, 0 crashes across 10 bad-input calls, tested with `--include-destructive` against a throwaway local db file (none of its 6 tools are annotated `readOnlyHint`, an older pre-annotation-convention server). No "valid call errored" flags at all. |
| [`Jpisnice/shadcn-ui-mcp-server`](https://github.com/Jpisnice/shadcn-ui-mcp-server) | 3k | TS | **60%/D** — 8 crashes across 20 bad-input calls, tested keyless (no GitHub token) with `--include-destructive` (none of its 10 tools are annotated `readOnlyHint`, though most are plainly read-only fetches). Two clean, unambiguous crashes verified directly against source, unrelated to the keyless rate limit: `get_component` and `get_component_demo` both raise a raw `TypeError` (`componentName.toLowerCase is not a function` / `Cannot read properties of undefined`) on a missing or wrong-typed `componentName`, then re-throw it as a plain `Error` rather than a structured MCP result — confirmed in `handleGetComponent`'s catch block, which always rethrows regardless of the failure's cause. The remaining crashes (`get_directory_structure`, `list_blocks`, `get_component_metadata`) show the same catch-and-rethrow pattern but are confounded by the same run exhausting the keyless 60-req/hour GitHub API limit partway through — real per source, but not independently re-verified clean of that noise. [Filed upstream](https://github.com/Jpisnice/shadcn-ui-mcp-server/issues/61) for the two verified crashes. |
| [`GongRzhe/Office-Word-MCP-Server`](https://github.com/GongRzhe/Office-Word-MCP-Server) | 2.1k | Python | Clean pass — **100%/A**, 0 crashes across 31 bad-input calls on 11 of 54 tools (the other 43 correctly skipped as not read-only). One notable side-observation, not a scored crash: the process prints `"Loading configuration from .env file..."` to stdout at import time (`main.py:12`, a plain `print()` before `load_dotenv()`), which is a real stdio-transport protocol hygiene issue — any unguarded stdout write can corrupt the JSON-RPC stream for a client that doesn't defensively skip malformed lines. Didn't affect this run's scored results (the `mcp` SDK's client logged and skipped the bad line rather than dying), but a stricter client could break on it. |
| [`brave/brave-search-mcp-server`](https://github.com/brave/brave-search-mcp-server) | 1.4k | TS | Official Brave server. Couldn't be run at all until `--env` existed (see below) — refuses to start without `BRAVE_API_KEY`. Tested with a dummy key and `--include-destructive` (none of its 8 tools are annotated `readOnlyHint`): clean, **100%/A**, 0 crashes across 95 bad-input calls. Every "valid call errored" flag is the real API correctly rejecting the fake key (`SUBSCRIPTION_TOKEN_INVALID`) — the standard synthetic-input caveat, one layer removed (a fake *credential* rather than a fake data value). |
| [`financial-datasets/mcp-server`](https://github.com/financial-datasets/mcp-server) | 2.3k | Python | Run with a dummy `FINANCIAL_DATASETS_API_KEY` and `--include-destructive` (none of its 11 tools are annotated `readOnlyHint`). Clean, **100%/A**, 0 crashes across 45 bad-input calls — every call gracefully returns a generic "unable to fetch" message rather than raising, because `make_request` catches every exception itself (see [mcp-reality-check's dogfooding of the same repo](https://github.com/vishalhabib99/mcp-reality-check) for the double-edged side of that same design: crash-proof here, but also unable to signal a real failure as an error). |
| [`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) | 42.7k | C | Native binary (darwin-arm64 release, checksum-verified), run directly with no build step. Default run tested only 1 of 15 tools — pulling the raw `tools/list` response showed 13 of 15 tools are annotated `destructiveHint:true`/`readOnlyHint:false` despite being pure read/query ops (`search_graph`, `get_architecture`, etc. all carry the identical annotation object, apparently a shared default rather than per-tool). Not a bug in mcp-fuzz — it correctly respected the (mislabeled) hints. Re-run with `--include-destructive`: clean, **100%/A**, 0 crashes across 99 bad-input calls on all 15 tools; every "valid call errored" flag is the standard synthetic-input false positive (a placeholder project/path that was never indexed), with a genuinely helpful structured error message every time, even for `index_repository` against a nonexistent repo path. [Filed upstream](https://github.com/DeusData/codebase-memory-mcp/issues/2118) on the annotation mislabeling. |
| [`arabold/docs-mcp-server`](https://github.com/arabold/docs-mcp-server) | 1.7k | TS | Run via `npx`. Well-annotated server (6 tools correctly `readOnlyHint`, unlike `codebase-memory-mcp` above). Clean on both passes — default: **100%/A**, 0 crashes across 15 bad-input calls on the 6 read-only tools; `--include-destructive`: still **100%/A**, 0 crashes across all 10 tools. All "valid call errored" flags are the standard synthetic-input false positive (a placeholder library/job ID that doesn't exist). No mcp-fuzz bug this round — see [mcp-doctor's pass on the same repo](https://github.com/vishalhabib99/mcp-doctor) for two real security-heuristic false positives found there instead. |

### A real usability gap: no way to pass an API key to the target server

Trying to fuzz `brave/brave-search-mcp-server` surfaced a gap that had nothing to do with crash detection: the server refuses to start at all without `BRAVE_API_KEY`, and mcp-fuzz's CLI had no way to pass *any* environment variable through to the target. The MCP SDK's own `stdio_client` deliberately doesn't inherit the operator's full shell environment when none is given — only a minimal, safe allowlist (`PATH`, `HOME`, ...) — a real security default against leaking secrets into an arbitrary launched process. But that meant any server needing an API key (Brave, `financial-datasets/mcp-server`, and presumably many others) simply couldn't be tested. Added `--env KEY=VALUE` (repeatable), merged onto the SDK's own safe baseline rather than replacing it — losing `PATH` entirely would break `npx` before the target server ever ran, a strictly worse outcome than the default. 4 new tests (45 total): the real fix (env passed through → connects), the safety property it must not regress (no env → the server still refuses to start, confirming this isn't quietly leaking the full shell environment instead), and the merge behavior itself as a pure unit test. Shipped as v0.1.4.

### A bug in mcp-fuzz itself, and the two-step fix it took to get right

The firecrawl false-0%/F above led to a genuinely tricky classification bug, worth documenting honestly rather than glossing over. The client SDK raises the same exception type, `MCPError`, for two completely different situations:

1. A **real, well-formed JSON-RPC error response actually received from the server** — e.g. `-32602 INVALID_PARAMS` when schema validation (zod, pydantic, ...) rejects a bad call. This is exactly the "structured error back" a well-behaved server is supposed to return — not a crash.
2. A **client-synthesized error** for a transport failure (`-32000 CONNECTION_CLOSED`) or the SDK's own internal request timeout (`-32001 REQUEST_TIMEOUT`) — no real response was ever received. Still a crash/timeout.

The first fix attempt only excluded case 2 and treated every other `MCPError` as graceful. That over-corrected: re-running it against `antvis/mcp-server-chart` (the 37.85%/F finding above) silently flipped it to a false **100%/A**. The real cause: many frameworks use a *third* code, `-32603 INTERNAL_ERROR`, to wrap an **unhandled exception from the tool's own business logic** (a raw `TypeError: Cannot read properties of null`) so it doesn't kill the whole process — a completed round trip, but not the server "behaving the way its schema claims" either. The correct rule, verified against both repos simultaneously (each has the other as its regression test): only `-32602 INVALID_PARAMS` is trusted as "properly handled"; every other code, including `-32603`, stays a crash. Re-checked against both real repos afterward: `firecrawl-mcp-server` 100%/A, `antvis/mcp-server-chart` back to the original, correct **37.85%/F** (exactly 133/214) — the fix removed the false positive without touching the real finding.

### Hardened against malformed tool metadata (not just bad tool-call arguments)

Everything above tests whether a *target server's tool handlers* fail safely on bad input. A [comment on the trilogy writeup](https://dev.to/vishalhabib99/i-built-three-tools-to-audit-mcp-servers-each-one-found-a-bug-in-itself-first-5dlc) pointed out the gap one level up: nothing tested whether **mcp-fuzz itself** stays standing when the `tools/list` response it's fuzzing *from* is malformed — the contract between mcp-fuzz and the server, not the server's own tool logic.

It didn't. Feeding the schema generator adversarial `tools/list` metadata by hand turned up three real crashes in mcp-fuzz's own code, all in the same place — real MCP servers just don't happen to send this kind of metadata, so nothing here had exercised it before:

- `properties` (or the whole `inputSchema`) coming back as a non-object — `AttributeError: 'list' object has no attribute 'items'`.
- `required` coming back as a string instead of a list — silently iterated character-by-character instead of raising, which is arguably worse than a crash (wrong output with no signal anything was off).
- A schema nested a few thousand levels deep — `RecursionError`, well within what a misbehaving code generator could produce by accident.

Fixed by treating any of these the same way the rest of the module already treats "no usable type information": generate a safe placeholder or nothing at all, never propagate the exception. A depth cap (50 levels — no real tool schema goes anywhere near that) stops the recursion case; type checks before every `.items()`/iteration stop the rest. 4 new regression tests (41 total).

## Known limitations

- Input generation is schema-only. A property with no `type` (or a genuinely ambiguous `anyOf`) is skipped from the wrong-type test set rather than guessed at.
- No semantic check of *what* a successful response actually contains — that's a deliberately separate, opt-in, LLM-backed capability planned for a later release, not v1.
- stdio transport only for now; no HTTP/SSE servers yet.

## License

MIT
