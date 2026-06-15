import asyncio
import base64
import json
import logging
import os
import time
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

from latex import gen_latex_png

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
OWNER_USER_ID = os.getenv("OWNER", "U08PUHSMW4V")
logging.info(f"Running for user {OWNER_USER_ID}")
NOTIF_CHANNEL_ID = _env("NOTIF_CHANNEL_ID")
RESUB_SITE_URL = os.getenv("RESUB_SITE_URL", "http://localhost:8080")
# event type user

remind_client = AsyncApp(token=_env("REMIND_BOT_TOKEN")).client

lastmessage: dict[str, Any] | None = None

REMINDERS_FILE = os.getenv("REMINDERS_FILE", "reminders.json")
reminders: dict[str, dict] = {}  # og_ts -> {users, og_ts, when, link}


def _load_reminders() -> dict[str, dict]:
    try:
        with open(REMINDERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_reminders() -> None:
    tmp = REMINDERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reminders, f)
    os.replace(tmp, REMINDERS_FILE)


AUTORESP_FILE = os.getenv("AUTORESP_FILE", "autoresponses.json")
autoresponses: dict[str, str] = {}  # trigger -> resp


def _load_autoresponses() -> dict[str, str]:
    try:
        with open(AUTORESP_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_autoresponses() -> None:
    tmp = AUTORESP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(autoresponses, f)
    os.replace(tmp, AUTORESP_FILE)


IMAGE_URL_REGEX = re.compile(
    r"(https?://[^\s]+(?:jpg|jpeg|png|gif|bmp|webp|tiff|svg))",
    re.IGNORECASE,
)
CDN_REPLY_REGEX = re.compile(
    r"<(https?://cdn\.hackclub\.com[^\s|>]+)(?:\|[^>]+)?>",
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
    cdnchannel = "C016DEDUL87"

    files_res = await user_client.files_upload_v2(
        channel=cdnchannel,
        file=img_data,
        filename=filename,
        initial_comment=f"gib cdn link - <#{source_channel}>",
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

    await asyncio.sleep(5)
    logger.info("cdn: fetching replies for ts=%s", ts)

    replies_resp = await user_client.conversations_replies(channel=cdnchannel, ts=ts)
    replies = replies_resp.get("messages", []) if replies_resp else []

    def reply_has_cdn(reply) -> bool:
        blocks = reply.get("blocks", [])
        if len(blocks) < 2:
            return False
        text_block = blocks[1].get("text", {})
        text = text_block.get("text", "")
        return "cdn.hackclub.com" in text

    found = next(
        (r for r in replies if reply_has_cdn(r)),
        None,
    )

    if not found:
        logger.warning("cdn: unable to locate CDN reply message")
        return None

    urlmatch = CDN_REPLY_REGEX.search(
        found.get("blocks", [])[1].get("text", {}).get("text", "") if found else ""
    )
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


async def handle_bangbang(next, original: str):
    transformed = original + " :bangbang:"
    await next(transformed)


unsubscribe_people = {}
handled_unsub_actions: set[str] = set()
AUTORESUB = os.getenv("AUTORESUB", "0") == "1"


RESUB_IMAGES = {
    "android": [
        "https://cdn.hackclub.com/019c2569-40d9-782f-a325-2474b95a917d/image.png",
        "https://cdn.hackclub.com/019c2569-3f71-79f5-ab70-8aa7bcde5783/image.png",
    ],
    "ios": [
        "https://cdn.hackclub.com/019c2569-406f-7fda-8741-668c5ebea6f0/image.png",
        "https://cdn.hackclub.com/019c2569-4074-76bc-9df7-46ac0d5ff30f/image.png",
    ],
    "desktop": [
        "https://cdn.hackclub.com/019c2569-7dc7-7dfd-aa58-c640df0fcfac/image.png",
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
    unsub_msg_ts = unsub_context.get("unsub_msg_ts")
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

    resub_response = await user_client.chat_postMessage(
        channel=target_channel,
        text=f'<@{unsubscribe_people.get(thread_ts)}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\nSee how: {RESUB_SITE_URL}',
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f'<@{unsubscribe_people.get(thread_ts)}> RESUBSCRIBE\n_(use "turn off notifications for replies" instead)_\n<{RESUB_SITE_URL}|See how>',
                },
            },
        ],
    )
    handled_unsub_actions.add(payload_value)
    resub_ts = resub_response.get("ts", "") if resub_response else ""
    if notif_channel and notif_ts:
        unsub_link = (
            f"https://hackclub.slack.com/archives/{target_channel}/p{unsub_msg_ts.replace('.', '')}"
            if unsub_msg_ts
            else ""
        )
        handled_link = (
            f"https://hackclub.slack.com/archives/{target_channel}/p{resub_ts.replace('.', '')}"
            if resub_ts
            else ""
        )
        await app.client.chat_update(
            channel=notif_channel,
            ts=notif_ts,
            text=f"UNSUBSCRIBE detected: {unsub_link} | handled: {handled_link}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"UNSUBSCRIBE <{unsub_link}|detected> and <{handled_link}|handled>.",
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

    if channel in ["G01DBHPLK25", "C07FL3G62LF", "C09A5ADLXKM"]:
        return

    if not isinstance(user_id, str):
        return

    for name, paren in re.findall(
        r"(?:#/?([a-z0-9_-]+)|\(([a-z0-9_-]+)\))", text or ""
    ):
        channel_name = name or paren
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://flaron.halceon.dev/channel/{channel_name}"
            ) as resp:
                j = await resp.json()
                logging.info(f"flaron: lookup for {channel_name} got {j}")

    if cid_rec := re.findall(r"C0[A-Z0-9]+", text or ""):
        for cid in cid_rec:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://flaron.halceon.dev/channel/{cid}"
                ) as resp:
                    if (r := await resp.json()).get("error"):
                        logging.info(
                            f"flaron: channel ID lookup for {cid} got {r.get("error")}!"
                        )

    thread_ts = event.get("thread_ts") or event.get("ts")
    if not thread_ts and user_id != OWNER_USER_ID:
        return

    # thread replies from not me
    if text == "UNSUBSCRIBE":
        unsubscribe_people[thread_ts] = user_id
        unsub_msg_ts = event.get("ts")
        action_value = json.dumps(
            {"channel": channel, "thread_ts": thread_ts, "unsub_msg_ts": unsub_msg_ts}
        )
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
        unsub_link = f"https://hackclub.slack.com/archives/{channel}/p{unsub_msg_ts.replace('.', '')}"
        await app.client.chat_postMessage(
            channel=NOTIF_CHANNEL_ID,
            text=f"<@{user_id}> UNSUBSCRIBED in {unsub_link}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<@{user_id}> *UNSUBSCRIBED!* <{unsub_link}|source>",
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

    # Handle both /me commands and normal "me " prefixed messages
    me_text: str | None = None
    if event.get("subtype") == "me_message":
        me_text = text
    elif isinstance(text, str) and text.startswith("me "):
        me_text = text[3:]

    if me_text is not None:
        logger.info("[] autoresp")
        # reserving this for autoresponses for now, prob should've used only this to begin with?
        if me_text.startswith("auto "):
            parts = me_text.split(" ")
            if len(parts) < 3:
                logging.warning(f"autoresponse: invalid format for auto add? {me_text}")
                return
            auto_trig, auto_resp = parts[1], " ".join(parts[2:])
            autoresponses[auto_trig] = auto_resp
            _save_autoresponses()
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
        elif me_text == "list":
            if not autoresponses:
                await user_client.chat_postEphemeral(
                    channel=channel,
                    user=user_id,
                    text="No autoresponses configured.",
                    thread_ts=event.get("thread_ts") or event.get("ts"),
                )
            else:
                lines = [f"`{trig}` -> {resp}" for trig, resp in autoresponses.items()]
                await user_client.chat_postEphemeral(
                    channel=channel,
                    user=user_id,
                    text=f"*Autoresponses ({len(autoresponses)}):*\n"
                    + "\n".join(lines),
                    thread_ts=event.get("thread_ts") or event.get("ts"),
                )
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
        elif isinstance(me_text, str) and me_text in autoresponses:
            response = autoresponses[me_text]
            await user_client.chat_delete(channel=channel, ts=event.get("ts"))
            if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
                await user_client.chat_postMessage(
                    channel=channel,
                    text=response,
                    thread_ts=event.get("thread_ts"),
                )
            else:
                await user_client.chat_postMessage(
                    channel=channel,
                    text=response,
                )
            logger.info(
                f"autoresponse: responded to trigger {me_text}",
            )

            return
        else:
            logger.warning(f"didn't hit any? {me_text}")

    if isinstance(text, str) and text.endswith("|"):
        resend = text.endswith("||")
        msg_text = text[:-2].rstrip() if resend else text[:-1].rstrip()
        channelnames = re.findall(r"#([a-z0-9_-]+)", msg_text)
        names = {}
        for c in channelnames:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://flaron.halceon.dev/channel/{c}"
                ) as resp:
                    j = await resp.json()
                    if err := j.get("error"):
                        logger.warning(f"flaron: error fetching channel {c}: {err}")
                    else:
                        names[c] = j.get("id", c)
        for c, n in names.items():
            msg_text = msg_text.replace(f"#{c}", f"<#{n}>")
        if resend:
            try:
                post_kwargs: dict[str, Any] = {
                    "channel": channel,
                    "text": msg_text,
                    "blocks": [{"type": "markdown", "text": msg_text}],
                }
                if thread_ts := event.get("thread_ts"):
                    post_kwargs["thread_ts"] = thread_ts
                await user_client.chat_postMessage(**post_kwargs)
                await user_client.chat_delete(channel=channel, ts=event.get("ts"))
                logger.info("pipe: resent message ts=%s", event.get("ts"))
            except SlackApiError as e:
                logger.error("pipe: resend failed: %s", e.response["error"])
        else:
            try:
                await user_client.chat_update(
                    channel=channel,
                    ts=event.get("ts"),
                    text=msg_text,
                    blocks=[{"type": "markdown", "text": msg_text}],
                )
                logger.info("pipe: reformatted message ts=%s", event.get("ts"))
            except SlackApiError as e:
                logger.error("pipe: update failed: %s", e.response["error"])
        lastmessage = event
        return

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
                icon_url="https://cdn.hackclub.com/019c2571-e463-79f9-8b48-1d4e56371087/image.png",
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
        context: str | None = None,
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

    async def update_md(
        newcontent: str,
        target_ts: str | None = None,
        delete_event: bool = True,
        thread_ts: str | None = None,
        context: str | None = None,
    ) -> None:
        ts_to_update = target_ts or lm_ts
        if not ts_to_update:
            logger.warning("update_md: missing target ts")
            return

        try:
            await user_client.chat_update(
                channel=channel,
                ts=ts_to_update,
                text=newcontent,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": newcontent,
                        },
                    }
                ],
            )
            logger.info("update_md: updated ts=%s", ts_to_update)
            if delete_event and event.get("ts"):
                await user_client.chat_delete(channel=channel, ts=event.get("ts"))
        except SlackApiError as e:
            logger.error(f"Error editing message (md): {e.response['error']}")

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

    # Replace trailing "!!" with :bangbang: emoji
    if isinstance(text, str) and text.endswith("!!") and text != "!!":
        new_text = text[:-2].rstrip() + " :bangbang:"
        try:
            await user_client.chat_update(
                channel=channel,
                ts=event.get("ts"),
                text=new_text,
            )
            logger.info("bangbang: updated trailing !! in ts=%s", event.get("ts"))
        except SlackApiError as e:
            logger.error("bangbang: update failed: %s", e.response["error"])
        lastmessage = event
        return

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
        if "everyone" in lower_text:
            await update(summary)
        else:
            await postephemeral(summary)

    elif isinstance(text, str) and text in ["bangbang", "!!"]:
        await update(lastmessagetext + " :bangbang:")

    elif lower_text.startswith("!remindme"):
        logging.info("remindme: begin")
        remind_time = lower_text.split("!remindme", 1)[1].strip()
        # e.g. 1d, 100d
        match = re.match(r"(\d+)([mhd])", remind_time)
        if not match or not match.groups():
            await postephemeral("remindme not valid?")
            lastmessage = event
            return
        amount, unit = match.groups()
        if not amount.isdigit() or unit not in "mhd":
            await postephemeral("???")
            lastmessage = event
            return
        amount = int(amount)
        sec = amount * 60 * (1 if unit == "m" else 60 if unit == "h" else 60 * 24)
        link = f"https://hackclub.slack.com/archives/{channel}/p{event.get('ts', '').replace('.', '')}"
        when = time.time() + sec
        formatted_time = f"<!date^{int(when)}^{{time}}|{amount}{unit}>"
        reminders[event.get("ts")] = {
            "users": [user_id],
            "og_ts": event.get("ts"),
            "when": when,
            "link": link,
            "formatted": formatted_time,
        }

        update_text = ""

        if len(lower_text.split()) > 2:
            remindtext = " ".join(lower_text.split()[2:])
            reminders[event.get("ts")]["text"] = remindtext
            update_text = f"Reminding you at {formatted_time}: `{remindtext}`"
        else:
            update_text = f"Reminding you at {formatted_time}."
        _save_reminders()

        context = f"\n\n React :check-check: to also be reminded."
        logging.info(
            await user_client.reactions_add(
                channel=channel, name="check-check", timestamp=event.get("ts")
            )
        )
        await user_client.chat_update(
            channel=channel,
            ts=event.get("ts"),
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": update_text},
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "image",
                            "image_url": "https://pbs.twimg.com/profile_images/625633822235693056/lNGUneLX_400x400.jpg",
                            "alt_text": "highly relevant cat",
                        },
                        {"type": "mrkdwn", "text": context},
                    ],
                },
            ],
        )

    elif text and text[0] == "$" and text[-1] == "$" and len(text) > 2:
        logger.info(f"rendering tex: {text[1:-1]}")
        img_bytes = gen_latex_png(text[1:-1])
        if img_bytes:
            try:
                # thread?
                if event.get("thread_ts"):
                    fr = await user_client.files_upload_v2(
                        file=img_bytes.getvalue(),
                        filename="latex.png",
                        channel=channel,
                        thread_ts=event.get("thread_ts"),
                    )
                else:
                    fr = await user_client.files_upload_v2(
                        file=img_bytes.getvalue(),
                        filename="latex.png",
                        channel=channel,
                    )
                file_id = fr.get("files", [])[0].get("id")
                logger.info(f"latex file {file_id}")

                await user_client.chat_delete(channel=channel, ts=event.get("ts"))
            except SlackApiError as e:
                logger.error(f"Error uploading LaTeX image: {e.response}")
        else:
            await postephemeral("Failed to render LaTeX?")

    lastmessage = event
    # endregion ME ONLY


rxn_reveal_cache: set[str] = set()  # mwahaha


@app.event("reaction_added")
async def handle_reaction_added(
    body: dict[str, Any], ack: AsyncAck, logger: logging.Logger
):
    await ack()

    event = body.get("event", {})
    reaction = event.get("reaction")

    og_ts = event.get("item", {}).get("ts")
    item_channel = event.get("item", {}).get("channel")

    if not og_ts:
        return

    if reaction == "private_channel":
        reacted_by_owner = event.get("user") == OWNER_USER_ID
        msg: dict[str, Any] | None = None

        if reacted_by_owner and og_ts not in rxn_reveal_cache:
            reaction_info = await user_client.reactions_get(
                channel=item_channel, timestamp=og_ts, full=True
            )
            msg = reaction_info.get("message") if reaction_info else None
            if not msg:
                return

            if msg.get("user") == OWNER_USER_ID:
                logger.info(f"private-channel, content: {msg.get('text', '')}")
                msg_text = msg.get("text", "")
                channelnames = re.findall(r"#([a-z0-9_-]+)", msg_text)
                names = {}
                for c in channelnames:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"https://flaron.halceon.dev/channel/{c}"
                        ) as resp:
                            j = await resp.json()
                            if err := j.get("error"):
                                logger.warning(
                                    f"flaron: error fetching channel {c}: {err}"
                                )
                            else:
                                names[c] = j.get("id", c)
                for c, n in names.items():
                    msg_text = msg_text.replace(f"#{c}", f"<#{n}>")
                try:
                    await user_client.chat_update(
                        channel=item_channel,
                        ts=og_ts,
                        text=msg_text,
                        blocks=[
                            {"type": "markdown", "text": msg_text},
                            # {
                            #     "type": "context",
                            #     "elements": [{"type": "plain_text", "text": "(unedited)"}],
                            # },
                        ],
                    )
                    await user_client.reactions_remove(
                        channel=item_channel, name="private_channel", timestamp=og_ts
                    )
                    logger.info("private-channel: reformatted message ts=%s", og_ts)
                except SlackApiError as e:
                    logger.error(
                        "private-channel: update failed: %s", e.response["error"]
                    )
                return  # self ends

            rxn_reveal_cache.add(og_ts)
            await user_client.chat_postEphemeral(
                channel=item_channel,
                user=OWNER_USER_ID,
                text="revealing mentioned channels!",
                thread_ts=og_ts,
            )

        if og_ts in rxn_reveal_cache or event.get("item_user") == OWNER_USER_ID:
            # I have now had a revelation
            if msg is None:
                reaction_info = await user_client.reactions_get(
                    channel=item_channel, timestamp=og_ts, full=True
                )
                msg = reaction_info.get("message") if reaction_info else None
            if not msg:
                logging.warning(
                    f"revelation couldn't find og message? ts={og_ts} channel={item_channel}"
                )
                return
            msg_text = msg.get("text", "")
            if not msg_text:
                logger.info(f"privchannel reveal: no text for {item_channel} {og_ts}")
                return
            channels_mentioned = re.findall(r"C[A-Z0-9]{6,}", msg_text)
            if not channels_mentioned:
                if reacted_by_owner:
                    await user_client.chat_postEphemeral(
                        channel=item_channel,
                        user=OWNER_USER_ID,
                        text="no channels here",
                        thread_ts=og_ts,
                    )
                return
            async with aiohttp.ClientSession() as session:

                async def fetch_name(ch_id: str) -> tuple[str, str | None]:
                    async with session.get(
                        f"https://flaron.halceon.dev/cid/{ch_id}"
                    ) as res:
                        data = await res.json()
                        name = data.get("name")
                        return ch_id, name if name and name != "unknown" else None

                results = await asyncio.gather(
                    *[fetch_name(ch_id) for ch_id in channels_mentioned]
                )
            id_to_name = {ch_id: name for ch_id, name in results if name}
            pre = (
                "what channels could they be? these ones: "
                if len(channels_mentioned) > 1
                else "what channel could that be? this one: "
            )
            reveal_text = pre + ", ".join(
                f"`#{id_to_name.get(cid, cid)}`" for cid in channels_mentioned
            )
            await user_client.chat_postEphemeral(
                channel=item_channel,
                user=event.get("user"),
                text=reveal_text,
                thread_ts=og_ts,
            )
        return

    if reaction == "private" and event.get("user") == OWNER_USER_ID:
        reaction_info = await user_client.reactions_get(
            channel=item_channel, timestamp=og_ts, full=True
        )
        msg = reaction_info.get("message") if reaction_info else None
        if not msg or msg.get("user") != OWNER_USER_ID:
            return
        logger.info(f"private, content: {msg.get('text', '')}")
        msg_text = msg.get("text", "")
        channelnames = re.findall(r"#([a-z0-9_-]+)", msg_text)
        names = {}
        for c in channelnames:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://flaron.halceon.dev/channel/{c}"
                ) as resp:
                    j = await resp.json()
                    if err := j.get("error"):
                        logger.warning(f"flaron: error fetching channel {c}: {err}")
                    else:
                        names[c] = j.get("id", c)
        for c, n in names.items():
            msg_text = msg_text.replace(f"#{c}", f"<#{n}>")

        try:
            post_kwargs: dict[str, Any] = {
                "channel": item_channel,
                "text": msg_text,
                "blocks": [{"type": "markdown", "text": msg_text}],
            }
            if thread_ts := msg.get("thread_ts"):
                post_kwargs["thread_ts"] = thread_ts
            await user_client.chat_postMessage(**post_kwargs)
            # await user_client.reactions_remove(
            #     channel=item_channel, name="private", timestamp=og_ts
            # )
            await user_client.chat_delete(channel=item_channel, ts=og_ts)
            logger.info("private: posted new message for ts=%s", og_ts)
        except SlackApiError as e:
            logger.error("private: post failed: %s", e.response["error"])
        return

    if reaction != "check-check":
        return

    if og_ts not in reminders:
        return

    r = reminders[og_ts]

    if event.get("user") in r["users"]:
        return

    r["users"].append(event.get("user"))
    _save_reminders()
    f = r.get("formatted")

    if r.get("text"):
        update_text = f"Reminding you at {f}: `{r.get("text")}`"
    else:
        update_text = f"Reminding you at {f}."

    context = f"\n\n{len(r['users']) - 1} other{'s' if len(r['users']) > 3 else ''} will also be reminded."

    await user_client.chat_update(
        channel=event.get("item", {}).get("channel"),
        ts=event.get("item", {}).get("ts"),
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": update_text},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "image",
                        "image_url": "https://pbs.twimg.com/profile_images/625633822235693056/lNGUneLX_400x400.jpg",
                        "alt_text": "highly relevant cat",
                    },
                    {"type": "mrkdwn", "text": context},
                ],
            },
        ],
    )

    # update og


async def periodic():
    while True:
        now = time.time()
        to_remove = []
        for og_ts, r in reminders.items():
            if now >= r["when"]:
                logging.info("IT'S TIME")
                for user in r["users"]:
                    await remind_client.chat_postMessage(
                        channel=user,
                        text=f"You asked me to remind you: `{r.get('text', '')}`\n{r['link']}",
                    )
                await user_client.chat_update(
                    channel=r["link"].split("/")[4],
                    ts=r["og_ts"],
                    text=f"reminder sent: `{r.get('text')}` \n\n{len(r['users'])} reminder(s) were sent.",
                )
                to_remove.append(og_ts)
        for og_ts in to_remove:
            del reminders[og_ts]
        if to_remove:
            _save_reminders()
        await asyncio.sleep(30)


async def main() -> None:
    global reminders, autoresponses
    reminders = _load_reminders()
    autoresponses = _load_autoresponses()
    logging.info("Loaded %d reminder(s) from %s", len(reminders), REMINDERS_FILE)

    auth = await app.client.auth_test()
    user_id = auth.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError("auth_test did not return user_id")
    os.environ["SLACK_BOT_USER_ID"] = user_id
    logging.info(f"Running with user ID: {user_id}")

    # this user id is the bot user

    asyncio.create_task(periodic())

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
