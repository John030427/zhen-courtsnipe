"""Table tennis and indoor basketball: venues, court names, lookups and slots. Offline."""
import unittest
from types import SimpleNamespace

from core.sports import (ALL_SPORTS, SPORT_BASKETBALL, SPORT_TABLE_TENNIS, is_sport, load_other_venue,
                         normalize_sport_id, public_sports_capabilities)
from engine import Engine


class SportTableTests(unittest.TestCase):
    def test_new_sports_are_known(self):
        self.assertEqual(ALL_SPORTS, ("badminton", "tennis", "table_tennis", "indoor_basketball"))
        for sid in ALL_SPORTS:
            self.assertEqual(normalize_sport_id(sid), sid)
            self.assertTrue(is_sport(sid))
            self.assertTrue(public_sports_capabilities()[sid]["rush"])
        self.assertEqual(normalize_sport_id("football"), "badminton")
        self.assertFalse(is_sport("football"))

    def test_venues_and_school_court_names(self):
        gym, name, courts = load_other_venue({}, SPORT_TABLE_TENNIS)
        self.assertEqual((gym, name, len(courts)), ("2017132944518397954", "综合训练馆乒乓球场", 15))
        self.assertEqual([c.no for c in courts], list(range(1, 16)))
        self.assertTrue(all(c.name == f"{c.no}号场" for c in courts))
        gym, name, courts = load_other_venue({}, SPORT_BASKETBALL)
        self.assertEqual((gym, name), ("2017137857432502273", "综合训练馆室内篮球场"))
        # Halves first, the full court last; saveOrder sends the school's own names.
        self.assertEqual([c.name for c in courts], ["1号场A场", "1号场B场", "2号场（半场）", "3号场（半场）", "1号场"])
        self.assertEqual(courts[0].id, "2017139568683368450")
        self.assertEqual(courts[4].id, "2017138599727841282")
        from core.sports import court_display
        self.assertEqual([court_display(SPORT_BASKETBALL, c.no, c.name) for c in courts],
                         [("1号场A场", "1A"), ("1号场B场", "1B"), ("2号场（半场）", "2半"),
                          ("3号场（半场）", "3半"), ("1号场（全场）", "1全")])
        self.assertEqual(court_display(SPORT_TABLE_TENNIS, 12, "12号场"), ("12号场", "12号"))

    def test_config_may_override_gym_and_a_ground(self):
        data = {"sports": {"table_tennis": {"gym_id": "fixture-gym", "courts": {"3": "fixture-ground"}}}}
        gym, _, courts = load_other_venue(data, SPORT_TABLE_TENNIS)
        self.assertEqual(gym, "fixture-gym")
        self.assertEqual(courts[2].id, "fixture-ground")
        self.assertEqual(courts[2].name, "3号场")


class EngineLookupTests(unittest.TestCase):
    def engine(self):
        e = Engine.__new__(Engine)
        e.courts, e.gym_id, e.gym_name = [], "bad-gym", "羽毛球馆"
        e.tennis_gym_id, e.tennis_gym_name = "ten-gym", "欣园网球场"
        e.venues_by_sport, e.courts_by_sport = {}, {"badminton": [], "tennis": []}
        for sid in (SPORT_TABLE_TENNIS, SPORT_BASKETBALL):
            gym, name, courts = load_other_venue({}, sid)
            e.venues_by_sport[sid] = (gym, name)
            e.courts_by_sport[sid] = courts
        return e

    def test_ground_venue_and_courts_per_sport(self):
        e = self.engine()
        self.assertEqual(e.sport_for_ground_id("2017139568683368450"), SPORT_BASKETBALL)
        self.assertEqual(e.court_for_sport(SPORT_BASKETBALL, 5).name, "1号场")
        self.assertEqual(e.sport_for_ground_id("2017136625569611778"), SPORT_TABLE_TENNIS)
        self.assertEqual(e.sport_for_ground_id("unknown"), "badminton")
        self.assertEqual(e.venue_for_sport(SPORT_BASKETBALL), ("2017137857432502273", "综合训练馆室内篮球场"))
        self.assertEqual(e.venue_for_sport("tennis"), ("ten-gym", "欣园网球场"))
        self.assertEqual(e.venue_for_sport("badminton"), ("bad-gym", "羽毛球馆"))
        self.assertEqual(len(e.courts_for_sport(SPORT_TABLE_TENNIS)), 15)
        self.assertEqual(e.court_for_sport(SPORT_BASKETBALL, 3).name, "2号场（半场）")


class SlotTests(unittest.TestCase):
    def test_rush_and_watch_have_a_slot_per_sport(self):
        from core.rush_registry import SportRushRegistry
        from core.watch_registry import SportWatchRegistry
        stub = SimpleNamespace(config_data={})
        self.assertEqual(set(SportRushRegistry(stub).slots), set(ALL_SPORTS))
        self.assertEqual(set(SportWatchRegistry(stub).all_states()), set(ALL_SPORTS))


if __name__ == "__main__":
    unittest.main()
