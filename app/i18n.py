# -*- coding: utf-8 -*-
"""jquery.i18n-compatible message renderer, replicating official undercards.net.

Verified by executing the official stack under node (jquery.i18n.js +
jquery.i18n.parser.js + jquery.i18n.emitter.js + the live translation.js
emitters + live en.json).  Semantics that are replicated exactly:

* ``$1`` substitution happens first; then emitters evaluate innermost-first
  (the official parser builds an AST and emits depth-first; a recursive
  tree evaluation is equivalent because emitters receive already-emitted
  strings).
* StatHelper ({{HP}}/{{ATK}}/{{GOLD}}/{{COST}}/{{DMG}}): with no number only
  the colored word is emitted (the "number = 1" initialization is dead code);
  with a number, the number itself is NOT colored, only the stat word.
* {{STATS:a|b}} colors every numeric run ``[\\d.,]+`` inside each node; with 2
  nodes the classes are atk-color then hp-color, with 3 nodes cost/atk/hp.
  Signs (+/-) are not colored.
* {{CARD:id|qty}} re-renders ``card-name-<id>`` (PLURAL with qty) and colors
  plain parts cyan (PATIENCE); it is not underlined.
* {{KW}}/{{ARTIFACT}}/{{ENCHANT}}/{{QUEST}}/{{TRIBE}}: white + underlined;
  ``override=...`` args replace the displayed text.
* {{SOUL}}/{{RARITY}}/{{KR}}/{{UCP}}/{{STYLE}} color words via the site's own
  color classes (see CLASS_COLORS below, copied from live style.css).
* {{PLURAL:$1|a|b}} on 'en': explicit ``N=...`` forms win, otherwise
  ``count == 1 ? forms[0] : forms[1]`` (even with more forms).
* {{IMAGE:name|alt|w|h|cardId}} is a genuine inline image (segments carry
  w/h so both HTML preview and the Pillow compositor draw it).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

__all__ = ["Seg", "I18n", "CLASS_COLORS", "class_color", "load_en_source"]

# ---------------------------------------------------------------------------
# Official color classes (copied from live style.css / cards.css)
# ---------------------------------------------------------------------------
CLASS_COLORS: Dict[str, str] = {
    "cost-color": "#00d0ff",
    "atk-color": "#f0003c",
    "hp-color": "#0dd000",
    "dmg-color": "#ffcc00",
    "yellow": "#ffd700",   # css "gold"
    "ucp": "#1bffb8",
    "DETERMINATION": "#ff0000",
    "PATIENCE": "#41fcff",
    "BRAVERY": "#fca500",
    "INTEGRITY": "#0064ff",
    "PERSEVERANCE": "#d535d9",
    "KINDNESS": "#00c000",
    "JUSTICE": "#ffff00",
    "MONSTER": "#ffffff",
    "COMMON": "#ffffff",
    "RARE": "#00b8ff",
    "EPIC": "#d535d9",
    "LEGENDARY": "#ffd700",
    "BASE": "#808080",
    "TOKEN": "#00c800",
    "red": "#ff0000",
    "blue": "#0000ff",
    "gray": "#808080",
    "grey": "#808080",
    "gold": "#ffd700",
    "green": "#00c000",
    "purple": "#8833ff",
    "orange": "#ff8000",
    "tropical": "#00ffb8",
}


def _color(cls: Optional[str]) -> Optional[str]:
    if not cls:
        return None
    return CLASS_COLORS.get(cls) or CLASS_COLORS.get(cls.upper())


@dataclass
class Seg:
    """One inline run; image=None means text."""

    text: str
    color: Optional[str] = None
    underline: bool = False
    image: Optional[Tuple[str, str, int, int]] = None   # (src, alt, w, h)
    url: Optional[str] = None                          # html preview only
    css: Optional[str] = None                          # html preview only

    def clone(self) -> "Seg":
        return Seg(self.text, self.color, self.underline,
                   self.image, self.url, self.css)

    def html(self) -> str:
        if self.image is not None:
            src, alt, w, h = self.image
            style = "vertical-align:middle;width:%dpx;height:%dpx;" % (w, h)
            return '<img class="inserted-img" alt="%s" style="%s">' % (_esc(alt), style)
        style = []
        if self.color:
            style.append("color:%s" % self.color)
        if self.underline:
            style.append("text-decoration:underline")
        st = (' style="%s"' % ";".join(style)) if style else ""
        cls = (' class="%s"' % self.css) if self.css else ""
        return "<span%s%s>%s</span>" % (cls, st, _esc(self.text))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


Node = Union[str, Tuple[str, str, list]]   # text | ("tmpl", name, args)


def load_en_source(path: str) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class I18n:
    """Message store + faithful {{...}} emitter evaluation."""

    _DOLLAR = re.compile(r"\$(\d+)")
    _NUM = re.compile(r"[\d\.,]+")

    def __init__(self, messages: Dict[str, str],
                 image_resolver: Optional[callable] = None) -> None:
        self.messages = messages
        self.image_resolver = image_resolver  # (src, w, h) -> local path | None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def get(self, key: str) -> Optional[str]:
        return self.messages.get(key)

    def has_key(self, key: str) -> bool:
        return key in self.messages

    def render(self, key: str, *args) -> List[Seg]:
        message = self.messages.get(key)
        if message is None:
            # official fallback: a missing message renders as the key itself
            return [Seg(key)]
        return self._eval_message(message, list(args))

    def plain(self, key: str, *args) -> str:
        return "".join(s.text for s in self.render(key, *args))

    def html(self, key: str, *args) -> str:
        return "".join(s.html() for s in self.render(key, *args))

    # ------------------------------------------------------------------
    # pipeline
    # ------------------------------------------------------------------
    def _eval_message(self, message: str, parameters: List[object]) -> List[Seg]:
        message = self._substitute(message, parameters)
        tree = self._parse(message)
        out: List[Seg] = []
        self._emit(tree, out)
        return self._merge(out)

    def _substitute(self, message: str, parameters: List[object]) -> str:
        def repl(match: "re.Match[str]") -> str:
            idx = int(match.group(1)) - 1
            if 0 <= idx < len(parameters) and parameters[idx] is not None:
                return str(parameters[idx])
            return "$" + match.group(1)
        return self._DOLLAR.sub(repl, message)

    # ------------------------------------------------------------------
    # parser: message -> tree of text / ("tmpl", name, [arg]) where each arg
    # is itself a tree. Mirrors jquery.i18n's MessageParser.ast().
    # ------------------------------------------------------------------
    def _parse(self, message: str) -> List[Node]:
        nodes: List[Node] = []
        buf: List[str] = []
        i = 0
        n = len(message)
        while i < n:
            if message.startswith("{{", i):
                end = self._find_template_end(message, i)
                if end is None:
                    buf.append(message[i])
                    i += 1
                    continue
                buf.append("")           # flush marker handled below
                if buf and any(buf):
                    nodes.append("".join(buf))
                buf = []
                inner = message[i + 2: end - 2]
                nodes.append(self._parse_template(inner))
                i = end
            else:
                buf.append(message[i])
                i += 1
        if any(buf):
            nodes.append("".join(buf))
        return nodes

    @staticmethod
    def _find_template_end(message: str, start: int) -> Optional[int]:
        """Index just past the matching '}}' or None if unbalanced."""
        depth = 0
        i = start
        n = len(message)
        while i < n:
            if message.startswith("{{", i):
                depth += 1
                i += 2
            elif message.startswith("}}", i):
                depth -= 1
                i += 2
                if depth == 0:
                    return i
            else:
                i += 1
        return None

    def _parse_template(self, inner: str) -> Node:
        # split top-level '|' respecting nested {{}}
        pieces = self._split_top(inner, "|")
        if ":" in pieces[0]:
            name, first_arg = pieces[0].split(":", 1)
            arg_strings = [first_arg] + pieces[1:]
        else:
            name = pieces[0]
            arg_strings = pieces[1:]
        # note: the official parser splits name from args at the first
        # top-level ':'; remaining '|' parts are further args.
        # {{ENCHANT:GREEN_TILE||override=X}} -> args ['', '', 'override=X'].
        arg_nodes: List[Node] = [self._parse(a) for a in arg_strings]
        return ("tmpl", name.lower(), arg_nodes)

    def _split_top(self, text: str, sep: str) -> List[str]:
        parts: List[str] = []
        depth = 0
        cur: List[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if text.startswith("{{", i):
                depth += 1
                cur.append("{{")
                i += 2
                continue
            if text.startswith("}}", i):
                depth -= 1
                cur.append("}}")
                i += 2
                continue
            if ch == sep and depth == 0:
                parts.append("".join(cur))
                cur = []
                i += 1
                continue
            cur.append(ch)
            i += 1
        parts.append("".join(cur))
        return parts

    # ------------------------------------------------------------------
    # emit: walk tree producing Segs
    # ------------------------------------------------------------------
    def _emit(self, nodes: List[Node], out: List[Seg]) -> None:
        for node in nodes:
            if isinstance(node, str):
                if node:
                    out.append(Seg(node))
            else:
                _, name, arg_nodes = node
                args: List[List[Seg]] = []
                for arg_tree in arg_nodes:
                    buf: List[Seg] = []
                    self._emit(arg_tree, buf)
                    args.append(self._merge(buf))
                handler = getattr(self, "em_" + name, None)
                if handler is not None:
                    handler(args, out)
                else:
                    # unknown emitter: emit its literal textual form
                    raw = name + ":" + "|".join(" ".join(s.text for s in arg) for arg in args)
                    raw = raw.rstrip("|: ")
                    out.append(Seg("{{%s}}" % raw) if raw else Seg(""))

    # ------------------------------------------------------------------
    # segment helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _args_text(args: List[List[Seg]]) -> List[str]:
        return ["".join(s.text for s in arg) for arg in args]

    @staticmethod
    def _color_inherit(segs: List[Seg], color: str, underline: bool | None = None) -> List[Seg]:
        res = []
        for seg in segs:
            if seg.image is None:
                seg = seg.clone()
                if seg.color is None:
                    seg.color = color
                if underline is not None:
                    seg.underline = underline
            res.append(seg)
        return res

    def _kw(self, args: List[List[Seg]], keyword: str, cls_prefix: str, out: List[Seg]) -> None:
        override = self._override_arg(args)
        if override is not None:
            text = override
        else:
            key = "%s-%s" % (cls_prefix, keyword.replace("_", "-").lower())
            text = self.plain_key(key)
        out.append(Seg(text, color=None, underline=True))

    @staticmethod
    def _override_arg(args: List[List[Seg]]) -> Optional[str]:
        for arg in args:
            joined = "".join(s.text for s in arg)
            if joined.startswith("override="):
                return joined.split("=", 1)[1]
        return None

    def plain_key(self, key: str, default: Optional[str] = None, *args) -> str:
        """Official fallback: a missing key renders as the key name itself."""
        message = self.messages.get(key)
        if message is None:
            return default if default is not None else key
        return "".join(s.text for s in self._eval_message(message, list(args)))

    def _stat(self, args: List[List[Seg]], key: str, color_class: str,
              out: List[Seg]) -> None:
        color = CLASS_COLORS.get(color_class)
        arg_text = self._args_text(args)
        number = arg_text[0] if args else ""
        if number:
            out.append(Seg(number))
            out.append(Seg(" "))
        out.append(Seg(self.plain_key(key), color=color))

    def _merge(self, segs: List[Seg]) -> List[Seg]:
        merged: List[Seg] = []
        for seg in segs:
            if (seg.text == "" and seg.image is None):
                continue
            if (merged and merged[-1].image is None and seg.image is None
                    and merged[-1].color == seg.color
                    and merged[-1].underline == seg.underline):
                merged[-1].text = merged[-1].text + seg.text
            else:
                merged.append(seg)
        return merged

    # ------------------------------------------------------------------
    # official emitters
    # args: list of (arg -> List[Seg])
    # ------------------------------------------------------------------
    def em_card(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        try:
            card_id = int("".join(c for c in arg_text[0] if c.isdigit() or c == "-"))
        except ValueError:
            return
        quantity = arg_text[1] if len(args) > 1 else "1"
        segs = self.render("card-name-%d" % card_id, quantity)
        out.extend(self._color_inherit(segs, color=CLASS_COLORS["PATIENCE"]))

    def em_kw(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        self._kw(args, arg_text[0], "kw", out)

    def em_enchant(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        keyword = arg_text[0]
        override = self._override_arg(args)
        if override is not None:
            text = override
        else:
            quantity = arg_text[1] if len(args) > 1 and arg_text[1] else "1"
            text = self.plain_key("enchant-%s" % keyword.replace("_", "-").lower(), None, quantity)
        out.append(Seg(text, color=None, underline=True))

    def em_quest(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        try:
            quest_id = int(arg_text[0])
        except ValueError:
            return
        override = self._override_arg(args)
        text = override if override is not None else self.plain_key("artifact-name-%d" % quest_id)
        out.append(Seg(text, color=None, underline=True))

    def em_artifact(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        override = self._override_arg(args)
        text = override if override is not None else self.plain_key("artifact-name-%s" % arg_text[0])
        out.append(Seg(text, color=None, underline=True))

    def em_soul(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if not args:
            return
        soul = arg_text[0]
        override = self._override_arg(args)
        text = override if override is not None else self.plain_key(
            "soul-%s" % soul.replace("_", "-").lower())
        out.append(Seg(text, color=CLASS_COLORS.get(soul.upper())))

    def em_hp(self, args, out: List[Seg]) -> None:
        self._stat(args, "stat-hp", "hp-color", out)

    def em_atk(self, args, out: List[Seg]) -> None:
        self._stat(args, "stat-atk", "atk-color", out)

    def em_gold(self, args, out: List[Seg]) -> None:
        self._stat(args, "stat-gold", "yellow", out)

    def em_cost(self, args, out: List[Seg]) -> None:
        self._stat(args, "stat-cost", "cost-color", out)

    def em_dmg(self, args, out: List[Seg]) -> None:
        self._stat(args, "stat-dmg", "dmg-color", out)

    def em_kr(self, args, out: List[Seg]) -> None:
        override = self._override_arg(args)
        text = override if override is not None else self.plain_key("stat-kr")
        out.append(Seg(text, color=CLASS_COLORS["PERSEVERANCE"]))

    def em_plural(self, args, out: List[Seg]) -> None:
        if not args:
            return
        arg_text = self._args_text(args)
        try:
            count = float(arg_text[0])
        except ValueError:
            count = None
        forms = arg_text[1:] if count is not None else []
        if count is None:
            return
        # explicit "N=..." forms first (like convertPlural)
        explicit_forms = []
        filtered: List[str] = []
        for form in forms:
            match = re.match(r"^(\d+)=(.*)$", form)
            if match:
                if int(match.group(1)) == count:
                    out.append(Seg(match.group(2)))
                    return
                explicit_forms.append((int(match.group(1)), match.group(2)))
            else:
                filtered.append(form)
        if not filtered:
            out.append(Seg(""))
            return
        text = None
        if hasattr(self, "_plural") and self._plural(count, filtered) is not None:
            text = self._plural(count, filtered)
        if text is None:
            text = filtered[0] if count == 1 else filtered[-1]
        out.append(Seg(text))

    def _plural(self, count: float, forms: List[str]) -> Optional[str]:
        if not forms:
            return ""
        if len(forms) == 1:
            return forms[0]
        # 'en' has no pluralRules entry in jquery.i18n's table -> fallback
        return forms[0] if count == 1 else forms[1]

    def em_stats(self, args, out: List[Seg]) -> None:
        if len(args) < 2 or len(args) > 3:
            out.append(Seg("ERROR!"))
            return
        classes = ["cost-color", "atk-color", "hp-color"]
        arg_text = self._args_text(args)
        for i, node in enumerate(arg_text):
            cls = classes[i + (len(classes) - len(arg_text))]
            color = CLASS_COLORS[cls]
            pos = 0
            for match in self._NUM.finditer(node):
                if match.start() > pos:
                    out.append(Seg(node[pos:match.start()]))
                out.append(Seg(match.group(0), color=color))
                pos = match.end()
            rest = node[pos:]
            if rest:
                out.append(Seg(rest))
            if i < len(arg_text) - 1:
                out.append(Seg("/"))

    def em_tribe(self, args, out: List[Seg]) -> None:
        if not args:
            return
        arg_text = self._args_text(args)
        tribe = arg_text[0]
        override = self._override_arg(args)
        if override is not None:
            text = override
        else:
            key = "tribe-%s" % tribe.replace("_", "-").lower()
            quantity = arg_text[1] if len(args) > 1 and arg_text[1] else "1"
            text = self.plain_key(key, None, quantity)
        out.append(Seg(text, color=None, underline=True))

    def em_rarity(self, args, out: List[Seg]) -> None:
        if not args:
            return
        arg_text = self._args_text(args)
        rarity = arg_text[0]
        override = self._override_arg(args)
        text = override if override is not None else self.plain_key(
            "rarity-%s" % rarity.replace("_", "-").lower())
        segs = [Seg(text, color=_color(rarity))]
        if len(args) > 1 and not override:
            src = "/images/rarity/%s_%s.png" % (arg_text[1], rarity)
            segs.append(Seg("", image=(src, rarity, 24, 24)))
        out.extend(segs)

    def em_mode(self, args, out: List[Seg]) -> None:
        if not args:
            return
        name = self._args_text(args)[0]
        self._plain_keys_em(out, "game-type-%s" % name.replace("_", "-").lower())

    def _plain_keys_em(self, out: List[Seg], key: str) -> None:
        text = self.plain_key(key, key)
        out.append(Seg(text))

    def em_cosmetic(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if len(args) == 2:
            out.append(Seg(self.plain_key("reward-%s" % arg_text[0]) + " - " + arg_text[1]))
        return

    def em_division(self, args, out: List[Seg]) -> None:
        if not args:
            return
        self._plain_keys_em(out, "division-%s" % self._args_text(args)[0].replace("_", "-").lower())

    def em_ucp(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if args:
            out.append(Seg(arg_text[0], color=CLASS_COLORS["ucp"]))

    def em_style(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if len(args) == 2:
            cls = arg_text[0]
            color = _color(cls)
            segs = args[1]
            if color is not None:
                segs = self._color_inherit(segs, color=color)
            out.extend(segs)

    def em_switch_left(self, args, out: List[Seg]) -> None:
        self._switch(out, args, "switch_left")

    def em_switch_right(self, args, out: List[Seg]) -> None:
        self._switch(out, args, "switch_right")

    def _switch(self, out: List[Seg], args, name: str) -> None:
        if not args:
            return
        opacity = 1
        arg_text = self._args_text(args)
        try:
            opacity = float(arg_text[0])
        except ValueError:
            opacity = 1
        if opacity <= 0:
            return
        segs = args[1] if len(args) > 1 else []
        out.extend(segs)

    def em_image(self, args, out: List[Seg]) -> None:
        arg_text = self._args_text(args)
        if len(args) >= 2:
            src = "/images/inserted/%s.png" % arg_text[0]
            alt = arg_text[1]
            width = 64
            height = 16
            if len(args) >= 3 and arg_text[2]:
                try:
                    width = int(arg_text[2])
                except ValueError:
                    width = 64
            if len(args) >= 4 and arg_text[3]:
                try:
                    height = int(arg_text[3])
                except ValueError:
                    height = 16
            if self.image_resolver is not None:
                src_real = self.image_resolver(src, width, height) or src
            else:
                src_real = src
            out.append(Seg(alt, image=(src_real, alt, width, height)))
