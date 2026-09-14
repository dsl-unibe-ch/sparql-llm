"""Compare the pipeline (MCP tools off) with the MCP-tools agent on the same questions.

Each question is sent to the running app's /chat in both modes; the final query each mode
used is re-run and scored against a reference query. Questions: the curated examples in
data/elites-suisses-examples.md plus the held-out set in compare_modes_heldout.py.

Run from the repo root, with the app on 127.0.0.1:8000 and AUTH_ENABLED=false (the agent
fetches its MCP tools from there):
  python scripts/compare_modes.py check-gold
  python scripts/compare_modes.py run --model gpustack/gpt-oss-120b --out results.jsonl [--only C05,H04] [--workers 3]
  python scripts/compare_modes.py report results.jsonl [results2.jsonl ...]

Results vary between runs, so compare several runs per model. Curated questions about "a
person" in general are open-ended: the agent may reasonably ask which person, which this
scoring counts as a miss.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from compare_modes_heldout import HELDOUT

from sparql_llm.utils import query_sparql

ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
CHAT_URL = "http://127.0.0.1:8000/chat"
EXAMPLES = Path("data/elites-suisses-examples.md")
# Curated questions about "a person" in general: any non-empty answer is acceptable.
OPEN_ENDED = {2, 13, 14, 15, 17, 18}
REFUSAL = re.compile(
    r"not (available|recorded|contain|in the (database|data|knowledge graph))|no (information|data|record)|"
    r"does not (contain|include|record|have)|cannot (find|answer)|could not find|isn't (recorded|available)|"
    r"pas (disponible|enregistr)|aucune (information|donnée)",
    re.I,
)
MAX_GOLD_ROWS = 200


def load_curated() -> list[dict]:
    text = EXAMPLES.read_text(encoding="utf-8")
    items = []
    for block in re.split(r"^## Example ", text, flags=re.M)[1:]:
        num = int(re.match(r"(\d+)", block).group(1))
        question = re.search(r"^Question: (.+)$", block, re.M).group(1).strip()
        gold = re.search(r"```sparql\n(.*?)```", block, re.S).group(1)
        heading = block.split("\n", 1)[0]
        scoring = "unanswerable" if "aspirational" in heading else "nonempty" if num in OPEN_ENDED else "values"
        items.append({"id": f"C{num:02d}", "set": "curated", "question": question, "gold": gold, "scoring": scoring})
    return items


def load_items() -> list[dict]:
    held = [{"set": "heldout", "scoring": "keys", **item} for item in HELDOUT]
    return load_curated() + held


def run_query(query: str) -> list[dict]:
    res = query_sparql(query, ENDPOINT, post=False, timeout=90, check_service_desc=False)
    return [{k: v.get("value", "") for k, v in row.items()} for row in res.get("results", {}).get("bindings", [])]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    # "11,216" / "11'216" / "11 216" -> "11216"
    return re.sub(r"(?<=\d)[,'\u2019\u202f\u00a0 .](?=\d{3}\b)", "", text)


def key_of(value: str) -> str:
    """A word an answer must contain for this reference value: a surname, a number or the label."""
    value = value.strip()
    if "," in value and not re.fullmatch(r"[\d,.]+", value):
        value = value.split(",")[0]
        value = re.split(r"[-\s]", value.strip())[0]
    return value


def keys_for(item: dict, gold_rows: list[dict]) -> list[str]:
    if item.get("keys"):
        return item["keys"]
    col = item.get("key_col")
    if not col:
        return []
    return sorted({key_of(r[col]) for r in gold_rows if r.get(col) and not r[col].startswith("http")})


def key_recall(keys: list[str], answer: str) -> float | None:
    if not keys:
        return None
    text = norm(answer)
    hits = sum(any(norm(alt) in text for alt in key.split("|")) for key in keys)
    return hits / len(keys)


def row_recall(gold_rows: list[dict], model_rows: list[dict]) -> float | None:
    """Share of reference rows with at least one value that the model's query also returned."""
    if not gold_rows:
        return None
    model_vals = {norm(v) for row in model_rows for v in row.values() if v}
    rows = gold_rows[:MAX_GOLD_ROWS]
    return sum(any(norm(v) in model_vals for v in row.values() if v) for row in rows) / len(rows)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c if isinstance(c, str) else (c.get("text") or "") for c in content)
    return str(content or "")


def last_sparql_block(text: str) -> str | None:
    blocks = re.findall(r"```sparql\s*(.*?)```", text, re.S | re.I)
    return blocks[-1].strip() if blocks else None


async def ask(client: httpx.AsyncClient, item: dict, mode: str, model: str) -> dict:
    payload = {
        "messages": [{"role": "user", "content": item["question"]}],
        "model": model,
        "stream": False,
        "use_tools": mode == "agent",
    }
    record = {"id": item["id"], "set": item["set"], "mode": mode, "model": model, "question": item["question"]}
    start = time.monotonic()
    try:
        resp = await client.post(CHAT_URL, json=payload)
        record["seconds"] = round(time.monotonic() - start, 1)
        record["status"] = resp.status_code
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        record["seconds"] = round(time.monotonic() - start, 1)
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        data = {}

    msgs = data.get("messages", []) if isinstance(data, dict) else []
    ai = [m for m in msgs if isinstance(m, dict) and m.get("type") in ("ai", "AIMessageChunk")]
    answer = re.sub(r"<think>.*?</think>", "", text_of(ai[-1].get("content")) if ai else "", flags=re.S).strip()
    calls = [tc for m in ai for tc in (m.get("tool_calls") or [])]
    executed = [tc for tc in calls if tc.get("name") == "execute_sparql_query"]
    steps = data.get("steps", []) if isinstance(data, dict) else []

    if mode == "agent":
        final_query = executed[-1].get("args", {}).get("sparql_query") if executed else last_sparql_block(answer)
        record["tool_rounds"] = sum(1 for m in ai if m.get("tool_calls") or m.get("invalid_tool_calls"))
        record["queries_run"] = len(executed)
        record["tools_used"] = [tc.get("name") for tc in calls]
    else:
        so = data.get("structured_output") if isinstance(data, dict) else None
        if isinstance(so, list):
            so = so[-1] if so else None
        final_query = (so or {}).get("sparql_query") if isinstance(so, dict) else None
        final_query = final_query or last_sparql_block(answer)
        record["try_count"] = data.get("try_count") if isinstance(data, dict) else None
        record["queries_run"] = sum(1 for s in steps if str(s.get("label", "")).startswith("⚡"))
    record["answer"] = answer
    record["final_query"] = final_query
    record["hit_step_limit"] = answer.startswith("I've reached the maximum")
    return record


async def score(record: dict, item: dict, gold_rows: list[dict]) -> dict:
    model_rows: list[dict] = []
    if record.get("final_query"):
        try:
            model_rows = await asyncio.to_thread(run_query, record["final_query"])
        except Exception as exc:
            record["final_query_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    record["model_rows"] = len(model_rows)
    record["gold_rows"] = len(gold_rows)
    scoring = item["scoring"]
    record["scoring"] = scoring
    if scoring == "unanswerable":
        record["refused"] = bool(REFUSAL.search(record["answer"])) or not model_rows
        record["correct"] = record["refused"]
    elif scoring == "nonempty":
        record["correct"] = len(model_rows) > 0
    elif scoring == "values":
        record["row_recall"] = row_recall(gold_rows, model_rows)
        record["correct"] = (record["row_recall"] or 0) >= 0.9
    else:
        keys = keys_for(item, gold_rows)
        record["keys"] = keys
        record["key_recall"] = key_recall(keys, record["answer"])
        record["row_recall"] = row_recall(gold_rows, model_rows)
        record["correct"] = record["key_recall"] == 1.0
    return record


def gold_rows_for(item: dict) -> list[dict]:
    return run_query(item["gold"]) if item.get("gold") else []


def check_gold() -> None:
    for item in load_items():
        try:
            rows = gold_rows_for(item)
            keys = keys_for(item, rows) if item["set"] == "heldout" else []
            print(f"{item['id']} {item['scoring']:12} {len(rows):6} rows  keys={keys[:10]}  {item['question'][:70]}")
        except Exception as exc:
            print(f"{item['id']} GOLD ERROR {type(exc).__name__}: {str(exc)[:200]}")


async def run(model: str, out: Path, only: set[str] | None, workers: int, modes: list[str]) -> None:
    items = [i for i in load_items() if not only or i["id"] in only]
    golds = {i["id"]: await asyncio.to_thread(gold_rows_for, i) for i in items}
    sem = asyncio.Semaphore(workers)
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0)) as client:

        async def one(item: dict, mode: str) -> None:
            async with sem:
                record = await ask(client, item, mode, model)
                record = await score(record, item, golds[item["id"]])
            async with lock:
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(
                    f"{record['id']} {mode:8} {record.get('status')} {record.get('seconds')}s "
                    f"correct={record.get('correct')} rows={record.get('model_rows')}",
                    flush=True,
                )

        await asyncio.gather(*(one(item, mode) for item in items for mode in modes))


def report(paths: list[Path]) -> None:
    records = [json.loads(line) for p in paths for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    by = {}
    for r in records:
        by.setdefault((r["model"], r["mode"]), []).append(r)
    print(f"{'model':28} {'mode':9} {'set':8} {'n':>3} {'correct':>8} {'median s':>9} {'mean s':>7} {'errors':>6} {'step limit':>10}")
    for (model, mode), rs in sorted(by.items()):
        for subset in ("curated", "heldout", "all"):
            sub = [r for r in rs if subset == "all" or r["set"] == subset]
            if not sub:
                continue
            secs = [r["seconds"] for r in sub if isinstance(r.get("seconds"), (int, float))]
            ok = sum(1 for r in sub if r.get("correct"))
            errs = sum(1 for r in sub if r.get("status") != 200)
            limit = sum(1 for r in sub if r.get("hit_step_limit"))
            print(
                f"{model:28} {mode:9} {subset:8} {len(sub):>3} {ok:>4}/{len(sub):<3} "
                f"{statistics.median(secs) if secs else 0:>9.1f} {statistics.mean(secs) if secs else 0:>7.1f} {errs:>6} {limit:>10}"
            )
    md = paths[0].with_suffix(".md")
    lines = ["# Pipeline vs MCP-tools agent: answers side by side", ""]
    for qid in sorted({r["id"] for r in records}):
        rs = sorted((r for r in records if r["id"] == qid), key=lambda r: (r["model"], r["mode"]))
        lines += [f"## {qid}: {rs[0]['question']}", ""]
        for r in rs:
            extra = {k: r.get(k) for k in ("correct", "key_recall", "row_recall", "model_rows", "gold_rows", "queries_run", "tool_rounds", "try_count") if r.get(k) is not None}
            lines += [f"**{r['model']} · {r['mode']}** — {r.get('seconds')}s — {extra}", "", "> " + (r.get("answer") or "(no answer)")[:1500].replace("\n", "\n> "), ""]
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"answers side by side: {md}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-gold")
    run_p = sub.add_parser("run")
    run_p.add_argument("--model", default="gpustack/gpt-oss-120b")
    run_p.add_argument("--out", type=Path, required=True)
    run_p.add_argument("--only", default="")
    run_p.add_argument("--workers", type=int, default=3)
    run_p.add_argument("--modes", default="pipeline,agent")
    rep = sub.add_parser("report")
    rep.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    if args.cmd == "check-gold":
        check_gold()
    elif args.cmd == "run":
        only = {s.strip() for s in args.only.split(",") if s.strip()} or None
        asyncio.run(run(args.model, args.out, only, args.workers, args.modes.split(",")))
    else:
        report(args.paths)


if __name__ == "__main__":
    main()
