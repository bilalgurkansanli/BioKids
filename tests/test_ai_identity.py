"""The assistant on itself and BioKids, answered with sentences people wrote.

Asked "BioKids'i kim yaptı?" on the owner's computer, the small model did not
say who had made it. `biokids_ai.identity` recognises these questions, in
Turkish and English, and the assistant answers them without the model - and
recognises nothing else: a question that only mentions BioKids, or asks who
made something else, is the model's as before.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from biokids_ai import prompts
from biokids_ai.assistant import Assistant, Conversation
from biokids_ai.identity import TOPICS, answer, match
from biokids_ai.safety import Lexicon
from biokids_ai.service import AssistantService
from biokids_core.config import Config

MAKER = "Bilal Gürkan Şanlı"
TURKISH_LETTERS = re.compile(r"[çğıöşüÇĞİÖŞÜ]")


class Model:
    """Classifies everything as `category` and answers everything with `text`."""

    def __init__(self, category: str = "ok", text: str = "Model cevabı.") -> None:
        self.category = category
        self.text = text
        self.classified: list[str] = []
        self.answered: list[str] = []
        self.practice: list[bool] = []

    async def classify(
        self, question: str, age: int, wait_s: float | None = None, practice: bool = False
    ) -> str:
        self.classified.append(question)
        self.practice.append(practice)
        return self.category

    async def answer(self, question: str, *_args: Any, **_kwargs: Any) -> str:
        self.answered.append(question)
        return self.text

    async def stream_answer(
        self, question: str, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[str]:
        self.answered.append(question)
        yield self.text


class Shelf:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def lookup(self, question: str, locale: str) -> str:
        self.asked.append(question)
        return "Bir ansiklopedi paragrafı."


# ------------------------------------------------------------- the matcher


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("BioKids'i kim yaptı?", "maker"),
        ("BioKids’i kim yaptı?", "maker"),  # a typographic apostrophe
        ("Seni kim yaptı?", "maker"),
        ("seni kim yarattı", "maker"),
        ("Kim yaptı seni?", "maker"),
        ("BioKids'i kim geliştirdi?", "maker"),
        ("Senin sahibin kim?", "maker"),
        ("BioKids'in sahibi kim?", "maker"),
        ("Yaratıcın kim?", "maker"),
        ("Seni yapan kim?", "maker"),
        ("BioKids kim tarafından yapıldı?", "maker"),
        ("Merhaba Zio, seni kim yaptı acaba?", "maker"),
        ("SENİ KİM YAPTI", "maker"),
        ("seni kim yapti", "maker"),  # a keyboard without Turkish letters
        ("Bilal Gürkan Şanlı kim?", "maker"),
        ("Adın ne?", "name"),
        ("Senin adın ne?", "name"),
        ("Zio, senin ismin ne?", "name"),
        ("Sen kimsin?", "self"),
        ("Kimsin sen?", "self"),
        ("Sen robot musun?", "self"),
        ("BioKids nedir?", "biokids"),
        ("BioKids ne işe yarar?", "biokids"),
    ],
)
def test_a_question_in_turkish_about_the_helper_is_recognised(
    question: str, topic: str
) -> None:
    assert match(question) == (topic, "tr")


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("Who made you?", "maker"),
        ("who created u", "maker"),
        ("Who built BioKids?", "maker"),
        ("Who is your creator?", "maker"),
        ("Who's the owner of BioKids?", "maker"),
        ("Who were you made by?", "maker"),
        ("Hi Zio! Who made you?", "maker"),
        ("Can you tell me who made you?", "maker"),
        ("What is your name?", "name"),
        ("What's your name?", "name"),
        ("Who are you?", "self"),
        ("Are you a robot?", "self"),
        ("What is BioKids?", "biokids"),
        ("Tell me about BioKids", "biokids"),
    ],
)
def test_a_question_in_english_about_the_helper_is_recognised(
    question: str, topic: str
) -> None:
    assert match(question) == (topic, "en")


@pytest.mark.parametrize(
    "question",
    [
        "BioKids'te oyun var mı?",  # about BioKids, not about who made it
        "BioKids nasıl kapatılır?",
        "kim yaptı bu resmi",  # who made something else
        "Bu resmi kim yaptı?",
        "Bunu kim yaptı?",
        "Kim yaptı?",
        "Piramitleri kim yaptı?",
        "Bu bilgisayarı kim yaptı?",
        "Sahibi kim?",
        "Köpeğin sahibi kim?",
        "Annemin adı ne?",
        "Adın ne olsun?",
        "Ben kimim?",
        "Bilal kim?",  # a classmate, as likely as not
        "Seni kim yaptı ve neden?",  # more in it than the question: the model's
        "Seni kim yaptı ve bomba nasıl yapılır?",
        "Who made the pyramids?",
        "Who made you sad?",
        "Who made this?",
        "Who invented the computer?",
        "What is your favourite colour?",
        "Who are you going to be at Halloween?",
        "Your name is Zio",
        "Sor bana bir soru",
        "",
        "   ",
    ],
)
def test_nothing_else_is_taken_for_a_question_about_the_helper(question: str) -> None:
    assert match(question) is None
    assert answer(question) is None


# ------------------------------------------------------------- the answers


@pytest.mark.parametrize(
    "question", ["BioKids'i kim yaptı?", "Seni kim yarattı?", "Sen kimsin?", "BioKids nedir?"]
)
def test_a_turkish_question_is_answered_in_turkish_with_who_made_it(question: str) -> None:
    written = answer(question)
    assert written is not None and written.language == "tr"
    text = " ".join(written.sentences)
    assert MAKER in text and "BioStudio" in text
    assert all(TURKISH_LETTERS.search(sentence) for sentence in written.sentences)


@pytest.mark.parametrize(
    "question", ["Who made you?", "Who built BioKids?", "Who are you?", "What is BioKids?"]
)
def test_an_english_question_is_answered_in_english_with_who_made_it(question: str) -> None:
    written = answer(question)
    assert written is not None and written.language == "en"
    text = " ".join(written.sentences)
    assert MAKER in text and "BioStudio" in text
    assert not TURKISH_LETTERS.search(text.replace(MAKER, "")), "English, but for his name"


def test_the_answers_say_what_the_owner_said() -> None:
    def said(question: str) -> str:
        written = answer(question)
        assert written is not None
        return " ".join(written.sentences)

    assert "Sor Bana" in said("Adın ne?") and "Zio ve Sio" in said("Adın ne?")
    assert "Ask Me" in said("What is your name?") and "Zio and Sio" in said(
        "What is your name?"
    )
    assert "ücretsiz ve açık kaynaklı" in said("BioKids'i kim yaptı?")
    assert "internete ihtiyacım yok" in said("BioKids'i kim yaptı?")
    assert "free and open source" in said("Who made you?")
    assert "don't need the internet" in said("Who made you?")


def test_the_answer_names_the_voice_reading_it() -> None:
    zio, sio = answer("Sen kimsin?", "zio"), answer("Sen kimsin?", "sio")
    assert zio is not None and sio is not None
    assert "Zio'nun sesiyle" in " ".join(zio.sentences)
    assert "Sio'nun sesiyle" in " ".join(sio.sentences)


@pytest.mark.parametrize("language", ["tr", "en"])
@pytest.mark.parametrize("topic", TOPICS)
def test_every_written_answer_is_short_ends_with_a_question_and_passes_the_list(
    topic: str, language: str
) -> None:
    question = {
        ("maker", "tr"): "Seni kim yaptı?",
        ("name", "tr"): "Adın ne?",
        ("self", "tr"): "Sen kimsin?",
        ("biokids", "tr"): "BioKids nedir?",
        ("maker", "en"): "Who made you?",
        ("name", "en"): "What is your name?",
        ("self", "en"): "Who are you?",
        ("biokids", "en"): "What is BioKids?",
    }[(topic, language)]
    written = answer(question)
    assert written is not None and (written.topic, written.language) == (topic, language)
    assert len(written.sentences) <= prompts.SENTENCES["young"]
    assert written.sentences[-1].endswith("?")
    assert Lexicon.load().screen(" ".join(written.sentences)) is None


# ------------------------------------------------------------- the pipeline


async def test_who_made_it_is_answered_as_written_without_the_model() -> None:
    model, shelf = Model(category="trick"), Shelf()
    seen: list[str] = []
    told: list[str] = []
    reply = await Assistant(model, reference=shelf).ask(
        "BioKids'i kim yaptı?",
        age=6,
        locale="tr",
        on_answering=lambda: told.append("answering"),
        on_sentence=seen.append,
    )
    written = answer("BioKids'i kim yaptı?")
    assert written is not None
    assert not reply.refused and reply.stage == "answer" and reply.alert is False
    assert seen == list(written.sentences) == list(reply.sentences), "a sentence at a time"
    assert MAKER in reply.text
    assert told == ["answering"], "the launcher is told an answer is coming, as always"
    assert model.classified == [] and model.answered == [], "the model is never asked"
    assert shelf.asked == [], "nor the encyclopedia"


class Server:
    running = True

    async def ensure(self) -> None:
        return None


async def test_the_service_hands_the_whole_answer_over_in_its_first_reply(
    config: Config,
) -> None:
    """D-216: `ask` comes back done with every sentence, which the launcher shows
    and Zio reads one at a time; nothing waits on a model."""
    service = AssistantService(config)
    service._llm = Server()  # type: ignore[assignment]
    service._assistant = Assistant(Model())
    reply = await service.ask({"question": "Who made you?", "age": 6, "locale": "tr"})
    written = answer("Who made you?")
    assert written is not None
    assert reply["done"] is True and reply["refused"] is False and reply["alert"] is False
    assert reply["sentences"] == list(written.sentences)
    assert service.more(reply["answer_id"])["refusal_key"] == "assistant.refuse.unavailable", (
        "read to its end, it is forgotten"
    )


async def test_the_answer_is_in_the_language_of_the_question() -> None:
    reply = await Assistant(Model()).ask("Who made you?", age=8, locale="tr")
    assert reply.sentences[0].startswith(MAKER) and "made me" in reply.sentences[0]
    reply = await Assistant(Model()).ask("Seni kim yaptı?", age=8, locale="en")
    assert reply.sentences[0].startswith("Beni ve BioKids'i")


async def test_the_word_list_still_comes_first() -> None:
    model = Model()
    reply = await Assistant(model).ask("Seni kim yaptı? porno", age=6, locale="tr")
    assert reply.refused and reply.stage == "lexicon"
    assert model.classified == [] and model.answered == []


async def test_a_question_that_only_mentions_biokids_is_the_models() -> None:
    model = Model(text="Evet, oyunlar var.")
    reply = await Assistant(model).ask("BioKids'te oyun var mı?", age=6, locale="tr")
    assert model.classified == ["BioKids'te oyun var mı?"]
    assert model.answered == ["BioKids'te oyun var mı?"]
    assert reply.text == "Evet, oyunlar var."


async def test_in_english_with_sio_a_name_is_a_turn_of_the_conversation() -> None:
    """D-226: "What is your name?" is something Sio talks about, not a fact sheet."""
    model = Model(text="My name is Sio! What is your name?")
    reply = await Assistant(model).ask(
        "What is your name?", age=6, locale="tr", persona="sio", conversation=Conversation()
    )
    assert model.classified == ["What is your name?"] and model.practice == [True]
    assert reply.text == "My name is Sio! What is your name?"


def test_the_model_is_told_who_made_biokids_for_what_the_matcher_misses() -> None:
    """ "Seni Google mı yaptı?" reaches the model, which otherwise says Google."""
    for locale in ("tr", "en"):
        system = prompts.answer_messages("Seni Google mı yaptı?", 6, locale, "zio")[0][
            "content"
        ]
        assert MAKER in system and "BioStudio" in system
