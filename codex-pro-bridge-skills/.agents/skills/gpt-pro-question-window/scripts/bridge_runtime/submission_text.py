"""Decode only the observed plaintext-to-Markdown serialization of user Copy."""
import re


def copied_prompt_codec(copied, expected, *, literal_user_text=False):
    if copied.strip() == expected.strip():
        return "exact"
    if not literal_user_text:
        return None
    # The new literal-user renderer escapes punctuation and emits hard breaks.
    # Decode representation syntax, not arbitrary Markdown formatting/whitespace.
    text = copied
    if expected.endswith("\n") and text.endswith("\\"):
        text += "\n"
    text = re.sub(r"(?<!\\)\[(https?://[^\]\s]+)\]\(\1\)", r"\1", text)
    text = re.sub(r"\\\r?\n", "\n", text)
    text = re.sub(r'''\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])''', r"\1", text)
    return "literal-user-markdown/v1" if text.strip() == expected.strip() else None
