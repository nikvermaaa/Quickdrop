import unittest
from datetime import datetime, date, timezone
from app.policy_engine import (
    calculate_trip_fare,
    is_within_7_days,
    to_ist_date,
    IST,
)

class TestPolicyEngine(unittest.TestCase):
    def test_fare_calculation_base(self):
        # 2 km or less -> ₹25 base
        self.assertEqual(calculate_trip_fare(1.5, 1.0), 25)
        self.assertEqual(calculate_trip_fare(2.0, 1.0), 25)

    def test_fare_calculation_distance(self):
        # 5.0 km -> 25 + (3.0 * 6) = 25 + 18 = 43
        self.assertEqual(calculate_trip_fare(5.0, 1.0), 43)
        # 7.4 km -> 25 + (5.4 * 6) = 25 + 32.4 = 57.4 -> rounds to 57
        self.assertEqual(calculate_trip_fare(7.4, 1.0), 57)

    def test_fare_calculation_surge_and_rounding(self):
        # T926334: 6.0 km with 1.5x surge: (25 + 24) * 1.5 = 73.5 -> rounds to 74
        self.assertEqual(calculate_trip_fare(6.0, 1.5), 74)
        # 1.2x surge on 4.0 km: (25 + 12) * 1.2 = 37 * 1.2 = 44.4 -> rounds to 44
        self.assertEqual(calculate_trip_fare(4.0, 1.2), 44)

    def test_ist_date_conversion(self):
        # UTC 2026-09-19 20:00:00 -> IST 2026-09-20 01:30:00
        dt_utc = datetime(2026, 9, 19, 20, 0, 0, tzinfo=timezone.utc)
        ist_d = to_ist_date(dt_utc)
        self.assertEqual(ist_d, date(2026, 9, 20))

    def test_within_7_days_window(self):
        ref = date(2026, 9, 22)
        # 2026-09-20 is 2 days ago -> within 7 days
        self.assertTrue(is_within_7_days(date(2026, 9, 20), ref))
        # 2026-09-15 is 7 days ago -> within 7 days
        self.assertTrue(is_within_7_days(date(2026, 9, 15), ref))
        # 2026-09-13 is 9 days ago -> outside 7 days
        self.assertFalse(is_within_7_days(date(2026, 9, 13), ref))

    def test_intent_detection(self):
        from app.agent import agent_orchestrator

        # 1. Last payment queries (including typos from user screenshots)
        self.assertTrue(agent_orchestrator.is_last_payment_query("can you tell me about my last payemnt"))
        self.assertTrue(agent_orchestrator.is_last_payment_query("bhai mera last payemnt kitna tha ek baar money phir se bata dijiye"))
        self.assertTrue(agent_orchestrator.is_last_payment_query("can you tell me baout my last payment"))
        self.assertTrue(agent_orchestrator.is_last_payment_query("what was my previous payment"))
        self.assertTrue(agent_orchestrator.is_last_payment_query("mera last payout kitna aaya"))
        self.assertTrue(agent_orchestrator.is_last_payment_query("check my payment details"))
        # Should NOT trigger if reporting a dispute
        self.assertFalse(agent_orchestrator.is_last_payment_query("last payment kam aaya"))
        self.assertFalse(agent_orchestrator.is_last_payment_query("bhai payout galat aaya hai"))

        # 2. Greeting / Help
        self.assertTrue(agent_orchestrator.is_greeting_or_help("hi"))
        self.assertTrue(agent_orchestrator.is_greeting_or_help("hello"))
        self.assertTrue(agent_orchestrator.is_greeting_or_help("namaste"))
        self.assertTrue(agent_orchestrator.is_greeting_or_help("help"))
        self.assertFalse(agent_orchestrator.is_greeting_or_help("order T926334 ka surge nahi mila"))

        # 3. Policy inquiries
        is_pol, topic = agent_orchestrator.is_policy_question("daily incentive policy kya hai")
        self.assertTrue(is_pol)
        self.assertEqual(topic, "incentive")

        is_pol, topic = agent_orchestrator.is_policy_question("surge kaise calculate hota hai")
        self.assertTrue(is_pol)
        self.assertEqual(topic, "surge")

if __name__ == "__main__":
    unittest.main()
