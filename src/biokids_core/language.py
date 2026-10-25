"""Which language a child asked in: Turkish or English (D-244).

The assistant answers in the language of the question, and Zio says the answer
in that language's voice. Only these two: they are the languages of the
launcher, of the voices, and of the encyclopedias on the machine.

A guess from the words, not a model: the letters only Turkish has, and the
small words every sentence needs. A question that shows neither way -
"Karabaş", "dinozor" - is in the child's own language.
"""

from __future__ import annotations

import re
from typing import Final

LANGUAGES: Final = ("tr", "en")

TURKISH_LETTERS: Final = frozenset("çğıöşüÇĞİÖŞÜ")

#: Small words of questions and chat, as a child types them - also without
#: the Turkish letters, which a keyboard without them leaves out.
TURKISH_WORDS: Final = frozenset(
    {"ne", "neden", "niye", "nicin", "niçin", "nasil", "nasıl", "nerede", "nereye"}
    | {"nereden", "kim", "kimin", "hangi", "kac", "kaç", "mi", "mı", "mu", "mü"}
    | {"misin", "musun", "midir", "mıdır", "nedir", "neler", "acaba", "ve", "ile"}
    | {"icin", "için", "gibi", "cok", "çok", "ama", "fakat", "de", "da", "ki", "bu"}
    | {"su", "şu", "ben", "sen", "biz", "siz", "onlar", "benim", "senin", "var"}
    | {"yok", "merhaba", "selam", "evet", "hayir", "hayır", "tamam", "lutfen"}
    | {"lütfen", "tesekkurler", "teşekkürler", "bir", "iki", "hep", "olur"}
    | {"olmaz", "neyi", "neye", "yapar", "yapilir", "yer", "olan", "olmak"}
)

#: The same for English, without the words that are Turkish words too: "on"
#: (ten), "can", "at" (a horse), "an", "in", "it", "do", "a", "i", "am".
ENGLISH_WORDS: Final = frozenset(
    {"what", "why", "how", "where", "when", "who", "whose", "which", "is", "are"}
    | {"was", "were", "does", "did", "could", "would", "should", "will", "the"}
    | {"of", "to", "for", "with", "and", "or", "but", "not", "you", "your", "my"}
    | {"me", "we", "they", "she", "its", "this", "that", "these", "those"}
    | {"there", "here", "have", "has", "had", "hello", "hi", "please", "thanks"}
    | {"thank", "yes", "tell", "about", "many", "much", "like", "love", "want"}
    | {"know", "see", "go", "play", "make", "look", "think", "eat", "live"}
)

_WORD: Final = re.compile(r"[^\W\d_]+")


def question_language(text: str, fallback: str) -> str:
    """ "tr" or "en": the language `text` is written in, or `fallback`'s when
    the words do not say. A `fallback` other than these two is read as English.
    """
    own = "tr" if fallback.startswith("tr") else "en"
    if any(letter in TURKISH_LETTERS for letter in text):
        return "tr"
    words = _WORD.findall(text.lower())
    turkish = sum(word in TURKISH_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    # One small word is enough when nothing points the other way; with words
    # of both, it takes two more, so that a name or a borrowed word ("Pokemon
    # Go") does not turn the answer's language.
    if (english and not turkish) or english >= turkish + 2:
        return "en"
    if (turkish and not english) or turkish >= english + 2:
        return "tr"
    return own
