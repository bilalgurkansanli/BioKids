"""Which language a child asked in (D-244)."""

from __future__ import annotations

import pytest

from biokids_core.language import question_language


@pytest.mark.parametrize(
    ("question", "fallback", "language"),
    [
        ("Why is the sky blue?", "tr", "en"),
        ("what is a dinosaur", "tr", "en"),
        ("I love dinosaurs", "tr", "en"),
        ("hello", "tr", "en"),
        ("Gökyüzü neden mavi?", "en", "tr"),
        ("gokyuzu neden mavi ve nasil olur", "en", "tr"),
        ("merhaba", "en", "tr"),
        ("Dinozorlar ne yer?", "en", "tr"),
        ("Karabaş", "en", "tr"),
        ("dinozor", "tr", "tr"),
        ("dinosaur", "en", "en"),
        ("T-Rex 66", "tr", "tr"),
        ("", "en", "en"),
        ("Pikachu", "en_US", "en"),
    ],
)
def test_the_answer_is_in_the_language_of_the_question(
    question: str, fallback: str, language: str
) -> None:
    assert question_language(question, fallback) == language


def test_a_borrowed_word_does_not_turn_a_turkish_question() -> None:
    """One English word in a longer Turkish question is not English."""
    assert question_language("Pokemon go oyunu nasil oynanir", "tr") == "tr"
