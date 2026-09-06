# mcp-fuzz

Runtime behavioral testing for [MCP](https://modelcontextprotocol.io) servers.

[`mcp-doctor`](https://github.com/vishalhabib99/mcp-doctor) reads an MCP server's *source code* and checks whether its tools are well-documented. `mcp-fuzz` does the opposite: it actually **launches the server and calls its tools**, with inputs derived from each tool's own declared JSON schema, and checks whether the server behaves the way that schema and its description claim — does a missing required field get a structured error back, or does the server crash? Does a wrong-typed field get rejected cleanly, or does it hang?

Static analysis can't see any of that. Only running the code can.

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

## Safety — read this before pointing it at anything real

`mcp-fuzz` actually **executes** tool calls. Unlike mcp-doctor, it has real side effects if a tool does. By default, **only tools annotated `readOnlyHint: true` are tested** — everything else is skipped and listed as such in the report. Pass `--include-destructive` to test everything, but only against a server you're confident is safe to call blindly (a local sandbox, a test/staging backend) — never a server wired to production data, a real inbox, a real payment system, etc. Many real-world servers don't set `readOnlyHint` accurately or at all, in which case those tools are conservatively skipped rather than assumed safe.

## What the score means

The reported "crash resilience" percentage covers only the **missing-required** and **wrong-type** cases — the fraction that came back as a structured error instead of a crash or hang. It does **not** grade whether the tool's "valid" call produced a *correct* result: a synthetic, schema-only-derived value (a placeholder string where the field really expects a real arXiv ID, or a URL that has to actually resolve) often isn't realistic enough for a failure there to be a fair judgment. A failed "valid" call is reported separately, flagged explicitly as **"may be a synthetic-input false positive, not a confirmed bug"** — worth a manual look, not proof of a bug.

## JSON output / CI

```bash
mcp-fuzz --json -- python server.py
mcp-fuzz --fail-under 90 -- python server.py   # non-zero exit if crash resilience < 90%
```

## Real-world spot check

| Repo | Stars | Lang | What mcp-fuzz found |
|---|---|---|---|
| [`modelcontextprotocol/server-everything`](https://github.com/modelcontextprotocol/servers/tree/main/src/everything) | — | TS | Official reference server, run cross-language via `npx`. Clean pass — 9/9 read-only tools handled every bad-input case cleanly (100%/A). `trigger-long-running-operation`'s valid call correctly timed out — it's deliberately a long-running operation, exactly the kind of result the report's own "may be a false positive" framing exists for. |
| [`blazickjp/arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server) | 3.1k | Python | Found a real bug in mcp-fuzz itself, not the target: installing this repo (which pins `mcp<2.0`) into the same environment downgraded the shared `mcp` package from 2.1.1 to 1.29.1. mcp<2.0 exposes several fields under their raw camelCase wire name (`isError`, `inputSchema`, `readOnlyHint`); mcp>=2.0 renamed them to snake_case. Every hardcoded snake_case attribute access broke with an `AttributeError` the moment an older `mcp` happened to be installed. Fixed with a small compatibility helper that tries the current name first, falls back to the old one. 11 tools tested cleanly afterward (100%/A) — the several "valid call errored" flags are exactly the documented synthetic-input false-positive case (a placeholder `"paper_id": "test"` isn't a real arXiv ID). |
| [`punitarani/fli`](https://github.com/punitarani/fli) | — | Python | Clean pass — all 4 read-only tools (Google Flights MCP) handled every bad-input case cleanly, and even the synthetic "valid" inputs succeeded without error (100%/A, no "worth investigating" flags at all). |
| [`modelcontextprotocol/server-sequential-thinking`](https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking) | — | TS | Official reference server. Clean pass, tested with `--include-destructive` (its one tool isn't read-only-annotated but has no real side effects) — 100%/A. |
| [`modelcontextprotocol/server-filesystem`](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) | — | TS | Official reference server, run against a throwaway sandbox directory. Clean pass — 6 of 10 read-only tools "valid call errored" on `ENOENT: no such file`, exactly the documented synthetic-input false positive: a generic placeholder string isn't a real path that exists in the sandbox. The 4 write/edit/move/create tools were correctly skipped as not read-only. |
| [`antvis/mcp-server-chart`](https://github.com/antvis/mcp-server-chart) | 4.3k | TS | The strongest real finding yet — **37.85%/F**. All 27 read-only chart/diagram tools crash (not a graceful structured error) on realistic bad input: 133 of 214 missing-required/wrong-type calls raised a raw internal exception through as a protocol-level error instead. `generate_bar_chart` alone: omitting the required `data` array → `Cannot read properties of null (reading 'map')`; passing the wrong type for `data` → `e.map is not a function`; a wrong-typed `title` even crashed a downstream call to a remote rendering API with an HTTP 500. Every one of these is a completely realistic mistake a real LLM agent could make (a hallucinated missing or wrong-typed argument), not an artifact of unrealistic synthetic data — the strongest, most legitimate signal this tool has produced. [Filed upstream](https://github.com/antvis/mcp-server-chart/issues/323) — traced to an already-merged-but-unreleased fix (`main`'s `9fd0bb4`/#292), [confirmed and commented](https://github.com/antvis/mcp-server-chart/issues/323#issuecomment-5555745287). |
| [`modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory) | — | TS | Official reference server (in-memory knowledge-graph store). Clean pass — **100%/A**, 0 crashes across 4 bad-input calls. Only 3 of 9 tools are annotated `readOnlyHint`/tested by default (`read_graph`, `search_nodes`, `open_nodes`); the 6 mutating tools (`create_entities`, `add_observations`, etc.) are correctly skipped without `--include-destructive`, exactly as documented. |
| [`upstash/context7`](https://github.com/upstash/context7) | 61k | TS | Widely-used documentation-lookup server (`@upstash/context7-mcp`). Clean pass — **100%/A**, 0 crashes across 8 bad-input calls on both of its tools (`resolve-library-id`, `query-docs`), both annotated read-only and tested. Largest-star repo checked so far. |

## Known limitations

- Input generation is schema-only. A property with no `type` (or a genuinely ambiguous `anyOf`) is skipped from the wrong-type test set rather than guessed at.
- No semantic check of *what* a successful response actually contains — that's a deliberately separate, opt-in, LLM-backed capability planned for a later release, not v1.
- stdio transport only for now; no HTTP/SSE servers yet.

## License

MIT
