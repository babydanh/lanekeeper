/**
 * lanekeeper bridge for omp.
 *
 * Forwards omp session, prompt, tool and shutdown events to the Python hook entry point:
 *   <python> -m lanekeeper hook omp <kind>
 * The event goes in as JSON on stdin; the decision comes back as JSON on stdout.
 *
 * Fail-open: if Python is missing, times out, or exits non-zero, the event passes through
 * unchanged. The only hard block is a destructive git command the user has not allowed.
 *
 * Environment: LANEKEEPER_PYTHON selects the interpreter (default "python").
 * Field names on the omp side (ctx.agent.kind, ctx.sessionManager, event.text, etc.) follow
 * omp's activity-ledger extension; they are not verified against a newer omp.
 */
import { spawnSync } from "node:child_process";

type Kind = "shell" | "edit" | "post_edit" | "prompt" | "session_start" | "stop" | "session_end";

interface Decision {
  block: boolean;
  reason?: string;
  additionalContext?: string;
  notify: string[];
}

interface SessionManagerLike {
  getSessionId?: () => unknown;
  getSessionFile?: () => unknown;
}
interface AgentLike {
  kind?: unknown;
  name?: unknown;
  parentId?: unknown;
}
interface UiLike {
  notify?: (message: string, type?: string) => void;
}
interface CtxLike {
  sessionManager?: SessionManagerLike;
  agent?: AgentLike;
  cwd?: unknown;
  ui?: UiLike;
}

interface InputEvent {
  text?: unknown;
}
interface AssistantEvent {
  message?: unknown;
  content?: unknown;
}
interface ToolCallEvent {
  toolName?: unknown;
  toolCallId?: unknown;
  input?: unknown;
}
interface ToolResultEvent {
  toolCallId?: unknown;
  isError?: unknown;
}
interface ToolCallResult {
  block?: boolean;
  reason?: string;
  additionalContext?: string;
}

interface PiLike {
  on(event: "session_start" | "session_stop" | "session_shutdown", handler: (event: unknown, ctx: CtxLike) => Promise<void>): void;
  on(event: "input", handler: (event: InputEvent, ctx: CtxLike) => Promise<void>): void;
  on(event: "assistant_message", handler: (event: AssistantEvent) => Promise<void>): void;
  on(event: "tool_call", handler: (event: ToolCallEvent, ctx: CtxLike) => Promise<ToolCallResult | undefined>): void;
  on(event: "tool_result", handler: (event: ToolResultEvent, ctx: CtxLike) => Promise<undefined>): void;
  sendMessage?: (
    message: { customType: string; content: string; display: boolean },
    options?: { deliverAs?: string },
  ) => void;
}

const PYTHON = process.env.LANEKEEPER_PYTHON || "python";
const TIMEOUT_MS = 15_000;
const NOTICE_TYPE = "lanekeeper-notice";
const MAX_CACHE = 500;

// toolCallId -> intent text from the assistant message (omp strips `i` from tool input).
const intents = new Map<string, string>();
// toolCallId -> tool name and input, kept for write/edit so tool_result can report the files.
const edits = new Map<string, { tool: string; input: Record<string, unknown> }>();

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function remember<V>(map: Map<string, V>, key: string, value: V): void {
  map.set(key, value);
  if (map.size > MAX_CACHE) {
    for (const old of [...map.keys()].slice(0, MAX_CACHE / 2)) map.delete(old);
  }
}

function parseDecision(text: string): Decision | undefined {
  const data: unknown = JSON.parse(text);
  if (typeof data !== "object" || data === null) return undefined;
  const block = "block" in data && data.block === true;
  const reason = "reason" in data ? asString(data.reason) : undefined;
  const additionalContext = "additionalContext" in data ? asString(data.additionalContext) : undefined;
  const notifyRaw: unknown[] = "notify" in data && Array.isArray(data.notify) ? data.notify : [];
  const notify = notifyRaw.filter((line): line is string => typeof line === "string");
  return { block, reason, additionalContext, notify };
}

function call(kind: Kind, payload: Record<string, unknown>): Decision | undefined {
  try {
    const run = spawnSync(PYTHON, ["-m", "lanekeeper", "hook", "omp", kind], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: TIMEOUT_MS,
      windowsHide: true,
    });
    if (run.error || run.status !== 0 || !run.stdout) return undefined;
    const text = run.stdout.trim();
    return text ? parseDecision(text) : undefined;
  } catch {
    return undefined;
  }
}

function sessionOf(ctx: CtxLike | undefined): Record<string, unknown> {
  let id: unknown;
  let file: unknown;
  try {
    id = ctx?.sessionManager?.getSessionId?.();
  } catch {
    id = undefined;
  }
  try {
    file = ctx?.sessionManager?.getSessionFile?.();
  } catch {
    file = undefined;
  }
  const agent: AgentLike = ctx?.agent ?? {};
  return {
    session_id: asString(id) ?? `pid-${process.pid}`,
    session_file: asString(file),
    cwd: asString(ctx?.cwd) ?? process.cwd(),
    pid: process.pid,
    agent_kind: asString(agent.kind),
    agent_name: asString(agent.name),
    parent_id: asString(agent.parentId),
  };
}

/** Show user notices in omp's UI and queue agent context for the next turn. */
function deliver(pi: PiLike, ctx: CtxLike | undefined, decision: Decision | undefined): void {
  if (!decision) return;
  for (const line of decision.notify) ctx?.ui?.notify?.(line, "warning");
  if (decision.additionalContext) {
    pi.sendMessage?.(
      { customType: NOTICE_TYPE, content: decision.additionalContext, display: true },
      { deliverAs: "nextTurn" },
    );
  }
}

export default function (pi: PiLike): void {
  pi.on("session_start", async (_event, ctx) => {
    deliver(pi, ctx, call("session_start", sessionOf(ctx)));
  });

  pi.on("input", async (event, ctx) => {
    deliver(pi, ctx, call("prompt", { ...sessionOf(ctx), prompt: asString(event.text) }));
  });

  pi.on("assistant_message", async (event) => {
    const content =
      event.message && typeof event.message === "object" && "content" in event.message
        ? event.message.content
        : event.content;
    if (!Array.isArray(content)) return;
    const blocks: unknown[] = content;
    for (const block of blocks) {
      if (typeof block !== "object" || block === null) continue;
      if (!("type" in block) || block.type !== "toolCall") continue;
      const id = "id" in block ? asString(block.id) : undefined;
      if (!id) continue;
      const fromBlock = "intent" in block ? asString(block.intent) : undefined;
      const args = "arguments" in block ? block.arguments : undefined;
      const fromArgs = args && typeof args === "object" && "i" in args ? asString(args.i) : undefined;
      const intent = fromBlock ?? fromArgs;
      if (intent) remember(intents, id, intent);
    }
  });

  pi.on("tool_call", async (event, ctx) => {
    const tool = (asString(event.toolName) ?? "").replace(/^_+/, "").toLowerCase();
    const kind: Kind | undefined =
      tool === "bash" ? "shell" : tool === "write" || tool === "edit" ? "edit" : undefined;
    if (!kind) return undefined;

    const input: Record<string, unknown> =
      event.input && typeof event.input === "object" ? { ...event.input } : {};
    const callId = asString(event.toolCallId);
    const intent = callId ? intents.get(callId) : undefined;
    if (callId) intents.delete(callId);
    if (callId && kind === "edit") remember(edits, callId, { tool, input });

    const decision = call(kind, {
      ...sessionOf(ctx),
      tool_name: tool,
      tool_input: input,
      call_id: callId,
      intent,
    });
    if (!decision) return undefined;
    if (decision.block) return { block: true, reason: decision.reason ?? "[lanekeeper] blocked" };
    for (const line of decision.notify) ctx?.ui?.notify?.(line, "warning");
    if (decision.additionalContext) return { additionalContext: decision.additionalContext };
    return undefined;
  });

  pi.on("tool_result", async (event, ctx) => {
    const callId = asString(event.toolCallId);
    const cached = callId ? edits.get(callId) : undefined;
    if (!callId || !cached) return undefined;
    edits.delete(callId);
    deliver(
      pi,
      ctx,
      call("post_edit", {
        ...sessionOf(ctx),
        tool_name: cached.tool,
        tool_input: cached.input,
        call_id: callId,
        ok: event.isError !== true,
      }),
    );
    return undefined;
  });

  pi.on("session_stop", async (_event, ctx) => {
    deliver(pi, ctx, call("stop", sessionOf(ctx)));
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    deliver(pi, ctx, call("session_end", sessionOf(ctx)));
  });
}
