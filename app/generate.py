import json
import re
import time

from google import genai
from google.genai import errors, types

from app import config
from app.reply_examples import format_for_prompt, load_examples
from app.sanitize import sanitize_draft

_client: genai.Client | None = None

MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_DELAY_SECONDS = 20.0


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        config.require("GEMINI_API_KEY")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _retry_delay_seconds(error: errors.ClientError) -> float:
    details = error.details if isinstance(error.details, dict) else {}
    for detail in details.get("error", {}).get("details", []):
        match = re.match(r"([\d.]+)s", str(detail.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    return DEFAULT_RETRY_DELAY_SECONDS


_PLATFORM_LABELS = {
    "youtube": "YouTube video",
    "facebook": "Facebook post",
    "instagram": "Instagram post",
}

# Instagram is the only supported platform where prefixing "@author" creates
# the intended commenter mention. YouTube replies are already nested beneath
# the original comment, so the username prefix is unnecessary.
_MENTION_PLATFORMS = {"instagram"}

# Keyed by config.reply_style_profile -- a page's mandatory rules on top of
# its persona. Two channels (a devotional Odia page vs. a travel channel) need
# different non-negotiable constraints, not just a different tone. This used
# to be keyed by the literal page key "hindolroad", which meant Gudiakateni --
# the same kind of page, sharing the same example files -- silently got the
# generic block instead. The devotional block is now a profile any such page
# can use; {label} is filled with the page's own label.
_REPLY_STYLE_INSTRUCTIONS: dict[str, str] = {
    "devotional": """
Mandatory reply style (takes precedence over persona and examples):
- This channel is {label}.
- Never add generic thanks or appreciation for watching, commenting, sharing,
  supporting the channel, or sharing love/devotion.
- Do not write sentences such as "Thank you for watching and sharing your devotion with us! ❤️",
  "Thanks for watching, and we're so glad this video touched your heart.",
  "We're so glad you enjoyed the video.",
  "ଆମ ଭିଡିଓ ଦେଖିଥିବାରୁ ଏବଂ କମେଣ୍ଟ କରିଥିବାରୁ ଆପଣଙ୍କୁ ଅନେକ ଧନ୍ୟବାଦ।",
  "ଆମ ଭିଡିଓ ଦେଖିଥିବାରୁ ବହୁତ ଧନ୍ୟବାଦ। ମା'ଙ୍କ କୃପା ସମସ୍ତଙ୍କୁ ଉପରେ ରହୁ।",
  or paraphrases/translations of them.
- Follow the persona's reply format before the examples. If the persona requires
  only 🙏 for devotional chants and greetings, the reply field must be exactly
  🙏, even when an example contains chant words. Otherwise use a short matching
  chant or 🙏. Do not append a thank-you sentence.
- For questions or feedback needing an answer, answer directly and briefly
  without a generic gratitude introduction or ending.
""",
}

_REPLY_STYLE_INSTRUCTIONS["generic"] = """
Mandatory reply style (takes precedence over persona and examples):
- Follow the persona's tone and reply format above.
- Never add generic thanks or appreciation for watching, commenting, sharing,
  or supporting the page/account.
- For questions or feedback needing an answer, answer directly and briefly
  without a generic gratitude introduction or ending.
"""

# Appended to whichever style block above applies, so the two concerns stay
# separate: the blocks above are about tone and format, these are only about
# which language a reply is written in. Keyed by the page's
# config.reply_language_policy -- see config.REPLY_LANGUAGE_POLICIES.
_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "match_commenter": """
- First identify the language of the COMMENT TEXT ITSELF and put its name in
  the `language` field. Decide it from the comment's words alone: the language
  of the caption or title, the channel's usual language, and the commenter's
  name tell you nothing about it. A German comment under an Odia caption gets
  a German reply.
- Latin letters do not mean English and do not mean an Indian language.
  German ("Viel Spaß", "danke", "schön"), French, Spanish, Portuguese,
  Italian, Indonesian and others are common; answer each in its own language.
  Call a Latin-script comment a romanized Indian language only when its words
  are Indian-language words -- "jay maa", "bhari sundar lagila", "tumba
  chennagide" -- and then reply romanized in that same language, not in the
  native script and not in English.
- Then write `reply` in exactly that language and script: Odia to Odia,
  Kannada to Kannada, Tamil to Tamil, Hindi to Hindi, German to German,
  English to English, and the same for any other language.
- Write it the way a native speaker of that language casually writes to a
  friend, not as a textbook translation of an English sentence.
- One language per reply. Never mix two languages or two scripts in the same
  reply, and never add a translation of your own reply.
- If the comment has no identifiable language -- only emoji, only punctuation,
  a bare name -- use the fallback language below if one is given, else English.
""",
    "odia_or_english": """
- Write the whole reply in ONE language: either Odia or English. Never mix Odia
  and English in the same reply.
- Do not use Hindi, Gujarati, Bengali, Telugu, Punjabi, or any other language.
  Do not mix Devanagari (जय, माँ) into an Odia reply; use Odia (ଜୟ, ମା)
  or English (Jay Maa).
""",
}


def _style_instruction(page_key: str) -> str:
    """The page's mandatory style block plus its reply-language rules."""
    profile = config.reply_style_profile(page_key)
    page = config.PAGES.get(page_key)
    base = _REPLY_STYLE_INSTRUCTIONS[profile].replace(
        "{label}", page.label if page else page_key
    )
    policy = config.reply_language_policy(page_key)
    language = _LANGUAGE_INSTRUCTIONS[policy]
    fallback = config.reply_fallback_language(page_key)
    if fallback:
        language = f"{language.rstrip()}\n- Fallback language for comments with no identifiable language: {fallback}.\n"
    return f"{base.rstrip()}\n{language.lstrip()}"


# Appended to every page's persona. These are the parts of the Hindolroad
# persona that have nothing to do with devotion -- reading the feeling in a
# comment, restraint with emoji, not guessing at the person -- plus a rule the
# original lacked. They used to live only in reply_examples/_reply_persona.txt,
# so a tenant whose persona was the one-line REPLY_PERSONA default got none of
# them, and the model filled the gap by inventing hotel names and visit dates
# for a travel channel. Tenants keep their own persona for flavour (travel,
# devotional, food); this is the floor beneath all of them.
_COMMON_PERSONA_RULES = """
Rules that apply whatever the persona above says:
- Match the reply to the feeling in the comment first. Warmth for praise and
  affection, 😄 for a joke, 🎉 for celebration, gentle support for sadness,
  calm acknowledgement for anger or criticism -- never copy or escalate anger.
- Keep it to one or two short sentences. Use at most one or two emoji, and
  only when they fit; never force one into every reply.
- Never infer gender from a username, name or avatar, and never compliment
  anyone's appearance.
- Only state facts that are in the caption, title or the comment itself.
  Never invent a place name, hotel, restaurant, price, date, route, or whether
  and when the creator visited somewhere. If a question cannot be answered
  from what is given, say so briefly and warmly, or ask what they would like
  to know -- do not guess.
- Answer a genuine question directly, without a generic thank-you before or
  after it.
"""

_OUTPUT_INSTRUCTION = '''
Respond with JSON only, no markdown. Name the comment's language first, then
write the reply in that language:
{"language": "<language of the comment>", "reply": "<public reply>"}
'''


def _parse_draft_payload(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return raw.strip()
    if not isinstance(data, dict) or "reply" not in data:
        return raw.strip()
    reply = str(data.get("reply") or "").strip()
    return reply or raw.strip()


def draft_reply(
    *,
    platform: str = "youtube",
    page_key: str = config.DEFAULT_PAGE_KEY,
    context_title: str,
    author: str,
    comment_text: str,
) -> str:
    label = _PLATFORM_LABELS.get(platform, platform)
    persona = config.PAGES[page_key].persona
    examples_block = format_for_prompt(load_examples(page_key=page_key), page_key=page_key)
    style_instruction = _style_instruction(page_key)
    system_instruction = (
        f"{persona}\n{_COMMON_PERSONA_RULES}\n"
        f"You are drafting a public reply to a comment on a {label}. "
        "The `reply` field is the public text only: no preamble, no quotes, "
        "no signature."
        f"{_OUTPUT_INSTRUCTION}"
    )
    if examples_block:
        system_instruction = f"{system_instruction}\n\n{examples_block}"
    system_instruction = f"{system_instruction}\n\n{style_instruction}"
    user_message = (
        f'{label.capitalize()} title/caption: "{context_title}"\n'
        f'Commenter: {author}\n'
        f'Comment: "{comment_text}"\n\n'
        "Draft a reply in the same style as the examples when they apply. "
        "The reply language is decided by the comment text alone, not by the "
        "caption, the title, or the commenter's name. "
    )
    client = _get_client()
    generation_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=300,
        response_mime_type="application/json",
    )

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            chat = client.chats.create(
                model=config.GEMINI_MODEL,
                config=generation_config,
            )
            response = chat.send_message(user_message)
            reply = _parse_draft_payload(response.text.strip())
            if platform in _MENTION_PLATFORMS:
                reply = f"@{author} {reply}"
            return sanitize_draft(reply, page_key=page_key)
        except errors.ClientError as e:
            if e.code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                delay = _retry_delay_seconds(e)
                print(f"Rate limited by Gemini API, waiting {delay:.0f}s before retry...")
                time.sleep(delay)
                continue
            raise
