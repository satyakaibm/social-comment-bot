import re
import time

from google import genai
from google.genai import errors, types

from app import config

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


def draft_reply(*, platform: str = "youtube", context_title: str, author: str, comment_text: str) -> str:
    label = _PLATFORM_LABELS.get(platform, platform)
    system_instruction = (
        f"{config.REPLY_PERSONA}\n\n"
        f"You are drafting a public reply to a comment on a {label}. "
        "Reply only with the text of the reply itself, no preamble, no quotes "
        "around it, no signature."
    )
    user_message = (
        f'{label.capitalize()} title/caption: "{context_title}"\n'
        f'Commenter: {author}\n'
        f'Comment: "{comment_text}"\n\n'
        "Draft a reply."
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
                ),
            )
            reply = response.text.strip()
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
