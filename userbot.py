import asyncio
import os
from urllib.parse import quote, urlencode, unquote

import logging

import aiohttp


def _env(val: str, fallback: str | None = None) -> str:
    if fallback is None:
        if os.getenv(val) is None:
            raise Exception(f"Env {val} not set")
    return os.getenv(val, fallback)  # type: ignore


XOXC = _env("C")
XOXD = unquote(_env("D"))


def sanitize_err(err: str, sensitive: list[str] | None = None) -> str:
    if not sensitive:
        sensitive = []
    sensitive.extend([XOXC, XOXD])
    sensitive = [s for s in sensitive if s]
    for s in sensitive:
        if s in err:
            err = err.replace(s, "[redacted]")
    return err


async def req(
    path: str,
    form: dict = {},
    params: dict[str, str] = {},
    override_XOXC=None,
    override_XOXD=None,
) -> dict:
    headers = {"Cookie": f"d={quote(override_XOXD or XOXD)}", "Accept": "*/*"}
    params.update({"slack_route": (f"E09U093LUNL:E09U093LUNL")})
    url = f"https://hackclub.enterprise.slack.com/api/{path}?{urlencode(params)}"
    logging.debug(f"requesting {url} with form {form}")
    form.update({"token": quote(override_XOXC or XOXC)})
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(url, data=form) as res:
            data = await res.json()
            if not data.get("ok"):
                logging.info(
                    f"error in {path}: {data.get('error', 'this should never be seen')}"
                )

                logging.info(f"form data was: {sanitize_err(str(form))}")
                return {"error": data.get("error")}

            return data


async def delete_send_to_channel(channel: str, ts: float):
    return await req(
        "chat.delete",
        {
            "channel": channel,
            "ts": ts,
            "broadcast_delete": True,
        },
    )
