"""D-216: the assistant's answer, a sentence at a time.

What has to stay true now that a child sees an answer before it is finished:
the word list and the classifier still run before anything is shown; every
sentence is screened, together with everything before it, before it is
released; the first sentence the word list stops ends the answer with a
sentence people wrote, and the parent is told as before; a model that dies or
runs out of time ends it with "I'm resting"; sentences arrive in order, once;
and an answer nobody is reading any more is stopped.

No model runs here, and no socket the development host cannot open.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from biokids_ai import service as service_module
from biokids_ai.assistant import CUT_KEY, Assistant
from biokids_ai.llm import ChatModel, ModelError
from biokids_ai.localhttp import Endpoint, HttpError, post_events
from biokids_ai.sentences import MAX_SENTENCE_CHARS, SentenceSplitter
from biokids_ai.service import ABANDONED_S, AssistantService
from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.ipc import Response
from biokids_daemon import assistant as gate_module
from biokids_daemon.assistant import AssistantGate

# ------------------------------------------------------------ sentences


def split(text: str, step: int = 1) -> list[str]:
    """Feed `text` in pieces of `step` characters, as a model would write it."""
    splitter = SentenceSplitter()
    out: list[str] = []
    for start in range(0, len(text), step):
        out += splitter.feed(text[start : start + step])
    return out + splitter.close()


def test_an_answer_comes_apart_into_its_sentences() -> None:
    text = "Gökyüzü mavidir. Çünkü ışık saçılır! Neden mi? Güneş yüzünden."
    expected = ["Gökyüzü mavidir.", "Çünkü ışık saçılır!", "Neden mi?", "Güneş yüzünden."]
    for step in (1, 3, 7, len(text)):
        assert split(text, step) == expected, step


def test_a_sentence_is_released_as_soon_as_the_next_one_begins() -> None:
    splitter = SentenceSplitter()
    assert splitter.feed("Gökyüzü mavidir.") == [], "the next word may still be lowercase"
    assert splitter.feed(" ") == []
    assert splitter.feed("Çü") == ["Gökyüzü mavidir."]
    assert splitter.feed("nkü ışık saçılır.") == []
    assert splitter.close() == ["Çünkü ışık saçılır."]


@pytest.mark.parametrize(
    "sentence",
    [
        "Dr. Ayşe Hanım çocuk doktorudur.",
        "Prof. Dr. Ali Bey bir bilim insanıdır.",
        "Yrd. Doç. Elif Hanım ders verir.",
        "Piramitler M.Ö. 2500 yılında yapıldı.",
        "Buhar makinesi 19. yüzyılda yaygınlaştı.",
        "II. Mehmet İstanbul'u fethetti.",
        "1. Dünya Savaşı 1914'te başladı.",
        "Kitabı J. K. Rowling yazdı.",
        "Hz. Ömer adaletiyle bilinir.",
        "Pi sayısı yaklaşık 3.14 kadardır.",
        "Meyveler, sebzeler vb. sağlıklıdır.",
    ],
)
def test_an_abbreviation_or_an_ordinal_is_not_the_end(sentence: str) -> None:
    assert split(f"{sentence} Başka bir şey var mı?") == [sentence, "Başka bir şey var mı?"]


def test_the_sentence_goes_on_when_the_next_word_is_lowercase() -> None:
    text = '"Neden?" diye sordu. Zio dedi ki: "Gökyüzü mavi." Sonra güldü.'
    assert split(text) == [
        '"Neden?" diye sordu.',
        'Zio dedi ki: "Gökyüzü mavi."',
        "Sonra güldü.",
    ]


def test_marks_that_end_together_stay_together() -> None:
    assert split("Vay be?! Harika… Bence de... Evet.") == [
        "Vay be?!",
        "Harika…",
        "Bence de...",
        "Evet.",
    ]


def test_a_list_is_a_sentence_a_line() -> None:
    text = "En sevilen meyveler:\n1. Elma\n2. Armut\n\nHangisini seversin?"
    assert split(text, 2) == [
        "En sevilen meyveler:",
        "1. Elma",
        "2. Armut",
        "Hangisini seversin?",
    ]


def test_a_run_on_sentence_is_cut_between_words_at_a_pause() -> None:
    clause = "kediler uyur, köpekler havlar"
    text = ", ".join([clause] * 20) + "."
    pieces = split(text, 4)
    assert len(pieces) > 1
    assert all(len(piece) <= MAX_SENTENCE_CHARS for piece in pieces)
    assert " ".join(pieces).split() == text.split(), "no word is ever cut in two"
    assert pieces[0].endswith(","), "a comma is the place to pause"


def test_a_word_is_never_cut_however_long() -> None:
    word = "a" * (MAX_SENTENCE_CHARS + 50)
    splitter = SentenceSplitter()
    assert splitter.feed(word) == [], "no space yet: no cut"
    assert splitter.feed(" ve sonra") == [word]
    assert splitter.close() == ["ve sonra"]


# ------------------------------------------------------- the pipeline


class Streaming:
    """A model whose answer arrives in pieces, and which can die part-way."""

    def __init__(
        self,
        answer: str = "Çünkü ışık saçılır. Mavi ışık en çok saçılır. Bu yüzden gök mavidir.",
        *,
        category: str = "ok",
        die_after: int | None = None,
        step: int = 4,
    ) -> None:
        self.answer_text = answer
        self.category = category
        self.die_after = die_after
        self.step = step
        self.calls: list[str] = []
        self.written: list[str] = []
        self.closed = False

    async def classify(self, question: str, age: int, wait_s: float | None = None) -> str:
        self.calls.append("classify")
        return self.category

    async def answer(self, *_args: Any, **_kwargs: Any) -> str:
        return self.answer_text

    async def stream_answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str = "",
        wait_s: float | None = None,
        context: str = "",
    ) -> AsyncIterator[str]:
        self.calls.append("answer")
        try:
            text = self.answer_text
            for index, start in enumerate(range(0, len(text), self.step)):
                if self.die_after is not None and index >= self.die_after:
                    raise ModelError("llama-server went away")
                piece = text[start : start + self.step]
                self.written.append(piece)
                yield piece
        finally:
            self.closed = True


async def ask(model: Streaming, question: str = "Gökyüzü neden mavi?") -> tuple[Any, list[str]]:
    seen: list[str] = []
    reply = await Assistant(model).ask(question, age=6, locale="tr", on_sentence=seen.append)
    return reply, seen


async def test_each_sentence_is_released_in_order_and_the_answer_ends_by_itself() -> None:
    reply, seen = await ask(Streaming())
    assert seen == [
        "Çünkü ışık saçılır.",
        "Mavi ışık en çok saçılır.",
        "Bu yüzden gök mavidir.",
    ]
    assert not reply.refused and reply.refusal_key == ""
    assert list(reply.sentences) == seen
    assert reply.text == " ".join(seen)


async def test_the_classifier_runs_before_the_model_writes_a_word() -> None:
    model = Streaming()
    order: list[str] = []
    reply = await Assistant(model).ask(
        "Gökyüzü neden mavi?",
        age=6,
        locale="tr",
        on_answering=lambda: order.append(f"answering after {model.calls}"),
        on_sentence=lambda s: order.append("sentence"),
    )
    assert model.calls == ["classify", "answer"]
    assert order[0] == "answering after ['classify']"
    assert order[1:] == ["sentence"] * len(reply.sentences)


async def test_a_refused_question_is_never_streamed() -> None:
    model = Streaming(category="violence")
    told: list[str] = []
    reply = await Assistant(model).ask(
        "Birini nasıl incitirim?", age=8, locale="tr", on_answering=lambda: told.append("x")
    )
    assert reply.refused and reply.stage == "classifier"
    assert model.calls == ["classify"], "the model was never asked to answer"
    assert told == [], "nobody was told an answer was coming"


async def test_a_sentence_the_word_list_stops_ends_the_answer_there() -> None:
    model = Streaming(
        "Bazı filmler çocuklar için değildir. Bazı insanlar porno izler. "
        "Başka bir şey sormak ister misin?"
    )
    reply, seen = await ask(model, "İnsanlar internette ne izler?")

    assert seen == ["Bazı filmler çocuklar için değildir."], "nothing at or after the stop"
    assert reply.refused and reply.stage == "answer" and reply.category == "sexual"
    assert reply.refusal_key == CUT_KEY, "a written ending, never the model's"
    assert reply.alert, "the parent hears about it, as before"
    assert model.closed, "the model is stopped, not left writing"
    assert "Başka" not in "".join(model.written), "and it wrote no further than it had to"


async def test_the_first_sentence_stopped_is_the_refusal_it_always_was() -> None:
    reply, seen = await ask(Streaming("Bazı insanlar porno izler. Sonra uyurlar."))
    assert seen == []
    assert reply.refusal_key == "assistant.refuse.default" and reply.text == ""
    assert reply.alert


async def test_self_harm_part_way_keeps_its_own_sentence() -> None:
    """It carries 183 and 112, which an answer that drifted there needs."""
    reply, seen = await ask(Streaming("Kleopatra bir kraliçeydi. Sonra intihar etti."))
    assert seen == ["Kleopatra bir kraliçeydi."]
    assert reply.refusal_key == "assistant.refuse.self_harm" and reply.alert


@pytest.mark.parametrize(
    "answer",
    [
        "Bu bir por. No mu?",  # a squashed spelling across two sentences
        "Ressam çıplak. Resim yaptı.",  # a phrase across two sentences
    ],
)
async def test_each_sentence_is_screened_with_everything_before_it(answer: str) -> None:
    """Each half is clean on its own; the whole answer was never allowed."""
    whole = Assistant(Streaming())
    assert whole.lexicon.screen(answer) is not None
    first, second = split(answer)
    assert whole.lexicon.screen(first) is None and whole.lexicon.screen(second) is None

    reply, seen = await ask(Streaming(answer))
    assert seen == [first] and reply.refused and reply.alert


async def test_a_model_that_dies_part_way_ends_with_im_resting() -> None:
    model = Streaming(die_after=8)
    reply, seen = await ask(model)
    assert seen == ["Çünkü ışık saçılır."], "what was shown stays"
    assert reply.refused and reply.category == "unavailable" and reply.stage == "error"
    assert reply.refusal_key == "assistant.refuse.unavailable"
    assert reply.alert is False, "a tired model is not the child's fault"


async def test_a_model_that_dies_before_a_sentence_is_a_plain_resting() -> None:
    reply, seen = await ask(Streaming(die_after=1))
    assert seen == [] and reply.refusal_key == "assistant.refuse.unavailable"


# ------------------------------------------------------------ the model


def events(*items: dict[str, Any], fail: Exception | None = None) -> Any:
    sent: list[dict[str, Any]] = []

    async def stream(
        _endpoint: Endpoint, _path: str, payload: dict[str, Any], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        sent.append({"payload": payload, **kwargs})
        for item in items:
            yield item
        if fail is not None:
            raise fail

    stream.sent = sent  # type: ignore[attr-defined]
    return stream


def delta(text: str, finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


async def test_the_model_streams_without_thinking_within_the_answers_time() -> None:
    from biokids_ai.llm import ANSWER_TIMEOUT_S

    stream = events(
        {"choices": [{"delta": {"role": "assistant", "content": None}}]},
        delta("Merhaba"),
        delta(" Elif!", "stop"),
        {"choices": [], "usage": {"completion_tokens": 3}},
    )
    model = ChatModel(Endpoint("unix:/x"), stream=stream)
    pieces = [piece async for piece in model.stream_answer("q", 6, "tr", "zio")]

    assert pieces == ["Merhaba", " Elif!"]
    sent = stream.sent[0]
    assert sent["payload"]["stream"] is True
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["wait_s"] == ANSWER_TIMEOUT_S, "the old timeout is the whole stream's"


async def test_an_answer_that_breaks_off_is_not_an_answer_that_ended() -> None:
    model = ChatModel(Endpoint("unix:/x"), stream=events(delta("Yarım cüm")))
    with pytest.raises(ModelError, match="broke off"):
        _ = [piece async for piece in model.stream_answer("q", 6, "tr", "zio")]


async def test_a_stream_that_times_out_is_a_model_error() -> None:
    model = ChatModel(
        Endpoint("unix:/x"),
        stream=events(delta("Bir "), fail=HttpError("POST: not finished within 120s")),
    )
    with pytest.raises(ModelError):
        _ = [piece async for piece in model.stream_answer("q", 6, "tr", "zio")]


class LocalServer:
    """A one-shot HTTP server on loopback that answers with `body`, then waits."""

    def __init__(self, head: bytes, parts: list[bytes], *, hang: bool = False) -> None:
        self.head = head
        self.parts = parts
        self.hang = hang
        self.closed = asyncio.Event()
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> Endpoint:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return Endpoint(f"http://127.0.0.1:{port}")

    async def __aexit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(self.head)
        for part in self.parts:
            writer.write(part)
            await writer.drain()
        if self.hang:
            await reader.read()  # until the client goes away
            self.closed.set()
        writer.close()


def chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


CHUNKED = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
)


async def test_server_sent_events_are_read_as_they_come() -> None:
    first = b'data: {"choices":[{"delta":{"content":"Mer"}}]}\n\n'
    second = (
        b'data: {"choices":[{"delta":{"content":"haba"},"finish_reason":"stop"}]}\n\ndata: [DO'
    )
    parts = [chunk(first), chunk(second), chunk(b"NE]\n\n"), b"0\r\n\r\n"]
    async with LocalServer(CHUNKED, parts) as endpoint:
        got = [event async for event in post_events(endpoint, "/v1/chat/completions", {})]
    assert [e["choices"][0]["delta"]["content"] for e in got] == ["Mer", "haba"]


async def test_an_error_while_writing_is_an_error() -> None:
    parts = [chunk(b'error: {"message":"slot unavailable"}\n\n'), b"0\r\n\r\n"]
    async with LocalServer(CHUNKED, parts) as endpoint:
        with pytest.raises(HttpError, match="slot unavailable"):
            _ = [event async for event in post_events(endpoint, "/x", {})]


async def test_a_stream_that_stops_writing_ends_at_its_deadline() -> None:
    server = LocalServer(CHUNKED, [chunk(b'data: {"choices":[]}\n\n')], hang=True)
    async with server as endpoint:
        with pytest.raises(HttpError, match="not finished within"):
            _ = [event async for event in post_events(endpoint, "/x", {}, wait_s=0.3)]
        await asyncio.wait_for(server.closed.wait(), 2)  # and the connection is closed


async def test_a_stream_cut_off_mid_chunk_is_an_error() -> None:
    parts = [b"40\r\n" + b'data: {"choices":[]}']  # a chunk promised and never finished
    async with LocalServer(CHUNKED, parts) as endpoint:
        with pytest.raises(HttpError):
            _ = [event async for event in post_events(endpoint, "/x", {})]


# ------------------------------------------------------------ the service


class Gated(Streaming):
    """A model that writes only what the test lets through."""

    def __init__(self) -> None:
        super().__init__()
        self.pieces: asyncio.Queue[str | None] = asyncio.Queue()

    async def stream_answer(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        self.calls.append("answer")
        try:
            while (piece := await self.pieces.get()) is not None:
                self.written.append(piece)
                yield piece
        finally:
            self.closed = True


class Server:
    running = True

    async def ensure(self) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def a_service(config: Config, model: Streaming, clock: Clock | None = None) -> AssistantService:
    service = AssistantService(config, clock=clock or Clock())
    service._llm = Server()  # type: ignore[assignment]
    service._assistant = Assistant(model)
    return service


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


ASK = {"question": "Gökyüzü neden mavi?", "age": 6, "locale": "tr"}


async def test_ask_returns_once_the_checks_pass_and_more_brings_the_rest(
    config: Config,
) -> None:
    model = Gated()
    service = a_service(config, model)

    begun = await service.ask(dict(ASK))
    assert begun["done"] is False and begun["sentences"] == [] and begun["answer_id"]
    assert model.calls == ["classify", "answer"]
    answer_id = begun["answer_id"]

    await model.pieces.put("Çünkü ışık saçılır. Ma")
    await settle()
    assert service.more(answer_id)["sentences"] == ["Çünkü ışık saçılır."]
    assert service.more(answer_id)["sentences"] == [], "each sentence comes once"

    for piece in ("vi ışık en çok saçılır. ", "Bu yüzden gök mavidir."):
        await model.pieces.put(piece)
    await model.pieces.put(None)
    await settle()
    last = service.more(answer_id)
    assert last["sentences"] == ["Mavi ışık en çok saçılır.", "Bu yüzden gök mavidir."]
    assert last["done"] is True and last["refused"] is False and last["refusal_key"] == ""

    gone = service.more(answer_id)
    assert gone["done"] and gone["refusal_key"] == "assistant.refuse.unavailable", (
        "read to its end, it is forgotten"
    )


async def test_a_refused_question_comes_back_done_from_ask(config: Config) -> None:
    model = Gated()
    model.category = "violence"
    service = a_service(config, model)
    reply = await service.ask(dict(ASK))
    assert reply["done"] and reply["refused"] and reply["alert"]
    assert reply["stage"] == "classifier" and model.calls == ["classify"]
    assert service._answers == {}


async def test_a_refusal_part_way_comes_with_the_sentences_before_it(config: Config) -> None:
    model = Gated()
    service = a_service(config, model)
    answer_id = (await service.ask(dict(ASK)))["answer_id"]
    await model.pieces.put("Bazı filmler çocuklar için değildir. Bazı insanlar porno izler. Ve")
    await settle()
    reply = service.more(answer_id)
    assert reply["sentences"] == ["Bazı filmler çocuklar için değildir."]
    assert reply["done"] and reply["refusal_key"] == CUT_KEY and reply["alert"]
    assert model.closed


async def test_an_answer_nobody_reads_is_stopped(config: Config) -> None:
    model = Gated()
    clock = Clock()
    service = a_service(config, model, clock)
    answer_id = (await service.ask(dict(ASK)))["answer_id"]
    await model.pieces.put("Çünkü ışık saçılır. ")
    await settle()

    clock.now += ABANDONED_S - 1
    assert service.forget_abandoned() == 0
    service.more(answer_id)
    clock.now += ABANDONED_S + 1
    assert service.forget_abandoned() == 1
    await settle()
    assert model.closed, "the model stops writing to nobody"
    assert service.more(answer_id)["refusal_key"] == "assistant.refuse.unavailable"


async def test_a_question_still_being_checked_is_not_abandoned(config: Config) -> None:
    clock = Clock()
    release = asyncio.Event()

    class SlowClassifier(Gated):
        async def classify(self, question: str, age: int, wait_s: float | None = None) -> str:
            await release.wait()  # the model is loading
            return "ok"

    service = a_service(config, SlowClassifier(), clock)
    asking = asyncio.create_task(service.ask(dict(ASK)))
    await settle()
    clock.now += ABANDONED_S * 10
    assert service.forget_abandoned() == 0
    release.set()
    assert (await asking)["done"] is False


async def test_cancel_stops_the_model(config: Config) -> None:
    model = Gated()
    service = a_service(config, model)
    answer_id = (await service.ask(dict(ASK)))["answer_id"]
    assert service.cancel(answer_id) is True
    await settle()
    assert model.closed and service._answers == {}
    assert service.cancel(answer_id) is False


async def test_the_operations_cross_the_socket_as_json(config: Config) -> None:
    from biokids_core.ipc import Request

    model = Gated()
    service = a_service(config, model)
    begun = await service.handle(Request(op="ask", args=dict(ASK)))
    answer_id = begun.data["answer_id"]
    json.dumps(begun.data)
    more = await service.handle(Request(op="more", args={"answer_id": answer_id}))
    assert more.ok and more.data["done"] is False
    cancelled = await service.handle(Request(op="cancel", args={"answer_id": answer_id}))
    assert cancelled.data == {"cancelled": True}


async def test_no_model_installed_is_a_resting_answer(config: Config) -> None:
    reply = await AssistantService(config).ask(dict(ASK))
    assert reply["done"] and reply["refusal_key"] == "assistant.refuse.unavailable"


def test_the_service_waits_longer_than_the_daemon() -> None:
    """The daemon collects an abandoned answer first, alert and all."""
    assert ABANDONED_S > gate_module.ABANDONED_S
    assert service_module.REAP_EVERY_S < service_module.ABANDONED_S


# ------------------------------------------------------------ the gate


@dataclass
class Policy:
    assistant_enabled: bool = True
    assistant_voice: bool = False


@dataclass
class Runtime:
    unix_user: str = "elif"
    display_name: str = "Elif"
    locale: str = "tr"
    policy: Policy | None = None


class Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None:
        return None


class Supervisor:
    def __init__(self) -> None:
        self.runtime = Runtime(policy=Policy())
        self.session = Session()

    def _require(self, _child_id: int) -> Runtime:
        return self.runtime


class Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, kind: Any, **payload: Any) -> None:
        self.published.append((kind, payload))


class AiService:
    """The assistant service's side of the socket, scripted per answer."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.down = False

    async def call(self, op: str, **args: Any) -> Response:
        self.calls.append((op, args))
        if self.down:
            raise IpcError("connection refused")
        if op == "ask":
            return Response(ok=True, data={"answer_id": "a1", "done": False, "sentences": []})
        if op == "cancel":
            return Response(ok=True, data={"cancelled": True})
        return Response(ok=True, data=self.replies.pop(0))


def step(*sentences: str, **ending: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {"answer_id": "a1", "sentences": list(sentences), "done": False}
    if ending:
        reply.update(done=True, **ending)
    return reply


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_module.repo, "get_child", lambda _session, _id: None)
    monkeypatch.setattr(gate_module.repo, "audit", lambda *_a, **_k: None)


def a_gate(config: Config, service: AiService, clock: Clock | None = None) -> AssistantGate:
    return AssistantGate(Supervisor(), Bus(), config, client=service, clock=clock or Clock())


async def test_the_gate_carries_an_answer_and_files_a_refusal_part_way(
    config: Config, no_db: None
) -> None:
    service = AiService(
        step("Bazı filmler çocuklar için değildir."),
        step(refused=True, category="sexual", stage="answer", refusal_key=CUT_KEY, alert=True),
    )
    gate = a_gate(config, service)
    begun = await gate.ask(1, "İnsanlar internette ne izler?")
    assert begun["done"] is False and begun["answer_id"] == "a1"

    first = await gate.more(1, "a1")
    assert first["sentences"] == ["Bazı filmler çocuklar için değildir."]
    assert gate.supervisor.session.added == []

    last = await gate.more(1, "a1")
    assert last["done"] and last["refusal_key"] == CUT_KEY
    alert = gate.supervisor.session.added[0]
    assert alert.text == "İnsanlar internette ne izler?", "the child's words, as before"
    assert alert.category == "sexual"
    assert gate.bus.published[0][1]["category"] == "sexual"
    assert gate._open == {}


async def test_another_answer_id_reaches_nothing(config: Config, no_db: None) -> None:
    service = AiService()
    gate = a_gate(config, service)
    await gate.ask(1, "q")
    reply = await gate.more(1, "someone-elses")
    assert reply["done"] and reply["refusal_key"] == "assistant.refuse.unavailable"
    assert [op for op, _ in service.calls] == ["ask"]


async def test_a_new_question_stops_the_last_answer(config: Config, no_db: None) -> None:
    service = AiService(step("Bir."))
    gate = a_gate(config, service)
    await gate.ask(1, "ilk")
    await gate.ask(1, "ikinci")
    ops = [op for op, _ in service.calls]
    assert ops == ["ask", "more", "cancel", "ask"], "collected, stopped, then the new one"


async def test_a_launcher_that_stopped_asking_is_noticed(config: Config, no_db: None) -> None:
    clock = Clock()
    service = AiService(
        step("Bir."),
        step(refused=True, category="sexual", stage="answer", refusal_key=CUT_KEY, alert=True),
    )
    gate = a_gate(config, service, clock)
    await gate.ask(1, "soru")
    await gate.more(1, "a1")
    clock.now += gate_module.ABANDONED_S + 1
    assert await gate.forget_abandoned() == 1
    assert gate.supervisor.session.added[0].text == "soru", "an alert is still an alert"
    assert gate._open == {}


async def test_a_service_that_vanishes_mid_answer_ends_it_kindly(
    config: Config, no_db: None
) -> None:
    service = AiService()
    gate = a_gate(config, service)
    await gate.ask(1, "q")
    service.down = True
    reply = await gate.more(1, "a1")
    assert reply["done"] and reply["refusal_key"] == "assistant.refuse.unavailable"
    assert gate._open == {}


async def test_switching_the_assistant_off_stops_the_answer(
    config: Config, no_db: None
) -> None:
    service = AiService(step("Bir."))
    gate = a_gate(config, service)
    await gate.ask(1, "q")
    gate.supervisor.runtime.policy = Policy(assistant_enabled=False)
    reply = await gate.more(1, "a1")
    assert reply["refusal_key"] == "assistant.refuse.off"
    assert [op for op, _ in service.calls][-1] == "cancel"


async def test_polling_is_not_asking(config: Config, no_db: None) -> None:
    """Twenty questions in ten minutes, not twenty polls."""
    small = replace(config, ai=replace(config.ai, assistant_per_10min=1))
    service = AiService(*[step() for _ in range(30)])
    gate = a_gate(small, service)
    await gate.ask(1, "q")
    for _ in range(30):
        assert not (await gate.more(1, "a1"))["done"]


# ------------------------------------------------------------ the launcher


LAUNCHER = "http://127.0.0.1:7879"
SAME = {"origin": LAUNCHER, "sec-fetch-site": "same-origin"}


class Daemon:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []
        self.polled: list[str] = []

    async def state(self) -> dict[str, Any]:
        return {"locale": "tr", "assistant": {"enabled": True}}

    async def ask_assistant(self, question: str) -> dict[str, Any]:
        self.asked.append(question)
        return self.replies.pop(0)

    async def assistant_more(self, answer_id: str) -> dict[str, Any]:
        self.polled.append(answer_id)
        return self.replies.pop(0)


@pytest.fixture
def launcher(config: Config) -> tuple[TestClient, Daemon]:
    from biokids_shell.app import create_app

    daemon = Daemon()
    app = create_app(config)
    app.state.shell.client = daemon
    return TestClient(app, base_url=LAUNCHER), daemon


def test_the_page_gets_the_start_then_the_rest(launcher: tuple[TestClient, Daemon]) -> None:
    client, daemon = launcher
    daemon.replies = [
        {"answer_id": "a1", "done": False, "sentences": []},
        {"answer_id": "a1", "done": False, "sentences": ["Çünkü ışık saçılır."]},
        {"answer_id": "a1", "done": True, "sentences": ["Bitti."], "refused": False},
    ]
    begun = client.post("/assistant/ask", json={"question": "Neden?"}, headers=SAME).json()
    assert begun == {"id": "a1", "sentences": [], "done": False, "ending": "", "refused": False}
    more = client.post("/assistant/more", json={"id": "a1"}, headers=SAME).json()
    assert more["sentences"] == ["Çünkü ışık saçılır."] and more["id"] == "a1"
    last = client.post("/assistant/more", json={"id": "a1"}, headers=SAME).json()
    assert last == {
        "id": "",
        "sentences": ["Bitti."],
        "done": True,
        "ending": "",
        "refused": False,
    }
    assert daemon.polled == ["a1", "a1"]


def test_an_ending_is_a_sentence_from_the_catalog(launcher: tuple[TestClient, Daemon]) -> None:
    client, daemon = launcher
    ending = {"refused": True, "refusal_key": CUT_KEY}
    daemon.replies = [{"answer_id": "a1", "done": True, "sentences": ["Bir."], **ending}]
    reply = client.post("/assistant/more", json={"id": "a1"}, headers=SAME).json()
    assert reply["sentences"] == ["Bir."]
    assert reply["ending"].startswith("Bu konuda sana bu kadarını"), reply["ending"]
    assert reply["refused"] is True


def test_a_resting_daemon_is_said_kindly(launcher: tuple[TestClient, Daemon]) -> None:
    from biokids_shell.client import resting

    client, daemon = launcher
    daemon.replies = [resting()]
    reply = client.post("/assistant/ask", json={"question": "q"}, headers=SAME).json()
    assert reply["done"] and reply["ending"].startswith("Şu an biraz dinleniyorum")


@pytest.mark.parametrize("path", ["/assistant/more", "/assistant/say"])
def test_a_website_cannot_read_or_speak_an_answer(
    launcher: tuple[TestClient, Daemon], path: str
) -> None:
    client, _daemon = launcher
    response = client.post(
        path,
        content="id=a1&text=hello",
        headers={"content-type": "application/x-www-form-urlencoded", "origin": LAUNCHER},
    )
    assert response.status_code == 403
    other = client.post(path, json={"id": "a1"}, headers={"origin": "https://example.com"})
    assert other.status_code == 403


def test_a_sentence_is_said_and_answered_when_said(launcher: tuple[TestClient, Daemon]) -> None:
    client, _daemon = launcher
    said: list[tuple[str, str]] = []

    async def say_and_wait(text: str, locale: str) -> bool:
        said.append((text, locale))
        return True

    client.app.state.shell.say_and_wait = say_and_wait  # type: ignore[attr-defined]
    reply = client.post("/assistant/say", json={"text": "  Gök  mavidir. "}, headers=SAME)
    assert reply.json() == {"ok": True}
    assert said == [("Gök mavidir.", "tr")]


# ------------------------------------------------- one sentence at a time


class Voice:
    """The voice service: `say` is answered at once, the end comes as an event."""

    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.said: list[str] = []

    async def call(self, op: str, **args: Any) -> Response:
        self.said.append(args["text"])
        return Response(ok=True, data={"id": "u7", "duration_ms": 200, "status": self.status})


class Writer:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def frame(kind: str, line_id: str) -> bytes:
    return json.dumps({"event": kind, "payload": {"id": line_id}}).encode() + b"\n"


async def a_shell_with_voice(
    config: Config, monkeypatch: pytest.MonkeyPatch, voice: Voice
) -> tuple[Any, asyncio.StreamReader, Writer]:
    from biokids_shell import app as shell_app

    shell = shell_app.ShellState(config)
    shell.voice = voice  # type: ignore[assignment]
    reader = asyncio.StreamReader()
    writer = Writer()

    async def open_voice(_path: Any) -> tuple[asyncio.StreamReader, Writer]:
        return reader, writer

    monkeypatch.setattr(shell_app, "_open_voice", open_voice)
    return shell, reader, writer


async def test_saying_waits_for_its_own_line_to_finish(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = Voice()
    shell, reader, writer = await a_shell_with_voice(config, monkeypatch, voice)
    reader.feed_data(b'{"id":"said","ok":true,"data":{"subscribed":true}}\n')
    saying = asyncio.create_task(shell.say_and_wait("Gök mavidir.", "tr"))
    await settle()
    assert b'"op":"subscribe"' in writer.sent, "listening before asking"
    assert voice.said == ["Gök mavidir."]

    reader.feed_data(frame("speak_started", "u7") + frame("speak_finished", "u3"))
    await settle()
    assert not saying.done(), "another line finishing is not this one"
    reader.feed_data(frame("speak_finished", "u7"))
    assert await asyncio.wait_for(saying, 1) is True
    assert writer.closed


async def test_a_line_the_queue_dropped_is_not_waited_for(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell, reader, _writer = await a_shell_with_voice(config, monkeypatch, Voice("dropped"))
    reader.feed_data(b'{"id":"said","ok":true}\n')
    assert await asyncio.wait_for(shell.say_and_wait("Bir.", "tr"), 1) is False


async def test_a_line_whose_end_never_comes_is_given_up_on(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_shell import app as shell_app

    monkeypatch.setattr(shell_app, "SAY_SLACK_S", 0.05)
    shell, reader, _writer = await a_shell_with_voice(config, monkeypatch, Voice())
    reader.feed_data(b'{"id":"said","ok":true}\n')
    assert await asyncio.wait_for(shell.say_and_wait("Bir.", "tr"), 2) is False


async def test_no_voice_service_is_silence_not_an_error(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_shell import app as shell_app

    async def refused(_path: Any) -> Any:
        raise FileNotFoundError("no such socket")

    shell = shell_app.ShellState(config)
    monkeypatch.setattr(shell_app, "_open_voice", refused)
    assert await shell.say_and_wait("Bir.", "tr") is False


def test_the_page_says_one_sentence_at_a_time() -> None:
    """The ordering lives in assistant.js; these are the lines it rests on."""
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parent.parent / "src/biokids_shell/static/assistant.js"
    ).read_text(encoding="utf-8")
    assert script.count('post("/assistant/say"') == 1, "one place sends a sentence to Zio"
    assert "if (saying || !unsaid.length) return;" in script, "never two at once"
    assert "unsaid.shift()" in script, "first in, first said"
    assert 'post("/assistant/more"' in script and "POLL_MS = 500" in script
