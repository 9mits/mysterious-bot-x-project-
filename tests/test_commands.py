import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import types as _types
from cogs.shared import (
    is_staff,
    respond_with_error,
    validate_image_fetch_url,
    _resolve_image_host_addresses,
    fetch_image_bytes,
    bot,
    prepare_modmail_relay_attachments,
    send_modmail_thread_intro,
    SCOPE_MODERATION,
)
from cogs.roles import RevokeAppealView, ConfirmRevokeView, DenyAppealModal
from cogs.automod import apply_automod_report_response

# Build a legacy namespace that mirrors modules.commands
legacy = _types.SimpleNamespace(
    is_staff=is_staff,
    respond_with_error=respond_with_error,
    validate_image_fetch_url=validate_image_fetch_url,
    _resolve_image_host_addresses=_resolve_image_host_addresses,
    fetch_image_bytes=fetch_image_bytes,
    bot=bot,
    prepare_modmail_relay_attachments=prepare_modmail_relay_attachments,
    send_modmail_thread_intro=send_modmail_thread_intro,
    SCOPE_MODERATION=SCOPE_MODERATION,
    RevokeAppealView=RevokeAppealView,
    ConfirmRevokeView=ConfirmRevokeView,
    DenyAppealModal=DenyAppealModal,
    apply_automod_report_response=apply_automod_report_response,
)


def make_interaction():
    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        defer=AsyncMock(),
        is_done=Mock(return_value=False),
    )
    followup = SimpleNamespace(send=AsyncMock())
    return SimpleNamespace(
        response=response,
        followup=followup,
        user=SimpleNamespace(id=42, mention="<@42>", display_name="Moderator", roles=[], guild_permissions=SimpleNamespace(moderate_members=False)),
        guild=SimpleNamespace(name="Guild", icon=None),
        message=SimpleNamespace(embeds=[]),
        client=SimpleNamespace(fetch_user=AsyncMock()),
    )


class FakeContent:
    def __init__(self, chunks=None):
        self._chunks = chunks or []

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, *, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def get(self, *_args, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class FakeAttachment:
    def __init__(self, filename, size):
        self.filename = filename
        self.size = size
        self.calls = 0

    async def to_file(self):
        self.calls += 1
        return self.filename


import cogs.roles as _cogs_roles
import cogs.automod as _cogs_automod
import cogs.shared as _cogs_shared


class MbxLegacyAuthTests(unittest.IsolatedAsyncioTestCase):
    def assert_denied_embed(self, interaction):
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.await_args
        self.assertTrue(kwargs.get("ephemeral"))
        self.assertIn("embed", kwargs)

    async def test_revoke_appeal_entrypoint_rejects_non_staff(self):
        interaction = make_interaction()
        view = legacy.RevokeAppealView(target_id=1, moderator_id=2, duration=0, timestamp="2026-01-01T00:00:00+00:00")

        with patch.object(_cogs_roles, "is_staff", return_value=False):
            await view.children[0].callback(interaction)

        self.assert_denied_embed(interaction)

    async def test_confirm_revoke_view_rejects_non_staff(self):
        interaction = make_interaction()
        parent_view = SimpleNamespace(finish_revoke=AsyncMock())
        view = legacy.ConfirmRevokeView(parent_view, SimpleNamespace())

        with patch.object(_cogs_roles, "is_staff", return_value=False):
            await view.children[0].callback(interaction)

        self.assert_denied_embed(interaction)
        parent_view.finish_revoke.assert_not_awaited()

    async def test_deny_appeal_modal_rejects_non_staff(self):
        interaction = make_interaction()
        modal = legacy.DenyAppealModal(
            target_id=1,
            origin_message=SimpleNamespace(embeds=[SimpleNamespace()]),
            view=SimpleNamespace(children=[]),
        )

        with patch.object(_cogs_roles, "is_staff", return_value=False):
            await modal.on_submit(interaction)

        self.assert_denied_embed(interaction)

    async def test_finish_revoke_rejects_non_staff(self):
        interaction = make_interaction()
        view = legacy.RevokeAppealView(target_id=1, moderator_id=2, duration=0, timestamp="2026-01-01T00:00:00+00:00")

        with patch.object(_cogs_roles, "is_staff", return_value=False):
            await view.finish_revoke(interaction, SimpleNamespace(embeds=[SimpleNamespace()]))

        self.assert_denied_embed(interaction)

    async def test_apply_automod_report_response_rejects_non_staff(self):
        interaction = make_interaction()

        with patch.object(_cogs_automod, "is_staff", return_value=False), patch.object(_cogs_automod, "respond_with_error", AsyncMock()) as mock_error:
            success = await legacy.apply_automod_report_response(
                interaction,
                guild_id=1,
                reporter_id=2,
                warning_id="warn-1",
                rule_name="Rule",
                response_key="acknowledge",
                response_text="Thanks",
                source_message=None,
            )

        self.assertFalse(success)
        mock_error.assert_awaited_once_with(interaction, "Access denied.", scope=legacy.SCOPE_MODERATION)


class MbxLegacyFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_image_fetch_url_rejects_non_https(self):
        _, error = await legacy.validate_image_fetch_url("http://example.com/image.png")
        self.assertEqual(error, "Image URLs must use HTTPS.")

    async def test_validate_image_fetch_url_rejects_credentials(self):
        _, error = await legacy.validate_image_fetch_url("https://user:pass@example.com/image.png")
        self.assertEqual(error, "Image URLs with embedded credentials are not allowed.")

    async def test_validate_image_fetch_url_rejects_private_host(self):
        with patch.object(_cogs_shared, "_resolve_image_host_addresses", AsyncMock(return_value=(["127.0.0.1"], None))):
            _, error = await legacy.validate_image_fetch_url("https://localhost/image.png")

        self.assertEqual(error, "Image URLs must use a public host.")

    async def test_fetch_image_bytes_rejects_redirects(self):
        session = FakeSession(FakeResponse(302))

        with patch.object(_cogs_shared, "validate_image_fetch_url", AsyncMock(return_value=("https://cdn.example/image.png", None))), patch.object(
            _cogs_shared,
            "bot",
            SimpleNamespace(session=session),
        ):
            payload, error = await legacy.fetch_image_bytes("https://cdn.example/image.png")

        self.assertIsNone(payload)
        self.assertEqual(error, "Image URLs cannot redirect.")
        self.assertFalse(session.last_kwargs["allow_redirects"])


class MbxLegacyModmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_modmail_relay_attachments_skips_oversized_and_extra_files(self):
        mib = 1024 * 1024
        attachments = [
            FakeAttachment("keep-1.png", mib),
            FakeAttachment("too-big.png", 9 * mib),
            FakeAttachment("keep-2.png", mib),
            FakeAttachment("keep-3.png", mib),
            FakeAttachment("keep-4.png", mib),
            FakeAttachment("keep-5.png", mib),
            FakeAttachment("extra.png", mib),
        ]

        files, notice = await legacy.prepare_modmail_relay_attachments(attachments)

        self.assertEqual(files, ["keep-1.png", "keep-2.png", "keep-3.png", "keep-4.png", "keep-5.png"])
        self.assertIn("first 5", notice)
        self.assertIn("over 8 MiB", notice)
        self.assertEqual(attachments[1].calls, 0)
        self.assertEqual(attachments[-1].calls, 0)

    async def test_prepare_modmail_relay_attachments_enforces_total_size_limit(self):
        mib = 1024 * 1024
        attachments = [
            FakeAttachment("keep-1.png", 8 * mib),
            FakeAttachment("keep-2.png", 8 * mib),
            FakeAttachment("skip-total.png", 5 * mib),
        ]

        files, notice = await legacy.prepare_modmail_relay_attachments(attachments)

        self.assertEqual(files, ["keep-1.png", "keep-2.png"])
        self.assertIn("20 MiB total", notice)
        self.assertEqual(attachments[-1].calls, 0)

    async def test_send_modmail_thread_intro_disables_mentions(self):
        thread = SimpleNamespace(send=AsyncMock())
        user = SimpleNamespace(mention="<@123>")

        await legacy.send_modmail_thread_intro(thread, user, "Report", ["**Subject**: @everyone"])

        allowed_mentions = thread.send.await_args.kwargs["allowed_mentions"]
        self.assertIsInstance(allowed_mentions, discord.AllowedMentions)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.users)


if __name__ == "__main__":
    unittest.main()
