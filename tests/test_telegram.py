import unittest
from datetime import datetime, timezone, timedelta
from app.telegram_client import TelegramClient

class MockTelegramClient(TelegramClient):
    def __init__(self):
        super().__init__("mock_test_token_123")
        self.sent_messages = []
        self.answered_callbacks = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, parse_mode=None):
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        })
        return {"message_id": len(self.sent_messages)}

    async def answer_callback_query(self, callback_query_id: str, text=None):
        self.answered_callbacks.append(callback_query_id)
        return True

class TestTelegramClient(unittest.IsolatedAsyncioTestCase):
    def test_client_configuration(self):
        empty_client = TelegramClient("")
        self.assertFalse(empty_client.is_configured)
        configured_client = TelegramClient("123456:ABC-DEF")
        self.assertTrue(configured_client.is_configured)
        self.assertEqual(configured_client.base_url, "https://api.telegram.org/bot123456:ABC-DEF")

    async def test_send_language_menu(self):
        client = MockTelegramClient()
        await client.send_language_menu("chat_123")
        self.assertEqual(len(client.sent_messages), 1)
        sent = client.sent_messages[0]
        self.assertEqual(sent["chat_id"], "chat_123")
        self.assertIn("inline_keyboard", sent["reply_markup"])
        buttons = sent["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], "lang_en")
        self.assertEqual(buttons[1]["callback_data"], "lang_hi")

    async def test_answer_callback_query(self):
        client = MockTelegramClient()
        success = await client.answer_callback_query("cb_test_99")
        self.assertTrue(success)
        self.assertIn("cb_test_99", client.answered_callbacks)

    def test_inactivity_timeout_calculation(self):
        timeout_seconds = 240
        last_active = datetime.now(timezone.utc) - timedelta(seconds=245)
        now = datetime.now(timezone.utc)
        is_expired = (now - last_active).total_seconds() > timeout_seconds
        self.assertTrue(is_expired)

        recent_active = datetime.now(timezone.utc) - timedelta(seconds=60)
        is_still_active = (now - recent_active).total_seconds() <= timeout_seconds
        self.assertTrue(is_still_active)

    def test_json_safe_datetime(self):
        import json
        from app.agent import _make_json_safe
        data = {
            "time": datetime(2026, 10, 31, 15, 30, 0, tzinfo=timezone.utc),
            "nested": {"nested_time": datetime(2026, 9, 20, 10, 0, 0)},
            "list": [datetime(2026, 9, 21, 12, 0, 0)],
            "normal": "value",
            "number": 123
        }
        safe_data = _make_json_safe(data)
        # Verify it serializes with standard json.dumps without error
        serialized = json.dumps(safe_data)
        self.assertIn("2026-10-31T15:30:00+00:00", serialized)
        self.assertIn("2026-09-20T10:00:00", serialized)

    def test_is_calculation_query(self):
        from app.agent import agent_orchestrator
        self.assertTrue(agent_orchestrator.is_calculation_query("show me the calculation"))
        self.assertTrue(agent_orchestrator.is_calculation_query("calculation batao"))
        self.assertTrue(agent_orchestrator.is_calculation_query("mere kal ke paise pure nhi ayye hai, kya aap mereko hisaab ek aur sahi bata sakte hai"))
        self.assertTrue(agent_orchestrator.is_calculation_query("show calculation"))
        self.assertTrue(agent_orchestrator.is_calculation_query("formula kya hai"))
        self.assertFalse(agent_orchestrator.is_calculation_query("hello bhai"))

    def test_telegram_reference_date_default(self):
        from app.config import settings
        self.assertEqual(settings.TELEGRAM_REFERENCE_DATE, "2026-09-21T18:00:00+05:30")

    def test_date_range_and_relative_parsing(self):
        from datetime import date
        from app.agent import agent_orchestrator
        ref_d = date(2026, 9, 21)

        # 1. Date range (16 sep se 20 sep tak)
        e1 = agent_orchestrator.parse_entities("mera 16 sep se 20 sep tak ka payment calculation can you explain", ref_d)
        self.assertTrue(e1["is_range_query"])
        self.assertEqual(e1["date_range"], (date(2026, 9, 16), date(2026, 9, 20)))

        # 2. Relative: ek din pehle (1 day before 21 Sep -> 20 Sep)
        e2 = agent_orchestrator.parse_entities("ek din pehle ka hisaab batao", ref_d)
        self.assertEqual(e2["dates"], [date(2026, 9, 20)])

        # 3. Relative: do din pehle (2 days before 21 Sep -> 19 Sep)
        e3 = agent_orchestrator.parse_entities("do din pehle ka calculation batao", ref_d)
        self.assertEqual(e3["dates"], [date(2026, 9, 19)])

        # 4. Kal (1 day before 21 Sep -> 20 Sep)
        e4 = agent_orchestrator.parse_entities("kal ka hisab batane ke liye", ref_d)
        self.assertEqual(e4["dates"], [date(2026, 9, 20)])

        # 5. Duration: 7 din ka hisaab (range 14 Sep to 21 Sep)
        e5 = agent_orchestrator.parse_entities("mere 7 din ka hisaab bta dijiyega pls", ref_d)
        self.assertTrue(e5["is_range_query"])
        self.assertEqual(e5["date_range"], (date(2026, 9, 14), date(2026, 9, 21)))

if __name__ == "__main__":
    unittest.main()
