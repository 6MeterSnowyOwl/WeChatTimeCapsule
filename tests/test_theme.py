from __future__ import annotations

import importlib
import unittest


class ThemeContractTests(unittest.TestCase):
    @staticmethod
    def _theme():
        try:
            return importlib.import_module("wechat_exporter.gui.theme")
        except ModuleNotFoundError:
            return None

    def test_small_text_semantic_colors_meet_wcag_aa_on_their_surfaces(self):
        """Catches mapping any ordinary text role to a low-contrast border color."""
        theme = self._theme()
        self.assertIsNotNone(theme, "RC3 has no centralized semantic color module")
        colors = theme.DARK_COLORS
        cases = (
            ("primary/window", colors.TEXT_PRIMARY, colors.SURFACE_BACKGROUND),
            ("secondary/card", colors.TEXT_SECONDARY, colors.SURFACE_CARD),
            ("muted/card", colors.TEXT_MUTED, colors.SURFACE_CARD),
            ("warning/card", colors.STATUS_WARNING, colors.SURFACE_CARD),
            ("success/card", colors.STATUS_SUCCESS, colors.SURFACE_CARD),
            ("error/card", colors.STATUS_ERROR, colors.SURFACE_CARD),
        )
        for label, foreground, background in cases:
            with self.subTest(label=label):
                self.assertGreaterEqual(
                    theme.contrast_ratio(foreground, background),
                    4.5,
                    f"{label} must remain readable without relying on OS palette roles",
                )

    def test_disabled_text_remains_visible_but_is_not_used_as_secondary_text(self):
        """Catches collapsing secondary metadata into the disabled palette role."""
        theme = self._theme()
        self.assertIsNotNone(theme, "RC3 has no centralized semantic color module")
        colors = theme.DARK_COLORS
        self.assertNotEqual(colors.TEXT_SECONDARY, colors.TEXT_DISABLED)
        self.assertGreaterEqual(
            theme.contrast_ratio(colors.TEXT_DISABLED, colors.SURFACE_CARD), 3.0
        )

    def test_theme_defines_all_required_semantic_roles(self):
        """Catches a return to widget-local ad-hoc colors."""
        theme = self._theme()
        self.assertIsNotNone(theme, "RC3 has no centralized semantic color module")
        required = {
            "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_MUTED", "TEXT_DISABLED",
            "STATUS_SUCCESS", "STATUS_WARNING", "STATUS_ERROR", "STATUS_NEUTRAL",
            "SURFACE_BACKGROUND", "SURFACE_CARD", "SURFACE_ELEVATED", "BORDER",
        }
        self.assertTrue(required.issubset(theme.DARK_COLORS.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
