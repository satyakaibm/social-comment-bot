import re

# Devanagari / Gujarati / Bengali chant fragments Gemini mixed into Odia replies.
_SCRIPT_FIXES = (
    ("जय जगन्नाथ", "ଜୟ ଜଗନ୍ନାଥ"),
    ("जय श्री जगन्नाथ", "ଜୟ ଶ୍ରୀ ଜଗନ୍ନାଥ"),
    ("जय माता दी", "ଜୟ ମା"),
    ("जय माँ दक्षिणकाली", "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ"),
    ("जय माँ काली", "ଜୟ ମା କାଳୀ"),
    ("जय माँ", "ଜୟ ମା"),
    ("जय मां", "ଜୟ ମା"),
    ("जय मा", "ଜୟ ମା"),
    ("जय", "ଜୟ"),
    ("जगन्नाथ", "ଜଗନ୍ନାଥ"),
    ("दक्षिणकाली", "ଦକ୍ଷିଣକାଳୀ"),
    ("माँ", "ମା"),
    ("માં", "ମା"),
    ("জয় জগନ୍ହାର୍", "ଜୟ ଜଗନ୍ନାଥ"),
    ("জয় মা", "ଜୟ ମା"),
    ("জয়", "ଜୟ"),
    ("જୟ", "ଜୟ"),
    ("ਜୟ", "ଜୟ"),
)

_EN_THANKS = re.compile(
    r"(?i)"
    r"\s*"
    r"(?:"
    r"(?:thank you(?: so much)?|thanks|many thanks|"
    r"our (?:heartfelt |sincere )?thanks)"
    r"[^.!\n]{0,160}"
    r"(?:watch(?:ing)?|video|commenting|devotion|supporting our channel|"
    r"tuning in|stopping by|sharing your)"
    r"[^.!\n]*"
    r"|"
    r"we(?:'re| are) glad you enjoyed the video[^.!\n]*"
    r"|"
    r"our video shares[^.!\n]*"
    r"|"
    r"thank you for your (?:wonderful |lovely )?support[^.!\n]*"
    r")"
    r"[.!?…]*"
    r"(?:\s*(?:❤️|❤|✨|🌺|🙏))*",
)

_ODIA_THANKS_MARKERS = (
    "ଦେଖିଥିବାରୁ",
    "କମେଣ୍ଟ କରିଥିବାରୁ",
    "ମତାମତ ଦେଇଥିବାରୁ",
    "ମତାମତ ପାଇଁ",
    "ଭିଡିଓ ଦେଖ",
    "ଭିଡିଓଟି ଦେଖ",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[।.!?])\s+")

# Indic scripts other than Odia (U+0B00–U+0B7F).
_OTHER_INDIC = re.compile(
    r"[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF"
    r"\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]+"
)


def _strip_odia_thanks(text: str) -> str:
    parts = _SENTENCE_SPLIT.split(text)
    kept = []
    for part in parts:
        chunk = part.strip()
        if not chunk:
            continue
        if any(marker in chunk for marker in _ODIA_THANKS_MARKERS):
            continue
        kept.append(chunk)
    return " ".join(kept)


def _fix_scripts(text: str) -> str:
    out = text
    for src, dst in _SCRIPT_FIXES:
        out = out.replace(src, dst)
    out = _OTHER_INDIC.sub("", out)
    return re.sub(r" {2,}", " ", out).strip()


def sanitize_draft(text: str) -> str:
    """Drop generic thanks-for-watching and keep Odia or English only."""
    if not text:
        return text
    mention = ""
    rest = text.strip()
    if rest.startswith("@") and " " in rest:
        mention, rest = rest.split(" ", 1)
        mention = mention + " "

    rest = _EN_THANKS.sub(" ", rest)
    rest = _strip_odia_thanks(rest)
    rest = _fix_scripts(rest)
    rest = re.sub(r"[ \t]{2,}", " ", rest)
    rest = re.sub(r"\s+([।.!?])", r"\1", rest).strip(" ,")
    if not rest:
        rest = "🙏"
    return f"{mention}{rest}".strip()
