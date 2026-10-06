"""Which writing system a piece of text is in.

Split out of sanitize.py so config.py can validate script names without
importing the sanitizer (which imports config right back). Pure text
inspection: no config, no database, no I/O.
"""

import re

# One entry per Indic script the bot can be asked to reply in, so a reply in
# any of them survives sanitizing. sanitize.py used to carry a single
# "everything that is not Odia" character class, which silently deleted every
# Kannada, Telugu, Hindi or Bengali reply the model produced -- the empty
# result then became a bare 🙏, with nothing in the logs to say why. (Tamil
# was absent from that class and so was the one language that happened to
# work.) Ranges are the Unicode blocks. A language is identified by its
# script, which is as fine-grained as this needs to be: Hindi and Marathi
# both being Devanagari is not a distinction text inspection has to make.
INDIC_SCRIPTS: dict[str, tuple[int, int]] = {
    "Devanagari": (0x0900, 0x097F),
    "Bengali": (0x0980, 0x09FF),
    "Gurmukhi": (0x0A00, 0x0A7F),
    "Gujarati": (0x0A80, 0x0AFF),
    "Odia": (0x0B00, 0x0B7F),
    "Tamil": (0x0B80, 0x0BFF),
    "Telugu": (0x0C00, 0x0C7F),
    "Kannada": (0x0C80, 0x0CFF),
    "Malayalam": (0x0D00, 0x0D7F),
    "Sinhala": (0x0D80, 0x0DFF),
}

SCRIPT_RUNS = {
    name: re.compile(f"[\\u{lo:04X}-\\u{hi:04X}]+")
    for name, (lo, hi) in INDIC_SCRIPTS.items()
}

ODIA = "Odia"

# Every reply with no Indic characters at all: English, and any Indian
# language the commenter typed in Latin letters. Romanized Kannada and
# English are indistinguishable here, so this is one bucket, not many.
LATIN = "Latin"

SCRIPT_NAMES = (*INDIC_SCRIPTS, LATIN)


def script_counts(text: str) -> dict[str, int]:
    """How many characters of each Indic script `text` contains."""
    counts = {}
    for name, pattern in SCRIPT_RUNS.items():
        length = sum(len(run) for run in pattern.findall(text))
        if length:
            counts[name] = length
    return counts


def dominant_script(text: str) -> str:
    """The Indic script `text` is mostly written in, or LATIN if it has none."""
    counts = script_counts(text)
    if not counts:
        return LATIN
    return max(counts, key=lambda name: (counts[name], name == ODIA))


def drop_scripts(text: str, names) -> str:
    """Delete every run of each named script."""
    out = text
    for name in names:
        out = SCRIPT_RUNS[name].sub("", out)
    return out
