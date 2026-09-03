"""
jukeboxstate.py  --  PHASE 0

Generic jukebox state. Has nothing to do with Spotify, and never did.

Lifted verbatim out of spotifyhelper.py: same Redis keys, same signatures,
same behavior. The ONLY change is which file it lives in.

Why this matters: price/history/donation were trapped inside the Spotify
module. Nothing else could plug in beside Spotify while the pricing logic
lived inside it. This file is the unlock.
"""

import settings
from time import time


# ---------- pricing ----------

async def get_price(chat_id):
    """Price per track in this group. Defaults to settings.price (21 sats)."""
    rediskey = f"group:{chat_id}"
    price = settings.rds.hget(rediskey, "price")
    if price is None:
        price = settings.price
    return int(price)


async def set_price(chat_id, price):
    rediskey = f"group:{chat_id}"
    settings.rds.hset(rediskey, "price", price)


async def get_donation_fee(chat_id: int) -> int:
    rediskey = f"group:{chat_id}"
    fee = settings.rds.hget(rediskey, "donation_fee")
    if fee is None:
        fee = settings.donation_fee
    fee = int(fee)
    if fee < 0:
        fee = settings.donation_fee
    return int(fee)


async def set_donation_fee(chat_id: int, fee: int) -> None:
    rediskey = f"group:{chat_id}"
    settings.rds.hset(rediskey, "donation_fee", fee)


# ---------- history ----------

async def get_history(chat_id, maxlen):
    rediskey = f"history:{chat_id}"
    titles = []
    for i in range(0, min(maxlen, settings.rds.llen(rediskey))):
        titles.append(settings.rds.lindex(rediskey, i).decode('utf-8'))
    return titles


async def update_history(chat_id: int, title: str) -> None:
    rediskey = f"history:{chat_id}"
    currenttitle = settings.rds.lindex(rediskey, 0)

    if currenttitle is None:
        settings.rds.lpush(rediskey, title)
    else:
        currenttitle = currenttitle.decode('utf-8')
        if currenttitle != title:
            settings.rds.lpush(rediskey, title)

        if settings.rds.llen(rediskey) > 100:
            settings.rds.rpop(rediskey)

    # update last played entry
    settings.rds.hset(f"lastplayed:{chat_id}", title, int(time()))

    # PHASE 4 HOOK: this is where playlog.log_play(track) will go.
    # History is a display string; the play log needs the full Track
    # (ISRC, rights_class, source). Don't conflate them.


# ---------- chat lifecycle ----------

async def delete_chat(chat_id):
    rediskey = f"group:{chat_id}"
    settings.rds.delete(rediskey)
