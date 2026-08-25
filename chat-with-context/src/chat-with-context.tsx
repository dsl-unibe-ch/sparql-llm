import {createSignal, For, createEffect, Show} from "solid-js";
import {customElement, noShadowDOM} from "solid-element";
import {marked} from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import "highlight.js/styles/default.min.css";

import {style} from "./utils";
import {hljsDefineSparql, hljsDefineTurtle} from "./highlight";
import arrowUpIcon from "./assets/arrow-up.svg";
import xIcon from "./assets/x.svg";
import editIcon from "./assets/edit.svg";
import squareIcon from "./assets/square.svg";
import thumbsDownIcon from "./assets/thumbs-down.svg";
import thumbsUpIcon from "./assets/thumbs-up.svg";
import "./style.css";
import {streamResponse, ChatState} from "./providers";

// Get icons svg from https://feathericons.com/
// SolidJS custom element: https://github.com/solidjs/solid/blob/main/src/solid-element/README.md

/**
 * Custom element to create a chat interface with a context-aware assistant.
 * @example <chat-with-context api="http://localhost:8000/"></chat-with-context>
 */
customElement(
  "chat-with-context",
  {chatEndpoint: "", examples: "", apiKey: "", feedbackEndpoint: "", model: "", models: ""},
  props => {
    noShadowDOM();
    hljs.registerLanguage("ttl", hljsDefineTurtle);
    hljs.registerLanguage("sparql", hljsDefineSparql);

    const [examples, setExamples] = createSignal<string[]>([]);
    const [warningMsg, setWarningMsg] = createSignal("");
    const [loading, setLoading] = createSignal(false);
    const [dialogOpen, setDialogOpen] = createSignal("");
    const [selectedDocsTab, setSelectedDocsTab] = createSignal("");
    const [feedbackEndpoint, setFeedbackEndpoint] = createSignal("");
    const [feedbackSent, setFeedbackSent] = createSignal(false);
    const [availableModels, setAvailableModels] = createSignal<string[]>([]);
    const [selectedModel, setSelectedModel] = createSignal("");
    const [naturalLanguageOnly, setNaturalLanguageOnly] = createSignal(false);

    const state = new ChatState({});
    let chatContainerEl!: HTMLDivElement;
    let inputTextEl!: HTMLTextAreaElement;

    marked.use({gfm: true});

    createEffect(() => {
      if (props.chatEndpoint === "") setWarningMsg("Please provide an API URL for the chat component to work.");
      state.apiUrl = props.chatEndpoint;
      state.apiKey = props.apiKey;
      state.scrollToInput = () => {};
      state.onMessageUpdate = () => highlightAll();
      setExamples(props.examples.split(",").map(value => value.trim()));
      setFeedbackEndpoint(props.feedbackEndpoint);
      fixInputHeight();

      // Parse models prop (comma-separated: "gpustack/foo,gpustack/bar")
      if (props.models) {
        const list = props.models.split(",").map(m => m.trim()).filter(Boolean);
        setAvailableModels(list);
        // Use props.model as default if provided and in list, otherwise first
        const def = props.model && list.includes(props.model) ? props.model : list[0];
        if (def) {
          setSelectedModel(def);
          state.model = def;
        }
      } else if (props.model) {
        state.model = props.model;
        setSelectedModel(props.model);
      }
    });

    const handleModelChange = (model: string) => {
      setSelectedModel(model);
      state.model = model;
    };

    const handleNaturalLanguageOnlyChange = (enabled: boolean) => {
      setNaturalLanguageOnly(enabled);
      state.naturalLanguageOnly = enabled;
    };

    // Display label: strip "provider/" prefix for readability
    const modelLabel = (m: string) => m.replace(/^[^/]+\//, "");

    const openDialog = (dialogId: string) => {
      setDialogOpen(dialogId);
      (document.getElementById(dialogId) as HTMLDialogElement).showModal();
      history.pushState({dialogOpen: true}, "");
      document.body.style.overflow = "hidden";
      highlightAll();
    };

    const closeDialog = () => {
      document.body.style.overflow = "";
      const dialogEl = document.getElementById(dialogOpen()) as HTMLDialogElement;
      if (dialogEl) dialogEl.close();
      setDialogOpen("");
    };

    createEffect(() => {
      window.addEventListener("popstate", event => {
        if (dialogOpen()) {
          event.preventDefault();
          closeDialog();
        }
      });
    });

    const highlightAll = () => {
      document.querySelectorAll("pre code:not(.hljs)").forEach(block => {
        hljs.highlightElement(block as HTMLElement);
      });
    };

    async function submitInput(question: string) {
      if (!question.trim()) return;
      if (loading()) return;
      inputTextEl.value = "";
      setLoading(true);
      setWarningMsg("");
      setTimeout(() => fixInputHeight(), 0);
      const startTime = Date.now();
      try {
        await streamResponse(state, question);
      } catch (error) {
        if (error instanceof Error && error.name !== "AbortError") {
          console.error("An error occurred when querying the API:", error);
          setWarningMsg("An error occurred when querying the API. Please try again or contact an admin.");
        }
      }
      setLoading(false);
      setFeedbackSent(false);
      highlightAll();
      state.scrollToInput();
      console.log(`Request completed in ${(Date.now() - startTime) / 1000} seconds`);
    }

    function sendFeedback(positive: boolean) {
      fetch(feedbackEndpoint(), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          like: positive,
          messages: state.messages().map(msg => ({
            role: msg.role,
            content: msg.content(),
            steps: msg.steps().map(step => ({
              label: step.label,
              details: step.details,
              node_id: step.node_id,
              substeps: step.substeps,
            })),
          })),
        }),
      });
      setFeedbackSent(true);
    }

    function fixInputHeight() {
      const scrollX = window.scrollX || window.pageXOffset;
      const scrollY = window.scrollY || window.pageYOffset;
      inputTextEl.style.height = "auto";
      inputTextEl.style.height = inputTextEl.scrollHeight + "px";
      window.scrollTo(scrollX, scrollY);
      setTimeout(() => window.scrollTo(scrollX, scrollY), 0);
    }

    return (
      <div
        class={`chat-with-context w-full h-full flex flex-col ${state.messages().length === 0 ? "justify-center" : ""}`}
        style={{"min-height": "0"}}
      >
        <style>{style}</style>

        {/* Messages area — flex-grow + min-height:0 so it scrolls within the flex parent */}
        <div
          ref={chatContainerEl}
          class="overflow-y-auto flex-1 px-1"
          style={{"min-height": "0"}}
        >
          <For each={state.messages()}>
            {(msg, iMsg) => (
              <div class={`w-full flex ${msg.role === "user" ? "justify-end" : "justify-start"} mb-4`}>
                <div
                  class={`max-w-3xl ${
                    msg.role === "user"
                      ? "bg-slate-700 text-white rounded-3xl rounded-br-md px-5 py-3"
                      : "w-full px-1"
                  }`}
                >
                  {/* Steps (only for assistant) */}
                  <Show when={msg.role === "assistant"}>
                    <div class="flex flex-col items-start mb-2">
                      <For each={msg.steps()}>
                        {(step, iStep) =>
                          step.substeps && step.substeps.length > 0 ? (
                            <>
                              <button
                                class="inline-flex items-center gap-1.5 text-xs text-slate-500 mb-2 px-3 py-1.5 border border-slate-200 rounded-full bg-slate-50 hover:bg-slate-100 hover:border-slate-300 transition-all"
                                title={`Click to see documents used\n\nNode: ${step.node_id}`}
                                onClick={() => {
                                  setSelectedDocsTab(step.substeps?.[0]?.label || "");
                                  openDialog(`step-dialog-${iMsg()}-${iStep()}`);
                                }}
                              >
                                <span>{step.label}</span>
                                <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                              </button>
                              <dialog
                                id={`step-dialog-${iMsg()}-${iStep()}`}
                                class="bg-white m-3 rounded-2xl shadow-2xl w-full max-w-4xl border border-slate-200"
                                onClose={() => closeDialog()}
                              >
                                <div class="flex items-center justify-between p-4 border-b border-slate-100">
                                  <h3 class="font-semibold text-slate-700">{step.label}</h3>
                                  <button
                                    class="p-1.5 rounded-full hover:bg-slate-100 transition-colors"
                                    title="Close"
                                    onClick={() => closeDialog()}
                                  >
                                    <img src={xIcon} alt="Close" class="iconBtn w-4 h-4" />
                                  </button>
                                </div>
                                <div class="p-4">
                                  <div class="flex flex-wrap gap-2 mb-4">
                                    <For each={step.substeps.map(substep => substep.label)}>
                                      {label => (
                                        <button
                                          class={`px-3 py-1.5 text-sm rounded-full transition-all ${
                                            selectedDocsTab() === label
                                              ? "bg-slate-700 text-white"
                                              : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                                          }`}
                                          onClick={() => {
                                            setSelectedDocsTab(label);
                                            highlightAll();
                                          }}
                                        >
                                          {label}
                                        </button>
                                      )}
                                    </For>
                                  </div>
                                  <For each={step.substeps.filter(substep => substep.label === selectedDocsTab())}>
                                    {substep => (
                                      <article
                                        class="prose max-w-full"
                                        // eslint-disable-next-line solid/no-innerhtml
                                        innerHTML={DOMPurify.sanitize(marked.parse(substep.details) as string, {
                                          ADD_TAGS: ["think"],
                                        })}
                                      />
                                    )}
                                  </For>
                                </div>
                              </dialog>
                            </>
                          ) : step.details ? (
                            <>
                              <button
                                class="inline-flex items-center gap-1.5 text-xs text-slate-500 mb-2 px-3 py-1.5 border border-slate-200 rounded-full bg-slate-50 hover:bg-slate-100 hover:border-slate-300 transition-all"
                                title={`Click to see details\n\nNode: ${step.node_id}`}
                                onClick={() => openDialog(`step-dialog-${iMsg()}-${iStep()}`)}
                              >
                                <span>{step.label}</span>
                                <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                              </button>
                              <dialog
                                id={`step-dialog-${iMsg()}-${iStep()}`}
                                class="bg-white m-3 rounded-2xl shadow-2xl w-full max-w-4xl border border-slate-200"
                                onClose={() => closeDialog()}
                              >
                                <div class="flex items-center justify-between p-4 border-b border-slate-100">
                                  <h3 class="font-semibold text-slate-700">{step.label}</h3>
                                  <button
                                    class="p-1.5 rounded-full hover:bg-slate-100 transition-colors"
                                    title="Close"
                                    onClick={() => closeDialog()}
                                  >
                                    <img src={xIcon} alt="Close" class="iconBtn w-4 h-4" />
                                  </button>
                                </div>
                                <article
                                  class="prose max-w-full p-6 max-h-[70vh] overflow-y-auto"
                                  // eslint-disable-next-line solid/no-innerhtml
                                  innerHTML={DOMPurify.sanitize(marked.parse(step.details) as string, {
                                    ADD_TAGS: ["think"],
                                  })}
                                />
                              </dialog>
                            </>
                          ) : (
                            <p class="inline-flex items-center gap-1.5 text-xs text-slate-400 mb-2 px-3 py-1.5" title={`Node: ${step.node_id}`}>
                              {step.label}
                            </p>
                          )
                        }
                      </For>
                    </div>
                  </Show>

                  {/* Message content */}
                  {msg.role === "user" ? (
                    // User messages: plain text, no prose override
                    <p class="text-sm leading-relaxed whitespace-pre-wrap">{msg.content()}</p>
                  ) : (
                    <article
                      class="prose max-w-full"
                      // eslint-disable-next-line solid/no-innerhtml
                      innerHTML={DOMPurify.sanitize(marked.parse(msg.content()) as string, {ADD_TAGS: ["think"]})}
                    />
                  )}

                  {/* Run query links */}
                  <For each={msg.links()}>
                    {link => (
                      <a href={link.url} title={link.title} target="_blank" class="hover:text-inherit">
                        <button class="mt-3 mr-1 px-3 py-1.5 text-xs font-medium bg-slate-100 hover:bg-slate-200 text-slate-600 rounded-full transition-colors">
                          {link.label} →
                        </button>
                      </a>
                    )}
                  </For>

                  {/* Feedback buttons */}
                  {feedbackEndpoint() &&
                    msg.role === "assistant" &&
                    iMsg() === state.messages().length - 1 &&
                    state.lastMsg().content() &&
                    !feedbackSent() && (
                      <div class="flex gap-1 mt-2">
                        <button
                          class="p-1.5 rounded-full hover:bg-slate-100 transition-colors"
                          title="Good response"
                          onClick={() => sendFeedback(true)}
                        >
                          <img src={thumbsUpIcon} alt="Thumbs up" height="16px" width="16px" class="iconBtn" />
                        </button>
                        <button
                          class="p-1.5 rounded-full hover:bg-slate-100 transition-colors"
                          title="Bad response"
                          onClick={() => sendFeedback(false)}
                        >
                          <img src={thumbsDownIcon} alt="Thumbs down" height="16px" width="16px" class="iconBtn" />
                        </button>
                      </div>
                    )}
                </div>
              </div>
            )}
          </For>
        </div>

        {/* Warning message */}
        {warningMsg() && (
          <div class="text-center px-4 mb-2 flex-shrink-0">
            <div class="bg-amber-50 border border-amber-200 p-3 text-amber-800 text-sm rounded-xl inline-block">
              ⚠️ {warningMsg()}
            </div>
          </div>
        )}

        {/* Input area — always pinned at the bottom */}
        <div class="px-4 pb-4 flex-shrink-0">
          <div class="mx-auto max-w-3xl">
            <form
              onSubmit={event => {
                event.preventDefault();
                if (loading()) {
                  state.abortRequest();
                  return;
                }
                submitInput(inputTextEl.value);
              }}
            >
            <div class="bg-white border border-slate-300 rounded-2xl shadow-sm focus-within:ring-2 focus-within:ring-slate-400 focus-within:border-transparent transition-all">
              {/* Textarea */}
              <textarea
                ref={inputTextEl}
                autofocus
                class="w-full px-4 pt-3 pb-2 bg-transparent rounded-2xl focus:outline-none resize-none overflow-y-hidden text-slate-800 placeholder-slate-400"
                style={{"overflow-anchor": "none"}}
                placeholder="Ask a question about Swiss Elites…"
                rows="1"
                onKeyDown={event => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (loading()) {
                      state.abortRequest();
                      return;
                    }
                    submitInput(inputTextEl.value);
                  }
                }}
                onInput={() => fixInputHeight()}
              />

              {/* Bottom toolbar */}
              <div class="flex items-center justify-between px-3 pb-2">
                {/* Left: new chat + model selector */}
                <div class="flex items-center gap-2 flex-wrap">
                  <button
                    title="New conversation"
                    class="p-1.5 rounded-full text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors"
                    onClick={() => state.resetSession()}
                    type="button"
                    aria-label="Start a new conversation"
                  >
                    <img src={editIcon} alt="New conversation" class="iconBtn w-4 h-4" />
                  </button>

                  <Show when={availableModels().length > 1}>
                    <div class="relative">
                      <select
                        class="appearance-none text-xs font-medium text-slate-500 bg-slate-100 hover:bg-slate-200 border-0 rounded-full pl-3 pr-7 py-1.5 cursor-pointer focus:outline-none focus:ring-2 focus:ring-slate-300 transition-colors"
                        value={selectedModel()}
                        onChange={e => handleModelChange((e.target as HTMLSelectElement).value)}
                        title="Select AI model"
                      >
                        <For each={availableModels()}>
                          {m => <option value={m}>{modelLabel(m)}</option>}
                        </For>
                      </select>
                      <svg class="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none text-slate-400" xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                    </div>
                  </Show>
                  
                  <label class="flex items-center gap-1.5 cursor-pointer text-xs font-medium text-slate-500 bg-slate-100 hover:bg-slate-200 border-0 rounded-full px-3 py-1.5 transition-colors">
                    <input 
                      type="checkbox" 
                      class="accent-slate-500 w-3 h-3 cursor-pointer"
                      checked={naturalLanguageOnly()} 
                      onChange={e => handleNaturalLanguageOnlyChange((e.target as HTMLInputElement).checked)} 
                    />
                    <span>Natural Language Only</span>
                  </label>
                </div>

                {/* Right: send / stop button */}
                <button
                  type="submit"
                  title={loading() ? "Stop generation" : "Send question"}
                  class={`w-8 h-8 flex items-center justify-center rounded-full transition-colors ${
                    loading() ? "bg-slate-700 loading-spark" : "bg-slate-700 hover:bg-slate-800"
                  }`}
                  aria-label={loading() ? "Stop generation" : "Send question"}
                >
                  {loading() ? (
                    <img src={squareIcon} alt="Stop" class="w-3 h-3" style="filter: invert(1)" />
                  ) : (
                    <img src={arrowUpIcon} alt="Send" class="w-4 h-4" style="filter: invert(1)" />
                  )}
                </button>
              </div>
            </div>
            </form>

            {/* Hint text */}
            <p class="text-center text-xs text-slate-400 mt-2">
              Press <kbd class="px-1 py-0.5 bg-slate-100 rounded text-slate-500 font-mono text-xs">Enter</kbd> to send · <kbd class="px-1 py-0.5 bg-slate-100 rounded text-slate-500 font-mono text-xs">Shift+Enter</kbd> for new line
            </p>
          </div>
        </div>

        {/* Example questions */}
        {state.messages().length < 1 && (
          <div class="px-4 pb-6 flex-shrink-0">
            <div class="mx-auto max-w-3xl">
              <p class="text-xs text-slate-400 text-center mb-3 uppercase tracking-wide font-medium">Try asking</p>
              <div class="flex flex-wrap gap-2 justify-center">
                <For each={examples()}>
                  {example => (
                    <button
                      onClick={() => submitInput(example)}
                      class="px-4 py-2 bg-slate-100 hover:bg-slate-200 text-slate-600 text-sm rounded-full transition-colors border border-slate-200 hover:border-slate-300"
                    >
                      {example}
                    </button>
                  )}
                </For>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  },
);

