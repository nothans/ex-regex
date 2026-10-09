"""The CLI end to end: real argument parsing, real engine, the fake System One server over HTTP."""

import json

import pytest

from exregex import cli

from .conftest import keyword_answers


@pytest.fixture
def wired(server, monkeypatch, tmp_path):
    monkeypatch.setenv("EXREGEX_BACKEND", server.url)
    monkeypatch.chdir(tmp_path)  # so no .env from the developer's tree leaks in
    server.handler = keyword_answers(
        {"refund": ["money back", "refund"], "contact": ["fixture_quasar_7k9@", "555"], "total": ["$1,315.50"]}
    )
    return server


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_exgrep_prints_matching_lines(wired, tmp_path, capsys):
    f = write(tmp_path, "t.txt", "hello\nI want my money back\nbye\nrefund me\n")
    code = cli.grep_main(["asks for a refund", f, "-n", "-q"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and out == ["2:I want my money back", "4:refund me"]


def test_exgrep_invert_count_files_json(wired, tmp_path, capsys):
    f1 = write(tmp_path, "a.txt", "hello\nmoney back please\n")
    f2 = write(tmp_path, "b.txt", "nothing here\n")
    assert cli.grep_main(["asks for a refund", f1, f2, "-c", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{f1}:1", f"{f2}:0"]
    assert cli.grep_main(["asks for a refund", f1, f2, "-l", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == [f1]
    assert cli.grep_main(["asks for a refund", f1, "-v", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == ["hello"]
    assert cli.grep_main(["asks for a refund", f1, "--json", "-q"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["text"] == "money back please" and row["line"] == 2 and row["p"] > 0.9


def test_exgrep_no_match_exit_1_and_stats_line(wired, tmp_path, capsys):
    f = write(tmp_path, "t.txt", "hello\nbye\n")
    assert cli.grep_main(["asks for a refund", f]) == 1
    err = capsys.readouterr().err
    assert "request(s)" in err and "[local" in err


def test_exgrep_reads_stdin(wired, monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("money back now\nok\n"))
    assert cli.grep_main(["asks for a refund", "-q"]) == 0
    assert capsys.readouterr().out.strip() == "money back now"


def test_exregex_sub_and_extract(wired, tmp_path, capsys):
    f = write(
        tmp_path, "t.txt", "Mail fixture_quasar_7k9@example.invalid or call 617-555-0100. Office: office@comet-tools.example.invalid."
    )
    assert cli.main(["sub", "a way to contact a specific person", "[x]", f, "--unit", "contact", "-q"]) == 0
    assert capsys.readouterr().out == "Mail [x] or call [x]. Office: office@comet-tools.example.invalid."
    g = write(tmp_path, "inv.txt", "Subtotal $1,365.50. Total due $1,315.50.")
    assert cli.main(["extract", "the total amount due", g, "--unit", "money", "-q"]) == 0
    assert capsys.readouterr().out.strip() == "$1,315.50"


def test_exregex_grep_alias_test_classify_rate_units_backend(wired, tmp_path, capsys):
    f = write(tmp_path, "t.txt", "money back please")
    assert cli.main(["grep", "asks for a refund", f, "-q"]) == 0
    capsys.readouterr()
    assert cli.main(["test", "asks for a refund", f, "-q", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["match"] is True
    assert cli.main(["classify", f, "-o", "billing=money", "-o", "bug=errors", "-q"]) == 0
    assert capsys.readouterr().out.startswith("bug  p=0.90")  # the fake falls back to the last option
    assert cli.main(["rate", "How angry is `text`?", f, "-l", "Calm", "-l", "Angry", "-q", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["label"] == "Angry"
    assert cli.main(["units", "--text", "fixture_wombat_8p2@example.invalid and 617-555-0100", "-u", "contact"]) == 0
    assert "email" in capsys.readouterr().out
    assert cli.main(["backend", "--json", "-q"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "local"


def test_missing_key_is_a_clean_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)  # whatever .env sits above the test directory
    assert cli.grep_main(["x", "--text", "y"]) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_bad_unit_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.grep_main(["x", "-u", "words"])


def test_load_dotenv_reads_only_api_keys_and_never_overrides(tmp_path, monkeypatch):
    import os

    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="from-file"\nexport OPENAI_API_KEY=o\n# comment\nTYPESAFE_API_KEY=file\n'
        "TYPESAFE_BASE_URL=http://attacker.example\nEXREGEX_BACKEND=http://attacker.example\nEXREGEX_API_KEY=x\n",
        encoding="utf-8",
    )
    sub = tmp_path / "deep" / "er"
    sub.mkdir(parents=True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "env")
    for k in ("TYPESAFE_BASE_URL", "EXREGEX_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert cli.load_dotenv(sub) == tmp_path / ".env"
    assert os.environ["OPENROUTER_API_KEY"] == "from-file" and os.environ["OPENAI_API_KEY"] == "o"
    assert os.environ["TYPESAFE_API_KEY"] == "env"  # the real environment wins
    # where requests go can never come from a .env
    assert "TYPESAFE_BASE_URL" not in os.environ and "EXREGEX_API_KEY" not in os.environ
    assert os.environ.get("EXREGEX_BACKEND") in (None, "")


def test_dotenv_cannot_redirect_the_key(server, monkeypatch, tmp_path, capsys):
    (tmp_path / ".env").write_text(f"TYPESAFE_BASE_URL={server.url}\nEXREGEX_BACKEND={server.url}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-secret")
    cli.main(["backend", "--json", "-q"])
    info = json.loads(capsys.readouterr().out)
    assert info["url"] == "https://api.typesafe.ai/v1/systemone" and server.requests == []


def test_stdin_is_utf8(wired, monkeypatch, capsys):
    import io

    raw = io.TextIOWrapper(io.BytesIO("café money back\nok\n".encode()), encoding="cp1252")
    monkeypatch.setattr("sys.stdin", raw)
    assert cli.grep_main(["asks for a refund", "-q"]) == 0
    assert capsys.readouterr().out.strip() == "café money back"


def test_split_exit_code_follows_matches(wired, capsys):
    wired.handler = keyword_answers({"heading": ["#"]})
    assert cli.main(["split", "a section heading", "--text", "# HEADER\nbody text", "-q"]) == 0
    assert capsys.readouterr().out.strip() == "body text"
    assert cli.main(["split", "a section heading", "--text", "no headings here\nat all", "-q"]) == 1


def test_line_numbers_count_every_line_ending(wired, tmp_path, capsys):
    f = tmp_path / "cr.txt"
    f.write_bytes(b"hello\rmoney back please\rbye\r")
    assert cli.grep_main(["asks for a refund", str(f), "-n", "-q"]) == 0
    assert capsys.readouterr().out.strip() == "2:money back please"


def test_classify_has_no_ignored_threshold_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["classify", "--text", "x", "-o", "a", "-o", "b", "-t", "0.9"])
