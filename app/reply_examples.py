from pathlib import Path

from app import config

_ALLOWED_SUFFIXES = {".txt", ".md"}

# Gemini may only append new chant types to these files.
CHANT_LEARN_FILES = frozenset(
    {
        "chant-folded-hands.txt",
        "chant-har-har-mahadev.txt",
        "chant-new-types.txt",
    }
)
DEFAULT_CHANT_LEARN_FILE = "chant-new-types.txt"


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


def load_examples() -> list[tuple[str, str]]:
    """Load operator-written comment/reply pairs from REPLY_EXAMPLES_DIR.

    Each file may contain one or many pairs. Gemini uses the full list as
    few-shot guidance.
    """
    folder = config.REPLY_EXAMPLES_DIR
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


def format_for_prompt(examples: list[tuple[str, str]]) -> str:
    if not examples:
        return ""
    lines = [
        "Match the style of these operator-written examples. "
        "Use the closest example as a guide. For greetings and chants, "
        "stay as short as the examples (often 🙏 or 2–3 words). "
        "Do not invent a long thank-you if the examples do not.",
        "",
        "Examples:",
    ]
    for comment, reply in examples:
        lines.append(f'Comment: "{comment}"')
        lines.append(f'Reply: "{reply}"')
        lines.append("")
    return "\n".join(lines).rstrip()


def listed_comments() -> set[str]:
    return {comment.casefold() for comment, _reply in load_examples()}


def append_chant_example(filename: str, comment: str, reply: str) -> bool:
    """Append a new chant pair if it is not already in the example list.

    Returns True when a pair was written.
    """
    comment = comment.strip()
    reply = reply.strip()
    if not comment or not reply:
        return False
    if comment.casefold() in listed_comments():
        return False

    name = Path(filename).name
    if name not in CHANT_LEARN_FILES:
        name = DEFAULT_CHANT_LEARN_FILE

    folder = config.REPLY_EXAMPLES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    prefix = "" if not path.exists() or path.read_text(encoding="utf-8").endswith("\n") else "\n"
    if not path.exists():
        prefix = (
            "# New chant/greeting types Gemini added when they were not already listed.\n"
            "# Review and edit these anytime.\n"
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{prefix}comment: {comment}\nreply: {reply}\n\n")
    print(f"Added new chant example to {name}: {comment!r} -> {reply!r}")
    return True
