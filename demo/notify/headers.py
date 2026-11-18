"""Metadata header sent by partner systems, for example: campaign=spring;;owner='ops;eu';retry=2

Partners sometimes double the separator, and a value may be wrapped in single quotes so it
can carry a semicolon. Fields without "=" are ignored.
"""


def parse_meta(text: str) -> dict[str, str]:
    fields: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == "'":
            quoted = not quoted
            continue
        if char == ";" and not quoted:
            fields.append("".join(current))
            current = []
            continue
        current.append(char)
    fields.append("".join(current))
    result: dict[str, str] = {}
    for field in fields:
        key, sep, value = field.partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result
