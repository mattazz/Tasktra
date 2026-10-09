"""Structural fingerprints for the two published pre-15 runtime lineages.

Fingerprints cover every application-owned sqlite_master object, including
table constraints, foreign keys, indexes and trigger bodies. Whitespace and
unquoted token case are normalized; quoted tokens remain exact.
"""
from hashlib import sha256
import json
import re
import sqlite3


_TOKENS = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|[^\s]")


def schema_signature(connection: sqlite3.Connection) -> str:
    objects = []
    for kind, name, table, sql in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
    ):
        tokens = []
        for token in _TOKENS.findall(sql or ""):
            if token[0] in {"'", '"', '`', '['}:
                tokens.append(token)
            else:
                tokens.append(token.lower())
        objects.append([kind, name, table, tokens])
    return sha256(json.dumps(objects, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


# Generated from checked-in historical source archives by the reconciliation
# fixture generator. This is data only; unknown structures cannot be migrated.
KNOWN_SCHEMAS: dict[int, frozenset[str]] = {
    10: frozenset(['1ccb14033a6a75e3ecd219da710e90aa9b76898b36fd9c882c817d83416e658b']),
    11: frozenset(['b628438b7f8da51b3b20df19880d94447a8092e7a546c30de89b8c170884f099', 'f303a4bbaaff0474a3c92d306617c5ba31b35ee6fd217983020ebde56fa42180']),
    12: frozenset(['8a1c363b483a2d03a9e63ceefc5f776986fc45c4a429d49047c4f2b27897e815', 'f303a4bbaaff0474a3c92d306617c5ba31b35ee6fd217983020ebde56fa42180']),
    13: frozenset(['409046cbfd08ed01402057e6697a6a0bdad0a09d909dfefb3ebf6f2a9994a47a']),
    14: frozenset(['d4b9bf1acdf2a149d70f1eb141183064d0069f8e9c6b2ab01a139b17ffb6ae82']),
}
