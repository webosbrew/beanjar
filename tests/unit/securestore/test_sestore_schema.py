#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""docs/user/sestore.schema.json stays in step with the loader and the user guide."""

import base64
import json
from pathlib import Path
import re
import sys
import tomllib

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import sestore

SCHEMA_PATH = TOP / "docs/user/sestore.schema.json"
GUIDE = TOP / "docs/user/sestore.md"
KEY = base64.b64encode(bytes(32)).decode()


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


def guide_example():
    return tomllib.loads(re.search(r"```toml\n(.*?)```", GUIDE.read_text(), re.S).group(1))


def definition(schema, name):
    return schema["definitions"][name]["allOf"][1]["properties"]


def test_name_pattern_and_limits_match_loader(schema):
    items = schema["properties"]["items"]
    pattern = items["propertyNames"]["pattern"]
    assert pattern == "^" + sestore.NAME.pattern + "$"
    assert items["maxProperties"] == sestore.MAX_ITEMS
    assert schema["properties"]["missing"]["enum"] == ["fallback", "error"]
    assert schema["additionalProperties"] is False
    assert schema["definitions"]["entry"]["additionalProperties"] is False
    assert set(schema["definitions"]["entry"]["properties"]) == {
        "text",
        "hex",
        "file",
        "nul_terminated",
    }
    for name in ("a" * 64, "0_x-", "Z"):
        assert re.fullmatch(pattern, name) and sestore.NAME.fullmatch(name)
    for name in ("a" * 65, "_x", "-x", "a b", "", "a.b"):
        assert not re.fullmatch(pattern, name) and not sestore.NAME.fullmatch(name)


def test_known_items_match_loader(schema):
    known = schema["properties"]["items"]["properties"]
    assert set(known) == set(sestore.MODELS) | {sestore.SSG, sestore.YOUTUBE, sestore.DVR_KEY}
    for name in sestore.MODELS:
        assert known[name] == {"$ref": "#/definitions/model_key"}
    assert known[sestore.SSG] == {"$ref": "#/definitions/ssg_key"}
    assert known[sestore.YOUTUBE] == {"$ref": "#/definitions/youtube_key"}
    assert known[sestore.DVR_KEY] == {"$ref": "#/definitions/dvr_key"}


def test_model_key_patterns_agree_with_validate_item(schema):
    props = definition(schema, "model_key")
    text, hexed = re.compile(props["text"]["pattern"]), re.compile(props["hex"]["pattern"])
    assert props["nul_terminated"] == {"const": False}
    for value in (
        KEY,
        base64.b64encode(bytes(range(32))).decode(),
        base64.b64encode(b"\xff" * 32).decode(),
    ):
        assert text.fullmatch(value) and hexed.fullmatch(value.encode().hex())
        sestore.validate_item(sestore.MODEL, value.encode())
    # Shapes the schema must reject, and the loader does too. Non-canonical base64 is left to
    # the host, which the schema documents.
    for value in (KEY[:-1], KEY + "A", KEY + "\n", "A" * 44, KEY.replace("A", "-", 1)):
        assert not text.fullmatch(value)
        with pytest.raises(sestore.Error):
            sestore.validate_item(sestore.MODEL, value.encode())


def test_dvr_and_youtube_patterns_agree_with_validate_item(schema):
    dvr = definition(schema, "dvr_key")
    assert re.fullmatch(dvr["hex"]["pattern"], "00" * 16)
    assert not re.fullmatch(dvr["hex"]["pattern"], "00" * 15)
    sestore.validate_item(sestore.DVR_KEY, bytes(16))
    with pytest.raises(sestore.Error):
        sestore.validate_item(sestore.DVR_KEY, bytes(15))
    props = definition(schema, "youtube_key")["text"]
    for value in ("id;secret", "id;sec;ret", "id;secret\n"):
        assert re.fullmatch(props["pattern"], value) and len(value) <= props["maxLength"]
        sestore.validate_item(
            sestore.YOUTUBE, value.encode() + (b"" if value[-1] == "\n" else b"\0")
        )
    for value in ("idsecret", ";secret", "id;", "id; secret"):
        assert not re.fullmatch(props["pattern"], value)
        with pytest.raises(sestore.Error):
            sestore.validate_item(sestore.YOUTUBE, value.encode() + b"\0")
    ssg = definition(schema, "ssg_key")["text"]
    assert ssg["maxLength"] == 1999 and ssg["minLength"] == 1


def test_guide_example_is_schema_shaped(schema):
    data = guide_example()
    assert set(data) <= set(schema["properties"])
    assert data["missing"] in schema["properties"]["missing"]["enum"]
    for name, entry in data["items"].items():
        assert sestore.NAME.fullmatch(name)
        assert set(entry) <= set(schema["definitions"]["entry"]["properties"])
        assert len(set(entry) & {"text", "hex", "file"}) == 1


DOCUMENTS = [
    ('missing = "error"\n', True),
    ("[items]\n", True),
    (f'[items]\n"1D492999_d" = {{ text = "{KEY}" }}\n', True),
    (f'[items]\n"1D492999" = {{ hex = "{KEY.encode().hex()}" }}\n', True),
    ('[items]\ndvr_key = { hex = "00112233445566778899aabbccddeeff" }\n', True),
    ('[items]\ngeneric = { file = "x.bin", nul_terminated = true }\n', True),
    ('[items]\n"C1FFB834" = { text = "a;b", nul_terminated = true }\n', True),
    ('missing = "maybe"\n', False),
    ("other = 1\n", False),
    ('[items]\n"_bad" = { text = "x" }\n', False),
    ('[items]\nx = { text = "x", hex = "00" }\n', False),
    ("[items]\nx = {}\n", False),
    ('[items]\nx = { text = "x", extra = 1 }\n', False),
    ('[items]\nx = { text = "x", nul_terminated = "yes" }\n', False),
    ('[items]\nx = { hex = "abc" }\n', False),
    ('[items]\nx = { file = "" }\n', False),
    ('[items]\n"1D492999" = { text = "short" }\n', False),
    (f'[items]\n"1D492999" = {{ text = "{KEY}", nul_terminated = true }}\n', False),
    ('[items]\ndvr_key = { hex = "0011" }\n', False),
    ('[items]\n"C1FFB834" = { text = "nosemicolon" }\n', False),
]


@pytest.mark.parametrize("document,valid", DOCUMENTS)
def test_documents_against_schema(schema, document, valid):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(schema)
    assert validator.is_valid(tomllib.loads(document)) is valid


def test_guide_example_validates(schema):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft7Validator(schema).validate(guide_example())
