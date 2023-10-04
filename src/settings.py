import redis
import logging
from lnbits import LNbits
import os
import random
import string
from time import time



def init():
    global environment
    global rds
    global lnbits
    global price
    global fund_max
    global fund_min
    global domain
    global spotify_redirect_uri
    global bot_token
    global delete_message_timeout_short
    global delete_message_timeout_medium
    global delete_message_timeout_long
    global secret_token
    global qrcode_path
    global port
    global max_connections
    global ipaddress
    global bot_id
    global donation_fee
    global superadmins

    domain=os.environ['JUKEBOX_DOMAIN']
    
    # set the new environment and fall back to development
    env = None
    if 'JUKEBOX_ENV' in os.environ:
        env = os.environ['JUKEBOX_ENV']
        
    # already initialised for this environment
    try:
        if environment == env:
            return True
    except NameError:
        pass

    # short time for deleting messages in seconds
    delete_message_timeout_short = 10
    delete_message_timeout_medium = 60
    delete_message_timeout_long = 300

    # set secret token for telegram
    secret_token = "".join(random.sample(string.ascii_letters,12))
    spotify_redirect_uri=f'https://{domain}/spotify' # this must literaly match the config in spotify
    max_connections = 5

    # webserver port
    port = 7000
    
    price = 21
    fund_max = 42000
    fund_min = price
    rds = redis.Redis(db=2)
    lnbits = LNbits(
        os.environ['LNBITS_PROTOCOL'],
        os.environ['LNBITS_HOST'],
        os.environ['LNBITS_ADMINKEY'],
        os.environ['LNBITS_INVOICEKEY'],
        os.environ['LNBITS_USRKEY'])
    bot_token=os.environ['BOT_TOKEN']
    qrcode_path = '/tmp'
    ipaddress = os.environ['BOT_IPADDRESS']
    bot_id=int(os.environ['BOT_ID'])
    donation_fee = 21  # default donation fee
    superadmins = [int(superadmin) for superadmin in os.environ['SUPERADMINS'].split(',')]

    environment = env
    if env == 'production':    
        logging.basicConfig(
            filename="logfile_{time}.dat".format(time=time()),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.INFO
        )
        return True
    elif env == 'development':
        logging.basicConfig(
            filename="logfile.dat",
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.INFO
        )
        return True
    else:
        print("unknown environment")
        quit()
        
