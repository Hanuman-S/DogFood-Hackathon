"""The one byte form a record payload is signed in, so anyone can re-derive exactly the bytes that were
signed from the payload they were shown.

    canonical(obj) = UTF-8 JSON, keys sorted, no whitespace, non-ASCII written as itself

Only dicts (with text keys), lists, whole numbers and text are allowed. Floats are refused (their text
form differs between languages and versions), and so are booleans and null (a verifier in another
language might write them differently, and a missing value should be said, not implied): callers write
a missing date as "" and a yes/no as "yes"/"no". A value of another type raises CanonicalError, so a
payload that would not survive a round trip through another verifier is never signed.
"""

import json

MAX_INT = 2 ** 53 - 1  # a whole number every JSON reader holds exactly


class CanonicalError(TypeError):
    pass


def check(value, where="payload"):
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise CanonicalError(f"{where}: {type(value).__name__} is not allowed in a signed payload "
                             "(use whole numbers and text; a missing date is \"\", yes/no is \"yes\"/\"no\")")
    if isinstance(value, int):
        if abs(value) > MAX_INT:
            raise CanonicalError(f"{where}: {value} is too large to be read exactly everywhere")
        return
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for n, item in enumerate(value):
            check(item, f"{where}[{n}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"{where}: key {key!r} is not text")
            check(item, f"{where}.{key}")
        return
    raise CanonicalError(f"{where}: {type(value).__name__} is not allowed in a signed payload")


def canonical(value) -> bytes:
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
