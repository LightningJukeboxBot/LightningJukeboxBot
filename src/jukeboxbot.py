#!/usr/bin/env python
import asyncio
import re
import os
import base64
import json
from time import time,sleep
import html
from dataclasses import dataclass
from http import HTTPStatus
import redis
import random
import logging
import string
import statshelper
import telegramhelper
from telegramhelper import TelegramCommand, adminonly, debounce, delete_message, send_telegram_message, group_chat_only, private_chat_only
import qrcode
from PIL import Image
import aiomqtt
import random
from telegram.error import BadRequest, ChatMigrated

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, RedirectResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

from telegram import __version__ as TG_VER

try:
    from telegram import __version_info__
except ImportError:
    __version_info__ = (0, 0, 0, 0, 0)  # type: ignore[assignment]

if __version_info__ < (20, 0, 0, "alpha", 1):
    raise RuntimeError(
        f"This example is not compatible with your current PTB version {TG_VER}. To view the "
        f"{TG_VER} version of this example, "
        f"visit https://docs.python-telegram-bot.org/en/v{TG_VER}/examples.html"
    )

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.constants import ParseMode 
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
    CallbackQueryHandler,
    CallbackContext,
    MessageHandler,
    filters,
    PicklePersistence
)

# import all local stuff
from lnbits import LNbits
import userhelper
from userhelper import User
import spotifyhelper
from spotifyhelper import SpotifySettings, CacheJukeboxHandler
import settings
import jukeboxtexts
import invoicehelper
from invoicehelper import Invoice

anonyms = ['Hal Finney','Satoshi Nakamoto']

jukeboxtexts.init()
settings.init()

# add to local player
def get_queue_requester_name(user):
    if user is None:
        return None

    username = getattr(user, "username", None)
    if username:
        if str(username).startswith("@"):
            return str(username)
        return f"@{username}"

    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    name = " ".join(filter(None, [first_name, last_name]))
    if name:
        return name

    return None


def add_to_queue_or_upvote(uri, chat_id, amount, requester_name=None):
    if not isinstance(uri,str):
        logging.error(f"{chat_id}:uri is not a string")
        return
    if not isinstance(amount,int):
        logging.error(f"{chat_id}:amount is not an integer")
        return
    
    if not chat_id in application.bot_data:
        application.bot_data[chat_id] = {}
    if not 'queue' in application.bot_data[chat_id]:
        application.bot_data[chat_id]['queue'] = {}
    if not 'queue_requesters' in application.bot_data[chat_id]:
        application.bot_data[chat_id]['queue_requesters'] = {}
        
    # upvote in the queue or add to queue
    if uri in application.bot_data[chat_id]['queue']:
        application.bot_data[chat_id]['queue'][uri] += amount
    else:
        application.bot_data[chat_id]['queue'][uri] = amount

    if requester_name and uri not in application.bot_data[chat_id]['queue_requesters']:
        application.bot_data[chat_id]['queue_requesters'][uri] = requester_name
        
    # reorder the queue by highest paying amount
    sorted_queue = sorted(application.bot_data[chat_id]['queue'].items(), key=lambda x:x[1], reverse=True)
    
    # store the queue
    application.bot_data[chat_id]['queue'] = dict(sorted_queue)

# start command handler, returns help information
@debounce
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # send the message
    await send_telegram_message(
        context=context,
        chat_id=update.effective_chat.id,
        text=jukeboxtexts.help,
        parse_mode='MarkdownV2',
        delete_timeout=settings.delete_message_timeout_medium)

# display stats
@debounce
@private_chat_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    userid: int = update.effective_user.id

    if userid not in settings.superadmins:
        return
    
    results = await statshelper.get_jukebox_groups()
    balance = await statshelper.get_bot_stack()

    statsText = f"Bot balance: {balance} sats \n"

    statsText += f"Number of groups: {results['numgroups']}. List of owners: \n"
    for group in results['group']:
        if group['owner'] is not None:
            statsText += f" - {group['groupid']} : @{group['owner'].username}"
        else:
            statsText += f" - {group['groupid']} : Unknown owner"

        statsText += f" price = {group['price']}, donation = {group['donation_fee']} \n"

    await send_telegram_message(
        context=context,
        chat_id=update.effective_chat.id,
        text=statsText,
        delete_timeout=settings.delete_message_timeout_long)

@debounce
@adminonly
@private_chat_only
async def service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This command sends a service message to all Jukebot group owners, that is all users marked as owner of a group
    """
    userid: int = update.effective_user.id
    chat_id: int = update.effective_chat.id,

    if userid not in settings.superadmins:
        logging.info(f"{chat_id}:User {userid} is not a superadmin. Access to stats denied")
        return

    result = re.search("^/service \S.*",update.message.text)
    if result is None:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"Use the /service command as follows: /service <message>\nThe message is sent to all owners of bot")
        return

    # set message, strip the command
    msgstr = update.message.text[9:]

    results = await statshelper.get_jukebox_groups()
    num = 0
    for group in results['group']:
        # skip if no owner is set
        if group['owner'] is None:
            continue

        # send a message each owner
        try:
            message = await context.bot.send_message(
                chat_id=group['owner'].userid,
                text=f"Service message from the Jukebox Bot:\n\n{msgstr}\n\nThank you!")
            num += 1
        except:
            pass

    # send a message each owner
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"The following message was sent to {num} users:\n\nService message from the Jukebox Bot:\n\n{msgstr}\n\nThank you!")

# get the current balance
@debounce
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # do not show balance in other than private chats
    chat_id : int = update.effective_chat.id
    if update.message.chat.type != "private":
        bot_me = await context.bot.get_me()
        
        # direct the user to their private chat
        await send_telegram_message(
            context=context,
            chat_id=chat_id,
            text=jukeboxtexts.balance_in_group,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
            ]),
            delete_timeout=settings.delete_message_timeout_medium)
        return

    # we're in a private chat now
    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)

    # get the balance from LNbits
    balance = await userhelper.get_balance(user)
    
    # create a message with the balance
    logging.info(f"{chat_id}:User {user.userid} balance is {balance} sats")
    await send_telegram_message(
        context=context,
        chat_id=update.effective_chat.id,
        text=f"Your balance is {balance} sats.")

# display the play queue
@debounce
@adminonly
@group_chat_only
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    price = await spotifyhelper.get_price(update.effective_chat.id)
    donation = await spotifyhelper.get_donation_fee(update.effective_chat.id)
    
    if update.message.text == '/price':
        await send_telegram_message(
            context=context,
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=f"Current track price is {price}. Per requested track, {donation} sats is donated to the Jukebox Bot.",
            delete_timeout=settings.delete_message_timeout_short
        )
        return

    # parse and validate the price command
    result = re.search("/price\s+([0-9]+)\s+([0-9]+)$",update.message.text)
    if result is None:
        await send_telegram_message(
            context=context,
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Use command as follows: /price &lt;price&gt; &lt;donation&gt;\n&lt;price&gt; is the track price in sats\n&lt;donation&gt; is the amount in sats donated to the bot per reqested track. The donation is substracted from the track price.",
            delete_timeout=settings.delete_message_timeout_medium
        )
        return

    newprice = int(result.groups()[0])
    newdonation = int(result.groups()[1])

    if newdonation > newprice:
        newdonation = newprice

    # update 
    await spotifyhelper.set_price(update.effective_chat.id, newprice)
    await spotifyhelper.set_donation_fee(update.effective_chat.id, newdonation)
        
    await send_telegram_message(
        context=context,
        chat_id=update.effective_chat.id,
        text=f"Updating price to {newprice} sats. Donation amount is {newdonation} sats.",
        delete_timeout=settings.delete_message_timeout_medium)

def create_queue_button_list(context, sp, chat_id: int):
    button_list = []
    count = 1
    if not chat_id in context.bot_data:
        return button_list
    if not 'queue' in context.bot_data[chat_id]:
        return button_list
    
    for uri in context.bot_data[chat_id]['queue']:
        amount = context.bot_data[chat_id]['queue'][uri]
        requester_name = context.bot_data[chat_id].get('queue_requesters', {}).get(uri)
        requester_text = f" - {requester_name}" if requester_name else ""
        if amount > 100000000 - 1:
            qtitle = f"Up next: {spotifyhelper.get_track_title_from_uri(sp,uri)}{requester_text}"
            button_list.append([
                InlineKeyboardButton(qtitle, callback_data = 0)
            ])
        else:
            qtitle = f"{count}. {spotifyhelper.get_track_title_from_uri(sp,uri)}{requester_text} ({amount} sats)"
            button_list.append([
                InlineKeyboardButton(qtitle, callback_data = telegramhelper.add_command(TelegramCommand(0,telegramhelper.upvote,uri)))
            ])
            
        count += 1
    return button_list    

# display the play queue
@debounce
@group_chat_only
#(message="Execute the /queue command in the group instead of the private chat.")
async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # get an auth managher, if no auth manager is available, dump a message
    chat_id = int(update.effective_chat.id)    
    sp = await spotifyhelper.get_sp(chat_id)
    if not sp:
        await send_telegram_message(
            context=context,
            chat_id=chat_id,
            text="Could not obtain player instance",
            delete_timeout=settings.delete_message_timeout_short)
        return
    
    # get the current track title from cache
    if len(application.bot_data[chat_id]['now_playing_title']) == 0:
        title = "Nothing is playing at the moment"    
    else:                    
        title = application.bot_data[chat_id]['now_playing_title']
        
    title = f"🎵 {title} 🎵"

    title += "\n\nUse the /add command to add your favourite track to the queue, or click on a track to pump it to the top of the queue."

        
    if chat_id in context.bot_data and 'queue' in context.bot_data[chat_id] and len(context.bot_data[chat_id]['queue']) > 0:
        pass
    else:
        title += "\nRequest queue is empty."

    await send_telegram_message(
        context=context,
        chat_id=update.effective_chat.id,
        text=title,
        reply_markup=InlineKeyboardMarkup(create_queue_button_list(context, sp, chat_id)),
        delete_timeout=settings.delete_message_timeout_medium
    )


    
# Disconnect a spotify player from the bot, the connect command
@debounce
@adminonly
@group_chat_only
async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # delete the owner of the group, all admins can do this
    await userhelper.delete_group_owner(update.effective_chat.id)
    result = await spotifyhelper.delete_auth_manager(update.effective_chat.id)
    
    # get an auth manager, if no auth manager is available, dump a message
    if result == True:        
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=jukeboxtexts.spotify_authorisation_removed)            
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})        
    else:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=jukeboxtexts.spotify_authorisation_removed_error)
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})        

# Connect a spotify player to the bot, the connect command
@debounce
#@adminonly
async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # get spotify settings for the user
    logging.info(f"couple command by user: {update.effective_user.id}")
    sps = await spotifyhelper.get_spotify_settings(update.effective_user.id)
    logging.info(f"Client ID = {sps.client_id}")
    logging.info(f"Client secret = {sps.client_secret}")

    
    
    # this command has to be execute from within a group
    if update.message.chat.type == "private":
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            parse_mode='HTML',
            text=f"""
To connect this bot to your spotify account, you have to create an app in the developer portal of Spotify <A href="https://developer.spotify.com/dashboard/applications">here</a>.

1. Click on the 'Create an app' button and give the bot a random name and description. Then click 'Create".

2. Record the 'Client ID' and 'Client Secret'. 

3. Click 'Edit Settings' and add EXACTLY this url <pre>{settings.spotify_redirect_uri}</pre> under 'Redirect URIs'. Do not forget to click 'Add' and 'Save'

4. Use the /setclientid and /setclientsecret commands to configure the 'Client ID' and 'Client Secret'. 

5. Give the '/couple' command in the group that you want to connect to your account. That will redirect you to an authorisation page.
 
""")
        
        # check that client_id is not None
        if sps.client_id is None:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=jukeboxtexts.no_client_id_set)
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=jukeboxtexts.client_id_set.format(sps.client_id))
            
        # check that client secret is not None
        if sps.client_secret is None:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=jukeboxtexts.no_client_secret_set)
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=jukeboxtexts.client_secret_set)

        # hint the user for the connect command
        if sps.client_id is not None and sps.client_secret is not None:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=jukeboxtexts.everything_set_now_do_connect)

        return

    # send message in group to go to private chat
    bot_me = await context.bot.get_me()

    logging.info(f"Client ID = {sps.client_id}")
    logging.info(f"Client secret = {sps.client_secret}")
    
    # if both variables are not none, ask the user to authorize
    if sps.client_id is not None and sps.client_secret is not None:

        # get an auth manaer 
        auth_manager = await spotifyhelper.get_auth_manager(update.effective_chat.id)
        if auth_manager is not None:
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="A player is already connected to this group chat. disconnect it first using the /decouple command before connecting a new one")
            context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})
            return
            
            
        auth_manager = await spotifyhelper.init_auth_manager(update.effective_chat.id,sps.client_id,sps.client_secret)

        # send instructions in the group
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=jukeboxtexts.instructions_in_private_chat,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(jukeboxtexts.button_to_private_chat,url=f"https://t.me/{bot_me.username}")]
            ]))
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})

        state = base64.b64encode(f"{update.effective_chat.id}:{update.effective_user.id}".encode('ascii')).decode('ascii')
        
        # send a message to the private chat of the bot
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=jukeboxtexts.click_the_button_to_authorize,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Authorize player",url=auth_manager.get_authorize_url(state=state))]
            ]))
    else:
        # send a message that configuration is required
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Additional configuration is required, execute this command in a private chat with me.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
            ]))

            
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})

# connect a spotify player to the bot, the setclient secret and set client id commands
@debounce
@private_chat_only
async def spotify_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # get spotify settings for the user
    logging.info(f"Spotify settings by user {update.effective_user.id}")
    sps = await spotifyhelper.get_spotify_settings(update.effective_user.id)

    result = re.search("/(setclientid|setclientsecret)\s+([a-z0-9]+)\s*$",update.message.text)
    if result is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Incorrect usage. ")
        return

    # after validation
    command = result.groups()[0]
    value = result.groups()[1]

    bSave = False
    if command == 'setclientid':
        sps.client_id = value
        bSave = True
        
    if command == 'setclientsecret':
        sps.client_secret = value
        bSave = True
        
    if bSave == True:
        await spotifyhelper.save_spotify_settings(sps)
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Settings updated. Type /couple for current settings and instructions.")


# fund the wallet of the user
@debounce
async def fund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)

    text = f"Click on the button to fund the wallet of @{user.username}."
    if user.lnaddress is not None:
        text += f"\nYou can also fund the wallet by sending sats to the following address: {user.lnaddress}"

    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Fund sats",url=f"https://{settings.domain}/jukebox/fund?command={telegramhelper.add_command(TelegramCommand(update.effective_user.id,'FUND'))}")]
            ]))
    context.job_queue.run_once(delete_message, settings.delete_message_timeout_long, data={'message':message})

# view the history of recently played tracks
@debounce
@group_chat_only
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # get an auth managher, if no auth manager is available, dump a message
    sp = await spotifyhelper.get_sp(update.effective_chat.id)
    if not sp:
        await send_telegram_message(
            context=context,
            chat_id=update.effective_chat.id,
            text="Could not obtain player instance",
            delete_timeout=settings.delete_message_timeout_short)
        return
        
    text = "Track history:\n"
    history = await spotifyhelper.get_history(update.effective_chat.id,20)
    for title in history:
        text += f"{title}\n"
        
    message = await context.bot.send_message(chat_id=update.effective_chat.id,text=text)    
    context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
    
# get lndhub link for user
@debounce
@private_chat_only
async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # we're in a private chat now
    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)

    # create QR code for the link
    filename = userhelper.get_qrcode_filename(user.lndhub)
    with open(filename,'rb') as file:
        await context.bot.send_photo(
            update.effective_chat.id,
            file,
            caption=f"Scan this QR code with an lndhub compatible wallet like BlueWallet or Zeus.",
            parse_mode='HTML')

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<pre>{user.lndhub}</pre>",
            parse_mode='HTML')


# pay a lightning invoice
@debounce
async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = re.search("/refund\s+(lnbc[a-z0-9]+)\s*$",update.message.text)
    if result is None:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Unknown lightning invoice format. Should start with 'lnbc'.")
        return

    # get the patment request from the regular expression
    payment_request = result.groups()[0]

    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)
    
    # pay the invoice
    payment_result = await settings.lnbits.payInvoice(payment_request,user.adminkey)
    if payment_result['result']:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Payment succes.")
        logging.info(f"User {user.userid} paid and invoice")
    else:
        logging.warning(payment_result)
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            parse_mode='HTML',
            text=payment_result['detail'])

# search for a track
async def search_track(update: Update, context: ContextTypes.DEFAULT_TYPE, searchstr: str,limit=5,offset=0) -> None:
    """
    This function searches for tracks in spotify and createas a list of tracks to play
    If a playlist URL is provided, that playlist is used
    This function only works in a group chat
    """
    chat_id : int = update.effective_chat.id
    
    # get an auth manager, if no auth manager is available, dump a message
    sp = await spotifyhelper.get_sp(chat_id)
    if not sp:
        await send_telegram_message(
            context=context,
            chat_id=chat_id,
            text="Could not obtain player instance",
            delete_timeout=settings.delete_message_timeout_short)
        return

    # check if the search string is a spotify URL
    match = re.search('https://open.spotify.com/playlist/([A-Za-z0-9]+).*$',searchstr)
    if (match):
        playlistid = match.groups()[0]
        result = sp.playlist(playlistid,fields=['name'])

        await send_telegram_message(
            context=context,
            chat_id=chat_id,
            text=f"@{update.effective_user.username} suggests to play tracks from the '{result['name']}' playlist.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Pay {await spotifyhelper.get_price(update.effective_chat.id)} sats for a random track", callback_data = telegramhelper.add_command(TelegramCommand(0,telegramhelper.playrandom,playlistid)))
            ]]),
            delete_timeout=settings.delete_message_timeout_long)

        # exit this function
        return

    # search for tracks
    numtries: int = 3
    while numtries > 0:
        numtries -= 1
        try:
            result = sp.search(searchstr,type='track',limit=limit,offset=offset)
        except spotipy.oauth2.SpotifyOauthError:
            # spotify not properly authenticated
            logging.info(f"{chat_id}:Spotify Oauth error")
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Music player not available, search aborted.")
            return
            
        except spotipy.exceptions.SpotifyException:
            if numtries == 0:
                # spotify still triggers an exception
                logging.error(f"{chat_id}:Spotify returned and exception, not returning search result")
                message = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Music player unavailable, search aborted.")
                return
            logging.warning(f"{chat_id}:Spotify returned and exception, retrying")
            continue
        
        break

    # create a list of max five buttons, each with a unique song title
    if len(result['tracks']['items']) > 0:
        tracktitles  = {}
        button_list = []
        for item in result['tracks']['items']:            
            title = spotifyhelper.get_track_title_from_item(item)
            if title not in tracktitles:
                tracktitles[title] = 1
                button_list.append([InlineKeyboardButton(title, callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.add,item['uri'])))])
                
                # max five suggestions
                if len(tracktitles) == 5:
                    break

        # Add a cancel button to the list
        button_list.append([InlineKeyboardButton('Cancel', callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.cancel,None)))])

        
        await send_telegram_message(
            context=context,
            chat_id=update.effective_chat.id,
            text=f"Results for '{searchstr}'",
            reply_markup=InlineKeyboardMarkup(button_list),
            delete_timeout=settings.delete_message_timeout_medium)
    else:
        await send_telegram_message(
            context=context,
            chat_id=update.effective_chat.id,
            text=f"No results for '{searchstr}'",
            delete_timeout=settings.delete_message_timeout_short)

# search for a pixies track
@debounce
async def pixies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    offset=random.randint(0,50)
    await search_track(update, context, 'The Pixies',offset=offset)

# search for a mark knopfler track
@debounce
async def markknopfler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    offset=random.randint(0,50)
    await search_track(update, context,'Mark Knopfler',offset=offset)
    
# search for a track
@debounce
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    searchstr = update.message.text.split(' ',1)
    if len(searchstr) > 1:
        searchstr = searchstr[1]            
        await search_track(update, context,searchstr)
    else:
        message = await context.bot.send_message(chat_id=update.effective_chat.id,text=jukeboxtexts.add_command_help)
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})

# send sats from user to user
@debounce
async def dj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send sats from one user to another
    """
    # verify that this is not a private chat
    # verify that the message is a reply
    if update.message.reply_to_message is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"The /dj command only works as a reply to another user. If no amount is specified, the price for a track, {await spotifyhelper.get_price(update.effective_chat.id)} is sent.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return

    # parse the amount to be paid
    amount = await spotifyhelper.get_price(update.effective_chat.id)
    result = re.search("/[a-z]+(\s+([0-9]+))?\s*$",update.message.text)
    if result is not None:
        amount = result.groups()[1]
        if amount is None:
            amount = 21
        else:
            amount = int(amount)
            
    # get the user that is sending the sats and check his balance
    sender = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)
    balance = await userhelper.get_balance(sender)
    
    if balance < amount:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Insufficient balance, /fund your balance first to /dj another user.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        

        # and stop here
        return 
        
    # get the receiving user and create an invoice
    recipient = await userhelper.get_or_create_user(update.message.reply_to_message.from_user.id,update.message.reply_to_message.from_user.username)
    invoice = await invoicehelper.create_invoice(recipient, amount, f"@{sender.username} thinks you're a DJ!" )
    invoice.recipient = recipient
    invoice.user = sender    

    # pay the invoice
    result = await invoicehelper.pay_invoice(sender, invoice)
    if result['result'] == True:
        # send message in the group chat
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"@{sender.username} sent {amount} sats to @{recipient.username}.")
        #context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})        

        # send a message in the private chat
        if not update.message.reply_to_message.from_user.is_bot:
            try:
                message = await context.bot.send_message(
                    chat_id=recipient.userid,
                    text=f"Received {amount} sats from @{sender.username}.")
            except:
                logging.info("Could not send message to user, probably not allowed")
        else:
            logging.info(f"@{sender.username} is sending {amount} sats to the bot")

        # send a message in the private chat
        try:
            message = await context.bot.send_message(
                chat_id=sender.userid,
                text=f"Sent {amount} sats to  @{recipient.username}.")
        except:
            logging.info("Could not send message to sender user, probably not allowed")

        logging.info(f"User {sender.userid} sent {amount} sats to {recipient.userid}")
    else:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f'Payment failed. Sorry.')
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
    
async def callback_paid_invoice(invoice: Invoice):
    if invoice is None:
        logging.error("Invoice is None")
        return

    if invoice.chat_id is None:
        logging.error("Invoice chat_id is None")
        return

    chat_id = invoice.chat_id
    logging.info(f"{chat_id}:callback_paid_invoice")

    

    
    if await invoicehelper.delete_invoice(invoice.payment_hash) == False:
        logging.info(f"{chat_id}:invoicehelper.delete_invoice returned False")
        return
    try:
        logging.info(f"{chat_id}:Trying to delete messageid {invoice.message_id}")
        await application.bot.delete_message(invoice.chat_id,invoice.message_id)            
    except:
        pass

    sp = await spotifyhelper.get_sp(invoice.chat_id)
    if not sp:
        await send_telegram_message(
            context=context,
            chat_id=invoice.chat_id,
            text="Could not obtain player instance",
            delete_timeout=settings.delete_message_timeout_short)
        return
    
    # change this if it is an upvote
    add_to_queue_or_upvote(
        invoice.spotify_uri_list[0],
        invoice.chat_id,
        invoice.amount_to_pay,
        get_queue_requester_name(invoice.user))
    
    try:
        message = f"{random.choice(anonyms)} added '{invoice.title}' to the queue."        
        if invoice.command == telegramhelper.upvote:
            f"{random.choice(anonyms)} pumped '{invoice.title}' with {invoice.amount_to_pay} sats."

        await send_telegram_message(
            context=application,
            chat_id=invoice.chat_id,
            parse_mode='HTML',
            text=message)
    except:
        logging.error(f"{chat_id}:Could not  send message to the group that track was added to the /queue")
    

    if False:
        try:
            await application.bot.send_message(
                chat_id=invoice.user.userid,
                parse_mode='HTML',
                text=f"You paid {invoice.amount_to_pay} sats for '{invoice.title}'.")
        except:
            logging.info(f"{chat_id}:Could not send individual message to user")
    
     # make donation to the bot
    jukeboxbot = await userhelper.get_or_create_user(settings.bot_id)
    donator = await userhelper.get_or_create_user(invoice.recipient.userid)
    donation_amount : int = await spotifyhelper.get_donation_fee(invoice.chat_id)
    donation_amount = min(donation_amount,invoice.amount_to_pay)
    if donation_amount > 0:
        donation_invoice = await invoicehelper.create_invoice(jukeboxbot, donation_amount, "donation to the bot")
        result = await invoicehelper.pay_invoice(donator, donation_invoice)

@debounce
@group_chat_only
async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id=update.effective_chat.id

    userid: int = update.effective_user.id

    if userid not in settings.superadmins:
        return
    

    
    await next_in_queue(chat_id, True)
    
    # remove now_playing and manage_queue jobs
    jobs  = context.job_queue.get_jobs_by_name(f"{chat_id}:now_playing")
    for job in jobs:
        job.schedule_removal()
    jobs  = context.job_queue.get_jobs_by_name(f"{chat_id}:manage_queue")
    for job in jobs:
        job.schedule_removal()

        
    # restart now_playing job
    context.job_queue.run_once(callback_now_playing, 5, name=f"{chat_id}:now_playing", data=chat_id, job_kwargs = {'misfire_grace_time':None})
        
@debounce
async def web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    return the Web URL where tracks can be requested using a browser
    """
    userid: int = update.effective_user.id
    chat_id: int = update.effective_chat.id
    
    if update.message.chat.type == "private":
        return

    if not chat_id in application.bot_data:
        application.bot_data[chat_id] = {}
    if not 'web' in application.bot_data[chat_id]:
        application.bot_data[chat_id]['web'] = True
        
    bAdmin = False
    for member in await context.bot.get_chat_administrators(chat_id):
        if member.user.id == update.effective_user.id and member.status in ['administrator','creator']:
            bAdmin = True

    if bAdmin:
        if ( update.message.text == '/web on' ):
            application.bot_data[chat_id]['web'] = True
        elif ( update.message.text == '/web off' ):
            application.bot_data[chat_id]['web'] = False
        elif (update.message.text == '/web' ):
            pass
        else:
            await send_telegram_message(
                context=context,
                chat_id=chat_id,
                text="Use the /web off comnmand to disable requesting tracks from the web, or /web on, to enable",
                delete_timeout=settings.delete_message_timeout_short)
            return

    if application.bot_data[chat_id]['web'] == False:
        await send_telegram_message(
            context=context,
            chat_id=chat_id,
            text="Web access is currently disabled",
            delete_timeout=settings.delete_message_timeout_short)
        return
        
    
    jukebox_url = f"https://{settings.domain}/jukebox/web/{update.effective_chat.id}"
    filename = os.path.join(settings.qrcode_path,f"web_url_{update.effective_chat.id}.png")

    if not os.path.isfile(filename):    
        img_bg = Image.open('../assets/web_jukebox_template.png')
        qr = qrcode.QRCode(box_size=7, border=0)
        qr.add_data(jukebox_url)
        qr.make()
        img_qr = qr.make_image()
        pos = (int((img_bg.size[0] - img_qr.size[0])/2), 385 - int(img_qr.size[1] / 2))
        img_bg.paste(img_qr, pos)
        img_bg.save(filename)


    with open(filename,'rb') as file:
        message = await context.bot.send_photo(
            update.effective_chat.id,
            file,
            caption=f'Access this Jukebox directly at the following URL: {jukebox_url}. Pro tip: print out this image and scan it with your phone.',
            parse_mode='HTML')


    context.job_queue.run_once(delete_message, settings.delete_message_timeout_long, data={'message':message})
    

async def check_invoice_callback(context: ContextTypes.DEFAULT_TYPE):
    """
    This function checks an invoice if it has been paid
    if it does not exist anymore, or the timeout is expired, the callback stops
    """
    invoice = context.job.data
    if invoice is None:
        logging.error("Got callback with a None invoice")
        return
    
    redis_invoice = await invoicehelper.get_invoice(invoice.payment_hash)
    if redis_invoice is None:
        logging.info("Invoice no longer exists, probably has been paid or canceled")
        return

    # check if invoice was paid    
    if await invoicehelper.invoice_paid(invoice) == True:
        await callback_paid_invoice(invoice)
        return

    # invoice has not been paid
    invoice.ttl -= 15
    if invoice.ttl <= 0:
        await invoicehelper.delete_invoice(invoice.payment_hash)
        try:
            await context.bot.delete_message(invoice.chat_id,invoice.message_id)            
        except:
            pass
    else:
        application.job_queue.run_once(check_invoice_callback, 15, data = invoice)
    

async def regular_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This function performs tasks to clean up stuff at regular intervals
    just empties the now playing list so that the callback_spotify function creates a new message
    """
    logging.info("Running regular clean up")
#    for chatid in list(now_playing_message.keys()):
#        del now_playing_message[chatid]

    telegramhelper.purge_commands()

async def callback_now_playing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Callback function that updates the nowplaying track and 
    plans the callback for updating the queue
    """
    chat_id = int(context.job.data)
    logging.info(f"{chat_id}:Callback now playing")




    
    # default interval
    interval = settings.max_spotify_poll_interval

    # make sure that chat_id exists in bot_data
    if not chat_id in application.bot_data:
        application.bot_data[chat_id] = {
            'now_playing_title': ''
        }
        
        
        
    try:
        application.bot_data[chat_id]['last_poll'] = time()

        
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:            
            logging.error(f"{chat_id}:No sp after succesfull payment")
            raise
    

        # query the current track
        currenttrack = None
        currenttrack = sp.current_user_playing_track()
        
        # query the current track
        title = "Nothing playing at the moment"
        if currenttrack is not None and 'item' in currenttrack and currenttrack['item'] is not None:
            title = spotifyhelper.get_track_title_from_item(currenttrack['item'])
            application.bot_data[chat_id]['now_playing_title'] = title
                        
            # update history
            await spotifyhelper.update_history(chat_id, title)                

            # update interval when to update now playing message
            interval  = ( currenttrack['item']['duration_ms'] - currenttrack['progress_ms'] ) / 1000

        if 'now_playing_message' in application.bot_data[chat_id]:
            logging.info(f"{chat_id}:Now playing message is present in bot_data")
            try:
                [message_id, prev_title] = application.bot_data[chat_id]['now_playing_message']
                if prev_title != title:                    
                    await context.bot.editMessageText(title,chat_id=chat_id,message_id=message_id)
                    application.bot_data[chat_id]['now_playing_message'] = [ message_id, title ]
                    logging.info(f"{chat_id}:Now playing: '{title}'")
            except BadRequest as err:
                if err.message == "Message to edit not found":
                    logging.info(f"{chat_id}:Now playing message not found, deleting from local cache")
                    del application.bot_data[chat_id]['now_playing_message']
                    interval = 30
                elif err.message == "Message is not modified":
                    logging.info(f"{chat_id}:Message is not modified")
                else:
                    logging.error(f"{chat_id}:BadRequest with unknown error message: {err.message}")                                               
            except Exception as err:
                logging.error(f"Exception of type {type(err).__name__} when refreshing now playing in chat {chat_id}")                
        else:
            logging.info(f"{chat_id}:Creating new pinned message")
            try:
                message = await context.bot.send_message(text=title,chat_id=chat_id)
                application.bot_data[chat_id]['now_playing_message'] = [ message.id, title ]
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=message.id)
            except ChatMigrated as err:
                logging.info(f"{chat_id}:Chat migrated from to {err.new_chat_id}. Deleting old settings")
                await spotifyhelper.delete_chat(chat_id)
            except BadRequest as err:
                if err.message == "Chat not found":
                    logging.info(f"{chat_id}:Chat not found, deleting")
                    await spotifyhelper.delete_chat(chat_id)
                elif err.message == "Not enough rights to send text messages to the chat":
                    logging.info(f"{chat_id}:Bot has insufficient privileges")
                    await spotifyhelper.delete_chat(chat_id)
                else:
                    logging.error(f"{chat_id}:BadRequest with unknown error message: {err.message}")                                       
            except Exception as e:
                logging.error(f"{chat_id}:exception when sending message of type {type(e).__name__}")                            
    except spotipy.oauth2.SpotifyOauthError:
        logging.error(f"{chat_id}:Spotify OAuth error")
        return
    except RuntimeError as err:
        logging.error(f"{chat_id}:Caught runtime error")
    except NameError as err:
        logging.error(f"{chat_id}:Caught NameError error")
    except Exception as err:
        logging.error(f"{chat_id}:Unhandled exception in callback_now_playing {type(err).__name__}")
        

    logging.info(f"{chat_id}:Next run in {interval} seconds")
    if interval > 0:
        context.job_queue.run_once(callback_now_playing, interval + 5, name=f"{chat_id}:now_playing",data=chat_id, job_kwargs = {'misfire_grace_time':None})
        context.job_queue.run_once(callback_manage_queue, interval - 15, name=f"{chat_id}:manage_queue", data=chat_id, job_kwargs = {'misfire_grace_time':None})
    
async def next_in_queue(chat_id: int, spotify_next) -> None:
    logging.info(f"{chat_id}:next in queue")

    # do nothing when there is no queue
    if chat_id not in application.bot_data:
        return
    if 'queue' not in application.bot_data[chat_id]:
        return
    if len(application.bot_data[chat_id]['queue']) == 0:
        return

    
    try:
        # get the auth manager
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:
            raise

        try:
            queue = sp.queue()
            logging.info(f"{chat_id}:Queue length = {len(queue['queue'])}")
            if queue:
                queued_uri = None
                if 'queued' in application.bot_data[chat_id]:
                    queued_uri = application.bot_data[chat_id]['queued']
                    logging.info(f"{chat_id}:Queued = {queued_uri} ")
                bQueued = False
                for item in queue['queue']:
                    if (queued_uri is not None) and (queued_uri == item['uri']):
                        bQueued = True
                if bQueued:
                    logging.info(f"{chat_id}:Queued item in queue, not adding another track")
                    return
        
        except Exception as err:
            logging.error("Caught exception")
            
                
        # add next track to the player queue
        if ((chat_id in application.bot_data) and ('queue' in application.bot_data[chat_id]) and (len(application.bot_data[chat_id]['queue']) > 0)):

            
            next_in_queue_uri = list(application.bot_data[int(chat_id)]['queue'].keys())[0]                        

            logging.info(f"{chat_id}:Adding next in queue '{next_in_queue_uri}' to sp queue")

            
            
            sp.add_to_queue(next_in_queue_uri)
            application.bot_data[chat_id]['queued'] = next_in_queue_uri

            
            # and remove from the bot queue
            application.bot_data[int(chat_id)]['queue'].pop(next_in_queue_uri)
            application.bot_data[int(chat_id)].get('queue_requesters', {}).pop(next_in_queue_uri, None)

        # skip to the next track
        if spotify_next:
            sp.next_track()

    except spotipy.exceptions.SpotifyException as err:
        logging.error(f"{chat_id}:SpotiyException, failed to add track to the queue code='{err.code}' msg='{err.msg}'")
    except spotipy.oauth2.SpotifyOauthError as err:
        logging.error(f"{chat_id}:SpotifyOAuth error")
    except RuntimeError as err:
        logging.error(f"Caught runtime error {err}")
    except Exception as err:
        logging.error(f"{chat_id}:Unhandled exception in callback_manage_queue {type(err).__name__}")
        
async def callback_manage_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    callback function that adds tracks from the local queue to the spotify queue
    """
    chat_id = int(context.job.data)
    
    logging.info(f"{chat_id}:Callback manage queue")

    await next_in_queue(chat_id,False)
            
async def check_spotify_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This function starts up the jobs that monitor the current ]playing track in spotify and manage the play queue
    """
    bFirst = False
    if 'first' in context.job.data:
        bFirst = context.job.data['first']

    if bFirst:
        logging.info("Forcing update of all player statusses (bFirst == True)")
    
        
    for key in settings.rds.scan_iter("group:*"):
        chat_id = int(key.decode('utf-8').split(':')[1])

        # make sure that chat_id exists in bot_data
        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
            
        if not 'last_poll' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['last_poll'] = 0

        if bFirst == True or time() - application.bot_data[chat_id]['last_poll'] > 3 * settings.max_spotify_poll_interval:
            logging.info(f"Skipping starting up spotify callback for {chat_id}")
            context.job_queue.run_once(callback_now_playing, 2, name=f"{chat_id}:now_playing", data=chat_id, job_kwargs = {'misfire_grace_time':None})

    context.job.data = {'first':False}

def get_amount_to_pay(sp, track_price: int, spotify_uri_list):
    if track_price == 0:
        return 0
    if len(spotify_uri_list) == 0:
        return 0

    amount_to_pay = 0
    for uri in spotify_uri_list:
        track = sp.track(uri)         
        track_len = track['duration_ms'] / 1000
        
        if track_len < 600:
            amount_to_pay += track_price
        else:
            amount_to_pay += int((track_len * track_price)  / 180)

    return amount_to_pay

    
#callback for button presses
async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This functions handles all button presses that are fed back into the application
    """
    key = update.callback_query.data
    chat_id = int(update.effective_chat.id)

    # CallbackQueries need to be answered, even if no notification to the user is needed    
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery    
    await update.callback_query.answer()

    if key is None:
        return
    
    command = telegramhelper.get_command(key)

    if command is None:
        logging.info("Command is None")
        return

    # parse the callback data.
    # TODO: Should convert this into an access reference map pattern

    # only the user that requested the track can select a track
    # or when the userid is explicitly set to 0
    if command.userid != 0 and command.userid != update.effective_user.id:
        logging.debug("Avoiding real click")
        return
    
    # process the various commands
    # cancel command
    if command.command == telegramhelper.cancel:
        """
        Cancel just deletes the message
        """
        await update.callback_query.delete_message()
        return
    
    elif command.command == telegramhelper.cancelinvoice:
        await update.callback_query.delete_message()

        invoice = command.data
        if invoice is not None:
            await invoicehelper.delete_invoice(invoice.payment_hash)
        return    

    # verify that player is available, otherwise it has no use to queue a track
    #track = sp.current_user_playing_track()
    if len(application.bot_data[chat_id]['now_playing_title']) == 0: 
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Player is not active at the moment. Payment aborted.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return

    # get the track price
    track_price = int(await spotifyhelper.get_price(update.effective_chat.id))

    # the commands from here on modify a list of tracks to be queue
    # and we have to check hat we have spotify available
    # get an auth managher, if no auth manager is available, dump a message
    sp = await spotifyhelper.get_sp(chat_id)
    if not sp:
        return



    
    # Play a random track from a playlist
    spotify_uri_list = []          
    if  command.command == telegramhelper.add:
        # add a single track to the list
        spotify_uri_list = [command.data]
        await update.callback_query.delete_message()
    elif  command.command == telegramhelper.playrandom:
        playlistid = command.data
        result = sp.playlist_items(playlistid,offset=0,limit=1)
        idxs = random.sample(range(0,result['total']),1)
        for idx in idxs:    
            result = sp.playlist_items(playlistid,offset=idx,limit=1)
            for item in result['items']:
                spotify_uri_list.append(item['track']['uri'])
    elif command.command == telegramhelper.upvote:
        spotify_uri_list = [command.data]
        logging.info(f"{chat_id}:Upvote of {command.data}")
    else:
        logging.error(f"{chat_id}:Unknown command: {command.command}")
        return

    # validate payment conditions
    payment_required = True
    amount_to_pay = get_amount_to_pay(sp, track_price, spotify_uri_list)
    
    
    logging.info(f"{chat_id}:Amount to pay = {amount_to_pay}")
    if amount_to_pay == 0:
        payment_required = False
            
    # if no payment required, add the tracks to the queue one by one
    if payment_required == False:
        # TODO: change this if it is an upvote
        #spotifyhelper.add_to_queue(sp, spotify_uri_list)
        logging.info(f"{chat_id}:No payment required")
        add_to_queue_or_upvote(
            spotify_uri_list[0],
            chat_id,
            0,
            get_queue_requester_name(update.effective_user))
        

        for uri in spotify_uri_list:

            tracktitle = spotifyhelper.get_track_title_from_uri(sp,uri)
            
            try:
                if command.command == telegramhelper.upvote:
                    await update.callback_query.edit_message_reply_markup(InlineKeyboardMarkup(create_queue_button_list(context, sp, update.effective_chat.id)))
             
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        parse_mode='HTML',
                        text=f"@{update.effective_user.username} pumped '{tracktitle}' to {int(application.bot_data[update.effective_chat.id]['queue'][uri])} sats.")
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        parse_mode='HTML',
                        text=f"@{update.effective_user.username} added '{tracktitle}' to the /queue.")
            except:
                pass

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    parse_mode='HTML',
                    text=f"You paid {amount_to_pay} sats for  '{tracktitle}'.")
            except:
                pass

            
        # return 
        return
        
    # create an invoice title
    logging.info(f"{chat_id}:Creating invoice")
    invoice_title = f"{spotifyhelper.get_track_title_from_uri(sp,spotify_uri_list[0])}"
    for i in range(1,len(spotify_uri_list)):
        title += f",'{spotifyhelper.get_track_title_from_uri(sp,spotify_uri_list[0])}"


    # create the invoice
    # the owner is the one that has his spotify player connected
    recipient = await userhelper.get_group_owner(update.effective_chat.id)
    invoice = await invoicehelper.create_invoice(recipient, amount_to_pay, invoice_title)


    # get the user wallet and try to pay the invoice
    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)
    invoice.user = user
    invoice.title = invoice_title
    invoice.recipient = recipient    
    invoice.spotify_uri_list = spotify_uri_list
    invoice.title = invoice_title
    invoice.chat_id = update.effective_chat.id
    invoice.amount_to_pay = amount_to_pay
    if command.command == telegramhelper.upvote:
        invoice.command = telegramhelper.upvote


    # pay the invoice
    payment_result = await invoicehelper.pay_invoice(invoice.user, invoice)


    logging.info(f"{chat_id}:Got payment result")
    
    # if payment success
    if payment_result['result'] == True:
        # TODO: change this if it is an upvote        
        #spotifyhelper.add_to_queue(sp, spotify_uri_list)
        add_to_queue_or_upvote(
            invoice.spotify_uri_list[0],
            invoice.chat_id,
            invoice.amount_to_pay,
            get_queue_requester_name(invoice.user))

        if invoice.command == telegramhelper.upvote:
            # update the queue message
            await update.callback_query.edit_message_reply_markup(InlineKeyboardMarkup(create_queue_button_list(context, sp, update.effective_chat.id)))

            # send a message that the track was pumped
            # TODO: add expiration to the message
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                parse_mode='HTML',
                text=f"@{update.effective_user.username} pumped {invoice_title} to {int(context.bot_data[update.effective_chat.id]['queue'][invoice.spotify_uri_list[0]])} sats")
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                parse_mode='HTML',
                text=f"@{update.effective_user.username} added {invoice_title} to the /queue.")
            
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                parse_mode='HTML',
                text=f"You paid {amount_to_pay} sats for {invoice_title}.")
        except:
            logging.info(f"{chat_id}:Could not send message to user")

            
        try:
            async with aiomqtt.Client("localhost") as client:
                await client.publish(f"{update.effective_chat.id}/added_to_queue", payload=invoice_title)
        except:
            logging.error(f"{chat_id}:Exception when publishing queue add to mqtt")
            pass

            
        # make donation to the bot
        jukeboxbot = await userhelper.get_or_create_user(settings.bot_id)
        donation_amount : int = await spotifyhelper.get_donation_fee(invoice.chat_id)
        donation_amount = min(donation_amount,invoice.amount_to_pay)
        if donation_amount > 0:
            donation_invoice = await invoicehelper.create_invoice(jukeboxbot, donation_amount, "donation to the bot")
            result = await invoicehelper.pay_invoice(recipient, donation_invoice)

        return

    # store the invoice in a list of open invoices        
    # add extra data 
    
    # we failed paying the invoice, popup the lnurlp
    if invoice.command == telegramhelper.upvote:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"@{update.effective_user.username} pump '{invoice_title}' in the /queue?\n\nClick to pay below or fund the bot with /fund@Jukebox_Lightning_bot.",       
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[        
                InlineKeyboardButton(f"Pay {amount_to_pay} sats",url=f"https://{settings.domain}/jukebox/payinvoice?payment_hash={invoice.payment_hash}"),
                InlineKeyboardButton('Cancel', callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.cancelinvoice,invoice)))
            ]]))
    else:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"@{update.effective_user.username} add '{invoice_title}' to the /queue?\n\nClick to pay below or fund the bot with /fund@Jukebox_Lightning_bot.",       
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[        
                InlineKeyboardButton(f"Pay {amount_to_pay} sats",url=f"https://{settings.domain}/jukebox/payinvoice?payment_hash={invoice.payment_hash}"),
                InlineKeyboardButton('Cancel', callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.cancelinvoice,invoice)))
            ]]))

    # add data to the invoice
    invoice.message_id = message.id

    # and save the invoice
    await invoicehelper.save_invoice(invoice)

    # change this into an SSE
    # start a loop to check the invoice, for a period of 10 minutes
    application.job_queue.run_once(check_invoice_callback, 15, data = invoice)


async def main() -> None:
    """Set up the application and a custom webserver."""
    global application

    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    persistence = PicklePersistence(filepath="jukeboxbot",single_file=False)
    application = (
        Application.builder().token(settings.bot_token).persistence(persistence).updater(None).build()
    )
 
    # register handlers
    application.add_handler(CommandHandler('add', search))  # search for a track
    application.add_handler(CommandHandler('pixies', pixies))  # search for a pixies track
    application.add_handler(CommandHandler('mark', markknopfler))  # search for a Mark Knopfler track
    application.add_handler(CommandHandler(['stack','balance'], balance)) # view wallet balance
    application.add_handler(CommandHandler('couple', connect)) # connect to spotify account
    application.add_handler(CommandHandler('decouple', disconnect)) # disconnect from spotify account
    application.add_handler(CommandHandler('fund',fund)) # add funds to wallet
    application.add_handler(CommandHandler('history', history)) # view history of tracks
    application.add_handler(CommandHandler('link',link)) # view LNDHUB QR 
    application.add_handler(CommandHandler('refund', pay)) # pay a lightning invoice
    application.add_handler(CommandHandler('price', price)) # set the track price
    application.add_handler(CommandHandler('queue', queue)) # view the queue
    application.add_handler(CommandHandler('service', service)) # service notifications to bot users
    application.add_handler(CommandHandler("setclientsecret",spotify_settings)) # set the secret for a spotify app
    application.add_handler(CommandHandler("setclientid",spotify_settings))  # set the clientid or a spotify app
    application.add_handler(CommandHandler("stats",stats)) # dump various stats
    application.add_handler(CommandHandler(["start","faq"],start))  # help message
    application.add_handler(CommandHandler('dj', dj))  # pay another user    
    application.add_handler(CommandHandler('web', web))  # display the web URL, or disable/enable web
    application.add_handler(CommandHandler('skip', skip))  # allow the admin to skip a track

    application.add_handler(CallbackQueryHandler(callback_button))
    application.job_queue.run_repeating(regular_cleanup, 12 * 3600)
    #application.job_queue.run_once(callback_spotify, 2)
    application.job_queue.run_repeating(check_spotify_callback,1800,first=10,data={'first':True})



    # Pass webhook settings to telegram
    logging.info(f"Jukebox url: \"https://{settings.domain}/jukebox/telegram\"")
    logging.info(f"Jukebox IP: {settings.ipaddress}")
    await application.bot.set_webhook(
        url=f"https://{settings.domain}/jukebox/telegram",
        allowed_updates=['callback_query','message'],
	    ip_address=settings.ipaddress
    )


    # Set up webserver
    async def telegram(request: Request) -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        try:
            await application.update_queue.put(
                Update.de_json(data=await request.json(), bot=application.bot)
            )
        finally:
            return Response()

    async def invoicepaid_callback(request: Request) -> Response:
        data = await request.json()
        payment_hash = data['payment_hash']
                       
        invoice = await invoicehelper.get_invoice(payment_hash)
        if invoice is None:
            return Response()
        
        # process in the bot
        await callback_paid_invoice(invoice)
            
        return Response()

    async def jukebox_fund(request: Request) -> Response:
        if 'command' not in request.query_params:
            return Response()

        key = request.query_params['command']
        if key is None:
            return Response()
        
        command = telegramhelper.get_command(key)
        if command is None:
            return Response()

        if command.command != 'FUND':
            return Response()

        user = await userhelper.get_or_create_user(command.userid) 
        if user is None:
            return      

        lnurl = await userhelper.get_funding_lnurl(user)
        
            

        return Response(f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Fund the wallet of '{user.username}'</title>
  <link rel="stylesheet" href="/jukebox/assets/JukeboxBot.css">
</head>
<body>
  <div class="container">
    <div class="image-container">
      <div class="image-content">
        <img src="/jukebox/assets/jukeboxbot_fund.png" alt="JukeboxBot" />
        <div class="qr-code-container">
          <img id="qr-code-image" alt="QR code image" lnurl="{lnurl}">
        </div>
        <button class="copy-data" aria-label="Copy LNRUL"></button>
      </div>
    </div>
  </div>
  <script src="/jukebox/assets/JukeboxBotFund.js"></script>   
</body>
</html>
""")

    async def payinvoice_callback(request: Request) -> Response:
        if 'payment_hash' not in request.query_params:
            return Response("Invoice not found")
        
        payment_hash = request.query_params["payment_hash"]

        invoice = await invoicehelper.get_invoice(payment_hash)
        if invoice is None:
            return Response("Invoice not found")


        return Response(f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Pay invoice to add '{invoice.title}' to queue</title>
  <link rel="stylesheet" href="/jukebox/assets/JukeboxBot.css">
</head>
<body>
  <div class="container">
    <div class="image-container">
      <div class="image-content">
        <img src="/jukebox/assets/jukeboxbot_payinvoice.png" alt="JukeboxBot" />
        <div class="qr-code-container">
          <img id="qr-code-image" alt="QR code image" invoice="{invoice.payment_request}" paymenthash="{invoice.payment_hash}">
        </div>
        <button class="copy-data" aria-label="Copy invoice"></button>
        <pay-with-ln payment-request="{invoice.payment_request}"></pay-with-ln>
      </div>
    </div>
  </div>
  <script src="/jukebox/assets/JukeboxBotInvoice.js"></script>
  <script src="https://unpkg.com/pay-with-ln@0.1.0/dist/pay-with-ln.js" integrity="sha384-Uid8n0M8dpAoE1SOQOXOcMfDy9hvqtSp+A3xMFilQn+Z6fxsnmCayPVP8na5vdAv" crossorigin="anonymous"></script>
<script>                                                                                                                                        
if(typeof window.webln !== 'undefined') {{
      sendPayment();
}}

async function sendPayment() {{
  try {{
    await window.webln.enable();
    await window.webln.sendPayment('{invoice.payment_request}');
  }} catch (error) {{
      alert("An error occurred during the payment.");
  }}
}}
  </script>
</body>
</html>
""")

    async def web_api_check_payment(request: Request) -> JSONResponse:
        chat_id = int(request.path_params['chat_id'])
        payment_hash = request.path_params['payment_hash']

        invoice = await invoicehelper.get_invoice(payment_hash)
        if invoice is None:            
            return JSONResponse({"status":404,"message":"Invoice not found"})

        if await invoicehelper.invoice_paid(invoice) == True:
            return JSONResponse({"status":200,"message":"Invoice paid"})

        return JSONResponse({"status":402,"message":"Invoice not paid"})

    
    async def web_api_request(request: Request) -> JSONResponse:
        chat_id = int(request.path_params['chat_id'])

        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'web' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['web'] = True
        if application.bot_data[chat_id]['web'] == False:
            return JSONResponse({"status":400,"message":"Web access disabled"})


        
        # get form        
        form = await request.json()

        # validate that form is present
        if form is None:
            return JSONResponse({"status":400,"message":"Incomplete request form is None"})

        # get query
        track_id = form["track_id"]

        # validate that track_id is present
        if track_id is None:
            return JSONResponse({"status":400,"message":"Incomplete request track_id is None"})

        # validate allow characters in track_id
        if not re.search("^[A-Za-z0-9]+$",track_id):
            return JSONResponse({"status":400,"message":"Incomplete request track_id is invalid"})
        
        # add spotify prefix to the track_id
        track_id = f"spotify:track:{track_id}"

        # get spotify auth manager
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:
            return JSONResponse({"status":400,"message":"Incomplete request, sp is None"})
        
        amount_to_pay = get_amount_to_pay(sp, int(await spotifyhelper.get_price(chat_id)), [track_id])

        recipient = await userhelper.get_group_owner(chat_id)
        invoice_title = f"'{spotifyhelper.get_track_title_from_item(track)}'"
        invoice = await invoicehelper.create_invoice(recipient, amount_to_pay, invoice_title)
        if invoice is None:
            return JSONResponse({"status":400,"message":"Payments not available"})

        invoice.user = User(80,"Web user")
        invoice.title = invoice_title
        invoice.recipient = recipient   
        logging.info("recipient when creating invoice: " + invoice.recipient.toJson())
        invoice.spotify_uri_list = [ track_id ]
        invoice.title = invoice_title
        invoice.chat_id = chat_id
        invoice.amount_to_pay = amount_to_pay

        # save the invoice
        await invoicehelper.save_invoice(invoice)

        application.job_queue.run_once(check_invoice_callback, 15, data = invoice)

        message = {
            'status':200,
            'result':{
                'title':invoice.title,
                'amount':invoice.amount_to_pay,
                'payment_hash':invoice.payment_hash,
                'payment_request':invoice.payment_request
            }
        }

        return JSONResponse(message)
                  
    async def web_api_search(request: Request) -> JSONResponse:
        chat_id = int(request.path_params['chat_id'])

        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'web' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['web'] = True
        if application.bot_data[chat_id]['web'] == False:
            return JSONResponse({"status":400,"message":"Web access disabled"})
        
        # get form        
        form = await request.json()

        # validate that form is present
        if form is None:
            return JSONResponse({"status":400,"message":"Incomplete request form is None"})

        # get query
        query = form["query"]

        # validate that query is present
        if query is None:
            return JSONResponse({"status":400,"message":"Incomplete request query is None"})

        # validate allow characters in query
        if not re.search("^[A-Za-z0-9 ]+$",query):
            return JSONResponse({"status":400,"message":"Incomplete request query is invalid"})
            
        # get spotify auth manager 
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:
            return JSONResponse({"status":400,"message":"Incomplete request, sp is None"})
            
        # search for tracks
        numtries: int = 3
        while numtries > 0:
            numtries -= 1
            try:
                result = sp.search(query)
            except spotipy.exceptions.SpotifyException:
                if numtries == 0:
                    logging.error("Spotify returned and exception, not returning search result")
                    return JSONResponse({"status":400,"message":"Search currently unavailable"})
                logging.warning("Spotify returned and exception, retrying")
                continue
            break

        # create a list of max five items
        if len(result['tracks']['items']) == 0:
            return JSONResponse({"status":200,"results":[]})


        message = {'result':{'tracks':[]}}
        tracktitles  = {}

        for item in result['tracks']['items']:            
            title = spotifyhelper.get_track_title_from_item(item)
            track_id = item['uri']
            result = re.search("^spotify:track:([A-Z0-9a-z]+)$",track_id)
            if not result:
                continue

            # strip spotify from the track id
            track_id = result.groups()[0]

            if title not in tracktitles:
                message['result']['tracks'].append({'title':title,'track_id':track_id})
                # max five suggestions
                if len(message['result']['tracks']) > 5:                    
                    break

        message['status'] = 200
                
        return JSONResponse(message)



    async def web_api_status(request: Request) -> JSONResponse:
        chat_id = int(request.path_params['chat_id'])

        message = {
            'now':{},
            'price': await spotifyhelper.get_price(chat_id),
            'donation': await spotifyhelper.get_donation_fee(chat_id)
        }

               
        message['now']['title'] = "Nothing is playing at the moment"


        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'queue' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['queue'] = {}

        if 'now_playing_title' in application.bot_data[chat_id] and len(application.bot_data[chat_id]['now_playing_title']) > 0:
            message['now']['title'] = application.bot_data[chat_id]['now_playing_title']
        
            

        message['status'] = 200
        return JSONResponse(message)

    # get the songs that are played
    async def web_api_playlist(request: Request) -> JSONResponse:
        return JSONResponse({"status":400,"message":"Currently unavailable"})
    
    
    async def jukebox_status(request: Request) -> JSONResponse:
        if 'chat_id' not in request.query_params:
            return JSONResponse({"status":400,"message":"chat_id not present"})
        
        chat_id = int(request.query_params["chat_id"])

        title = "Nothing is playing at the moment"
        if 'now_playing_title' in application.bot_data[chat_id]:
            if len(application.bot_data[chat_id]['now_playing_title']) > 0:
                title = application.bot_data[chat_id]['now_playing_title']

        return JSONResponse({"title":title})
        
    async def spotify_callback(request: Request) -> PlainTextResponse:
        """
        This function handles the callback from spotify when authorizing request to an account

        A typical request will like like the following

        GET /spotify?code=AQB5sDUcKql9oULl10ftgo9Lhmyzr3lpMRQl7i65drdM4WGaWvfx9ANBcUXVg-yR1FAqS2_yINbf6ej41lNr9ghmBGik0Bjwcgf90yxYLgk_H5c_ZcV2AKz9-eiqsnjZxqVoJyWqc5LnRHn0aEGG8YwBsk5ZHKIQh82uHikDZyAxKSCdLGCIaPbQtNUR0ej8WZH1y_gg_YOJa5aoC-f4ODJYOrokOTolxnlEy3zaJvddOgCF_GC8Fd9upxmovV5JR8LfACvrurjGYW7MaGeDKWMCb29GNXtg3lovTh2rwzE HTTP/1.0
        """

        logging.info("Got callback from spotify")

        if 'code' not in request.query_params:
            logging.error("no code in response from spotify")
            # callback without code
            return Response()

        code = request.query_params["code"]
        if not re.search("^[A-Za-z0-9\-\_]+$",code):
            logging.warning("authorisation code does not match regex")
            return Response()

        state = request.query_params["state"]
        if not re.search("^[0-9A-Za-z\-]+",state):
            logging.warning("state parameter does not match regex")
            return Response()

        try:
            state = base64.b64decode(state.encode('ascii')).decode('ascii')        
            [chatid, userid] = state.split(':')
            chatid = int(chatid)            
            userid = int(userid)
        except:
            logging.error("Failure during state query parameter parsing")
            return Response()

        logging.info(f"Spotify callback for {chatid} {userid} with code {code}")

        try:
            auth_manager = await spotifyhelper.get_auth_manager(chatid)                               
            if auth_manager is not None:
                auth_manager.get_access_token(code)                    
                await userhelper.set_group_owner(chatid, userid)
                await application.bot.send_message(
                    chat_id=userid,
                    text=f"Spotify connected to the chat. All revenues of requested tracks are coming your way. Execute the /decouple command in the group to remove the authorisation.")
        except Exception as e:            
            logging.error(e)
            logging.error("Failure during auth_manager instantiation")
            return Response()

        return Response("""    
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Authorisation succesfull!</title>
  <link rel="stylesheet" href="/jukebox/assets/JukeboxBot.css">
</head>
<body>
  <div class="container">
    <div class="image-container">
      <div class="image-content">
        <img src="/jukebox/assets/auth_success.png" alt="JukeboxBot" />
    </div>
  </div>
</body>
</html>    
""") 
    
    async def lnbits_lnurlp_callback(request: Request) -> PlainTextResponse:
        """
        Callback from LNbits when a wallet is funded. Send a message to the telegram user

        The callback is a POST request with a userid parameter in the URL

        The body content should be similar to the following

        {"payment_hash": "6d0734e641013e56767c6d8e4c0d02e6db0ddfdaa35cc370320e0cafb66565e7", "payment_request": "lnbc210n1p3l7k46pp5d5rnfejpqyl9vanudk8ycrgzumdsmh765dwvxupjpcx2ldn9vhnshp58aq3spsfd2263qpprxz7pqvhqm03mf2np6n9j8v0fpu7zqma94dqcqzpgxqzjcsp52d0hk2pzn75ugwzxev8tnf8xype05eaeadpmmv6rg72zchm649xs9qyyssqptzl7wwwkkjmaggr4s0m6gghmtla0uv38036m9py535ezng3pmxne69tw5p99f4e2vv4kqpwyx2kle6yn2xw4qes5268j97d3ycn97gp954uw3", "amount": 21000, "comment": null, "lnurlp": "2bP7Xn", "body": ""}'

        """
        tguserid = request.query_params['userid']
        if re.search("^[0-9]+$",tguserid):            
            obj = json.loads(await request.body())
            amount = int(obj['amount'] / 1000)            
            await application.bot.send_message(
                chat_id=int(tguserid),
                text=f"Received {amount} sats. Type /stack to view your sats stack.")
        return Response()

    async def web_home(request: Request) -> PlainTextResponse:
        chat_id = request.path_params['chat_id']

        
        
        if chat_id is None:
            return HTMLResponse("Incomplete request")

        try:
            chat_id = int(chat_id)
        except:
            return HTMLResponse("Incomplete request")


        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'web' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['web'] = True
        if application.bot_data[chat_id]['web'] == False:
            return HTMLResponse("Web access disabled")
        
        
        # get spotify auth manager 
        sp = await spotifyhelper.get_sp(chat_id)                               
        if sp is None:
            return HTMLResponse("Incomplete request")

        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <title>Add Music</title>
    <link rel="stylesheet" href="/jukebox/assets/JukeboxBot.css?4689">
  </head>
  <body>
    <form onsubmit="submitSearch(); return false;" action="">
      <div class="container">
        <div class="image-container">
          <div class="image-content">
            <img src="/jukebox/assets/jukeboxbot_addmusic.png" alt="JukeboxBot" />
            <div class="search-container">
              <input name="query" value="" placeholder="Enter song title">
            </div>
            <button class="search-button" type="submit" aria-label="Search"></button>
            <div class="search-results-container">
              <div class="search-result-container"></div>
              <div class="search-result-container"></div>
              <div class="search-result-container"></div>
              <div class="search-result-container"></div>
              <div class="search-result-container"></div>
            </div>
          </div>
        </div>
      </div>
    </form>
  </body>
  <script>
   const req1 = new XMLHttpRequest();	
   req1.onreadystatechange = function() {{
      if (this.readyState == 4 && this.status == 200) {{
        obj = JSON.parse(this.responseText);
        if ( obj.status == 200 ) {{
          var nodes = document.querySelectorAll(".search-result-container");
          for(let i=0;(i<obj.results.length);i++) {{
            nodes[i].innerText = obj.results[i].title;
            nodes[i].onclick = function(){{
              req2.open("GET","/jukebox/web/{chat_id}/add?track_id=" + obj.results[i].track_id);
              req2.setRequestHeader("Accept", "application/json");	
              req2.setRequestHeader("Content-Type", "application/json"); 
              req2.send();             
            }};
          }}
        }}
      }}
   }};

   const req2 = new XMLHttpRequest();
   req2.onreadystatechange = function() {{        
      if (this.readyState == 4 && this.status == 200) {{
        obj = JSON.parse(this.responseText);
        if ( obj.status == 200 ) {{
          window.location.href = obj.payment_url;
        }}
      }}
   }};

  function submitSearch() {{  
    req1.open("POST", "/jukebox/web/{chat_id}/search"); 
    req1.setRequestHeader("Accept", "application/json");	
    req1.setRequestHeader("Content-Type", "application/json"); 
    req1.send(JSON.stringify({{query:document.getElementsByName("query")[0].value}}));
  }}  

  </script> </html>""")

    

    
    async def web_search(request: Request) -> JSONResponse:
        chat_id = request.path_params['chat_id']
        print("Web search")
        
        # validate that chat_id is present
        if chat_id is None:
            return JSONResponse({"status":400,"message":"Incomplete request. Chat ID is None"})
        
        # validate that chat_id is a digit
        try:
            chat_id = int(chat_id)
        except:
            return JSONResponse({"status":400,"message":"Incomplete request. Failed to cast chat_id"})


        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'web' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['web'] = True
        if application.bot_data[chat_id]['web'] == False:
            return JSONResponse({"status":400,"message":"Web access disabled"})
        
        
        # get form        
        form = await request.json()
        
        print("Web search for ",form)
        # validate that form is present
        if form is None:
            return JSONResponse({"status":400,"message":"Incomplete request form is None"})

        # get query
        query = form["query"]

        # validate that query is present
        if query is None:
            return JSONResponse({"status":400,"message":"Incomplete request query is None"})

        # validate allow characters in query
        if not re.search("^[A-Za-z0-9 ]+$",query):
            return JSONResponse({"status":400,"message":"Incomplete request query is invalid"})
            
        # get spotify auth manager 
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:
            return JSONResponse({"status":400,"message":"Incomplete request, sp is None"})
            
        # search for tracks
        numtries: int = 3
        while numtries > 0:
            numtries -= 1
            try:
                result = sp.search(query)
            except spotipy.exceptions.SpotifyException:
                if numtries == 0:
                    logging.error("Spotify returned and exception, not returning search result")
                    return JSONResponse({"status":400,"message":"Search currently unavailable"})
                logging.warning("Spotify returned and exception, retrying")
                continue
            break

        # create a list of max five items
        if len(result['tracks']['items']) == 0:
            return JSONResponse({"status":200,"results":[]})
        
        tracktitles  = {}
        results = []

        for item in result['tracks']['items']:            
            title = spotifyhelper.get_track_title_from_item(item)
            track_id = item['uri']
            result = re.search("^spotify:track:([A-Z0-9a-z]+)$",track_id)
            if not result:
                continue

            # strip spotify from the track id
            track_id = result.groups()[0]

            if title not in tracktitles:
                tracktitles[title] = 1
                results.append({"title":title,"track_id":track_id})
                                
                # max five suggestions
                if len(results) == 5:
                    break

        return JSONResponse({"status":200,"results":results})


    async def web_add(request: Request) -> JSONResponse:
        chat_id = request.path_params['chat_id']
        track_id = request.query_params['track_id']        

        # validate that chat_id is present
        if chat_id is None:
            logging.info("chat_id is NULL")
            return JSONResponse({"status":400,"message":"Incomplete request"})

        # validate that chat_id is a digit
        try:
            chat_id = int(chat_id)
        except:
            logging.warning("chat_id is not an integer")
            return JSONResponse({"status":400,"message":"Incomplete request"})

        if not chat_id in application.bot_data:
            application.bot_data[chat_id] = {}
        if not 'web' in application.bot_data[chat_id]:
            application.bot_data[chat_id]['web'] = True
        if application.bot_data[chat_id]['web'] == False:
            return JSONResponse({"status":400,"message":"Web access disabled"})

        

        # validate track_id is not None
        if track_id is None:
            logging.info("track_id is None")
            return JSONResponse({"status":400,"message":"Incomplete request"})


        # vaidate track id is spotify track
        #spotify:track:1532ejaMFnQPcHD9BAeqwr
        if not re.search("^[A-Za-z0-9]+$",track_id):
            logging.warning("track_id does not match regular expression")
            return JSONResponse({"status":400,"message":"Incomplete request"})


        # add spotify prefix to the track_id
        track_id = f"spotify:track:{track_id}"

        # get spotify auth manager
        sp = await spotifyhelper.get_sp(chat_id)
        if not sp:
            return JSONResponse({"status":400,"message":"Incomplete request, sp is None"})

        track = sp.track(track_id)        
        track_len = track['duration_ms'] / 1000
        
        amount_to_pay = int(await spotifyhelper.get_price(chat_id))
        if ( track_len > 600 ):
            amount_to_pay = 10 * amount_to_pay
#        if ( track_len > 1800 ):
#            amount_to_pay = 10 * amount_
#        elif ( track_len > 600 ):
#            amount_to_pay = amount_to_pay * 1.0166428 ** (track_len - 300)
        recipient = await userhelper.get_group_owner(chat_id)
        invoice_title = f"'{spotifyhelper.get_track_title_from_item(track)}'"
        invoice = await invoicehelper.create_invoice(recipient, amount_to_pay, invoice_title)
        if invoice is None:
            return JSONResponse({"status":400,"message":"Payments not available"})


        invoice.user = User(80,"Web user")
        invoice.title = invoice_title
        invoice.recipient = recipient   
        logging.info("recipient when creating invoice: " + recipient.toJson())
        invoice.spotify_uri_list = [ track_id ]
        invoice.title = invoice_title
        invoice.chat_id = chat_id
        invoice.amount_to_pay = amount_to_pay

        # save the invoice
        await invoicehelper.save_invoice(invoice)

        application.job_queue.run_once(check_invoice_callback, 15, data = invoice)

        return JSONResponse({"status":200,"payment_url":f"https://{settings.domain}/jukebox/payinvoice?payment_hash={invoice.payment_hash}"});
    

    starlette_app = Starlette(
        routes=[
            Route(f"/jukebox/telegram", telegram, methods=["GET","POST"]),
            Route("/jukebox/lnbitscallback", lnbits_lnurlp_callback, methods=["POST"]),
            Route("/spotify", spotify_callback, methods=["GET"]),
            Route("/jukebox/payinvoice",payinvoice_callback, methods=["GET"]),
            Route("/jukebox/invoicecallback",invoicepaid_callback, methods=["POST"]),
            Route("/jukebox/status.json",jukebox_status, methods=["GET"]),
            Route("/jukebox/fund",jukebox_fund, methods=["GET"]),
            Route("/jukebox/web/{chat_id}",web_home, methods=["GET"]),
            Route("/jukebox/web/{chat_id}/search",web_search, methods=["POST"]),
            Route("/jukebox/web/{chat_id}/add",web_add, methods=["GET"]),
            Route("/jukebox/api/{chat_id}/status",web_api_status,methods=["GET"]),   # get current status
            Route("/jukebox/api/{chat_id}/playlist",web_api_playlist,methods=["GET"]),   # get current status
            Route("/jukebox/api/{chat_id}/search",web_api_search,methods=["POST"]),   # search for a track
            Route("/jukebox/api/{chat_id}/request",web_api_request,methods=["POST"]),   # request a track
            Route("/jukebox/api/{chat_id}/payment/{payment_hash}",web_api_check_payment,methods=["GET"]),   # check payment status
        ]
    )

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=starlette_app,
            port=settings.port,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
