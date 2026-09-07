import {describe, expect, test} from "vitest";

import {ChatState} from "./providers";

const ANSWER = "Ernst Brenner's spouse was Lina Sturzenegger.";

function newTurn(): ChatState {
  const state = new ChatState({apiUrl: "http://localhost/chat"});
  state.appendMessage("Who was the spouse of Ernst Brenner?", "user");
  state.appendMessage("", "assistant");
  state.beginTurn();
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
    state.appendAnswerChunk(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendAnswerChunk(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendAnswerChunk(ANSWER);
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
    state.appendAnswerChunk(ANSWER);
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
    state.recordToolResult("📡 Execute sparql query", "the query rows");
    state.recordToolResult("🔧 Search sparql docs", "the docs");
    state.finishActivityStep();

    const details = assistantMessages(state)[0].steps()[0].details;
    expect(details).toContain("the query rows");
    expect(details).toContain("the docs");
  });

  test("a single-step label reads in the singular", () => {
    const state = newTurn();
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.finishActivityStep();
    expect(assistantMessages(state)[0].steps()[0].label).toContain("1 search step ");
  });

  test("the answer survives when the turn ends on a tool call", () => {
    // The agent answered, then kept exploring and never spoke again. Clearing
    // the prose on each tool result threw the only answer away and left the
    // turn with nothing but an activity pill.
    const state = newTurn();
    state.appendAnswerChunk(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.recordToolResult("📡 Execute sparql query", "more rows");
    state.finishActivityStep();

    expect(assistantMessages(state)[0].content().trim()).toBe(ANSWER);
  });

  test("a superseded draft is replaced, not appended, when a later round speaks", () => {
    const state = newTurn();
    state.appendAnswerChunk("first draft");
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendAnswerChunk(ANSWER);
    state.finishActivityStep();

    const shown = assistantMessages(state)[0].content().trim();
    expect(shown).toBe(ANSWER);
    expect(shown).not.toContain("first draft");
  });

  test("a superseded draft is preserved in the activity details", () => {
    const state = newTurn();
    state.appendAnswerChunk("first draft");
    state.recordToolResult("📡 Execute sparql query", "rows");
    state.appendAnswerChunk(ANSWER);
    state.finishActivityStep();

    expect(assistantMessages(state)[0].steps()[0].details).toContain("first draft");
  });

  test("prose streamed in one round is not cleared until the next round speaks", () => {
    // Avoids the flicker of blanking the answer the moment a tool result lands
    // when nothing may ever replace it.
    const state = newTurn();
    state.appendAnswerChunk(ANSWER);
    state.recordToolResult("📡 Execute sparql query", "rows");
    expect(assistantMessages(state)[0].content().trim()).toBe(ANSWER);
  });

  test("a turn with no tool calls adds no activity step", () => {
    const state = newTurn();
    state.appendAnswerChunk(ANSWER);
    state.finishActivityStep();

    const msg = assistantMessages(state)[0];
    expect(msg.steps().length).toBe(0);
    expect(msg.content()).toBe(ANSWER);
  });
});
