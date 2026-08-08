"""Persistent storage for conversations.

One JSON file per conversation under conversations/. Small, human-readable, and
trivially recoverable — a call transcript is worth more than the app, so it is
kept in a format that survives the app being rewritten or uninstalled.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from src.config import ROOT

CONVERSATIONS_DIR = ROOT / "conversations"

# Longest title kept in the sidebar list, in characters.
TITLE_MAX = 60


@dataclass
class Conversation:
    id: str
    created_at: str
    updated_at: str
    context: str = ""
    exchanges: list[dict] = field(default_factory=list)

    @property
    def title(self) -> str:
        """First question asked, which is what makes a call recognisable later."""
        for exchange in self.exchanges:
            question = (exchange.get("question") or "").strip()
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

    def matches(self, needle: str) -> bool:
        """Search across every question, answer and translation in the call."""
        if not needle:
            return True
        needle = needle.lower()
        if needle in self.title.lower():
            return True
        for exchange in self.exchanges:
            blob = " ".join(
                str(exchange.get(key, "")) for key in ("question", "answer", "translation")
            )
            if needle in blob.lower():
                return True
        return False


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
