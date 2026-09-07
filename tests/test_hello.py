"""GET /hello: is this a butler, and is that the token?

Three distinct answers: nothing answered at all, something answered but not
a butler, and a butler that did not accept the token. Only the last is about
the token.
"""

import pathlib
import subprocess
import tomllib

import pytest

import butler
from conftest import TOKEN


def hello(client, token=TOKEN):
    headers = {"X-Token": token} if token is not None else {}
    return client.get("/hello", headers=headers)


def test_the_right_token_gets_the_version(client):
    answer = hello(client)
    assert answer.status_code == 200, answer.text
    assert answer.text.strip() == f"butler={butler.VERSION}"


def test_a_wrong_token_is_refused_in_the_backend_s_own_words(client):
    answer = hello(client, token="not-the-token")
    assert answer.status_code == 401
    # the app shows refusals verbatim, so this text is user-facing
    assert answer.text.strip() == "bad token"


def test_no_token_header_at_all_is_a_wrong_token(client):
    assert hello(client, token=None).status_code == 401


def test_a_non_ascii_token_is_a_401_and_not_a_500(client):
    # httpx won't encode a non-ASCII header itself; Starlette hands it on as
    # latin-1 mojibake, and compare_digest needs bytes here or this is a 500.
    answer = client.get("/hello", headers={"X-Token": "tökén".encode("utf-8")})
    assert answer.status_code == 401


def test_hello_answers_when_the_database_cannot_be_opened(db, client):
    """/hello is about the address and the token, never the disk: a butler
    whose volume came unmounted must still say whether the token was wrong."""
    for leftover in db.parent.glob("butler.db*"):
        leftover.unlink()
    db.mkdir()  # sqlite cannot open a path that is a directory
    assert client.get("/health").status_code == 503
    assert hello(client).status_code == 200


def test_the_version_matches_pyproject():
    """The container copies the source and installs no package, so VERSION
    cannot be read from package metadata; nothing else keeps the two in step."""
    root = pathlib.Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert butler.VERSION == declared


def test_a_coverage_run_leaves_nothing_for_git_to_add():
    """coverage.py writes `.coverage` beside pyproject: a SQLite file with
    this checkout's absolute paths in it, rewritten on every run, so it must
    stay untracked and ignored."""
    root = pathlib.Path(__file__).resolve().parent.parent

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)

    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        pytest.skip("not a git checkout")
    assert git("ls-files", ".coverage").stdout == ""
    assert git("check-ignore", "-q", ".coverage").returncode == 0
