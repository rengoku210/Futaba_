"""
Futaba Semantic UI Targeting.

Resolves human language descriptors into concrete UI controls using Windows UI Automation (UIA):
- "third Short" -> filters video/short items, picks index 2
- "first video" -> filters video items, picks index 0
- "download button" -> filters Button controls with 'download' in accessible name
- "General channel" -> filters TreeItem / ListItem / Button with 'general'
- "search box" -> filters Edit / Document controls

Avoids brittle coordinate assumptions by resolving elements via UIA hierarchy,
control type, accessible name, bounding rectangle, and visual ordering.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("futaba.intelligence.semantic_ui")

# Number and ordinal word mappings
ORDINALS: dict[str, int] = {
    "first": 0, "1st": 0, "one": 0, "1": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
    "sixth": 5, "6th": 5, "six": 5, "6": 5,
    "seventh": 6, "7th": 6, "seven": 6, "7": 6,
    "eighth": 7, "8th": 7, "eight": 7, "8": 7,
    "ninth": 8, "9th": 8, "nine": 8, "9": 8,
    "tenth": 9, "10th": 9, "ten": 9, "10": 9,
    "last": -1,
}

# Control type keywords mapping to UIA control types
ROLE_MAPPINGS: dict[str, set[str]] = {
    "button": {"Button", "SplitButton", "MenuItem"},
    "link": {"Hyperlink", "Text", "Button"},
    "video": {"Hyperlink", "ListItem", "Custom", "Button", "Image", "Pane"},
    "short": {"Hyperlink", "ListItem", "Custom", "Button", "Image", "Pane"},
    "shorts": {"Hyperlink", "ListItem", "Custom", "Button", "Image", "Pane"},
    "channel": {"TreeItem", "ListItem", "Button", "Hyperlink", "Text"},
    "server": {"TreeItem", "ListItem", "Button", "Custom"},
    "tab": {"TabItem", "Button"},
    "search": {"Edit", "Document", "ComboBox"},
    "box": {"Edit", "Document", "ComboBox"},
    "input": {"Edit", "Document", "ComboBox"},
    "field": {"Edit", "Document", "ComboBox"},
    "option": {"RadioButton", "CheckBox", "ListItem", "MenuItem", "Button"},
    "checkbox": {"CheckBox"},
    "menu": {"Menu", "MenuItem", "MenuBar"},
    "item": {"ListItem", "TreeItem", "Custom", "Button"},
}


@dataclass
class SemanticUIElement:
    """A resolved concrete UI control."""
    name: str
    control_type: str
    bounds: tuple[int, int, int, int]  # (left, top, right, bottom)
    center: tuple[int, int]            # (x, y)
    ordinal: int = 0
    confidence: float = 0.8
    handle: int = 0
    raw_wrapper: Any = None

    def click(self) -> bool:
        """Click this element via native UIA or mouse."""
        if self.raw_wrapper is not None:
            try:
                # Try UIA pattern first (doesn't move physical mouse cursor)
                if hasattr(self.raw_wrapper, "invoke"):
                    self.raw_wrapper.invoke()
                    return True
            except Exception:
                pass
            try:
                if hasattr(self.raw_wrapper, "click_input"):
                    self.raw_wrapper.click_input()
                    return True
            except Exception:
                pass

        # Fallback to mouse coordinate click
        cx, cy = self.center
        if cx > 0 and cy > 0:
            try:
                import pywinauto.mouse as mouse
                mouse.click(coords=(cx, cy))
                return True
            except Exception as e:
                logger.debug("Mouse coordinate click failed: %s", e)
        return False


class SemanticUITargeter:
    """Resolves natural language queries to active Windows UIA controls."""

    def __init__(self):
        self._last_scan: list[SemanticUIElement] = []
        self._last_window: str = ""

    def parse_query(self, query: str) -> tuple[Optional[int], Optional[str], str]:
        """
        Parse natural query into: (ordinal, role, clean_text)
        Example: "third Short" -> (2, "short", "short")
        Example: "the first video" -> (0, "video", "video")
        Example: "download button" -> (None, "button", "download")
        Example: "General channel" -> (None, "channel", "general")
        """
        q = query.lower().strip()
        words = q.split()

        target_ordinal: Optional[int] = None
        target_role: Optional[str] = None
        clean_words: list[str] = []

        for w in words:
            # Check for ordinals
            if w in ORDINALS and target_ordinal is None:
                target_ordinal = ORDINALS[w]
                continue
            # Check for roles
            if w in ROLE_MAPPINGS and target_role is None:
                target_role = w
                continue
            # Filter noise
            if w in ("the", "a", "an", "on", "in", "to", "at", "click", "open", "select"):
                continue
            clean_words.append(w)

        clean_text = " ".join(clean_words).strip()
        if not clean_text and target_role:
            clean_text = target_role

        return target_ordinal, target_role, clean_text

    def find_target(
        self,
        query: str,
        window_title: str = "",
        app_name: str = "",
        timeout_seconds: float = 3.0,
    ) -> Optional[SemanticUIElement]:
        """
        Scan foreground/target window and resolve the requested element.
        """
        ordinal, role, clean_text = self.parse_query(query)
        logger.debug(
            "Semantic targeting: query='%s' -> ordinal=%s, role=%s, text='%s'",
            query, ordinal, role, clean_text
        )

        candidates = self.scan_controls(window_title=window_title, app_name=app_name)
        if not candidates:
            return None

        # Filter candidates by role if specified
        allowed_types = ROLE_MAPPINGS.get(role, set()) if role else set()
        matched: list[SemanticUIElement] = []

        for elem in candidates:
            score = 0.0
            elem_name_lower = elem.name.lower()

            # Type match
            if allowed_types:
                if elem.control_type in allowed_types:
                    score += 0.4
                else:
                    # Skip if specific role requested and type doesn't match
                    if role in ("button", "search", "checkbox", "tab") and elem.control_type not in allowed_types:
                        continue

            # Text match
            if clean_text:
                if clean_text == elem_name_lower:
                    score += 0.8
                elif clean_text in elem_name_lower:
                    score += 0.5
                elif any(word in elem_name_lower for word in clean_text.split()):
                    score += 0.3
            else:
                score += 0.2

            if score > 0.2:
                elem.confidence = score
                matched.append(elem)

        if not matched:
            # Fallback: return any element containing clean_text
            if clean_text:
                for elem in candidates:
                    if clean_text in elem.name.lower():
                        matched.append(elem)

        if not matched:
            return None

        # Sort candidates geometrically: top to bottom, then left to right
        matched.sort(key=lambda e: (e.bounds[1], e.bounds[0]))

        # Select by ordinal
        if ordinal is not None:
            if ordinal == -1 and matched:
                return matched[-1]
            if 0 <= ordinal < len(matched):
                selected = matched[ordinal]
                selected.ordinal = ordinal
                return selected
            # If ordinal is out of bounds, pick closest
            return matched[min(max(0, ordinal), len(matched) - 1)]

        # No ordinal: pick highest confidence
        matched.sort(key=lambda e: e.confidence, reverse=True)
        return matched[0]

    def scan_controls(
        self,
        window_title: str = "",
        app_name: str = "",
        max_controls: int = 150,
    ) -> list[SemanticUIElement]:
        """
        Inspect visible UIA controls in target window.
        Safe against crashes and returns bounded results.
        """
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
        except Exception as e:
            logger.debug("pywinauto unavailable for semantic scan: %s", e)
            return []

        target_win = None
        target_name = (window_title or app_name).lower().strip()

        try:
            windows = desktop.windows()
            if target_name:
                for w in windows:
                    try:
                        t = w.window_text().lower()
                        if target_name in t:
                            target_win = w
                            break
                    except Exception:
                        pass

            if not target_win:
                # Use foreground window
                try:
                    import win32gui
                    hwnd = win32gui.GetForegroundWindow()
                    for w in windows:
                        try:
                            if w.handle == hwnd:
                                target_win = w
                                break
                        except Exception:
                            pass
                except Exception:
                    pass

            if not target_win and windows:
                for w in windows:
                    try:
                        if w.is_visible() and w.window_text():
                            target_win = w
                            break
                    except Exception:
                        pass

            if not target_win:
                return []

            elements: list[SemanticUIElement] = []
            for d in target_win.descendants()[:max_controls]:
                try:
                    txt = d.window_text().strip()
                    info = d.element_info
                    ctype = getattr(info, "control_type", "") or ""
                    rect = d.rectangle()
                    # Only collect visible elements with non-zero dimensions
                    if (rect.right > rect.left) and (rect.bottom > rect.top):
                        cx = (rect.left + rect.right) // 2
                        cy = (rect.top + rect.bottom) // 2
                        elem = SemanticUIElement(
                            name=txt or f"{ctype}_{len(elements)}",
                            control_type=ctype,
                            bounds=(rect.left, rect.top, rect.right, rect.bottom),
                            center=(cx, cy),
                            handle=getattr(d, "handle", 0),
                            raw_wrapper=d,
                        )
                        elements.append(elem)
                except Exception:
                    pass

            self._last_scan = elements
            return elements

        except Exception as err:
            logger.debug("Error scanning UIA controls: %s", err)
            return []


_targeter: Optional[SemanticUITargeter] = None


def get_semantic_targeter() -> SemanticUITargeter:
    global _targeter
    if _targeter is None:
        _targeter = SemanticUITargeter()
    return _targeter
