import redis
from redis import RedisError
import asyncio
import settings
import json
import logging
import userhelper
from time import time

async def get_bot_stack() -> int:
    """
    Returns the amount of sats of the Jukebox Bot itself.
    """
    jukeboxbot : userhelper.User = await userhelper.get_or_create_user(settings.bot_id)
    balance : int = await userhelper.get_balance(jukeboxbot)
    return balance


async def get_jukebox_groups() -> dict:
    """
    Returns a list of Jukebox groups and some stats about them
    """
    result = {
        "numgroups": 0,
        "group": []
    }
    for key in settings.rds.scan_iter("group:*"):
        chatid : int = int(key.decode('utf-8').split(':')[1])

        result['numgroups'] += 1

        owner = None
        try:
	        owner = await userhelper.get_group_owner(chatid)
        except:
                logging.error("problem getting group stats")

        price = settings.rds.hget(key,"price")
        if price is None:
            price = settings.price
        price = int(price)
            
        donation_fee = settings.rds.hget(key,"donation_fee")
        if donation_fee is None:
            donation_fee = settings.donation_fee
        donation_fee = int(donation_fee)
            
        result["group"].append({
            "groupid": chatid,
            "owner": owner,
            "price": price,
            "donation_fee": donation_fee
        })


    return result
