"""Persistent storage for conversations.

One JSON file per conversation under conversations/. Small, human-readable, and
trivially recoverable — a call transcript is worth more than the app, so it is
kept in a format that survives the app being rewritten or uninstalled.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from src import config

# Rebound rather than imported by value so tests can point the store elsewhere.
CONVERSATIONS_DIR = config.CONVERSATIONS_DIR

# Longest title kept in the sidebar list, in characters.
TITLE_MAX = 60


@dataclass
class Conversation:
    id: str
    created_at: str
    updated_at: str
    context: str = ""
    exchanges: list[dict] = field(default_factory=list)
    # Set by the user; empty means "derive it from the first question".
    custom_title: str = ""
    mode: str = "assistant"  # "assistant" | "translator"
    preset_id: str = ""  # "" for Translator sessions and pre-multi-mode files

    @property
    def title(self) -> str:
        """What the call is called: the user's own name for it if they gave one,
        otherwise the first question, which is what makes it recognisable.

        Translator exchanges (spec's resolved question #2) use "heard_text"
        instead of "question" - checked second so an Assistant exchange's
        "question" key always wins when both happen to be present.
        """
        if self.custom_title.strip():
            return self.custom_title.strip()
        for exchange in self.exchanges:
            question = (
                exchange.get("question") or exchange.get("heard_text") or ""
            ).strip()
            if question:
                return question[:TITLE_MAX] + ("…" if len(question) > TITLE_MAX else "")
        return "Conversación vacía"

    @property
    def when(self) -> str:
        """Short human date for the sidebar, e.g. '07 ago 21:32'."""
        try:
            stamp = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return ""
        months = ("ene", "feb", "mar", "abr", "may", "jun",
                  "jul", "ago", "sep", "oct", "nov", "dic")
        return f"{stamp.day:02d} {months[stamp.month - 1]} {stamp:%H:%M}"

    # Every field either exchange shape may carry, searched together:
    # Assistant's (question/answer/translation) and Translator's
    # (heard_text/translated_text/detected_source_language/target_language) -
    # spec's resolved question #2 gives Translator its own exchange shape,
    # reusing this same per-file JSON storage.
    _SEARCHABLE_FIELDS = (
        "question", "answer", "translation",
        "heard_text", "translated_text",
        "detected_source_language", "target_language",
    )

    def matches(self, needle: str) -> bool:
        """Search across every field either exchange shape carries."""
        if not needle:
            return True
        needle = needle.lower()
        if needle in self.title.lower():
            return True
        for exchange in self.exchanges:
            blob = " ".join(
                str(exchange.get(key, "")) for key in self._SEARCHABLE_FIELDS
            )
            if needle in blob.lower():
                return True
        return False


def translator_exchange(
    heard_text: str,
    detected_source_language: str,
    translated_text: str,
    target_language: str,
    timestamp: str | None = None,
) -> dict:
    """Build one Translator-mode exchange dict (spec's resolved question #2:
    "Translator session as a Conversation" - no question/answer fields).

    Pure and side-effect free (task 7.6/7.7) so chat_app's `_on_result` can
    build the dict without duplicating the shape, and so the shape itself is
    testable without Qt or a live worker.
    """
    return {
        "heard_text": heard_text,
        "detected_source_language": detected_source_language,
        "translated_text": translated_text,
        "target_language": target_language,
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
    }


def _path(conv_id: str):
    return CONVERSATIONS_DIR / f"{conv_id}.json"


# Last id handed out, so a repeated clock reading cannot produce a duplicate.
_last_id = ""


def new_conversation(context: str = "") -> Conversation:
    global _last_id
    now = datetime.now()
    # Microseconds, plus a collision guard: two conversations sharing an id would
    # silently overwrite each other's transcript on the next save.
    base = now.strftime("%Y%m%d-%H%M%S-%f")
    conv_id, suffix = base, 0
    while conv_id == _last_id or _path(conv_id).exists():
        suffix += 1
        conv_id = f"{base}-{suffix}"
    _last_id = conv_id
    stamp = now.isoformat(timespec="microseconds")
    return Conversation(id=conv_id, created_at=stamp, updated_at=stamp, context=context)


def save(conversation: Conversation) -> bool:
    """Write the conversation to disk. Empty ones are not persisted."""
    if not conversation.exchanges:
        return False
    conversation.updated_at = datetime.now().isoformat(timespec="microseconds")
    try:
        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        _path(conversation.id).write_text(
            json.dumps(asdict(conversation), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def load(conv_id: str) -> Conversation | None:
    try:
        raw = json.loads(_path(conv_id).read_text(encoding="utf-8"))
        return Conversation(
            id=raw["id"],
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            context=raw.get("context", ""),
            exchanges=raw.get("exchanges", []),
            custom_title=raw.get("custom_title", ""),
            mode=raw.get("mode", "assistant"),
            preset_id=raw.get("preset_id", ""),
        )
    except (OSError, ValueError, KeyError):
        return None


def list_all() -> list[Conversation]:
    """Every stored conversation, most recently updated first.

    Transcripts are small, so they are read whole: it keeps search able to look
    inside the calls instead of only at their titles.
    """
    if not CONVERSATIONS_DIR.exists():
        return []
    found = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        conversation = load(path.stem)
        if conversation is not None:
            found.append(conversation)
    # Id breaks ties: without it, two calls saved in the same instant would come
    # back in whatever order the filesystem happened to list them.
    found.sort(key=lambda c: (c.updated_at, c.id), reverse=True)
    return found


def delete(conv_id: str) -> None:
    try:
        _path(conv_id).unlink()
    except OSError:
        pass
