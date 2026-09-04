# mcp-fuzz

Runtime behavioral testing for [MCP](https://modelcontextprotocol.io) servers.

[`mcp-doctor`](https://github.com/vishalhabib99/mcp-doctor) reads an MCP server's *source code* and checks whether its tools are well-documented. `mcp-fuzz` does the opposite: it actually **launches the server and calls its tools**, with inputs derived from each tool's own declared JSON schema, and checks whether the server behaves the way that schema and its description claim — does a missing required field get a structured error back, or does the server crash? Does a wrong-typed field get rejected cleanly, or does it hang?

Static analysis can't see any of that. Only running the code can.

## Install

```bash
pip install mcp-fuzz
```

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

## Known limitations

- Input generation is schema-only. A property with no `type` (or a genuinely ambiguous `anyOf`) is skipped from the wrong-type test set rather than guessed at.
- No semantic check of *what* a successful response actually contains — that's a deliberately separate, opt-in, LLM-backed capability planned for a later release, not v1.
- stdio transport only for now; no HTTP/SSE servers yet.

## License

MIT
