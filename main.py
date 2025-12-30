import asyncio
import logging
import os
from typing import Any
from zalgolib.zalgolib import enzalgofy
import re
import aiohttp

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
    user_id = event.get("user")
    text = event.get("text")
    channel = event.get("channel")

    if not isinstance(user_id, str):
        return

    if user_id != "U08PUHSMW4V":
        return

    global lastmessage
    lastmessagetext = "" if not lastmessage else lastmessage.get("text")

    logger.info(text)

    if not lastmessage or not isinstance(
        lastmessagetext, str
    ):  # just get off my back pls linter
        lastmessage = event
        return

    lm_ts = lastmessage.get("ts")
    if not lm_ts:
        lastmessage = event
        return

    async def postnewas(newcontent: str, name: str, pfp_path: str) -> None:
        try:
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
            await user_client.chat_delete(channel=channel, ts=lm_ts)
            # invite bot to channel via user client if it's a group
            # if channel.startswith("G"):
            try:
                await user_client.conversations_invite(
                    channel=channel, users=_env("SLACK_BOT_USER_ID")
                )
            except SlackApiError as e:
                if e.response["error"] != "already_in_channel":
                    raise e
            await app.client.chat_postMessage(
                channel=channel,
                text=newcontent,
                username=name,
                icon_url="https://hc-cdn.hel1.your-objectstorage.com/s/v3/24bbfcf1d14005a8_image.png",
            )
        except SlackApiError as e:
            logger.error(f"Error postnewas message: {e.response}")

    async def update(
        newcontent: str, target_ts: str | None = None, delete_event: bool = True
    ) -> None:
        ts_to_update = target_ts or lm_ts
        if not ts_to_update:
            logger.warning("update: missing target ts")
            return
        try:
            await user_client.chat_update(
                channel=channel,
                ts=ts_to_update,
                text=newcontent,
                unfurl_links=False,
                unfurl_media=False,
            )
            logger.info("update: updated ts=%s", ts_to_update)
            if delete_event and event.get("ts"):
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

    elif text.lower() == "moleme":
        await postnewas(lastmessagetext, "The Mole", "pfp/mole.jpeg")

    elif text.lower() == "cdn that":
        # regex match last message as URL
        url_regex = re.compile(
            r"(https?://[^\s]+(?:jpg|jpeg|png|gif|bmp|webp|tiff|svg))", re.IGNORECASE
        )
        match = url_regex.search(lastmessagetext)
        # or attachments
        attachments = lastmessage.get("files", [])
        logger.info(lastmessage)

        if not match and not attachments:
            return

        img_data: bytes | None = None
        filename = "image.png"

        if match:
            url = match.group(0)
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        img_data = await resp.read()
                        filename = os.path.basename(url.split("?")[0]) or filename
        elif attachments:
            attachment = attachments[0]
            filename = attachment.get("name") or filename

            url = attachment.get("url_private_download")
            if url:
                headers = {"Authorization": f"Bearer {_env("SLACK_XOXP")}"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()

        if not img_data:
            logger.warning("cdn that: no image bytes to upload")
            return

        cdnchannel = "C0A574CSLRM"

        files_res = await user_client.files_upload_v2(
            channel=cdnchannel,
            file=img_data,
            filename=filename,
            initial_comment=f"Automated CDN upload from <#{channel}>",
        )

        # logging.info(f"wee woo: {files_res['file']}")
        if not files_res:
            return

        file_objs = files_res.get("files") or []
        if not file_objs and files_res.get("file"):
            file_objs = [files_res.get("file")]

        file_id = file_objs[0].get("id") if file_objs else None  # type: ignore
        if not file_id:
            logger.warning("cdn that: missing file id from upload response")
            return

        file_info = await user_client.files_info(file=file_id)
        file_ts = file_info.get("file", {}).get("timestamp")
        oldest = max((file_ts or 0) - 300, 0)
        if not oldest or not isinstance(oldest, (int, float)):
            return
        history = await user_client.conversations_history(
            channel=cdnchannel, limit=50, inclusive=True, oldest=str(oldest)
        )
        msgs = history.get("messages", []) if history else []
        with_file = [
            m for m in msgs if any(f.get("id") == file_id for f in m.get("files", []))
        ]
        ts = with_file[0].get("ts") if with_file else None
        logger.info(
            "cdn that: searched history (n=%d, oldest=%s) found_ts=%s",
            len(msgs),
            oldest,
            ts,
        )
        if not ts:
            return

        await asyncio.sleep(10)
        logger.info("cdn that: fetching replies for ts=%s", ts)

        replies_resp = await user_client.conversations_replies(
            channel=cdnchannel, ts=ts
        )
        # logger.info(len(replies_resp.get("messages", [])) if replies_resp else 0)
        replies = replies_resp.get("messages", []) if replies_resp else []
        found = next(
            (
                r
                for r in replies
                if "hc-cdn.hel1.your-objectstorage.com" in r.get("text", "")
            ),
            None,
        )
        if not found:
            logger.warning("cdn that: unable to locate CDN reply message")
            return

        urlmatch = re.search(
            r"<(https?://hc-cdn\.hel1\.your-objectstorage\.com[^\s|>]+)(?:\|[^>]+)?>",
            found.get("text", ""),
            re.IGNORECASE,
        )
        if not urlmatch:
            logger.warning(f"cdn that: no url found in text {found.get('text', '')}")
            return
        cdn_url = urlmatch.group(1)
        logger.info(
            "cdn that: updating command message ts=%s with %s",
            event.get("ts"),
            cdn_url,
        )
        await update(f"CDN: {cdn_url}", target_ts=event.get("ts"), delete_event=False)

    lastmessage = event


async def main() -> None:
    auth = await app.client.auth_test()
    user_id = auth.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError("auth_test did not return user_id")
    os.environ["SLACK_BOT_USER_ID"] = user_id
    logging.info(f"Running with user ID: {user_id}")

    handler: AsyncSocketModeHandler = AsyncSocketModeHandler(
        app, _env("SLACK_APP_TOKEN")
    )
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
