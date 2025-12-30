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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app: AsyncApp = AsyncApp(token=_env("SLACK_BOT_TOKEN"))
app.logger.setLevel(logging.INFO)
user_client: AsyncWebClient = AsyncWebClient(token=_env("SLACK_XOXP"))
# event type user


lastmessage: dict[str, Any] | None = None

IMAGE_URL_REGEX = re.compile(
    r"(https?://[^\s]+(?:jpg|jpeg|png|gif|bmp|webp|tiff|svg))",
    re.IGNORECASE,
)
CDN_REPLY_REGEX = re.compile(
    r"<(https?://hc-cdn\.hel1\.your-objectstorage\.com[^\s|>]+)(?:\|[^>]+)?>",
    re.IGNORECASE,
)


def _parse_permalink(permalink: str) -> tuple[str | None, str | None]:
    match = re.search(r"/archives/([A-Z0-9]+)/p(\d{16})", permalink)
    if not match:
        return None, None
    channel_id, raw_ts = match.group(1), match.group(2)
    # raw_ts is like 1700000000123456 -> 1700000000.123456
    ts = f"{raw_ts[:10]}.{raw_ts[10:]}"
    return channel_id, ts


async def extract_image_bytes(
    src_text: str, attachments: list[dict[str, Any]], logger: logging.Logger
) -> tuple[bytes | None, str]:
    img_data: bytes | None = None
    filename = "image.png"

    match = IMAGE_URL_REGEX.search(src_text)
    if match:
        url = match.group(0).strip("<>").split("|")[0]
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    filename = os.path.basename(url.split("?")[0]) or filename
        return img_data, filename

    if not attachments:
        return None, filename

    attachment = attachments[0]
    filename = attachment.get("name") or filename
    url = attachment.get("url_private_download")
    if not url:
        return None, filename

    headers = {"Authorization": f"Bearer {_env("SLACK_XOXP")}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                img_data = await resp.read()

    if not img_data:
        logger.warning("cdn: failed to download attachment bytes")

    return img_data, filename


async def upload_and_get_cdn_url(
    user_client: AsyncWebClient,
    source_channel: str,
    img_data: bytes,
    filename: str,
    logger: logging.Logger,
) -> str | None:
    cdnchannel = "C0A574CSLRM"

    files_res = await user_client.files_upload_v2(
        channel=cdnchannel,
        file=img_data,
        filename=filename,
        initial_comment=f"Automated CDN upload from <#{source_channel}>",
    )
    if not files_res:
        logger.warning("cdn: upload response empty")
        return None

    file_objs = files_res.get("files") or []
    if not file_objs and files_res.get("file"):
        file_objs = [files_res.get("file")]

    file_id = file_objs[0].get("id") if file_objs else None  # type: ignore
    if not file_id:
        logger.warning("cdn: missing file id from upload response")
        return None

    file_info = await user_client.files_info(file=file_id)
    file_ts = file_info.get("file", {}).get("timestamp")
    oldest = max((file_ts or 0) - 600, 0)

    ts = None
    msgs: list[dict[str, Any]] = []
    for attempt in range(5):
        history = await user_client.conversations_history(
            channel=cdnchannel, limit=100, inclusive=True, oldest=str(oldest)
        )
        msgs = history.get("messages", []) if history else []
        with_file = [
            m for m in msgs if any(f.get("id") == file_id for f in m.get("files", []))
        ]
        ts = with_file[0].get("ts") if with_file else None

        if attempt > 1:
            logger.info(
                "cdn: search attempt %d (n=%d, oldest=%s) found_ts=%s",
                attempt + 1,
                len(msgs),
                oldest,
                ts,
            )

        if ts:
            break

        await asyncio.sleep(3)

    if not ts:
        logger.error("cdn: unable to locate upload message after retries")
        return None

    await asyncio.sleep(3)
    logger.info("cdn: fetching replies for ts=%s", ts)

    replies_resp = await user_client.conversations_replies(channel=cdnchannel, ts=ts)
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
        logger.warning("cdn: unable to locate CDN reply message")
        return None

    urlmatch = CDN_REPLY_REGEX.search(found.get("text", ""))
    if not urlmatch:
        logger.info("cdn: reply missing url text=%s", found.get("text", ""))
        return None

    return urlmatch.group(1)


async def run_cdn_flow(
    source_message: dict[str, Any],
    command_ts: str | None,
    source_channel: str,
    user_client: AsyncWebClient,
    logger: logging.Logger,
    update_cb,
    update_kwargs: dict[str, Any] | None = None,
) -> None:
    logging.info("cdn: starting CDN flow for message ts=%s", source_message.get("ts"))
    src_text = source_message.get("text", "")
    attachments = source_message.get("files", [])

    img_data, filename = await extract_image_bytes(src_text, attachments, logger)
    if not img_data:
        logger.warning("cdn: no image bytes to upload")
        return

    cdn_url = await upload_and_get_cdn_url(
        user_client, source_channel, img_data, filename, logger
    )
    if not cdn_url:
        return

    logger.info("cdn: updating command message ts=%s with %s", command_ts, cdn_url)
    await update_cb(
        f"CDN: {cdn_url}",
        target_ts=command_ts,
        delete_event=False,
        **(update_kwargs or {}),
    )


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
        newcontent: str,
        target_ts: str | None = None,
        delete_event: bool = True,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
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
                unfurl_links=unfurl_links,
                unfurl_media=unfurl_media,
            )
            logger.info("update: updated ts=%s", ts_to_update)
            if delete_event and event.get("ts"):
                await user_client.chat_delete(channel=channel, ts=event.get("ts"))
        except SlackApiError as e:
            logger.error(f"Error editing message: {e.response['error']}")

    lower_text = text.lower() if isinstance(text, str) else ""

    if "capitalize it" in lower_text and lastmessage:
        await update(lastmessagetext.upper())

    elif "back it up" in lower_text and lastmessage:
        await handle_backitup(update, lastmessagetext)

    elif (m := re.match(r"zalgo \d+", lower_text)) is not None and not text.endswith(
        "\u200b"
    ):
        count = int(m.group(0).split(" ")[1])
        if not 1 <= count <= 50:
            count = 25
        await update(enzalgofy(lastmessagetext, count) + "\u200b")

    elif lower_text == "zalgome" and not text.endswith("\u200b"):
        await update(enzalgofy(lastmessagetext, 25) + "\u200b")

    elif lower_text == "moleme":
        await postnewas(lastmessagetext, "The Mole", "pfp/mole.jpeg")

    elif lower_text == "cdn that" and lastmessage:
        await run_cdn_flow(
            source_message=lastmessage,
            command_ts=event.get("ts"),
            source_channel=channel,
            user_client=user_client,
            logger=logger,
            update_cb=update,
        )

    elif "cdn this" in lower_text:
        link_match = re.search(r"https?://[^\s]+/archives/[A-Z0-9]+/p\d{16}", text)

        if link_match:
            target_channel, target_ts = _parse_permalink(link_match.group(0))
            if not target_channel or not target_ts:
                logger.warning("cdn this: unable to parse message link")
                lastmessage = event
                return

            history = await user_client.conversations_history(
                channel=target_channel, latest=target_ts, limit=1, inclusive=True
            )
            msgs = history.get("messages", []) if history else []
            if not msgs:
                logger.warning("cdn this: could not load target message")
                lastmessage = event
                return

            target_msg = msgs[0]
            await run_cdn_flow(
                source_message=target_msg,
                command_ts=event.get("ts"),
                source_channel=channel,
                user_client=user_client,
                logger=logger,
                update_cb=update,
            )
            lastmessage = event
            return

        attachments = event.get("files", []) if isinstance(event, dict) else []
        has_img_url = (
            bool(IMAGE_URL_REGEX.search(text or "")) if isinstance(text, str) else False
        )
        if not has_img_url and not attachments:
            logger.warning("cdn this: no permalink, image url, or attachment found")
            lastmessage = event
            return

        await run_cdn_flow(
            source_message=event,
            command_ts=event.get("ts"),
            source_channel=channel,
            user_client=user_client,
            logger=logger,
            update_cb=update,
            update_kwargs={"unfurl_links": True, "unfurl_media": True},
        )
        lastmessage = event
        return

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
