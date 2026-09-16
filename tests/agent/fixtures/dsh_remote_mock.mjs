import { LlmAdapter } from "__DSH_LLM_MODULE__";

export const name = "octomate-smoke";
export const inject = ["llm", "approval", "userQuestions"];

class Mock extends LlmAdapter {
  async listModels() {
    return [{ provider: "octomate-test", id: "mock", name: "Mock" }];
  }

  async resolveModel(provider, model) {
    return { provider, id: model, name: model };
  }

  async *stream(options) {
    const result = options.messages.at(-1)?.content.find(
      (block) => block.type === "tool-result",
    );
    if (!result) {
      const args = JSON.stringify({
        command: "printf OCTOMATE_SMOKE",
        description: "Test isolated integration",
      });
      yield { type: "block-start", index: 0, blockType: "tool-call" };
      yield {
        type: "tool-call-delta", index: 0, id: "smoke-call",
        name: "bash", argumentsDelta: args,
      };
      yield {
        type: "block-end", index: 0,
        block: { type: "tool-call", id: "smoke-call", name: "bash", arguments: args },
      };
      yield { type: "finish", reason: { kind: "tool-calls" } };
      return;
    }
    const text = "Octomate Remote API works";
    yield { type: "block-start", index: 0, blockType: "text" };
    yield { type: "text-delta", index: 0, text };
    yield { type: "block-end", index: 0, block: { type: "text", text } };
    yield { type: "usage", usage: { inputTokens: 11, outputTokens: 6 } };
    yield { type: "finish", reason: { kind: "stop" } };
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(["octomate-test"], new Mock());
  ctx.on("agent/request", async ({ agent, step, signal }, next) => {
    if (step === 1) {
      const outcome = await ctx.approval.request({
        agent, toolName: "smoke-approval", reason: "Isolated test", signal,
      });
      if (outcome !== "allowed-once") {
        throw new Error("Unexpected approval reply: " + outcome);
      }
      const answer = await ctx.userQuestions.ask({
        agent, signal,
        questions: [{ id: "smoke-question", question: "Continue?" }],
      });
      if (answer.answers[0]?.custom !== "continue") {
        throw new Error("Unexpected question reply");
      }
    }
    return next();
  });
}
