import {describe, expect, test} from "vitest";

import {ChatState} from "./providers";

const ANSWER = "Ernst Brenner's spouse was Lina Sturzenegger.";

function newTurn(): ChatState {
  const state = new ChatState({apiUrl: "http://localhost/chat"});
  state.appendMessage("Who was the spouse of Ernst Brenner?", "user");
  state.appendMessage("", "assistant");
  state.toolCallCount = 0;
  return state;
}

function assistantMessages(state: ChatState) {
  return state.messages().filter(m => m.role === "assistant");
}

describe("tool activity folding", () => {
  test("a model that restates its answer each tool round renders it once", () => {
    // The reported bug: the agent wrote the same prose in three ReAct rounds and
    // the chat body showed the answer three times.
    const state = newTurn();
    state.appendContentToLastMsg(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendContentToLastMsg(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendContentToLastMsg(ANSWER);
    state.finishActivityStep();

    const shown = assistantMessages(state)
      .map(m => m.content())
      .join("");
    expect(shown.match(/Ernst Brenner/g)?.length).toBe(1);
    expect(shown.trim()).toBe(ANSWER);
  });

  test("tool results do not each open a new message bubble", () => {
    const state = newTurn();
    for (let i = 0; i < 7; i++) state.recordToolResult("📡 Execute sparql query", `rows ${i}`);
    state.appendContentToLastMsg(ANSWER);
    state.finishActivityStep();

    expect(assistantMessages(state).length).toBe(1);
  });

  test("seven tool calls collapse into a single activity step", () => {
    const state = newTurn();
    for (let i = 0; i < 7; i++) state.recordToolResult("📡 Execute sparql query", `rows ${i}`);
    state.finishActivityStep();

    const steps = assistantMessages(state)[0].steps();
    expect(steps.filter(s => s.isActivity).length).toBe(1);
    expect(steps.length).toBe(1);
  });

  test("the activity label is live while running and settles when the turn ends", () => {
    const state = newTurn();
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.recordToolResult("🔧 Search sparql docs", "docs");
    const live = assistantMessages(state)[0].steps()[0].label;
    expect(live).toContain("Searching the knowledge graph");

    state.finishActivityStep();
    const settled = assistantMessages(state)[0].steps()[0].label;
    expect(settled).toContain("2 search steps");
    expect(settled).not.toContain("Searching the knowledge graph");
  });

  test("nothing the agent did is lost — details keep every tool result", () => {
    const state = newTurn();
    state.appendContentToLastMsg("draft prose that was replaced");
    state.recordToolResult("📡 Execute sparql query", "the query rows");
    state.recordToolResult("🔧 Search sparql docs", "the docs");
    state.finishActivityStep();

    const details = assistantMessages(state)[0].steps()[0].details;
    expect(details).toContain("the query rows");
    expect(details).toContain("the docs");
    expect(details).toContain("draft prose that was replaced");
  });

  test("a single-step label reads in the singular", () => {
    const state = newTurn();
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.finishActivityStep();
    expect(assistantMessages(state)[0].steps()[0].label).toContain("1 search step ");
  });

  test("a turn with no tool calls adds no activity step", () => {
    const state = newTurn();
    state.appendContentToLastMsg(ANSWER);
    state.finishActivityStep();

    const msg = assistantMessages(state)[0];
    expect(msg.steps().length).toBe(0);
    expect(msg.content()).toBe(ANSWER);
  });
});
