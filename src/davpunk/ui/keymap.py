"""Key bindings: defaults, ``[davpunk.keys]`` overrides, and the ``?`` overlay.

Duplicate bindings are a **config error**, never silently last-wins — that is
enforced in :mod:`davpunk.config` at load, so by the time a :class:`Keymap`
exists the bindings are already known to be unambiguous.

**Hard requirement:** every action is reachable without a mouse.
"""

from __future__ import annotations

from dataclasses import dataclass

from davpunk.config import DEFAULT_KEYS, KEY_CONTEXTS, DavPunkConfig, normalize_binding


def is_chord(binding: str) -> bool:
    """Is this a multi-keystroke sequence rather than one Qt shortcut?

    A chord is comma-*separated* (``g,g``).  A binding whose key simply *is* a
    comma (``Ctrl+,``, the conventional Preferences accelerator) splits into an
    empty second part, and treating it as a chord silently unbinds it.
    """
    parts = binding.split(",")
    return len(parts) > 1 and all(part.strip() for part in parts)


@dataclass(frozen=True)
class Action:
    name: str
    binding: str
    context: str
    label: str

    @property
    def is_chord(self) -> bool:
        """``gg`` and ``dd`` are two-keystroke sequences, not Qt shortcuts."""
        return is_chord(self.binding)


#: Human labels for the overlay, grouped the way the HLD documents them.
LABELS: dict[str, str] = {
    "down": "Down",
    "up": "Up",
    "top": "Jump to top",
    "bottom": "Jump to bottom",
    "search": "Search",
    "clear": "Clear search / close",
    "view_list": "List view",
    "view_kanban": "Kanban view",
    "view_search": "Search view",
    "sync_now": "Sync now",
    "help_overlay": "This overlay",
    "new_task": "New task",
    "new_subtask": "New subtask of the selection",
    "open_editor": "Open editor",
    "toggle_complete": "Toggle complete",
    "delete_task": "Delete task",
    "move_task": "Move to another list",
    "cut_task": "Cut (to reparent by pasting)",
    "paste_task": "Paste under the selection",
    "inline_rename": "Inline rename",
    "indent": "Indent (make a subtask)",
    "outdent": "Outdent (promote to root)",
    "reorder_down": "Move down among siblings",
    "reorder_up": "Move up among siblings",
    "set_priority": "Set priority",
    "set_tags": "Set tags",
    "set_due": "Set due date",
    "expand_all": "Unfold every subtree",
    "collapse_all": "Fold every subtree",
    "card_prev_column": "Card to previous column",
    "card_next_column": "Card to next column",
    "focus_prev_column": "Focus previous column",
    "focus_next_column": "Focus next column",
    "take_local": "Take my value into the result",
    "take_server": "Take the server value into the result",
    "take_all_local": "Take all mine",
    "take_all_server": "Take all server",
    "save_resolution": "Accept the result",
    "skip_resolution": "Skip — resolve at a later sync",
}

CONTEXT_TITLES = {
    "global": "Global",
    "kanban": "Kanban",
    "conflict": "Merge window",
}


class Keymap:
    def __init__(self, config: DavPunkConfig | None = None) -> None:
        self._bindings = (config or DavPunkConfig()).keymap()

    def __getitem__(self, action: str) -> str:
        return self._bindings[action]

    def get(self, action: str, default: str | None = None) -> str | None:
        return self._bindings.get(action, default)

    def actions(self) -> list[Action]:
        return [
            Action(
                name=name,
                binding=binding,
                context=KEY_CONTEXTS.get(name, "global"),
                label=LABELS.get(name, name.replace("_", " ")),
            )
            for name, binding in self._bindings.items()
        ]

    def by_context(self) -> dict[str, list[Action]]:
        grouped: dict[str, list[Action]] = {}
        for action in self.actions():
            grouped.setdefault(action.context, []).append(action)
        return grouped

    def action_for(self, binding: str, context: str = "global") -> str | None:
        """Reverse lookup, used by the chord handler."""
        wanted = normalize_binding(binding)
        for action in self.actions():
            if action.context == context and normalize_binding(action.binding) == wanted:
                return action.name
        return None

    def chords(self, context: str = "global") -> dict[str, str]:
        """``{"g,g": "top"}`` — sequences Qt cannot express as one shortcut."""
        return {
            action.binding: action.name
            for action in self.actions()
            if action.is_chord and action.context == context
        }

    def qt_shortcuts(self, context: str = "global") -> dict[str, str]:
        """``{binding: action}`` for everything Qt *can* bind directly."""
        return {
            action.binding: action.name
            for action in self.actions()
            if not action.is_chord and action.context == context
        }

    def overlay_text(self) -> str:
        """The ``?`` overlay, as plain text so it is testable without Qt."""
        lines: list[str] = []
        for context in ("global", "kanban", "conflict"):
            actions = sorted(
                (a for a in self.actions() if a.context == context), key=lambda a: a.label
            )
            if not actions:
                continue
            lines.append(CONTEXT_TITLES.get(context, context))
            width = max(len(a.binding) for a in actions)
            lines.extend(f"  {a.binding:<{width}}  {a.label}" for a in actions)
            lines.append("")
        return "\n".join(lines).rstrip()


def defaults() -> dict[str, str]:
    return dict(DEFAULT_KEYS)
