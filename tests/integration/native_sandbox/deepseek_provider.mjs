import { writeFileSync } from "node:fs";
import { LlmAdapter } from "__DSH_LLM_MODULE__";

export const name = "sandbox-audit";
export const inject = ["llm"];
const call = __AUDIT_CALL__;

class ScriptedAdapter extends LlmAdapter {
  async listModels() {
    return [{ provider: "octomate-test", id: "mock", name: "Mock" }];
  }

  async resolveModel(provider, model) {
    return { provider, id: model, name: model };
  }

  async *stream(options) {
    const last = options.messages.at(-1);
    if (last?.role === "tool") {
      const text = last.content.map(block => block.type === "text" ? block.text : JSON.stringify(block)).join("\n");
      writeFileSync(__AUDIT_RESULT__, JSON.stringify({
        text,
        denied: last.isError === true && /permission denied|access denied|outside.*(?:workspace|root)|EACCES|EPERM/i.test(text),
      }));
      yield { type: "block-start", index: 0, blockType: "text" };
      yield { type: "text-delta", index: 0, text: "done" };
      yield { type: "block-end", index: 0, block: { type: "text", text: "done" } };
      yield { type: "finish", reason: { kind: "stop" } };
      return;
    }
    yield { type: "block-start", index: 0, blockType: "tool-call" };
    yield { type: "tool-call-delta", index: 0, id: "audit", name: call.name, argumentsDelta: call.arguments };
    yield { type: "block-end", index: 0, block: { type: "tool-call", id: "audit", ...call } };
    yield { type: "finish", reason: { kind: "tool-calls" } };
  }
}

export async function apply(ctx) {
  ctx.llm.registerAdapter(["octomate-test"], new ScriptedAdapter());
}
