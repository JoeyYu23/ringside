"""The playbook: the only source of words the coach may show during a call.

A line is eligible only when every slot it needs is a known fact (from the account record or
from what the prospect said in this call). Nothing is ever guessed to fill a slot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PLAYBOOK_PATH = Path(__file__).with_name("playbook.yaml")
SLOT_RE = re.compile(r"\{([a-z_0-9]+)\}")


@dataclass(frozen=True)
class Line:
    id: str
    situation: str
    text: str
    requires: tuple[str, ...]
    when: str          # reflex | genuine | any
    kind: str          # say | stop | ask | info
    role: str          # gatekeeper | dm | any
    label: str

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(SLOT_RE.findall(self.text)))


class Playbook:
    def __init__(self, path: Path | str = PLAYBOOK_PATH) -> None:
        raw = yaml.safe_load(Path(path).read_text())
        self.version = raw.get("version")
        self.slot_names: list[str] = list(raw.get("slots", []))
        self.situations: dict[str, dict] = {}
        self.lines: dict[str, Line] = {}
        self.by_situation: dict[str, list[Line]] = {}
        for sit, spec in raw["situations"].items():
            self.situations[sit] = {"label": spec.get("label", sit), "role": spec.get("role", "any"), "kind": spec.get("kind", "say")}
            lines = []
            for entry in spec["lines"]:
                line = Line(id=entry["id"], situation=sit, text=entry["text"], requires=tuple(entry.get("requires", [])),
                            when=entry.get("when", "any"), kind=entry.get("kind", spec.get("kind", "say")),
                            role=spec.get("role", "any"), label=spec.get("label", sit))
                if line.id in self.lines:
                    raise ValueError(f"duplicate line id {line.id}")
                self.lines[line.id] = line
                lines.append(line)
            self.by_situation[sit] = lines

    # ---- rendering ---------------------------------------------------------------
    def render(self, line: Line, facts: dict) -> str | None:
        """Fill the slots from facts; None if any slot the text uses is unknown."""
        vals = {}
        for slot in line.slots:
            v = facts.get(slot)
            if not v:
                return None
            vals[slot] = str(v)
        return line.text.format_map(vals)

    def choose(self, situation: str, facts: dict, when: str = "any", exclude: set[str] | frozenset[str] = frozenset(),
               used: set[str] | frozenset[str] = frozenset()) -> tuple[Line, str] | None:
        """Most specific eligible line for the situation. Prefers lines not yet used in this call and
        lines matching `when` (reflex/genuine); falls back to any-when lines, never to guessed slots."""
        cands = self.by_situation.get(situation, [])

        def eligible(pool):
            out = []
            for ln in pool:
                if ln.id in exclude:
                    continue
                if not all(facts.get(s) for s in ln.requires):
                    continue
                text = self.render(ln, facts)
                if text is None:
                    continue
                out.append((ln, text))
            out.sort(key=lambda lt: (lt[0].id in used, -len(lt[0].requires)))
            return out

        exact = eligible([ln for ln in cands if ln.when == when or ln.when == "any" or when == "any"])
        if exact:
            return exact[0]
        rest = eligible(cands)
        return rest[0] if rest else None

    def is_rendered_line(self, text: str, facts: dict) -> str | None:
        """Return the line id if `text` is exactly one of our lines rendered with these facts (the audit hook)."""
        for ln in self.lines.values():
            if self.render(ln, facts) == text:
                return ln.id
        return None

    def label(self, situation: str) -> str:
        return self.situations.get(situation, {}).get("label", situation)


_DEFAULT: Playbook | None = None


def load() -> Playbook:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Playbook()
    return _DEFAULT
