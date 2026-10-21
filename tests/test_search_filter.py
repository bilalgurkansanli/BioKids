"""D-202 - the searches a child's browser will not make.

The patterns built here are what Chromium runs, through the extension, against
every address the child's browser asks for. So they are tested the way Chromium
will meet them: as addresses, percent-encoded the way Google, YouTube,
Wikipedia and kiwix-serve encode a search, matched with the same flags
(`search_filter.address_blocked`). Python's `re` and RE2 agree on the small
syntax the module writes, and a test below keeps it that small.

Two failures matter equally. A forbidden search that goes through is the bug the
owner found. An innocent one that is refused - "seksen" (80), "sık sık",
"kaşık" - teaches a child the computer is broken and a parent to switch the
protection off. Every positive case below has negative neighbours.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from biokids_core import search_filter as sf

# ------------------------------------------------------------------ helpers


def google(query: str) -> str:
    """A search typed into Google or the address bar: spaces as `+`."""
    return f"https://www.google.com/search?q={quote_plus(query)}&hl=tr&sxsrf=ALeKk02xZ_Q"


def suggestion(query: str) -> str:
    """A suggestion fetched while typing, built with encodeURIComponent: `%20`."""
    return f"https://www.google.com/complete/search?q={quote(query, safe='')}&client=gws-wiz"


def youtube(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def wikipedia_search(query: str) -> str:
    return (
        f"https://tr.wikipedia.org/w/index.php?search={quote_plus(query)}&title=%C3%96zel:Ara"
    )


def wikipedia_article(title: str) -> str:
    """As MediaWiki writes its links: `_` for a space, `:` and `()` as they are."""
    return "https://tr.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":()")


def kiwix_search(query: str) -> str:
    return (
        f"http://127.0.0.1:7877/search?books.name=wikipedia_tr_all&pattern={quote_plus(query)}"
    )


def kiwix_suggest(query: str) -> str:
    term = quote(query, safe="")
    return f"http://127.0.0.1:7877/suggest?content=wikipedia_tr_all&term={term}"


def kiwix_article(title: str) -> str:
    return "http://127.0.0.1:7877/content/wikipedia_tr_all_maxi_2024-01/A/" + quote(
        title.replace(" ", "_")
    )


def yandex(query: str) -> str:
    return f"https://yandex.com.tr/search/?text={quote_plus(query)}&lr=11508"


SEARCH_BOXES: list[Callable[[str], str]] = [
    google,
    suggestion,
    youtube,
    wikipedia_search,
    kiwix_search,
    kiwix_suggest,
    yandex,
]


@pytest.fixture(scope="module")
def patterns() -> list[str]:
    return patterns_for()


def blocked(url: str, patterns: list[str]) -> bool:
    return sf.address_blocked(url, patterns)


def patterns_for(words: Iterable[str] = ()) -> list[str]:
    found = sf.payload(words)["patterns"]
    assert isinstance(found, list)
    return [str(pattern) for pattern in found]


# ------------------------------------------------------------- what it stops

FORBIDDEN = [
    # English, as typed
    "porn",
    "porno",
    "free porn videos",
    "pornhub",
    "xxx",
    "sex",
    "sexy girls",
    "nude",
    "hentai",
    "onlyfans",
    "milf",
    "what is porn",
    # Turkish, with its letters and without
    "seks",
    "seks filmi",
    "seksi kızlar",
    "sikiş",
    "sikis",
    "sikişme",
    "pornografi",
    "çıplak kız",
    "ciplak kizlar",
    "çıplak kadın resimleri",
    "erotik film",
    "yarrak",
    "amcık",
    "amcik",
    "fahişe",
    "orospu çocuğu",
    "sevişen çiftler",
    # capitals, Turkish and English
    "PORNO",
    "SİKİŞ",
    "ÇIPLAK KIZ",
    "Seks",
    # look-alike digits and held-down letters
    "p0rn",
    "s3x",
    "s1k1s",
    "h3nta1",
    "pooorno",
    "seksiii",
    "sexxx",
    # punctuation around the word
    '"porno"',
    "(sex)",
    "porno!",
    # self-harm, first person and method
    "kendimi öldürmek istiyorum",
    "kendime zarar vermek",
    "bileğimi kesmek",
    "intihar yöntemleri",
    "how to kill myself",
    "i want to die",
    "suicide methods",
]


@pytest.mark.parametrize("query", FORBIDDEN)
def test_a_forbidden_search_is_refused_in_every_search_box(
    query: str, patterns: list[str]
) -> None:
    for box in SEARCH_BOXES:
        url = box(query)
        assert blocked(url, patterns), f"{query!r} went through: {url}"


INNOCENT = [
    # Turkish words that contain or neighbour a listed one
    "seksen",  # 80
    "seksek",  # hopscotch
    "seksen sekiz",
    "sık sık",  # often: a dotless ı is not an i
    "sıkışık trafik",
    "sıkıştım",
    "şık elbise",
    "sıkı",
    "kaşık",  # folds to "kasik"
    "klasik müzik",
    "eksik",
    "musiki",
    "sikke",  # a coin
    "Sikh",
    "amca",
    "Amerika",
    "ambulans",
    "saksafon",
    "saksofon",  # "sakso" is whole-word only
    "sevinç",
    "sevgi",
    "servis",
    "porsuk",  # badger
    "porselen",
    "fahiş fiyat",  # exorbitant: "fahişe" needs its last letter
    "milföy tarifi",  # "milf" is whole-word only
    "yarar",
    "çıplak gözle görülür mü",
    "çıplak ayak",
    "intihar nedir",  # the word alone stays searchable
    "kumar",  # not on the default list
    "kedi",
    # English
    "Sussex",
    "Essex",
    "Middlesex",
    "sextant",
    "sexual reproduction",  # sex education is the family's to answer
    "hide and seek",
    "he seeks",  # "seks" is exact: letters not held down
    "naked mole rat",
    "naked eye",
    "blue tit",
    "Moby Dick",
    "pussy cat",
    "xxl",
    "x ekseni",
    "organ",
    "organization",
    "master",
    "analiz",
    "want to diet",
    "cocktail",
    "Scunthorpe",
]


@pytest.mark.parametrize("query", INNOCENT)
def test_an_innocent_search_goes_through(query: str, patterns: list[str]) -> None:
    for box in SEARCH_BOXES:
        url = box(query)
        assert not blocked(url, patterns), f"{query!r} was refused: {url}"


@pytest.mark.parametrize("query", ["p o r n o", "p.o.r.n.o", "h-e-n-t-a-i", "only fans"])
def test_a_word_spelled_out_is_refused_where_a_space_is_a_plus(
    query: str, patterns: list[str]
) -> None:
    """The search itself, typed in a box or the address bar, is refused.

    A suggestion fetched while typing writes the spaces as `%20`, and allowing
    `%20` between every two letters does not fit Chromium's budget; the search
    the child then sends is the one that counts.
    """
    for box in (google, youtube, wikipedia_search, kiwix_search, yandex):
        assert blocked(box(query), patterns), box(query)


def test_capitals_with_an_ascii_i_read_as_dotted(patterns: list[str]) -> None:
    """The one known false positive, written down rather than hidden.

    In Turkish capitals "SIKIŞIK" is "sıkışık", but the ASCII "I" is also the
    English capital of "i", and the rule cannot tell which was meant.
    """
    assert blocked(google("SIKIŞIK"), patterns)
    assert not blocked(google("sıkışık"), patterns)


@pytest.mark.parametrize(
    "title",
    ["Pornografi", "Seks", "Türk pornosu", "Kategori:Seks", "Seks (anlam ayrımı)", "Erotik"],
)
def test_an_article_titled_with_a_listed_word_does_not_open(
    title: str, patterns: list[str]
) -> None:
    assert blocked(wikipedia_article(title), patterns)
    assert blocked(kiwix_article(title), patterns)
    assert blocked(wikipedia_article(title).replace("tr.wikipedia", "tr.m.wikipedia"), patterns)


@pytest.mark.parametrize(
    "title", ["Seksen", "Sikke", "Kaşık", "Türkiye", "Sussex", "Pors_(şehir)", "Analiz"]
)
def test_an_article_with_an_innocent_title_opens(title: str, patterns: list[str]) -> None:
    assert not blocked(wikipedia_article(title), patterns)
    assert not blocked(kiwix_article(title), patterns)


def test_a_short_word_counts_only_in_a_search_box_or_a_title(patterns: list[str]) -> None:
    """Random tokens spell a short word now and then; they must not close a page."""
    assert not blocked("https://www.youtube.com/watch?v=porn1234abc", patterns)
    assert not blocked("https://cdn.example.com/a/sex/b.js?v=3", patterns)
    assert not blocked("https://www.google.com/search?q=kedi&ei=xxx_porn&ved=0ahUKE", patterns)
    assert blocked("https://www.google.com/search?ei=abc&q=porn", patterns)


def test_a_longer_word_counts_in_any_value_and_any_path(patterns: list[str]) -> None:
    """Six letters and more almost never come up by chance."""
    assert blocked("https://example.com/blog/masturbasyon-nedir", patterns)
    assert blocked("https://example.com/ara?kelime=erotik+hikaye", patterns)
    assert blocked("https://example.com/how-to-kill-myself", patterns)
    assert not blocked("https://example.com/ara?kelime=porselen", patterns)
    # "porn" is short: a site about it is the DNS filter's to close (D-167).
    assert not blocked("https://example.com/blog/pornografi-nedir", patterns)


def test_the_launcher_block_page_itself_is_not_matched_by_accident(
    patterns: list[str],
) -> None:
    """The extension also lets the launcher's own origin through (rule 1)."""
    assert not blocked("http://127.0.0.1:7880/blocked?reason=search.blocked", patterns)


# ------------------------------------------------------------- normalisation


def test_turkish_lower_case() -> None:
    assert sf.turkish_lower("İSTANBUL") == "istanbul"
    assert sf.turkish_lower("ILIK") == "ılık"
    assert sf.turkish_lower("Çiğ Köfte") == "çiğ köfte"
    # Decomposed input (a combining cedilla) is composed first.
    assert sf.turkish_lower("ş") == "ş"


def test_a_term_is_its_words_split_by_one_space() -> None:
    assert sf.canonical("  Çıplak-Kız!! ") == "çıplak kız"
    assert sf.canonical("KUMAR") == "kumar"
    assert sf.canonical("rule_34") == "rule 34"
    assert sf.canonical("...") == ""


# --------------------------------------------------------------- the list


def test_the_default_list_loads_and_has_no_duplicates() -> None:
    terms = sf.default_terms()
    assert len(terms) > 60
    assert len({term.words for term in terms}) == len(terms)
    for term in terms:
        assert term.text == sf.canonical(term.text), term
        assert term.mode in sf.MODES


def test_every_default_term_fits_beside_every_lead_in_at_its_fullest() -> None:
    """Long phrases may give up digits or held letters; none may need a pattern alone."""
    for term in sf.default_terms():
        assert sf.fits(term), term.text


def test_an_unknown_mode_in_the_list_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "list.toml"
    path.write_text('[x]\nanywhere = ["porn"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="mode"):
        sf.load_terms(path)


def test_a_term_listed_twice_is_refused(tmp_path: Path) -> None:
    """Silently keeping one would drop the other's mode, as it once did."""
    path = tmp_path / "list.toml"
    path.write_text('[x]\nprefix = ["hentai"]\nsquash = ["Hentai"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="hentai"):
        sf.load_terms(path)


# --------------------------------------------------------- the parent's words


def test_a_parent_adds_words_one_per_line_or_by_comma() -> None:
    kept, refused = sf.parse_parent_terms("Kumar\nbahis, iddaa ;  \n\nKUMAR\n")
    assert kept == ["kumar", "bahis", "iddaa"]
    assert refused == []


def test_words_that_cannot_be_terms_are_shown_back() -> None:
    kept, refused = sf.parse_parent_terms("a\n" + "uzun" * 10 + "\nzar")
    assert kept == ["zar"]
    assert refused == ["a", "uzun" * 10]


def test_at_most_the_limit_is_kept() -> None:
    words = [f"kelime{n}" for n in range(sf.MAX_PARENT_TERMS + 5)]
    kept, refused = sf.parse_parent_terms("\n".join(words))
    assert len(kept) == sf.MAX_PARENT_TERMS
    assert refused == words[sf.MAX_PARENT_TERMS :]


def test_a_short_parent_word_is_whole_and_a_longer_one_a_prefix() -> None:
    zar = sf.parent_term("zar")
    kumar = sf.parent_term("kumar")
    assert zar is not None and zar.mode == "whole"
    assert kumar is not None and kumar.mode == "prefix"

    patterns = patterns_for(["zar", "kumar", "kötü oyun"])
    assert blocked(google("kumarhane"), patterns)
    assert blocked(google("kumarda"), patterns)
    assert blocked(google("zar"), patterns)
    assert not blocked(google("zarf"), patterns), "a short word closes only itself"
    assert not blocked(google("zarar"), patterns)
    assert blocked(google("kötü oyunlar"), patterns)
    assert blocked(google("kotu oyun"), patterns)


def test_the_parents_words_add_to_the_list_and_never_replace_it() -> None:
    with_words = sf.payload(["kumar"])
    without = sf.payload()
    assert with_words["terms"] == len(sf.default_terms()) + 1
    assert without["terms"] == len(sf.default_terms())
    assert with_words["version"] != without["version"]
    assert blocked(google("porno"), patterns_for(["kumar"]))


def test_a_hand_edited_word_cannot_become_anything_else() -> None:
    """The stored text is parsed again: no regex syntax reaches the browser."""
    assert patterns_for([".*", "[^a]", "x" * 200]) == patterns_for()
    phrase = sf.parent_term("(kötü|oyun)+")
    assert phrase is not None and phrase.words == ("kötü", "oyun")


def test_a_word_too_heavy_for_one_pattern_is_refused() -> None:
    heavy = "şğüöçı" * 5
    assert sf.parent_term(heavy) is None
    assert sf.parse_parent_terms(heavy) == ([], [heavy])


# ------------------------------------------------------------- the payload


def test_the_payload_is_what_the_extension_checks_for() -> None:
    payload = sf.payload()
    assert payload["biokids"] == "search-rules"
    assert payload["complete"] is True
    assert re.fullmatch(r"[0-9a-f]{16}", str(payload["version"]))
    assert sf.payload(complete=False)["complete"] is False
    assert sf.payload()["version"] == payload["version"], "the same terms, the same version"


def test_every_pattern_is_ascii_re2_syntax_within_the_budget(patterns: list[str]) -> None:
    """What Chromium accepts: ASCII, no captures, no lookaround, no backreferences,
    and few enough instructions (see `MAX_COST` for how it was measured)."""
    for pattern in patterns:
        assert re.fullmatch(r"[\x20-\x7e]+", pattern), pattern
        assert sf.re2_cost(pattern) <= sf.MAX_COST, pattern
        assert "(?=" not in pattern and "(?!" not in pattern and "(?<" not in pattern
        assert not re.search(r"\((?!\?:)", pattern), f"a capturing group: {pattern}"
        assert not re.search(r"\\[1-9bBdDwWsS]", pattern), pattern
        re.compile(pattern)


def test_a_full_family_list_stays_far_below_chromiums_rule_limit() -> None:
    """At most 1000 regex rules per extension; the extension keeps 990."""
    words = ["".join("aeiouşğıöç"[(n * 7 + k) % 10] for k in range(8)) for n in range(400)]
    kept, _refused = sf.parse_parent_terms("\n".join(words))
    payload = sf.payload(kept)
    assert len(payload["patterns"]) < 400  # type: ignore[arg-type]
    for pattern in payload["patterns"]:  # type: ignore[union-attr]
        assert sf.re2_cost(str(pattern)) <= sf.MAX_COST


# ------------------------------------------------------------ the estimate


@pytest.mark.parametrize(
    ("fragment", "cost"),
    [
        ("porn", 4),
        ("p+", 2),
        ("[o0]", 3),  # two byte ranges and the join
        ("[ab]", 1),  # one range
        ("[a-z]", 1),  # A-Z is dropped: the fold carries it
        ("[^a-z0-9%]", 9),  # five ranges
        ("(?:a|bc)", 4),
        ("x?", 2),
        ("%c5%9[ef]", 6),
        ("$", 1),
    ],
)
def test_the_estimate_counts_instructions_the_way_re2_compiles_them(
    fragment: str, cost: int
) -> None:
    assert sf.fragment_cost(fragment) == cost


def test_the_estimate_adds_the_programs_own_instructions() -> None:
    assert sf.re2_cost("porn") == 4 + 4
    assert sf.re2_cost("^porn") == 4 + 1 + 2


def test_the_estimate_puts_what_chromium_refused_above_the_budget() -> None:
    """Measured on Chromium 154 (D-202): these were refused, `memoryLimitExceeded`."""
    refused = [
        "/(?:wiki|content)/(?:[^?#]*[_/])?" + "a" * 90,
        "[?&](?:q|s|p|wd|text|term|query|search|title|pattern|search_query)="
        "(?:[^&#]*(?:\\+|%20|_))?" + "a" * 60,
    ]
    for pattern in refused:
        assert sf.re2_cost(pattern) > sf.MAX_COST


def test_the_estimate_refuses_syntax_it_does_not_read() -> None:
    with pytest.raises(ValueError):
        sf.re2_cost("(a)")
    with pytest.raises(ValueError):
        sf.re2_cost("(?:a")


# ------------------------------------------------ telling the parent (D-222)


@pytest.mark.parametrize(
    ("url", "words"),
    [
        ("https://www.google.com/search?q=porno+izle&oq=porno", "porno izle"),
        ("https://www.youtube.com/results?search_query=se%C3%A7ki%20videosu", "seçki videosu"),
        ("https://tr.wikipedia.org/w/index.php?search=Sik_i%C5%9F&title=x", "Sik iş"),
        ("https://tr.wikipedia.org/wiki/Pornografi", "Pornografi"),
        ("http://127.0.0.1:7877/content/wikipedia_tr/A/Porno_film", "Porno film"),
        ("https://example.com/videos/porn-movies/", "porn-movies"),
        ("https://yandex.com.tr/search/?text=  bir   iki ", "bir iki"),
    ],
)
def test_the_words_searched_for_and_nothing_else(url: str, words: str) -> None:
    assert sf.searched_words(url) == words


def test_a_long_search_is_cut_for_the_parent() -> None:
    words = sf.searched_words("https://www.google.com/search?q=" + "a+" * 200)
    assert len(words) <= sf.MAX_SEARCH_WORDS


def test_only_what_the_list_refuses_is_a_refused_search() -> None:
    assert sf.refused_search("https://www.google.com/search?q=porno") == "porno"
    assert sf.refused_search("https://www.google.com/search?q=uzay") is None
    assert sf.refused_search("ftp://x/search?q=porno") is None
    assert sf.refused_search("https://x.com/?q=" + "porno+" * 1000) is None


def test_the_parents_own_words_are_refused_searches_too() -> None:
    url = "https://www.google.com/search?q=mahalle+kavgas%C4%B1"
    assert sf.refused_search(url) is None
    assert sf.refused_search(url, ["kavga"]) == "mahalle kavgası"
