import userhelper
import settings
import json
import redis
import asyncio
from userhelper import User

settings.init()

users = settings.rds.scan_iter("user:*")

for userkey in users:
    userdata = settings.rds.hget(userkey,"userdata")

    if userdata is not None:
        userdata = json.loads(userdata)

        tgusername = userdata['telegram_username']
        tguserid = userdata['telegram_userid']

        print(tgusername)
        settings.rds.hdel(userkey,"userdata")
        user = asyncio.run(userhelper.get_or_create_user(tguserid, tgusername))
        print(user.lnurlp)
