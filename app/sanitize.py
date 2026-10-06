import json
import re

from app import config
from app.scripts import (
    INDIC_SCRIPTS,
    ODIA,
    drop_scripts,
    script_counts,
)

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
    r"tuning in|stopping by|sharing your|touched your heart)"
    r"[^.!\n]*"
    r"|"
    r"we(?:['’]re| are) (?:so )?glad "
    r"(?:you enjoyed the video|this video touched your heart)[^.!\n]*"
    r"|"
    r"thanks for watching[^.!\n]*"
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
    "କୃପା ସମସ୍ତଙ୍କୁ ଉପରେ ରହୁ",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[।.!?])\s+")

_REPLY_WRAPPER = re.compile(
    r"^\{\s*[\"']?reply[\"']?\s*:\s*(.*?)\s*\}+\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_reply_payload(text: str) -> str:
    """Extract reply text from valid or slightly malformed Gemini JSON."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and "reply" in payload:
        return str(payload.get("reply") or "").strip()

    match = _REPLY_WRAPPER.match(text)
    if not match:
        return text
    reply = match.group(1).strip()
    if len(reply) >= 2 and reply[0] == reply[-1] and reply[0] in "\"'":
        reply = reply[1:-1].strip()
    return reply


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


def _fix_scripts(text: str, *, policy: str) -> str:
    """Keep one Indic script per reply, chosen by `policy`.

    Both policies enforce the same end state -- no reply mixes two Indic
    scripts -- and differ only in which script wins.

    odia_or_english keeps Odia, whatever else the draft contains: the chant
    words in _SCRIPT_FIXES are transliterated and any other Indic run is
    deleted. That is the original Hindolroad behaviour.

    match_commenter keeps whichever script the draft is mostly written in, so
    a Kannada reply stays Kannada, and only deletes the minority runs -- the
    shape a leaked chant word takes. When the majority script is Odia the
    _SCRIPT_FIXES transliterations run first, so a stray `जय` still becomes
    `ଜୟ` rather than vanishing. The majority rule is the honest limit of what
    sanitizing can do from the draft text alone: a reply written entirely in
    the "wrong" language is indistinguishable from a correct reply to a
    comment in that language, so enforcing *which* language belongs here is
    the prompt's job (generate.py), not this function's.
    """
    out = text
    if policy == "odia_or_english":
        for src, dst in _SCRIPT_FIXES:
            out = out.replace(src, dst)
        out = drop_scripts(out, [n for n in INDIC_SCRIPTS if n != ODIA])
        return re.sub(r" {2,}", " ", out).strip()

    counts = script_counts(out)
    if len(counts) > 1:
        dominant = max(counts, key=lambda name: (counts[name], name == ODIA))
        if dominant == ODIA:
            for src, dst in _SCRIPT_FIXES:
                out = out.replace(src, dst)
            counts = script_counts(out)
        out = drop_scripts(out, [n for n in counts if n != dominant])
    return re.sub(r" {2,}", " ", out).strip()


def sanitize_draft(
    text: str, *, page_key: str = "", policy: str | None = None
) -> str:
    """Drop generic thanks-for-watching and keep one Indic script per reply.

    page_key selects the page's reply-language policy; '' means the default
    page, matching how comment rows store it. policy overrides that lookup
    directly, for callers testing one policy without a configured page.

    The thanks-stripping below only knows English and Odia phrasings. Under
    match_commenter a reply in another language relies on the prompt alone for
    that rule -- worth knowing when reviewing drafts in a new language.
    """
    if not text:
        return text
    policy = policy or config.reply_language_policy(page_key)
    mention = ""
    rest = text.strip()
    if rest.startswith("@") and " " in rest:
        mention, rest = rest.split(" ", 1)
        mention = mention + " "

    rest = _unwrap_reply_payload(rest)
    rest = _EN_THANKS.sub(" ", rest)
    rest = _strip_odia_thanks(rest)
    rest = _fix_scripts(rest, policy=policy)
    rest = re.sub(r"[ \t]{2,}", " ", rest)
    rest = re.sub(r"\s+([।.!?])", r"\1", rest).strip(" ,")
    if not rest:
        rest = "🙏"
    return f"{mention}{rest}".strip()


def remove_leading_mention(text: str) -> str:
    """Remove the automated leading @username from a platform reply."""
    return re.sub(r"^\s*@+\S+(?:\s+|$)", "", text).strip()


def sanitize_stored_drafts(*, platform: str = "youtube") -> int:
    """Rewrite stored draft_reply text after sanitize_draft. Returns rows changed."""
    from app import db

    db.init_db()
    updated = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT comment_id, page_key, draft_reply FROM comments WHERE platform = ?",
            (platform,),
        ).fetchall()
        for row in rows:
            old = row["draft_reply"] or ""
            # Each row carries the page it belongs to, so a re-sanitize pass
            # applies that page's own language policy rather than the default
            # page's to every tenant's drafts.
            new = sanitize_draft(old, page_key=row["page_key"] or "")
            if new != old:
                conn.execute(
                    "UPDATE comments SET draft_reply = ? WHERE comment_id = ?",
                    (new, row["comment_id"]),
                )
                updated += 1
    return updated
