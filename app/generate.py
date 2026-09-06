import json
import re
import time

from google import genai
from google.genai import errors, types

from app import config
from app.reply_examples import append_chant_example, format_for_prompt, load_examples

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

# Platforms where prefixing "@author" on a reply tags the commenter and
# triggers a notification. Facebook is excluded: a plain "@Name" in comment
# text is not a real mention there (Facebook mentions require the
# commenter's numeric user id, which the Graph API doesn't expose for
# arbitrary public commenters).
_MENTION_PLATFORMS = {"youtube", "instagram"}

_LEARN_INSTRUCTION = """
When the incoming comment is a NEW type of chant or short greeting that is not
already in the examples, also record it so the chant files stay up to date.

Choose one file:
- chant-folded-hands.txt — namaste / Maa / Jai / 🙏-only greetings
- chant-har-har-mahadev.txt — Shiva chants (Har Har Mahadev, Om Namah Shivaya)
- chant-new-types.txt — other short chants/greetings that do not fit those two

Do not record questions, complaints, spam, long feedback, or a comment that is
only a spelling/emoji variant of an existing example.

Respond with JSON only, no markdown:
{"reply": "<public reply>", "learn": null}
or
{"reply": "<public reply>", "learn": {"file": "<filename>", "comment": "<typical comment>", "reply": "<example reply>"}}
"""


def _parse_draft_payload(raw: str) -> tuple[str, dict | None]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return raw.strip(), None
    if not isinstance(data, dict) or "reply" not in data:
        return raw.strip(), None
    reply = str(data.get("reply") or "").strip()
    learn = data.get("learn")
    if not isinstance(learn, dict):
        learn = None
    return reply or raw.strip(), learn


def draft_reply(*, platform: str = "youtube", context_title: str, author: str, comment_text: str) -> str:
    label = _PLATFORM_LABELS.get(platform, platform)
    examples_block = format_for_prompt(load_examples())
    system_instruction = (
        f"{config.REPLY_PERSONA}\n\n"
        f"You are drafting a public reply to a comment on a {label}. "
        "The `reply` field is the public text only: no preamble, no quotes, "
        "no signature."
        f"{_LEARN_INSTRUCTION}"
    )
    if examples_block:
        system_instruction = f"{system_instruction}\n\n{examples_block}"
    user_message = (
        f'{label.capitalize()} title/caption: "{context_title}"\n'
        f'Commenter: {author}\n'
        f'Comment: "{comment_text}"\n\n'
        "Draft a reply in the same style as the examples when they apply. "
        "If this is a new chant/greeting type, set learn; otherwise learn is null."
    )
    client = _get_client()

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=300,
                    response_mime_type="application/json",
                ),
            )
            reply, learn = _parse_draft_payload(response.text.strip())
            if learn:
                append_chant_example(
                    str(learn.get("file") or ""),
                    str(learn.get("comment") or comment_text),
                    str(learn.get("reply") or reply),
                )
            if platform in _MENTION_PLATFORMS:
                reply = f"@{author} {reply}"
            return reply
        except errors.ClientError as e:
            if e.code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                delay = _retry_delay_seconds(e)
                print(f"Rate limited by Gemini API, waiting {delay:.0f}s before retry...")
                time.sleep(delay)
                continue
            raise
