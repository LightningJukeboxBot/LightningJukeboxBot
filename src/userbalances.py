import userhelper
import settings
import json
import redis
import asyncio
from userhelper import User

settings.init()

total = 0
for userkey in settings.rds.scan_iter("user:*"):
    userdata = settings.rds.hget(userkey,"userdata")

    if userdata is not None:
        userdata = json.loads(userdata)

        tgusername = userdata['telegram_username']
        tguserid = userdata['telegram_userid']
        lnbits_userid = userdata['lnbits_userid']
        
        user = asyncio.run(userhelper.get_or_create_user(tguserid, tgusername))
        balance = asyncio.run(userhelper.get_balance(user))
        print(user.username, tguserid, lnbits_userid, balance)
        total += balance
    else:
        print("got a none user")
print(total)
