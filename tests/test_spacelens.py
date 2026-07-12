from __future__ import annotations

import unittest

from SpaceLens_SOTA import (
    CleanupSuggestion,
    format_size,
    parse_size_to_bytes,
    truncate_middle,
    unique_cleanup_suggestions,
)


class HelperTests(unittest.TestCase):
    def test_size_helpers(self) -> None:
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(parse_size_to_bytes("1.5", "GB"), int(1.5 * 1024**3))

    def test_truncate_middle_respects_the_limit(self) -> None:
        result = truncate_middle("a" * 120, 25)
        self.assertEqual(len(result), 25)
        self.assertIn("...", result)

    def test_cleanup_suggestions_are_unique_and_largest_first(self) -> None:
        suggestions = [
            CleanupSuggestion("old huge file", 500, 0, "/same", "file"),
            CleanupSuggestion("large archive", 500, 0, "/same", "file"),
            CleanupSuggestion("large video", 900, 0, "/video", "file"),
        ]

        result = unique_cleanup_suggestions(suggestions)

        self.assertEqual([item.path for item in result], ["/video", "/same"])
        self.assertEqual(sum(item.size for item in result), 1400)


if __name__ == "__main__":
    unittest.main()
