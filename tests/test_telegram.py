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

if __name__ == "__main__":
    unittest.main()
