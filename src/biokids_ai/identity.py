"""Questions about the helper itself and BioKids, answered with sentences people wrote.

Asked "BioKids'i kim yaptı?" on the owner's computer, Gemma 4 E2B did not say
who had made it. A model knows nothing of a system made after it was trained,
and a small one answers anyway: Gemma's own answer to "who made you?" is
Google. No instruction makes that reliable, so the assistant does what it does
with a sum (`arithmetic.py`, D-248): the question is recognised here, in code,
and answered in the language it was asked in - who made the helper and
BioKids, the helper's name, who it is, and what BioKids is.

Only a whole message that is one of these questions is recognised, with at
most a greeting, the helper's name or a "tell me" around it. "BioKids'te oyun
var mı?" and "Bu resmi kim yaptı?" are not about the helper, and a question
with anything more in it ("Seni kim yaptı ve neden?") goes to the model as
before. That is also why a recognised question skips the classifier, as a
greeting does: there is nothing in it to judge, and "who are you?" is close
enough to the classifier's "trick" (reveal your instructions) for a small
model to confuse them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from biokids_ai.safety import fold, turkish_lower

TOPICS: Final = ("maker", "name", "self", "biokids")

# Every pattern is written for the text as `_normal` leaves it: lower case the
# Turkish way, Turkish letters folded to ASCII ("adın" and "adin" are one word,
# as a keyboard without them types it), apostrophes dropped ("BioKids'i" is
# "biokidsi"), and nothing but words and single spaces.

#: "BioKids", "Bio Kids", "biyokids": as a child types it or Whisper hears it.
_BIOKIDS: Final = r"bi(?:y)?o ?kids?"
_NAMED: Final = r"(?:bilal gurkan(?: sanli)?|gurkan sanli|bilal sanli)"

# ------------------------------------------------------------------ Turkish

_HELPER_TR: Final = r"(?:seni|sizi|sor banayi|zioyu|sioyu)"
_OBJECT_TR: Final = rf"(?:{_HELPER_TR}|{_BIOKIDS}(?:i|yi)?)"
_OWNER_TR: Final = rf"(?:senin|sizin|sor bananin|zionun|sionun|{_BIOKIDS}in)"
_STEMS_TR: Final = r"(?:yap|yarat|gelistir|programla|kodla|tasarla|yaz|icat et|uret|olustur)"
_MADE_TR: Final = rf"{_STEMS_TR}(?:[dt][iu]|m[iu]s)(?:lar|ler)?"
_MAKING_TR: Final = (
    r"(?:yapan|yaratan|gelistiren|programlayan|kodlayan|tasarlayan|yazan|icat eden"
    r"|ureten|olusturan)"
)
_PASSIVE_TR: Final = (
    r"(?:yapil|yaratil|gelistiril|programlan|kodlan|tasarlan|yazil|icat edil|uretil"
    r"|olusturul)"
)
#: "senin yaratıcın", "BioKids'in sahibi": the maker, as someone's.
_ITS_MAKER_TR: Final = (
    r"(?:(?:yapimci|yaratici|gelistirici|tasarimci|programci)(?:si|n)|(?:sahib|mucid)(?:i|in))"
)
#: "yaratıcın", "sahibin": the second person says whose without "senin".
_YOUR_MAKER_TR: Final = (
    r"(?:(?:yapimci|yaratici|gelistirici|tasarimci|programci)n|(?:sahib|mucid)in)"
)

_TURKISH: Final = {
    "maker": (
        rf"{_OBJECT_TR} kim(?:ler)? {_MADE_TR}",  # seni kim yaptı
        rf"kim(?:ler)? {_MADE_TR} {_OBJECT_TR}",  # kim yaptı seni
        rf"{_OBJECT_TR} {_MAKING_TR}(?: kisi)? kim(?:dir)?",  # seni yapan kim
        rf"{_OWNER_TR} {_ITS_MAKER_TR} kim(?:dir)?",  # BioKids'in sahibi kim
        rf"kim(?:dir)? {_OWNER_TR} {_ITS_MAKER_TR}",
        rf"{_YOUR_MAKER_TR} kim(?:dir)?",  # yaratıcın kim
        rf"(?:sen|{_BIOKIDS}|sor bana) kim tarafindan {_PASSIVE_TR}(?:d[iu]n?|m[iu]s)",
        rf"kim tarafindan {_PASSIVE_TR}(?:d[iu]n|m[iu]ss[iu]n)",  # ... yapıldın
        rf"{_BIOKIDS} kimin(?: eseri)?",
        rf"{_NAMED} kim(?:dir)?",
        r"biostudio (?:ne|nedir|kim|kimdir)",
    ),
    "name": (
        r"(?:senin |sizin )?(?:adin|ismin|adiniz|isminiz) (?:ne|nedir|neydi)(?: senin)?",
        r"(?:senin )?(?:adin|ismin) var mi",
        r"(?:bana )?(?:adini|ismini) (?:soyle|soylesene|soyler misin)",
    ),
    "self": (
        r"(?:sen )?(?:kimsin|nesin)(?: sen)?",
        r"(?:sen )?(?:bir )?(?:robot|insan|bilgisayar|makine|yapay zeka|program) m[iu]s[iu]n",
        r"kendini (?:tanit|tanitsana|tanitir m[iu]s[iu]n)",
        r"(?:sor bana|zio|sio) (?:kim|kimdir|nedir)",
    ),
    "biokids": (
        rf"{_BIOKIDS} (?:nedir|ne|ne demek|ne demektir|neymis|ne ise yarar)",
        rf"{_BIOKIDS} (?:ucretsiz|bedava|parasiz) m[iu]",
    ),
}

# ------------------------------------------------------------------ English

_YOU_EN: Final = r"(?:you|u)"
_OBJECT_EN: Final = rf"(?:you|u|ask me|sor bana|zio|sio|{_BIOKIDS})"
_MADE_EN: Final = (
    r"(?:made|make|makes|created|create|creates|built|build|builds|developed|develops"
    r"|designed|invented|programmed|coded|wrote)"
)
_BY_EN: Final = r"(?:made|created|built|developed|designed|programmed|coded|written)"
_MAKER_EN: Final = (
    r"(?:maker|creator|developer|inventor|owner|designer|programmer|author|builder)s?"
)
_WHO_IS_EN: Final = r"(?:who is|whos|who was|who are|who were)"

_ENGLISH: Final = {
    "maker": (
        rf"(?:who|which company)(?: did)? {_MADE_EN} {_OBJECT_EN}",  # who made you
        rf"who owns {_OBJECT_EN}",
        rf"{_WHO_IS_EN} (?:your|ur) {_MAKER_EN}",  # who is your creator
        rf"{_WHO_IS_EN} (?:the )?{_MAKER_EN} of {_OBJECT_EN}",
        rf"{_WHO_IS_EN} {_BIOKIDS}s? {_MAKER_EN}",
        rf"(?:who|whom) (?:are|were) {_YOU_EN} {_BY_EN} by",  # who were you made by
        rf"(?:who|whom) (?:is|was) {_BIOKIDS} {_BY_EN} by",
        rf"{_OBJECT_EN} (?:is|was|are|were) {_BY_EN} by (?:who|whom)",
        rf"(?:who is|whos) {_NAMED}",
        r"(?:what is|whats|who is|whos) biostudio",
    ),
    "name": (
        r"(?:what is|whats|what s) (?:your|ur) name",
        rf"do {_YOU_EN} have a name",
        rf"what (?:are|r) {_YOU_EN} called",
        rf"(?:what|how) (?:should|can|do) i call {_YOU_EN}",
        r"(?:your|ur) name",
    ),
    "self": (
        rf"(?:who|what) (?:are|r) {_YOU_EN}",
        rf"(?:are|r) {_YOU_EN} (?:a |an )?(?:robot|human|person|computer|machine|ai|real)",
        r"(?:about|introduce) yourself",
        r"(?:who|what) is (?:ask me|zio|sio)",
    ),
    "biokids": (
        rf"(?:what is|whats|what s) {_BIOKIDS}",
        rf"what does {_BIOKIDS} (?:mean|do)",
        rf"what is {_BIOKIDS} for",
        rf"is {_BIOKIDS} free",
        rf"about {_BIOKIDS}",
    ),
}

#: What may stand around the question: a greeting, the helper's name, a "tell
#: me". Nothing that carries a question of its own.
_AROUND: Final = (
    r"(?:merhaba|selam|hey|hi|hello|zio|sio|sor bana|peki|acaba|ya|lutfen|please|tamam"
    r"|ok|okay|so|well|canim|bana soyle|soyle|soylesene|soyle bakalim|soyler misin"
    r"|biliyor musun|merak ediyorum|tell me|can you tell me|could you tell me"
    r"|do you know|i wonder)"
)


def _whole(core: str) -> re.Pattern[str]:
    return re.compile(rf"(?:{_AROUND} )*(?:{core})(?: {_AROUND})*")


_PATTERNS: Final = tuple(
    (topic, language, _whole("|".join(patterns[topic])))
    for language, patterns in (("tr", _TURKISH), ("en", _ENGLISH))
    for topic in TOPICS
)

_APOSTROPHE: Final = re.compile(r"['’‘`´]")
_WORDS: Final = re.compile(r"[a-z0-9]+")


def _normal(text: str) -> str:
    folded = fold(turkish_lower(_APOSTROPHE.sub("", text)))
    return " ".join(_WORDS.findall(folded))


def match(question: str) -> tuple[str, str] | None:
    """(topic, language) of a question about the helper or BioKids, or None.

    The language is the one the question was asked in: the patterns are
    Turkish or English, never both.
    """
    text = _normal(question)
    if not text:
        return None
    for topic, language, pattern in _PATTERNS:
        if pattern.fullmatch(text):
            return topic, language
    return None


# ------------------------------------------------------------------ answers

PERSONA_NAMES: Final = {"zio": "Zio", "sio": "Sio"}

#: The facts, as the owner gave them: the helper is "Sor Bana" ("Ask Me"),
#: BioKids' helper; BioKids was made by Bilal Gürkan Şanlı (BioStudio); it is
#: free and open source; it runs on this computer and needs no internet; Zio
#: and Sio are its voices. At most five short sentences, the young band's
#: length, ending with a question, as every answer does (`prompts.py`).
_SENTENCES: Final = {
    "tr": {
        "maker": (
            "Beni ve BioKids'i, BioStudio'dan Bilal Gürkan Şanlı yaptı.",
            "BioKids ücretsiz ve açık kaynaklı: nasıl yapıldığını herkes görebilir.",
            "Ben bu bilgisayarın içinde çalışıyorum, internete ihtiyacım yok.",
            "Sen de bir gün bir bilgisayar programı yapmak ister misin?",
        ),
        "name": (
            "Benim adım Sor Bana; BioKids'in yardımcısıyım.",
            "Zio ve Sio benim iki sesim; şimdi sana {voice}'nun sesiyle konuşuyorum.",
            "Bugün ne merak ediyorsun?",
        ),
        "self": (
            "Ben Sor Bana, BioKids'in yardımcısıyım.",
            "İnsan değilim; bu bilgisayarın içinde çalışan bir programım ve internete "
            "ihtiyacım yok.",
            "Zio ve Sio benim iki sesim; şimdi sana {voice}'nun sesiyle konuşuyorum.",
            "Beni ve BioKids'i, BioStudio'dan Bilal Gürkan Şanlı yaptı.",
            "Bugün ne merak ediyorsun?",
        ),
        "biokids": (
            "BioKids, çocukların bilgisayarı güvenle kullanması için yapılmış bir sistem.",
            "Onu BioStudio'dan Bilal Gürkan Şanlı yaptı.",
            "Ücretsiz ve açık kaynaklı: nasıl yapıldığını herkes görebilir.",
            "Ben de onun yardımcısı Sor Bana'yım ve bu bilgisayarda internetsiz çalışıyorum.",
            "BioKids'te en çok ne yapmayı seviyorsun?",
        ),
    },
    "en": {
        "maker": (
            "Bilal Gürkan Şanlı of BioStudio made me and BioKids.",
            "BioKids is free and open source: anyone can see how it is made.",
            "I work right here inside this computer, and I don't need the internet.",
            "Would you like to make a computer program one day?",
        ),
        "name": (
            "My name is Ask Me, or Sor Bana in Turkish, and I am BioKids' helper.",
            "Zio and Sio are my two voices; right now I am talking to you in {voice}'s voice.",
            "What are you wondering about today?",
        ),
        "self": (
            "I am Ask Me, BioKids' helper.",
            "I am not a person: I am a program inside this computer, and I don't need "
            "the internet.",
            "Zio and Sio are my two voices; right now I am talking to you in {voice}'s voice.",
            "Bilal Gürkan Şanlı of BioStudio made me and BioKids.",
            "What are you wondering about today?",
        ),
        "biokids": (
            "BioKids is a system made so that children can use the computer safely.",
            "Bilal Gürkan Şanlı of BioStudio made it.",
            "It is free and open source: anyone can see how it is made.",
            "I am its helper, Ask Me, and I work on this computer without the internet.",
            "What do you like doing most on BioKids?",
        ),
    },
}


@dataclass(frozen=True, slots=True)
class Answer:
    topic: str
    language: str
    """The language of the question, "tr" or "en"; the answer is in it."""
    sentences: tuple[str, ...]


def answer(question: str, persona: str = "zio") -> Answer | None:
    """The written answer to a question about the helper or BioKids, or None.

    `persona` is the voice the answer is read in, which the answer names.
    """
    found = match(question)
    if found is None:
        return None
    topic, language = found
    voice = PERSONA_NAMES.get(persona, "Zio")
    return Answer(
        topic=topic,
        language=language,
        sentences=tuple(s.format(voice=voice) for s in _SENTENCES[language][topic]),
    )
