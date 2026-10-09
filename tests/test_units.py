import pytest

from exregex.units import Span, find, lines, paragraphs, sentences

TEXT = """Hi team, reach Quasar Marmot at quasar.marmot.7k9@example.invalid or fixture_quasar_7k9 at example dot invalid,
or fixture_wombat_8p2 [at] example [dot] invalid.
Call (415) 555-0177 x12, or four one five five five five oh one seven seven. London: +44 7700 900129.
Order #A-104 shipped on March 3, 2026. Total due: $1,315.50 (credit of $50.00, about 4%).
See https://comet-tools.example.invalid/inv?id=9. Ping @fixture_quasar_7k9. Mr. Marmot met Dr. Nebula Wombat at 3pm next Tuesday.
Server 10.0.0.12 is down."""


def texts(unit):
    return [s.text for s in find(TEXT, unit)]


def test_every_span_is_a_verbatim_slice():
    for unit in ("line", "sentence", "paragraph", "text", "contact", "money", "date", "time", "percent", "number", "ipv4", "name", "quote"):
        for s in find(TEXT, unit):
            assert TEXT[s.start : s.end] == s.text, (unit, s)


def test_emails_including_written_out():
    assert texts("email") == [
        "quasar.marmot.7k9@example.invalid",
        "fixture_quasar_7k9 at example dot invalid",
        "fixture_wombat_8p2 [at] example [dot] invalid",
    ]


def test_phones_including_spoken_digits():
    assert texts("phone") == ["(415) 555-0177 x12", "four one five five five five oh one seven seven", "+44 7700 900129"]


def test_written_out_email_with_any_domain_and_bare_extension():
    assert [s.text for s in find("Reach QuasarMarmot7k9 [at] example [dot] invalid today.", "email")] == [
        "QuasarMarmot7k9 [at] example [dot] invalid"
    ]
    assert [s.text for s in find("Ping Quasar Marmot on ext. 4471 please.", "phone")] == ["ext. 4471"]
    assert [s.text for s in find("Call 617-555-0100 x22.", "phone")] == ["617-555-0100 x22"]


def test_contact_group_is_the_union_in_order():
    got = texts("contact")
    assert got[0] == "quasar.marmot.7k9@example.invalid"
    assert "https://comet-tools.example.invalid/inv?id=9" in got and "@fixture_quasar_7k9" in got
    assert got == sorted(got, key=TEXT.index)


def test_money_dates_times():
    assert texts("money") == ["$1,315.50", "$50.00"]
    assert texts("date") == ["March 3, 2026", "next Tuesday"]
    assert texts("time") == ["3pm"]
    assert texts("percent") == ["4%"]
    assert texts("ipv4") == ["10.0.0.12"]


def test_names_keep_titles_and_skip_common_starts():
    got = texts("name")
    assert "Mr. Marmot" in got and "Dr. Nebula Wombat" in got
    assert "Order" not in got and "Total" not in got and "March" not in got


def test_overlap_longer_span_wins():
    text = "Call 415-555-0177 on 2026-10-07."
    spans = find(text, ("phone", "date"))
    assert [s.text for s in spans] == ["415-555-0177", "2026-10-07"]


def test_sentences_respect_abbreviations_and_initials():
    got = [s.text for s in sentences("Mr. Marmot met Q. Puffin at 5 p.m. today. It went well! Did it? Yes.")]
    assert got == ["Mr. Marmot met Q. Puffin at 5 p.m. today.", "It went well!", "Did it?", "Yes."]


def test_sentences_split_list_items_and_paragraphs():
    text = "Intro line\n- first item\n- second item\n\nNew paragraph here"
    assert [s.text for s in sentences(text)] == ["Intro line", "- first item", "- second item", "New paragraph here"]


def test_lines_and_paragraphs_offsets():
    text = "  one  \r\n\ntwo\nthree\n\n\nfour"
    assert [(s.text, text[s.start : s.end]) for s in lines(text)] == [("one", "one"), ("two", "two"), ("three", "three"), ("four", "four")]
    assert [s.text for s in paragraphs(text)] == ["one", "two\nthree", "four"]


def test_custom_callable_unit():
    def words(t):
        import re

        return [(m.start(), m.end()) for m in re.finditer(r"\w+", t)]

    spans = find("ab cd", words)
    assert spans == [Span(0, 2, "ab", "words"), Span(3, 5, "cd", "words")]


def test_unknown_unit():
    with pytest.raises(ValueError, match="unknown unit"):
        find("x", "paragraphz")


def test_empty_text():
    for unit in ("line", "sentence", "paragraph", "text", "contact"):
        assert find("", unit) == []


def test_custom_unit_offsets_are_bounds_checked():
    assert find("hello world", lambda t: [(-5, -1), (0, 10**9), (0, 5)]) == [Span(0, 5, "hello", "<lambda>")]


def test_pathological_inputs_stay_fast():
    import time

    cases = [
        ("The fox ran over the hill. Mr. Marmot saw it. " * 2000, "sentence"),
        ("a" * 50000 + " b.", "sentence"),
        ("." * 50000 + "a", "sentence"),
        ("1" * 50000, "money"),
        ("1." * 25000 + "a", "email"),
        ("www." * 12500, "email"),
        ("1 " * 25000, "number"),
    ]
    for text, unit in cases:
        t0 = time.perf_counter()
        find(text, unit)
        assert time.perf_counter() - t0 < 2.0, (unit, text[:20])


def test_sentences_no_etc_and_plan_b():
    got = [s.text for s in sentences("I said no. She left. See item No. 5 here. Plan B. Then we go, etc. Then more.")]
    assert got == ["I said no.", "She left.", "See item No. 5 here.", "Plan B.", "Then we go, etc.", "Then more."]


def test_real_addresses_win_over_written_out_forms():
    assert [s.text for s in find("You can reach me directly at quasar.marmot.7k9@example.invalid, I check it daily.", "email")] == [
        "quasar.marmot.7k9@example.invalid"
    ]
    assert [s.text for s in find("Drop me a line: nebula dot wombat dot 8p2 at example dot invalid.", "email")] == [
        "nebula dot wombat dot 8p2 at example dot invalid"
    ]
    # Unknown TLDs are now proposed for judgment; syntax alone cannot distinguish these.
    assert [s.text for s in find("She is directly at quasar.marmot in the org chart.", "email")] == ["directly at quasar.marmot"]
    assert find("Meet me at the cafe dot later.", "email") == []


def test_written_out_email_chains_stay_linear():
    import time

    for text in ("x dot " * 10000 + "at y", "x." * 20000 + " at y dot com", "x [dot] " * 8000 + "[at] y"):
        t0 = time.perf_counter()
        find(text, "contact")
        assert time.perf_counter() - t0 < 1.0


def test_written_out_address_at_the_end_of_a_sentence():
    assert [s.text for s in find("Easiest is my own address, comet.puffin.4r6 at example.invalid.", "email")] == [
        "comet.puffin.4r6 at example.invalid"
    ]
