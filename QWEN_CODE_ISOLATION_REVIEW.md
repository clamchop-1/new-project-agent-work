# Qwen Code compatibility and isolation review

**Review status:** capability discovery complete; integration is blocked at the isolation gate. No Qwen process was launched, no dependency was installed, and no harness prototype or default-path change was made.

## Local environment observed

- The installed package is `@qwen-code/qwen-code` 0.26.4. Its package metadata requires Node.js 22 or later; the available Node runtime reports v20.18.1. The CLI was not launched.
- The Qwen launcher is an npm wrapper around the installed package entry point. The Python and TypeScript SDK packages checked for this review were not installed.
- Docker and Podman commands were not available. The documented Windows whole-CLI sandbox therefore could not be exercised.
- A user Qwen settings file exists. Its contents are not reproduced here. It includes environment/provider configuration, so a default launch would inherit configuration that has not been reviewed for this use.
- This review did not inspect runtime tool registration or execute bypass probes. Claims below distinguish documentation from local runtime verification.

## Capability matrix

| Capability | Classification | Evidence and implication |
|---|---|---|
| Headless prompt mode (`-p`) | Verified in v0.26.4 docs | Headless mode is documented. The installed CLI was not launched because its Node requirement is unmet. |
| JSON output | Verified in v0.26.4 docs | Documented as a headless output format. |
| JSONL event stream (`stream-json`) | Verified in v0.26.4 docs | Documented structured streaming output; stream input is described as under development in this version. |
| Session persistence | Verified in v0.26.4 docs | Chat JSONL is persisted below `~/.qwen/projects/<cwd>/chats`. |
| Session resume / continue | Verified in v0.26.4 docs | `--resume` and `--continue` are documented. Restoring a session can reintroduce prior tool outputs and context; no project/run binding was verified. |
| Cancellation | Verified in documentation; runtime unverified | SIGINT/AbortController cancellation is documented. Cleanup behavior under Windows process trees was not exercised. |
| Execution limits | Verified in v0.26.4 docs; enforcement unverified | Turn, wall-time, and tool-call budgets are documented. Tool-call counts do not necessarily include nested subagent work, so these are not security limits. |
| Model selection | Verified in docs | Provider and model configuration is supported. |
| Ollama compatibility | Verified in docs; local compatibility unverified | Ollama/OpenAI-compatible endpoints are documented; compatibility depends on tool-calling support. No request was made. |
| MCP registration | Verified in docs | MCP servers can register tools. MCP is an additional tool authority surface that would need a single explicitly approved Broker bridge. |
| Native shell and filesystem tools | Verified in docs | Built-in tools exist. Configuration allowlists do not establish that these are all possible system, synthetic, MCP, or subagent tools. |
| Native web access | Verified in current documentation; local v0.26.4 runtime unverified | Web search capability is documented in current docs. It is network authority outside the Broker. |
| Hooks | Verified in docs | Hooks can execute commands at lifecycle points and may be configured in project/user context. |
| Skills | Verified in docs | Skills can contribute instructions/tools and may have trust/activation behavior. |
| Subagents | Verified in docs | Built-in/custom agents and nested execution exist; parent tool-call budgets may not account for nested work. |
| Automatic memory / learning | Verified in docs/settings | Managed automatic memory/dream and skill learning are enabled by documented v0.26.4 defaults. These can write to Qwen-owned state outside LocalCodingAgent's memory controls. |
| Configuration isolation | Partially supported; insufficient as a security boundary | QWEN_HOME redirects user state, but system settings, environment, CLI flags, project configuration, workspace instructions, and startup inputs still need control. A user settings file currently exists. |
| Disable all built-in authority | Not established | Safe mode disables many customizations, but leaves built-in tools and built-in agents. `--core-tools` is not a complete registry restriction; empty allowlists have special unrestricted/unspecified semantics. |
| Effective tool registry inspection | Unverified / no sufficient documented guarantee found | No reviewed interface guarantees complete enumeration of every effective core, system, synthetic, MCP, skill, and nested-agent tool before execution. |
| SDK permission callback as sole enforcement | Not sufficient | SDKs are experimental in current docs; callbacks may not cover already-approved tools and read-only/system tools. Core-tool allowlists do not constrain every tool class. SDK packages are absent locally. |
| Windows whole-CLI sandbox | Documented, unavailable and insufficient as-is | Docker/Podman is required by docs and unavailable locally. The documented default mounts the workspace and Qwen state; a writable project mount would bypass Broker authorization. |
| macOS sandbox | Documented, untested and insufficient as-is | Seatbelt modes are documented; permissive defaults allow network. OS containment still needs a read-only view or mediated write path to preserve Broker-only writes. |

## Threat model and required controls

Qwen Code is an untrusted planner/agent process. It must not be the authority for project identity, filesystem access, commands, network access, memory, or verification.

| Bypass path | Risk | Required control before integration |
|---|---|---|
| Native file write/edit tools | Writes directly around Broker permits, compare-and-swap checks, protected paths, and audit receipts. | Deny direct project writes at the OS boundary. Provide only Broker-mediated, explicitly registered tools. |
| Native shell | Can write files, spawn tools, access credentials/network, and evade pinned-test scope. | Disable at tool registry and enforce OS sandbox policy that denies process creation or restricts it to a separately reviewed fixed executor. Never rely on prompt/config alone. |
| Hooks/startup scripts | Execute before/during/after a task and can mutate files or launch processes. | Use a fresh private Qwen home, no project hooks/config, safe mode where useful, and OS-enforced execution denial. Verify effective settings before each run. |
| Skills/subagents | Inject instructions/tools; nested agents may have unbounded or broader capabilities. | Disable skills/custom agents/team features; reject unknown tools and nested agent tools. Enforce resource budgets outside Qwen. |
| MCP servers | An arbitrary MCP server can perform actions outside the Broker. | Start no inherited servers. Allow only a host-created Broker endpoint with exact versioned schemas; reject every other server/tool. The endpoint must not transfer Windows execution authority to a Mac inference host. |
| Automatic memory writes | Creates competing persistent/authoritative memory and can persist poisoned instructions. | Disable automatic memory/skill/dream features; use disposable Qwen home; never import Qwen memory as project authority. |
| Configuration inheritance | System/user/project settings and env/CLI precedence may activate tools or alter policy. | Construct a scrubbed environment, private home/config roots, explicit config, no project `.qwen` inputs, and verify effective configuration/registry against a pinned manifest. Unknown fields fail closed. |
| Credentials in environment | Harness tools or subprocesses can read provider tokens, Git credentials, or other secrets. | Use a minimal allowlisted environment; provide only the inference credential needed, scoped to loopback/local endpoint; do not expose general user credentials. |
| Workspace instructions | AGENTS/QWEN files and source comments are untrusted task data and may contain prompt injection. | Preserve LocalCodingAgent AGENTS.md authority; delimit repository text as data. Instructions may not alter Broker policy or expand scope. |
| Session restoration | Old tool outputs, paths, authorizations, or prompt-injected context can be replayed. | Default to new sessions. Resume only with run ID, exact project identity, configuration/tool-manifest digest, and session provenance match; otherwise reject. |
| Network/web tools | Exfiltration, remote tool execution, or unapproved source retrieval. | Deny network at OS boundary except a separately scoped local inference channel. Do not grant Qwen direct web access. |
| Process spawning | Child processes can outlive cancellation, escape accounting, or retain authority. | OS job/process-group containment, kill-on-close, wall-clock/resource limits, and post-cancel process-tree verification. |

The documented CLI controls are useful defense-in-depth but do not demonstrate the required complete mediation. A writable mounted project or inherited settings file is enough to violate the Broker-only write invariant. Since the local runtime cannot be launched compatibly and no OS isolation backend is available for this probe, the integration gate fails closed.

## Recommendation and gated acceptance criteria

**Recommendation: remain experimental and do not enable a Qwen adapter.** No `QwenCodeHarness` implementation or CLI wiring was added. The existing custom harness/default execution path remains authoritative and unchanged.

Reopen the probe only after an approved disposable environment is available and can demonstrate all of the following:

1. A pinned Qwen version and supported runtime, started with an allowlisted environment and isolated configuration/home.
2. An OS-enforced sandbox with no direct project writes, shell/process spawning, network, or access to authoritative memory; local inference connectivity is separately bounded.
3. A complete, inspectable effective tool registry checked against an explicit allowlist before the first model turn. Unknown tools, dynamic tools, nested agents, hooks, skills, and MCP servers fail closed.
4. The only enabled action tools are explicit Broker-backed schemas, with every call revalidated by the host Broker for project identity, permit, hold, pinned-test scope, path, and instruction-file policy.
5. Startup and runtime tests prove unauthorized writes, shell attempts, network attempts, prompt injection, hooks, MCP injection, and session replay cannot cross the boundary.
6. Cancellation kills the complete process tree; limits are enforced by the host/OS, including child and subagent work.
7. Harness output remains untrusted. Host verification independently hashes filesystem state, checks Git snapshots and test receipts, and owns final result classification.
8. Qwen session/memory/skill state is disposable and cannot become LocalCodingAgent authoritative memory without a separate evidence/review workflow.

## Evidence sources

Documentation references are pinned to the locally installed Qwen Code version when possible; current SDK/web-search pages may describe a newer release and are labeled accordingly.

- [v0.26.4 headless mode](https://github.com/QwenLM/qwen-code/blob/v0.26.4/docs/users/features/headless.md)
- [v0.26.4 settings](https://github.com/QwenLM/qwen-code/blob/v0.26.4/docs/users/configuration/settings.md)
- [v0.26.4 sandbox](https://github.com/QwenLM/qwen-code/blob/v0.26.4/docs/users/features/sandbox.md)
- [v0.26.4 model providers](https://github.com/QwenLM/qwen-code/blob/v0.26.4/docs/users/configuration/model-providers.md)
- [Python SDK documentation](https://github.com/QwenLM/qwen-code/blob/main/docs/developers/sdk-python.md)
- [TypeScript SDK documentation](https://github.com/QwenLM/qwen-code/blob/main/docs/developers/sdk-typescript.md)
- [Hooks](https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/hooks.md)
- [Memory](https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/memory.md)
- [Web search tool](https://github.com/QwenLM/qwen-code/blob/main/docs/developers/tools/web-search.md)
