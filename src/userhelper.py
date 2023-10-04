import redis
from redis import RedisError
import asyncio
from lnbits import LNbits
import settings
import json
import logging
import qrcode
import os
import re
from time import time

class User:
    def __init__(self, tguserid: int, tgusername:str = None) -> None:
        self.userid = int(tguserid)
        self.username = tgusername
        self.rediskey = f"user:{self.userid}"
        self.invoicekey = None
        self.adminkey = None 
        self.lnbitsuserid = None
        self.walletid = None
        self.lnurlp = None
        self.lndhub = None
        self.lnaddress = None

    def toJson(self) -> str:
        userdata = {
            'telegram_userid':self.userid,
            'lnbits_userid':self.lnbitsuserid,
            'telegram_username':self.username,
            'invoicekey':self.invoicekey,
            'adminkey':self.adminkey,
            'walletid':self.walletid,
            'lnurlp':self.lnurlp,
            'lndhub':self.lndhub
        }

        if self.lnaddress is not None:
            userdata['lnaddress'] = self.lnaddress

        return json.dumps(userdata)
        
    def loadJson(self, data: str) -> None:
        assert(data is not None)
        userdata = json.loads(data)
        assert(userdata is not None)
        if (int(userdata['telegram_userid']) != self.userid):
            logging.error(userdata['telegram_userid'])
            logging.error(self.userid)
            return
        
        if self.username is None:
            self.username = userdata['telegram_username']
        self.invoicekey = userdata['invoicekey']
        self.adminkey = userdata['adminkey']
        self.walletid = userdata['walletid']
        self.lnurlp = userdata['lnurlp']
        self.lndhub = userdata['lndhub']
        for legacy in ['bot.wholestack.nl']:
            if self.lnurlp is not None:
                self.lnurlp = self.lnurlp.replace(legacy,settings.domain)
            if self.lndhub is not None:
                self.lndhub = self.lndhub.replace(legacy,settings.domain)

        self.lnbitsuserid = userdata['lnbits_userid']

        # get lnaddress
        if 'lnaddress' in userdata and userdata['lnaddress'] is not None:
            self.lnaddress = userdata['lnaddress']
        else:
            self.lnaddress = None
       

# Get/Create a QR code and store in filename
def get_qrcode_filename(data: str) -> str:
    filename = os.path.join(settings.qrcode_path,"{}.png".format(hash(data)))
    if not os.path.isfile(filename):
        img = qrcode.make(data)
        file = open(filename,'wb')
        if file:
            img.save(file)
            file.close()
    return filename


async def get_group_owner(chat_id: int) -> User:
    data = settings.rds.hget(f"group:{chat_id}","owner")
    assert(data is not None)
    
    userid = data.decode('utf-8')

    return await get_or_create_user(userid)    

  
async def delete_group_owner(chat_id: int) -> None:
    data = settings.rds.hdel(f"group:{chat_id}","owner")

async def get_balance(user:User) -> int:
    return await settings.lnbits.getBalance(user.invoicekey)


async def set_group_owner(chat_id: int, userid: int) -> None:
    data = settings.rds.hget(f"group:{chat_id}","owner")
    if data is not None:
        rds_userid = data.decode('utf-8')
        assert(userid == rds_userid)
    data = settings.rds.hset(f"group:{chat_id}","owner",userid)

async def get_funding_lnurl(user: User) -> str:
    """
    Return the funding LNURL
    """
    if user is None:
        logging.info(f"User is None")
        return None
    if user.lnurlp is None:
        logging.info(f"User.lnurlp is None")
        return None

    logging.info(f"Get funding URL for {user.lnurlp}")
    
    result = re.search(".*\/([A-Za-z0-9]+)",user.lnurlp)
    if result:
        payid = result.groups()[0]
        details = await settings.lnbits.getLnurlp(f"https://{settings.domain}/",user.invoicekey,payid)
        if details is None:
            logging.error("getLnurlp returned None")
            return None
        
        return details['lnurl']
    else:
        logging.error("No LNURL found for user")
        return None

async def get_or_create_user(userid: int,username: str = None) -> User:
    """
    Get or create a user in redis and lnbits and return the user object
    """    
    user = User(userid,username)
        
    userdata = settings.rds.hget(user.rediskey,"userdata")

    if userdata is not None:
        try:
            user.loadJson(userdata)
            logging.info("Got the fast path for retrieving the user")
            return user
        except AssertionError:
            if userdata == b'null':
                logging.warning(f"Null userdata for redis key '{user.rediskey}', resetting userdata")
                userdata = None
            else:
                logging.error(f"Parse error while loading userdata from redis for key '{user.rediskey}'")
                raise
    
    # no entry in redis, get user and wallet from lnbits 
    if userdata is None:
        print("no user in redis")
        # maybe it is an existing user
        lnusers = await settings.lnbits.getUsers()
        for lnuser in lnusers:
            print(lnuser['name'], user.rediskey)
            if lnuser['name'] == user.rediskey:
                print("Found user")
                user.lnbitsuserid = lnuser['id']
                break

        # create if not existing
        if user.lnbitsuserid is None:
            print("lnbitsuserid is None")
            user.lnbitsuserid = await settings.lnbits.createUser(user.rediskey)

        # get or create wallet if not existing
        wallet = await settings.lnbits.getWallet(user.lnbitsuserid)
        if wallet is None:
            print("Creating wallet")
            wallet = await settings.lnbits.createWallet(user.lnbitsuserid,user.rediskey)

        # copy parameters
        user.invoicekey = wallet['inkey']
        user.adminkey = wallet['adminkey']
        user.walletid = wallet['id']

        # enable extensions for user
        await settings.lnbits.enableExtension("lnurlp",user.lnbitsuserid)
        await settings.lnbits.enableExtension("lndhub",user.lnbitsuserid)
    
        # create lndhub link
        user.lndhub = f"lndhub://admin:{user.adminkey}@https://{settings.domain}/lndhub/ext/"


        payload = {
            "amount": settings.price,
            "max": settings.fund_max,
            "min": settings.fund_min,
            "comment_chars": 0,
            "webhook_url": f"http://127.0.0.1:7000/jukebox/lnbitscallback?userid={user.userid}"
        }

        print(user.toJson())

        if user.username is not None:
            lnuname = user.username.lower()
            lnuname.replace(' ','_')
            lnuname = lnuname[:15]

            if re.search('^[a-z0-9\-_\.]+$',lnuname): 
                payload['username'] = lnuname
            else:
                logging.info(f"Username not allowed for lnaddress {lnuname}")
            payload["description"] = f"Fund the Jukebox wallet of @{user.username}"
        else:
            logging.error(f"username is None for telegram user: {user.userid}")
            payload['description'] = "Fund your personal Jukebox Wallet"
        

        # create lnurlp link
        result = await settings.lnbits.createLnurlp(user.adminkey,payload)
        if result is not None:
            user.lnurlp = f"https://{settings.domain}/lnurlp/link/{result['id']}"    
            if result['username'] is not None:
                user.lnaddress = f"{result['username']}@{settings.domain}"
        else:
            user.lnurlp = None

        # save parameters
        settings.rds.hset(user.rediskey,"userdata",user.toJson())
        
        return user
        
        
