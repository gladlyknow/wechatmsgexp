from __future__ import annotations

import os
import struct
import tempfile
import unittest

from Cryptodome.Cipher import AES

from wx4.export import _message_body_html, csv_safe, export_html, export_markdown
from wx4.media import SIG_V2, dat_layout, decode_dat


class V2MediaTests(unittest.TestCase):
    def test_pkcs7_adds_full_block_when_aes_size_is_aligned(self):
        key = b"0123456789abcdef"
        plain = b"\xff\xd8\xff" + b"A" * 13
        cipher = AES.new(key, AES.MODE_ECB).encrypt(plain + bytes([16]) * 16)
        payload = SIG_V2 + struct.pack("<LLB", len(plain), 0, 0) + cipher
        layout = dat_layout(payload)
        self.assertIsNotNone(layout)
        self.assertEqual(len(layout[0]), 32)
        self.assertEqual(decode_dat(payload, key, 0), plain)


class HtmlMediaTests(unittest.TestCase):
    def test_csv_formula_prefix_is_neutralized(self):
        self.assertEqual(csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csv_safe("  @cmd"), "'  @cmd")
        self.assertEqual(csv_safe("普通文本"), "普通文本")

    @staticmethod
    def _message(**overrides):
        message = {
            "kind": "voice", "type": 34, "text": "[语音]", "content": "",
            "extra": {}, "is_sender": True, "sender": "我", "sender_wxid": "wxid_me",
            "time_str": "2026-10-16 12:00:00", "session": "wxid_friend",
        }
        message.update(overrides)
        return message

    def test_export_uses_media_enriched_messages_without_refetch(self):
        class FakeData:
            @staticmethod
            def messages(_session):
                raise AssertionError("导出器不应重新读取消息")

            @staticmethod
            def session_display(_session):
                return "测试会话"

            @staticmethod
            def avatar_of(_username):
                return ""

        with tempfile.TemporaryDirectory() as temp:
            audio = os.path.join(temp, "voice.mp3")
            with open(audio, "wb") as stream:
                stream.write(b"test")
            out = os.path.join(temp, "chat.html")
            msg = self._message(extra={"voice_file": audio})
            export_html(FakeData(), "wxid_friend", out, media_base=temp, msgs=[msg])
            with open(out, encoding="utf-8") as stream:
                page = stream.read()
            self.assertIn("<audio class='media-audio' controls", page)
            self.assertIn("src='voice.mp3'", page)

            markdown = os.path.join(temp, "chat.md")
            export_markdown(FakeData(), "wxid_friend", markdown,
                            media_base=temp, msgs=[msg])
            with open(markdown, encoding="utf-8") as stream:
                md = stream.read()
            self.assertIn('<audio controls preload="metadata" src="voice.mp3">', md)

    def test_links_allow_only_http_and_add_safe_click_attributes(self):
        valid = self._message(
            kind="link", text="[链接]", extra={"url": "https://example.com/a?q=1",
                                                  "title": "Example", "description": "desc"})
        rendered = _message_body_html(valid, None)
        self.assertIn("class='linkcard'", rendered)
        self.assertIn("target='_blank'", rendered)
        self.assertIn("rel='noopener noreferrer'", rendered)

        invalid = self._message(
            kind="link", text="[链接]", extra={"url": "javascript:alert(1)",
                                                  "title": "bad"})
        rendered = _message_body_html(invalid, None)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("class='linkcard'", rendered)

    def test_quote_is_structured_and_escaped(self):
        message = self._message(
            kind="quote", text="reply", extra={"reply_text": "回复 <b>",
                                                "refer_name": "Alice",
                                                "refer_text": "<script>x</script>"})
        rendered = _message_body_html(message, None)
        self.assertIn("<blockquote class='quote'>", rendered)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)


if __name__ == "__main__":
    unittest.main()
