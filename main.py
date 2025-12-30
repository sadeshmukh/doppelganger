import asyncio
import logging
import os
from typing import Any
from zalgolib.zalgolib import enzalgofy
import re

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


def _env(name: str) -> str:
    """Fetch a required environment variable or fail fast."""
    if not (value := os.getenv(name)):
        raise RuntimeError(f"Missing required env var: {name}")
    return value


# Configure root logger to stdout so Slack handlers emit.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app: AsyncApp = AsyncApp(token=_env("SLACK_BOT_TOKEN"))
app.logger.setLevel(logging.INFO)
user_client: AsyncWebClient = AsyncWebClient(token=_env("SLACK_XOXP"))
# event type user


lastmessage: dict[str, Any] | None = None


async def handle_backitup(update, original: str):
    flipped_text = " ".join(original.split(" ")[::-1])
    await update(flipped_text)


# on message
@app.event("message")
async def handle_message_events(
    body: dict[str, Any], ack: AsyncAck, logger: logging.Logger
) -> None:
    await ack()

    event = body.get("event", {})
    if not event.get("user") == "U08PUHSMW4V":
        return
    text: str = event.get("text")
    channel = event.get("channel")

    global lastmessage
    lastmessagetext = "" if not lastmessage else lastmessage.get("text")

    logging.info(text)

    if not lastmessage or not isinstance(
        lastmessagetext, str
    ):  # just get off my back pls linter
        lastmessage = event
        return

    lm_ts = lastmessage.get("ts")
    if not lm_ts:
        lastmessage = event
        return

    async def postnew(newcontent: str) -> None:
        try:
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
            await user_client.chat_delete(channel=channel, ts=lm_ts)
            await user_client.chat_postMessage(channel=channel, text=newcontent)
        except SlackApiError as e:
            logger.error(f"Error updating message: {e.response['error']}")

    async def update(newcontent: str) -> None:
        try:
            await user_client.chat_update(channel=channel, ts=lm_ts, text=newcontent)
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
        except SlackApiError as e:
            logger.error(f"Error editing message: {e.response['error']}")

    if "capitalize it" in text.lower() and lastmessage:
        await update(lastmessagetext.upper())

    elif "back it up" in text.lower() and lastmessage:
        await handle_backitup(update, lastmessagetext)

    elif (m := re.match(r"zalgo \d+", text.lower())) is not None and not text.endswith(
        "\u200b"
    ):

        count = int(m.group(0).split(" ")[1])
        if not 1 <= count <= 50:
            count = 25
        await update(enzalgofy(lastmessagetext, count) + "\u200b")
    elif text.lower() == "zalgome" and not text.endswith("\u200b"):
        await update(enzalgofy(lastmessagetext, 25) + "\u200b")
    lastmessage = event


async def main() -> None:
    handler: AsyncSocketModeHandler = AsyncSocketModeHandler(
        app, _env("SLACK_APP_TOKEN")
    )
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
