import json

import pytest

from macropad_mixer import protocol


def test_encode_compact_line():
    assert protocol.encode({"t": "ping"}) == b'{"t":"ping"}\n'


def test_encode_utf8_and_too_long():
    assert protocol.encode({"t": "vol", "id": "é"}).decode("utf-8").endswith('"é"}\n')
    with pytest.raises(ValueError):
        protocol.encode({"t": "x", "pad": "a" * protocol.MAX_LINE})


def test_decode_valid_messages():
    assert protocol.decode(b'{"t":"hello","fw":"0.1.0"}\n') == {"t": "hello", "fw": "0.1.0"}
    assert protocol.decode('{"t":"delta","id":"a","d":-2,"extra":1}') == {
        "t": "delta", "id": "a", "d": -2, "extra": 1}
    assert protocol.decode(b'{"t":"pong"}') == {"t": "pong"}


@pytest.mark.parametrize("line", [
    b"", b"not json", b"[1,2]", b'{"x":1}', b'{"t":"bogus"}', b'{"t":"apps","apps":[]}',
    b'{"t":"delta","id":"a","d":0}', b'{"t":"delta","id":"a","d":1.5}', b'{"t":"delta","id":"a","d":true}',
    b'{"t":"mute"}', b'{"t":"sel","id":3}', b"\xff\xfe",
])
def test_decode_rejects(line):
    assert protocol.decode(line) is None


def test_decode_rejects_overlong():
    msg = {"t": "sel", "id": "a" * (protocol.MAX_LINE + 10)}
    assert protocol.decode(json.dumps(msg)) is None


def test_encode_apps_trims_to_fit_and_caps(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_LINE", 1024)  # small limit forces trimming
    apps = [protocol.app_entry(f"com.example.application{i:02d}", "A very long application name",
                               100, False) for i in range(40)]
    data = protocol.encode_apps(apps)
    assert len(data) <= protocol.MAX_LINE
    msg = json.loads(data)
    assert msg["t"] == "apps"
    assert 0 < len(msg["apps"]) < 33
    assert msg["apps"][0]["id"] == "com.example.application00"  # trims from the end
    assert all(len(a["name"]) <= 20 for a in msg["apps"])


def test_encode_apps_caps_at_33(monkeypatch):
    # Lift the limit so only the 33-app cap applies.
    monkeypatch.setattr(protocol, "MAX_LINE", 100_000)
    small = [protocol.app_entry(str(i), "", 1, False) for i in range(40)]
    assert len(json.loads(protocol.encode_apps(small))["apps"]) == 33


def test_line_buffer_splits_and_discards_overlong():
    buf = protocol.LineBuffer()
    assert buf.feed(b'{"t":"po') == []
    assert buf.feed(b'ng"}\n{"t":"hello"}\n{"t"') == [b'{"t":"pong"}', b'{"t":"hello"}']
    assert buf.feed(b"x" * (protocol.MAX_LINE + 500)) == []           # partial over-long line is dropped...
    assert buf.feed(b"yyy\n") == []              # ...including its tail
    assert buf.feed(b'{"t":"pong"}\n') == [b'{"t":"pong"}']
    assert buf.feed(b"z" * (protocol.MAX_LINE + 10) + b"\nok\n") == [b"ok"]
