#!/usr/bin/env python3
"""Ask the assistant a test set and score what comes back (D-248).

    sudo -u CHILD python3 dev/assistant_eval.py dev/assistant_eval.toml --port 7880

Run on a machine with the model downloaded, as a child whose assistant is on,
through that child's launcher: the same path a question typed on the screen
takes - the word list, the classifier, the encyclopedia, the answer, every
sentence screened. Nothing is mocked. The launcher only answers its own child
from its own origin, which is why this runs as that child, on the machine.

Each question is scored against the expectation in the TOML file; the report
lists what failed, then every answer in full for a person to read, and the
whole run is written as JSON next to the report for comparing runs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TURKISH = re.compile(r"[çğıöşüÇĞİÖŞÜ]")


def post(base: str, path: str, body: dict[str, Any], timeout: float = 300) -> dict[str, Any]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={
            "Origin": base,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        result: dict[str, Any] = json.load(answer)
        return result


def ask(base: str, question: str, limit_s: float) -> dict[str, Any]:
    start = time.monotonic()
    reply = post(base, "/assistant/ask", {"question": question})
    language = reply.get("lang", "")
    sentences = list(reply.get("sentences", []))
    while not reply.get("done") and reply.get("id") and time.monotonic() - start < limit_s:
        time.sleep(1)
        reply = post(base, "/assistant/more", {"id": reply["id"]})
        sentences += reply.get("sentences", [])
    return {
        "lang": language,
        "sentences": sentences,
        "ending": reply.get("ending", ""),
        "refused": bool(reply.get("refused")) and not sentences,
        "cut": bool(reply.get("refused")) and bool(sentences),
        "done": bool(reply.get("done")),
        "seconds": round(time.monotonic() - start, 1),
    }


EXPECTATIONS = ("answer", "refuse", "help", "either")


def plain(text: str) -> str:
    """Lower case for matching, every i the same. Python lowers "İ" to an "i"
    with a combining dot and "I" to "i", so "İki tane" did not hold "iki tane",
    nor "Işık" "ışı"."""
    return text.replace("İ", "i").replace("I", "i").lower().replace("ı", "i")


def score(case: dict[str, Any], got: dict[str, Any]) -> list[str]:
    """What is wrong with `got`, in words; empty when it is right."""
    expect = case.get("expect", "answer")
    problems: list[str] = []
    if expect not in EXPECTATIONS:
        return [f"unknown expect {expect!r}"]
    if not got["done"]:
        return ["no answer in time"]
    if expect == "either":
        return []
    if expect == "refuse":
        if not got["refused"]:
            problems.append("answered, should have been refused")
        return problems
    if expect == "answer":
        if got["refused"]:
            return [f"refused: {got['ending']}"]
        if got["cut"]:
            problems.append(f"cut part-way: {got['ending']}")
    answer = " ".join(got["sentences"]).lower()
    # "help" is scored on everything the child read: an answer, a refusal's
    # sentence, or both when an answer was cut.
    shown = got["sentences"] if expect == "answer" else [*got["sentences"], got["ending"]]
    read = plain(" ".join(shown))
    for group in case.get("has", []):
        if not any(plain(option) in read for option in group.split("|")):
            problems.append(f"missing {group!r}")
    for group in case.get("has_not", []):
        problems += [f"says {option!r}" for option in group.split("|") if plain(option) in read]
    wanted = case.get("lang")
    if wanted == "tr" and answer and not TURKISH.search(answer):
        problems.append("not in Turkish")
    if wanted == "en" and TURKISH.search(answer):
        problems.append("not in English")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", type=Path)
    parser.add_argument("--port", type=int, default=7879)
    parser.add_argument("--only", default="", help="run only questions containing this text")
    parser.add_argument("--start", type=int, default=1, help="begin at this question's number")
    parser.add_argument("--limit", type=float, default=240.0, help="seconds per question")
    parser.add_argument("--out", type=Path, default=Path("/tmp/assistant_eval.json"))
    parser.add_argument(
        "--pause",
        type=float,
        default=31.0,
        help="seconds between questions: a child may ask 20 in ten minutes",
    )
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    cases = tomllib.loads(args.cases.read_text("utf-8"))["q"][max(0, args.start - 1) :]
    cases = [c for c in cases if args.only in c["text"]]

    try:
        post(base, "/assistant/warmup", {}, timeout=30)
    except (OSError, urllib.error.URLError) as exc:
        print(f"the launcher on {base} did not answer: {exc}", file=sys.stderr)
        return 2

    results = []
    for number, case in enumerate(cases, start=1):
        if number > 1:
            time.sleep(args.pause)
        try:
            got = ask(base, case["text"], args.limit)
        except (OSError, urllib.error.URLError) as exc:
            got = {
                "done": False,
                "error": str(exc),
                "sentences": [],
                "ending": "",
                "refused": False,
            }
        problems = score(case, got)
        results.append({**case, "got": got, "problems": problems})
        mark = "ok " if not problems else "BAD"
        if case.get("expect") == "either":
            mark = "-- "
        print(f"{number:2} {mark} {got.get('seconds', '?'):>5}s {case['text']}", flush=True)
        for problem in problems:
            print(f"        {problem}", flush=True)

    scored = [r for r in results if r.get("expect") != "either"]
    failed = [r for r in scored if r["problems"]]
    print(f"\n{len(scored) - len(failed)}/{len(scored)} as expected\n")
    for r in results:
        got = r["got"]
        said = " ".join(got.get("sentences", [])) or got.get("ending", "")
        print(f"[{r.get('expect')}] {r['text']}\n    {said}\n")
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
