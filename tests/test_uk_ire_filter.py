"""Regression test for the --uk-ire country filter.

Sporting Life labels UK meetings "United Kingdom" before ~2017 and "England",
"Scotland", "Wales" or "Northern Ireland" afterwards. ``UK_IRE`` used to omit
"United Kingdom", so ``--uk-ire`` silently dropped every older UK meeting: for
2016-06-15 it returned only the Irish races (7) instead of all 40 UK and Irish
races, including Royal Ascot.

Repro before the fix:
    python3 sl_results.py --date 2016-06-15 --uk-ire --out /tmp/sl-old
    # only Irish races are written

After the fix the same command writes the full card, Ascot included.
"""

import unittest
from datetime import date

import sl_results


class FakeClient:
    """Minimal stand-in for ``Client``: no network, one page of meetings."""

    def __init__(self, meetings):
        self.meetings = meetings

    def page_props(self, path):
        return {"meetings": self.meetings}


def meeting(country, race_ids):
    return {
        "meeting_summary": {
            "course": {"country": {"long_name": country}},
            "course_name": f"{country} course",
        },
        "races": [
            {
                "race_summary_reference": {"id": race_id},
                "name": f"Race {race_id}",
                "date": "2016-06-15",
            }
            for race_id in race_ids
        ],
    }


class UkIreFilterTest(unittest.TestCase):
    def test_united_kingdom_meetings_are_kept(self):
        client = FakeClient(
            [
                meeting("United Kingdom", [1, 2]),
                meeting("Ireland", [3]),
                meeting("France", [4]),
            ]
        )

        kept = [
            race["race_summary_reference"]["id"]
            for _, race in sl_results.day_races(
                client, date(2016, 6, 15), uk_ire_only=True
            )
        ]

        self.assertEqual(kept, [1, 2, 3])

    def test_filter_is_off_when_not_requested(self):
        client = FakeClient([meeting("United Kingdom", [1]), meeting("France", [4])])

        kept = [
            race["race_summary_reference"]["id"]
            for _, race in sl_results.day_races(
                client, date(2016, 6, 15), uk_ire_only=False
            )
        ]

        self.assertEqual(kept, [1, 4])


if __name__ == "__main__":
    unittest.main()
