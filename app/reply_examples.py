from pathlib import Path

from app import config

_ALLOWED_SUFFIXES = {".txt", ".md"}

def _parse_examples(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    comment = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key in ("comment", "incoming", "from"):
            if comment:
                print(
                    f"{path.name}: comment without a reply was skipped: {comment!r}"
                )
            comment = value
        elif key in ("reply", "response", "to"):
            if comment and value:
                pairs.append((comment, value))
                comment = ""
            else:
                print(f"{path.name}: `reply:` needs a `comment:` above it.")
    if comment:
        print(f"{path.name}: comment without a reply was skipped: {comment!r}")
    if not pairs:
        print(f"Skipping {path.name}: need at least one `comment:` / `reply:` pair.")
    return pairs


def load_examples(*, page_key: str = config.DEFAULT_PAGE_KEY) -> list[tuple[str, str]]:
    """Load operator-written comment/reply pairs from page_key's persona_dir.

    Each file may contain one or many pairs. Gemini uses the full list as
    few-shot guidance. Falls back to config.REPLY_EXAMPLES_DIR for an
    unrecognized page_key.
    """
    page = config.PAGES.get(page_key)
    folder = page.persona_dir if page else config.REPLY_EXAMPLES_DIR
    if not folder.is_dir():
        return []

    examples: list[tuple[str, str]] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        if path.name.startswith("_"):
            continue  # templates / notes
        examples.extend(_parse_examples(path))
    return examples


def format_for_prompt(
    examples: list[tuple[str, str]], *, page_key: str = config.DEFAULT_PAGE_KEY
) -> str:
    if not examples:
        return ""
    intro = (
        "Match the style of these operator-written examples only when compatible "
        "with the persona and mandatory reply style. "
        "Use the closest relevant example as a guide. "
        "Never add generic thanks for watching, commenting, supporting, or "
        "sharing love/devotion."
    )
    if page_key == config.DEFAULT_PAGE_KEY:
        # Hindolroad-specific language policy -- also stated in generate.py's
        # per-page style instruction, repeated here for extra emphasis in
        # the examples section of the prompt. Not appropriate for a page
        # with a different persona/language, so kept default-page-only.
        intro += " Use only Odia or only English in each reply, never Hindi or mixed scripts."
    lines = [intro, "", "Examples:"]
    for comment, reply in examples:
        lines.append(f'Comment: "{comment}"')
        lines.append(f'Reply: "{reply}"')
        lines.append("")
    return "\n".join(lines).rstrip()
