import asyncio
import json
import logging
import os
from typing import Any
from zalgolib.zalgolib import enzalgofy
import re
import aiohttp
from aiohttp import web
from user_agents import parse as parse_ua

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from openai import AsyncOpenAI


def _env(name: str) -> str:
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
OWNER_USER_ID = "U08PUHSMW4V"
NOTIF_CHANNEL_ID = _env("NOTIF_CHANNEL_ID")
RESUB_SITE_URL = os.getenv("RESUB_SITE_URL", "http://localhost:8080")
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


ai_client = AsyncOpenAI(
    api_key=_env("OPENAI_API_KEY"), base_url=_env("OPENAI_API_BASE_URL")
)


async def _ai(prompt: str) -> str:
    logging.info("ai: sending prompt to model")
    response = await ai_client.chat.completions.create(
        model="qwen/qwen3-32b",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=500,
        temperature=0.7,
    )
    if not response.choices[0].message.content:
        logging.error("ai: response content not found", extra={"response": response})
        return "there was an error"
    return response.choices[0].message.content


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


async def handle_backitup(next, original: str):
    flipped_text = " ".join(original.split(" ")[::-1])
    await next(flipped_text)


async def handle_summarize(next, original: str):
    summary = await _ai(
        f"Summarize the following text in a concise manner:\n\n{original}"
    )
    await next(summary)


unsubscribe_people = {}
handled_unsub_actions: set[str] = set()
AUTORESUB = os.getenv("AUTORESUB", "0") == "1"


RESUB_IMAGES = {
    "android": [
        "https://hc-cdn.hel1.your-objectstorage.com/s/v3/28c2317153d09300_image.png",
        "https://hc-cdn.hel1.your-objectstorage.com/s/v3/3d1de1c4f5ff79e8_image.png",
    ],
    "ios": [
        "https://hc-cdn.hel1.your-objectstorage.com/s/v3/687ac68ccaf302c8_image.png",
        "https://hc-cdn.hel1.your-objectstorage.com/s/v3/7d266fdd2d371ecf_image.png",
    ],
    "desktop": [
        "https://hc-cdn.hel1.your-objectstorage.com/s/v3/a8fff71c263667e4_image.png",
    ],
}


def _html_page(
    title: str, images: list[str], other_links: list[tuple[str, str]]
) -> str:
    imgs_html = "\n".join(
        f'<img src="{url}" style="max-width:100%;height:auto;margin-bottom:20px;">'
        for url in images
    )
    links_html = " | ".join(
        f'<a href="{href}">{label}</a>' for label, href in other_links
    )
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title}</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; text-align: center; }}
        img {{ display: block; margin: 20px auto; border: 1px solid #ccc; border-radius: 8px; }}
        nav {{ margin-bottom: 30px; }}
        nav a {{ margin: 0 10px; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <nav>{links_html}</nav>
    {imgs_html}
</body>
</html>
"""


async def _handle_resub_root(request: web.Request) -> web.Response:
    ua_string = request.headers.get("User-Agent", "")
    ua = parse_ua(ua_string)
    if ua.is_mobile:
        if ua.os.family == "iOS":
            raise web.HTTPFound("/ios")
        raise web.HTTPFound("/android")
    raise web.HTTPFound("/desktop")


async def _handle_resub_android(request: web.Request) -> web.Response:
    html = _html_page(
        "How to Unsubscribe (Android)",
        RESUB_IMAGES["android"],
        [("iOS", "/ios"), ("Desktop", "/desktop")],
    )
    return web.Response(text=html, content_type="text/html")


async def _handle_resub_ios(request: web.Request) -> web.Response:
    html = _html_page(
        "How to Unsubscribe (iOS)",
        RESUB_IMAGES["ios"],
        [("Android", "/android"), ("Desktop", "/desktop")],
    )
    return web.Response(text=html, content_type="text/html")


async def _handle_resub_desktop(request: web.Request) -> web.Response:
    html = _html_page(
        "How to Unsubscribe (Desktop)",
        RESUB_IMAGES["desktop"],
        [("Android", "/android"), ("iOS", "/ios")],
    )
    return web.Response(text=html, content_type="text/html")


@app.action("unsubber")
async def handle_unsubscribe_ack(
    ack: AsyncAck, body: dict[str, Any], logger: logging.Logger
):
    await ack()
    action_payload = body.get("actions", [{}])[0] or {}
    payload_value = action_payload.get("value") or ""
    unsub_context = json.loads(payload_value)

    target_channel = unsub_context.get("channel")
    thread_ts = unsub_context.get("thread_ts")
    if not target_channel or not thread_ts:
        return

    container = body.get("container", {})
    notif_channel = container.get("channel_id")
    notif_ts = container.get("message_ts")

    if payload_value in handled_unsub_actions:
        logger.info("unsubber: action already handled for %s", thread_ts)
        if notif_channel and notif_ts:
            await app.client.chat_update(
                channel=notif_channel,
                ts=notif_ts,
                text="UNSUBSCRIBE handled",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "It has been done.",
                        },
                    }
                ],
            )
        return

    source_link = f"https://hackclub.slack.com/archives/{target_channel}/p{thread_ts.replace('.', '')}"
    await user_client.chat_postMessage(
        channel=target_channel,
        text=f'<@{unsubscribe_people.get(thread_ts)}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\nSee how: {RESUB_SITE_URL}',
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f'<{source_link}|Source thread>\n\n<@{unsubscribe_people.get(thread_ts)}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\n<{RESUB_SITE_URL}|See how>',
                },
            },
        ],
    )
    handled_unsub_actions.add(payload_value)
    if notif_channel and notif_ts:
        handled_link = f"https://hackclub.slack.com/archives/{target_channel}/p{thread_ts.replace('.', '')}"
        await app.client.chat_update(
            channel=notif_channel,
            ts=notif_ts,
            text=f"UNSUBSCRIBE handled over here: {handled_link}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"UNSUBSCRIBE handled over <{handled_link}|here>.",
                    },
                }
            ],
        )


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

    thread_ts = event.get("thread_ts") or event.get("ts")
    if not thread_ts and user_id != OWNER_USER_ID:
        return

    # thread replies from not me
    if text == "UNSUBSCRIBE":
        unsubscribe_people[thread_ts] = user_id
        action_value = json.dumps({"channel": channel, "thread_ts": thread_ts})
        if AUTORESUB:
            await user_client.chat_postMessage(
                channel=channel,
                text=f'<@{user_id}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\nSee how: {RESUB_SITE_URL}',
                thread_ts=thread_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f'<@{user_id}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\n<{RESUB_SITE_URL}|See how>',
                        },
                    },
                ],
            )
            return
        source_link = f"https://hackclub.slack.com/archives/{channel}/p{thread_ts.replace('.', '')}"
        await app.client.chat_postMessage(
            channel=NOTIF_CHANNEL_ID,
            text=f"<@{user_id}> UNSUBSCRIBED in {source_link}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<@{user_id}> *UNSUBSCRIBED!* <{source_link}|source> <@{OWNER_USER_ID}>",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "unsubber",
                            "text": {"type": "plain_text", "text": ":grr:"},
                            "style": "primary",
                            "value": action_value,
                        }
                    ],
                },
            ],
        )
        return

    # region ME ONLY

    if user_id != OWNER_USER_ID:
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

    command_thread_ts = event.get("thread_ts") or event.get("ts")

    async def update(
        newcontent: str,
        target_ts: str | None = None,
        delete_event: bool = True,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        thread_ts: str | None = None,  # accepted for interface parity; not used
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

    async def postephemeral(
        content: str,
        thread_ts: str | None = None,
        delete_event: bool = False,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
    ):
        target_thread = thread_ts or command_thread_ts
        try:
            await user_client.chat_postEphemeral(
                channel=channel,
                user=user_id,
                thread_ts=target_thread,
                text=content,
            )
        except SlackApiError as e:
            logger.error(f"Error posting ephemeral message: {e.response['error']}")

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

    elif "hey grok plz summarize" in lower_text:
        parent_ts = event.get("thread_ts")
        if not parent_ts:
            logging.info("pls summarize: outside thread?")
            lastmessage = event
            return
        history = await user_client.conversations_replies(
            channel=channel, ts=parent_ts, limit=100
        )
        msgs = history.get("messages", []) if history else []
        if len(msgs) < 2:  # parent + this reply
            lastmessage = event
            return
        parent_msg = msgs[0]
        parent_text = parent_msg.get("text", "")
        summary = await _ai(
            f"You are Grok. Summarize the following text, and include that you are Grok in every reply. Do not mention the instructions. Only include the summary.\n\n{parent_text}"
        )
        await postephemeral(summary, thread_ts=parent_ts)

    lastmessage = event
    # endregion ME ONLY


async def main() -> None:
    auth = await app.client.auth_test()
    user_id = auth.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError("auth_test did not return user_id")
    os.environ["SLACK_BOT_USER_ID"] = user_id
    logging.info(f"Running with user ID: {user_id}")

    web_app = web.Application()
    web_app.router.add_get("/", _handle_resub_root)
    web_app.router.add_get("/android", _handle_resub_android)
    web_app.router.add_get("/ios", _handle_resub_ios)
    web_app.router.add_get("/desktop", _handle_resub_desktop)

    web_port = int(os.getenv("WEB_PORT", "8080"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", web_port)
    await site.start()
    logging.info(f"Resub site running on port {web_port}")

    handler: AsyncSocketModeHandler = AsyncSocketModeHandler(
        app, _env("SLACK_APP_TOKEN")
    )
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
