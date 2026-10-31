"""Acceptance test for the result card layout and the README section."""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from qdelegate.ui import ANSI, Style, report_card, width  # noqa: E402

LONG_PATH = "/Users/someone/.qdelegate/logs/" + "very-long-folder-name/" * 6 + "run.txt"
LONG_STEPS = "read 18, searched 7, edited 13, commands 18, and some more text at the end of this line"


def raw_card(columns=60, merge="git -C /r merge b"):
    style = Style(sys.stdout)
    style.on = True
    with mock.patch.dict(os.environ, {"COLUMNS": str(columns)}):
        return report_card(style, 0, {"Tests": "GREEN", "Steps": LONG_STEPS, "Log": LONG_PATH},
                           "f.py | 3 ++-\n 1 file changed", ["done"], merge, "qdelegate clean /r b")


def card(columns=60):
    return ANSI.sub("", raw_card(columns))


class CardTest(unittest.TestCase):
    def box_lines(self, text):
        return [l for l in text.splitlines() if l[:1] in "╭│╰" and l]

    def test_no_line_is_wider_than_the_terminal(self):
        for columns in (50, 60, 100):
            for line in card(columns).splitlines():
                self.assertLessEqual(width(line), columns, line)

    def test_box_edges_line_up(self):
        lines = self.box_lines(card())
        self.assertEqual(len({len(l) for l in lines}), 1, lines)
        self.assertTrue(all(l.endswith("│") or l[0] != "│" for l in lines))

    def test_long_values_wrap_and_lose_nothing(self):
        joined = "".join(re.sub(r"[│\s]", "", l) for l in self.box_lines(card()))
        self.assertIn(LONG_PATH, joined)
        self.assertIn(re.sub(r"\s", "", LONG_STEPS), joined)
        self.assertNotIn("…", card())

    def test_wrapped_lines_stay_in_the_value_column(self):
        lines = self.box_lines(card())
        start = next(i for i, l in enumerate(lines) if "Log" in l)
        column = lines[start].index("/Users")
        self.assertEqual(lines[start + 1][2:column].strip(), "")

    def test_merge_and_discard_commands_start_in_the_same_column(self):
        text = card()
        merge = next(l for l in text.splitlines() if "git -C" in l)
        discard = next(l for l in text.splitlines() if "qdelegate clean" in l)
        self.assertEqual(merge.index("git -C"), discard.index("qdelegate clean"))


    def test_color_codes_stay_intact(self):
        long_merge = "git -C " + LONG_PATH + " merge b"
        for columns in (50, 100):
            text = ANSI.sub("", raw_card(columns, long_merge))
            self.assertNotIn("\x1b", text)
            self.assertNotIn("[38;5", text)
            self.assertIn(LONG_PATH.replace("/", ""), re.sub(r"[\s/]", "", text))
            for line in text.splitlines():
                self.assertLessEqual(width(line), columns, line)


class ReadmeTest(unittest.TestCase):
    def test_terminal_interface_is_documented(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## Terminal output", text)
        section = text.split("## Terminal output")[1].split("\n## ")[0]
        for word in ("NO_COLOR", "FORCE_COLOR", "--quiet", "plain"):
            self.assertIn(word, section)


if __name__ == "__main__":
    unittest.main()
