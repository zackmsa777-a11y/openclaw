// Offline boundary reproduction of PR #142470's Discord report. No Discord API calls.
// Queue admission and the Discord REST encoder are outside this test's scope.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setActivePluginRegistry } from "../../plugins/runtime.js";
import { createChannelTestPluginBase, createTestRegistry } from "../../test-utils/channel-plugins.js";
import type { ReplyPayload } from "../types.js";
import type { AgentTurnExecutionResult } from "./agent-runner-execution.types.js";
import { deliverFollowupDecision, resolveFollowupDeliveryDecision } from "./followup-delivery.js";
import type { AdmittedFollowupTurn } from "./followup-turn-admission.js";
import { applyReplyThreading } from "./reply-payloads-base.js";
import { routeReply } from "./route-reply.js";

const transport = vi.hoisted(() => ({ deliver: vi.fn() }));
vi.mock("../../agents/runtime-plan/build.js", () => ({
  buildAgentRuntimeDeliveryPlan: () => ({
    isSilentPayload: () => false,
    resolveFollowupRoute: () => undefined,
  }),
}));
vi.mock("../../infra/outbound/deliver.js", () => ({
  deliverOutboundPayloadsInternal: (...args: unknown[]) => transport.deliver(...args),
}));

beforeEach(() => {
  // Match Discord's resolver-less contract; don't replace it with a resolver
  // that would conceal the missing implicit-target handoff.
  const discord = createChannelTestPluginBase({
    id: "discord", label: "Discord",
    config: { listAccountIds: () => [], resolveAccount: () => ({}) },
  });
  setActivePluginRegistry(createTestRegistry([
    { pluginId: "discord", plugin: discord, source: "test" },
  ]));
  transport.deliver.mockReset();
  transport.deliver.mockResolvedValue([{ channel: "discord", messageId: "sent" }]);
});
afterEach(() => setActivePluginRegistry(createTestRegistry()));

function turnFor(messageId: string): AdmittedFollowupTurn {
  return {
    runId: `run-${messageId}`,
    queued: {
      prompt: "queued user question", enqueuedAt: 1, messageId,
      originatingChannel: "discord", originatingTo: "channel:C1",
      originatingChatType: "group", originatingReplyToMode: "all",
      run: {
        agentId: "agent", agentDir: "/tmp/agent", sessionId: "session",
        sessionKey: "main", sessionFile: "/tmp/session.jsonl", workspaceDir: "/tmp",
        config: {}, provider: "anthropic", model: "claude", messageProvider: "discord",
        inputProvenance: { kind: "external_user" }, timeoutMs: 1_000,
        blockReplyBreak: "message_end",
      },
    },
    // The delivery boundary does not invoke the admission operation.
    operation: {} as AdmittedFollowupTurn["operation"], config: {},
    session: { kind: "session", key: "main", current: () => undefined,
      publish: () => undefined, adopt: () => undefined },
    sendPolicy: "allow", preflightCompactionApplied: false,
  };
}

async function finalFor(turn: AdmittedFollowupTurn, payload: ReplyPayload) {
  const execution: AgentTurnExecutionResult = {
    runId: turn.runId,
    outcome: {
      kind: "settled", status: "ok",
      result: { payloads: [payload], meta: { durationMs: 0 } },
      resolved: { provider: "anthropic", model: "claude" },
      fallback: { exhausted: false, attempts: [] }, autoCompactionCount: 0,
      didLogHeartbeatStrip: false,
    },
  };
  const decision = resolveFollowupDeliveryDecision({
    turn, execution,
    accounting: {
      payloadArray: execution.outcome.kind === "settled" ? execution.outcome.result.payloads : [],
      providerUsed: "anthropic", modelUsed: "claude", preserveUserFacingSessionState: false,
      replyUsageState: {}, usage: undefined, terminalFailurePayload: undefined,
    } as never,
  });
  expect(decision.kind).toBe("deliver");
  const fallbackDispatcher = vi.fn(async () => {});
  await deliverFollowupDecision({
    decision, turn, runId: turn.runId, runFollowup: vi.fn(async () => {}),
    defaults: {
      defaultModel: "claude", typingMode: "never",
      typing: {
        onReplyStart: vi.fn(async () => {}), startTypingLoop: vi.fn(async () => {}),
        startTypingOnText: vi.fn(async () => {}), refreshTypingTtl: vi.fn(),
        isActive: vi.fn(() => false), markRunComplete: vi.fn(),
        markDispatchIdle: vi.fn(), cleanup: vi.fn(),
      },
      opts: { onBlockReply: fallbackDispatcher },
    },
  });
  expect(fallbackDispatcher).not.toHaveBeenCalled();
}

async function ordinaryPayload(messageId: string, text: string) {
  const [payload] = applyReplyThreading({
    payloads: [{ text }], replyToMode: "all", replyToChannel: "discord",
    currentMessageId: messageId,
  });
  await routeReply({ payload, channel: "discord", to: "channel:C1", cfg: {},
    currentMessageId: messageId, mirror: false });
}

function deliveredReplyIds() {
  return transport.deliver.mock.calls.map(([request]) => request.replyToId);
}

describe("Discord queued-final overlap boundary", () => {
  it("keeps B and C final replies attached after their three progress replies", async () => {
    // Replay the report's delivery order in one session, without a restart.
    // A is ordinary; B and C are already-admitted follow-ups.
    await ordinaryPayload("discord-message-A", "A final");
    for (const messageId of ["discord-message-B", "discord-message-C"]) {
      for (let n = 1; n <= 3; n++) await ordinaryPayload(messageId, `progress ${n}`);
      await finalFor(turnFor(messageId), { text: `final for ${messageId}` });
    }
    // Assert the durable-send boundary, not just the prepared payload.
    expect(deliveredReplyIds()).toEqual([
      "discord-message-A",
      "discord-message-B", "discord-message-B", "discord-message-B", "discord-message-B",
      "discord-message-C", "discord-message-C", "discord-message-C", "discord-message-C",
    ]);
  });

  it("keeps explicit final targets authoritative", async () => {
    await finalFor(turnFor("discord-message-B"), { text: "explicit final", replyToId: "explicit-target" });
    expect(deliveredReplyIds()).toEqual(["explicit-target"]);
  });

  it("does not inject a final target when reply mode is off", async () => {
    const turn = turnFor("discord-message-B");
    turn.queued.originatingReplyToMode = "off";
    await finalFor(turn, { text: "standalone final" });
    expect(deliveredReplyIds()).toEqual([null]);
  });
});
