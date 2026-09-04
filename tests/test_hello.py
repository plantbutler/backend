"""GET /hello: is this a butler, and is that the token?

The phone asks this once, on the setup screen, and the whole point is that
its three answers are three different sentences: nothing answered at all
(wrong address, or the tailnet is down), something answered but not a
butler, and a butler that did not accept the token. Only the last one is
about the token, and only the user can tell which mistake they made.
"""

import pathlib
import tomllib

import pytest
from fastapi.testclient import TestClient

import butler
from butler import create_app

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    return TestClient(create_app(db_path=str(db), token=TOKEN))


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
    # The app shows refusals verbatim, so this text is user-facing.
    assert answer.text.strip() == "bad token"


def test_no_token_header_at_all_is_a_wrong_token(client):
    assert hello(client, token=None).status_code == 401


def test_a_non_ascii_token_is_a_401_and_not_a_500(client):
    # Bytes, because httpx will not encode a non-ASCII header itself — which
    # is also why this is the shape the mistake really arrives in. Starlette
    # hands it on as latin-1 mojibake; compare_digest raises TypeError on a
    # non-ASCII str, so bad_token compares bytes, and a token pasted with a
    # smart quote in it must be a 401 rather than a traceback.
    answer = client.get("/hello", headers={"X-Token": "tökén".encode("utf-8")})
    assert answer.status_code == 401


def test_hello_answers_when_the_database_cannot_be_opened(db, client):
    """The claim in the docstring, tested: /hello is about the address and
    the token, never about the disk. A butler whose volume came unmounted
    must still be able to say the token was wrong."""
    for leftover in db.parent.glob("butler.db*"):
        leftover.unlink()
    db.mkdir()  # sqlite cannot open a directory
    assert client.get("/health").status_code == 503
    assert hello(client).status_code == 200


def test_the_version_matches_pyproject():
    """The container copies butler.py and installs no package, so VERSION
    cannot be read from the metadata; this is what keeps the two in step."""
    root = pathlib.Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert butler.VERSION == declared
