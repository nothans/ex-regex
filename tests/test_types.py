import math

import pytest

from exregex import BackendError, Choice, LimitError, Noul, Score
from exregex._types import ChoiceAnswer, NoulAnswer, Refusal, ScoreAnswer, answer_to_wire, parse_answer


def test_noul_wire_and_criteria():
    assert Noul("Is it?").to_wire() == {"type": "noul", "instructions": "Is it?"}
    assert Noul("Is it?", {"true": "yes", "false": "no"}).to_wire()["criteria"] == {"true": "yes", "false": "no"}
    with pytest.raises(LimitError):
        Noul("Is it?", {"maybe": "x"})
    with pytest.raises(LimitError):
        Noul("   ")


def test_choice_limits():
    Choice("Which?", {"a": None, "b": "desc"})
    with pytest.raises(LimitError):
        Choice("Which?", {"a": None})
    with pytest.raises(LimitError):
        Choice("Which?", {str(i): None for i in range(256)})
    Choice("Which?", {str(i): None for i in range(255)})
    with pytest.raises(LimitError):
        Choice("Which?", ["a", "b"])  # type: ignore[arg-type]
    with pytest.raises(LimitError):
        Choice("Which?", {"": None, "b": None})


def test_score_limits():
    Score("How?", ["low", "high"])
    with pytest.raises(LimitError):
        Score("How?", ["only"])
    with pytest.raises(LimitError):
        Score("How?", [str(i) for i in range(11)])
    with pytest.raises(LimitError):
        Score("How?", "low,high")  # type: ignore[arg-type]


def test_parse_each_type():
    assert parse_answer({"type": "noul", "noul": 0.25}) == NoulAnswer(0.25)
    c = parse_answer({"type": "choice", "choice": "a", "probabilities": {"a": 0.8, "b": 0.2}, "confidence": 0.7})
    assert isinstance(c, ChoiceAnswer) and c.choice == "a" and c.p() == 0.8 and c.p("b") == 0.2
    s = parse_answer(
        {
            "type": "score",
            "score": 1.4,
            "legend": {"0": "lo", "1": "mid", "2": "hi"},
            "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5},
            "confidence": 0.5,
        }
    )
    assert isinstance(s, ScoreAnswer) and s.level == 2 and s.legend[1] == "mid" and math.isclose(s.score, 1.4)
    assert isinstance(parse_answer({"type": "refusal", "refusal": "no"}), Refusal)


def test_parse_choice_without_pick_uses_max():
    c = parse_answer({"type": "choice", "probabilities": {"a": 0.1, "b": 0.9}})
    assert c.choice == "b"


def test_parse_rejects_garbage():
    for bad in ({"type": "noul", "noul": "x"}, {"type": "noul", "noul": float("nan")}, {"type": "mystery"}, "nope", {"type": "choice"}):
        with pytest.raises(BackendError):
            parse_answer(bad)


def test_probabilities_are_clamped():
    assert parse_answer({"type": "noul", "noul": 1.0000001}).p == 1.0


def test_wire_roundtrip():
    for a in (
        NoulAnswer(0.5),
        ChoiceAnswer("a", {"a": 0.6, "b": 0.4}, 0.6),
        ScoreAnswer(1.0, {0: 0.0, 1: 1.0}, 1.0, {0: "lo", 1: "hi"}),
        Refusal("policy"),
    ):
        assert parse_answer(answer_to_wire(a)) == a
