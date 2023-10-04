#!/usr/bin/env python
import asyncio
import re
import os
import base64
import json
from time import time
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus
import redis
import random
import string
import telegramhelper
from telegramhelper import TelegramCommand
import statshelper
import qrcode
from PIL import Image
import asyncio_mqtt as aiomqtt

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
        f"This script is not compatible with your current PTB version {TG_VER}. "
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
    filters    
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

jukeboxtexts.init()
settings.init()

# the local cache of messages that disply current playing track
now_playing_message = {}

# message debouncer to prevent processing the same message twice
message_debounce = {}

def adminonly(func):
    """
    This decorator function manages that only admin in a group chat are allowed to execute the function
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:        
        admin = False
        if update.message.chat.type == "private":
            admin = True
        else:
            for member in await context.bot.get_chat_administrators(update.message.chat.id):
                if member.user.id == update.effective_user.id and member.status in ['administrator','creator']:
                    admin = True            
        if admin == True:
            await func(update, context)
            return

        # say to user to go away
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=jukeboxtexts.you_are_not_admin)
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})
        return
        

            
    return wrapper

def debounce(func):
    """
    This decorator function manages the debouncing of message when executing commands
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # debounce to prevent the same message being processed twice
        if update.effective_chat.id in message_debounce and update.message.id <= message_debounce[update.effective_chat.id]:
            logging.info("Message bounced")
            return wrapper
        else:
            message_debounce[update.effective_chat.id] = update.message.id
            await func(update, context)

            # delete the command from the user
            try:
                await update.message.delete()
            except:
                logging.warning("Failed to delete message")
            
    return wrapper

# delete telegram messages
# This function is used in callbacks to enable the deletion of messages from users or the bot itself after some time
async def delete_message(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.job.data['message'].delete()
    except:
        logging.warning("Could not delete message")
        
# start command handler, returns help information
@debounce
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # send the message
    message = await context.bot.send_message(chat_id=update.effective_chat.id,text=jukeboxtexts.help)

    
    # only create a callback to delete the message when not in a private chat
    if update.message.chat.type != "private":
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})

# display stats
@debounce
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    userid: int = update.effective_user.id

    if update.message.chat.type != "private":
        return

    if userid not in settings.superadmins:
        logging.info(f"User {userid} is not a superadmin. Access to stats denied")
        return
    
    results = await statshelper.get_jukebox_groups()
    balance = await statshelper.get_bot_stack()

    statsText = f"Bot balance: {balance} sats \n"

    statsText += f"Number of groups: {results['numgroups']}. List of owners: \n"
    for group in results['group']:
        if group['owner'] is not None:
            statsText += f" - {group['groupid']} : @{group['owner'].username}\n"
        else:
            statsText += f" - {group['groupid']} : Unknown owner\n"
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=statsText)
    


# get the current balance
@debounce
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # do not show balance in other than private chats
    if update.message.chat.type != "private":
        bot_me = await context.bot.get_me()
        
        # direct the user to their private chat
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=jukeboxtexts.balance_in_group,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
            ]))

        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return

    # we're in a private chat now
    user = await userhelper.get_or_create_user(update.effective_user.id,update.effective_user.username)

    # get the balance from LNbits
    balance = await userhelper.get_balance(user)
    
    # create a message with the balance
    logging.info(f"User {user.userid} balance is {balance} sats")
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Your balance is {balance} sats.")


# Disconnect a spotify player from the bot, the connect command
@debounce
@adminonly
async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # this command can only be used in group chats, send instructions if used in a private chat
    if update.message.chat.type == "private":
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=jukeboxtexts.disconnect_in_private_chat)
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})        

        # stop here
        return

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
    sps = await spotifyhelper.get_spotify_settings(update.effective_user.id)    
    
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


# display the play queue
@debounce
@adminonly
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type == "private":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"The /price command only works in a group chat.")
        return

    price = await spotifyhelper.get_price(update.effective_chat.id)
    donation = await spotifyhelper.get_donation_fee(update.effective_chat.id)
    
    if update.message.text == '/price':
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=f"Current track price is {price}. Per requested track, {donation} sats is donated to the Jukebox Bot.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return


    # parse and validate the price command
    result = re.search("/price\s+([0-9]+)\s+([0-9]+)$",update.message.text)
    if result is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Use command as follows: /price <price> <donation>\n<price> is the track price in sats\n<donation> is the amount in sats donated to the bot per reqested track. The donation is substracted from the track price.")
        return

    newprice = int(result.groups()[0])
    newdonation = int(result.groups()[1])

    if newdonation > newprice:
        newdonation = newprice

    # update 
    await spotifyhelper.set_price(update.effective_chat.id, newprice)
    await spotifyhelper.set_donation_fee(update.effective_chat.id, newdonation)
        
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Updating price to {newprice} sats. Donation amount is {newdonation} sats.")
    
    context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':update.message})

# display the play queue
@debounce
async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type == "private":
        message = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Execute the /queue command in the group instead of the private chat.")
        return
    
    # get an auth managher, if no auth manager is available, dump a message
    auth_manager = await spotifyhelper.get_auth_manager(update.effective_chat.id)
    if auth_manager is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Bot not connected to player. The admin should perform the /couple command to authorize the bot.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return

    # create spotify instance
    try:
        sp = spotipy.Spotify(auth_manager=auth_manager)
    except:
        message = await context.bot.send_message(chat_id=update.effective_chat.id,text="Failed to connect to music player")    
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return
    
    # get the current track
    try:
        track = sp.current_user_playing_track()
    except:
        track = None
        
    title = "Nothing is playing at the moment"    
    if track:                    
        title = "🎵 {title} 🎵".format(title=spotifyhelper.get_track_title(track['item']))
    
    # query the queue
    try:
        result = sp.queue()
    except:
        message = await context.bot.send_message(chat_id=update.effective_chat.id,text="Failed to retrieve queue")    
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return
        
    text = ""
    for i in range(min(10,len(result['queue']))):
        item = result['queue'][i]       
        text += " {count}. {title}\n".format(count=(i+1),title=spotifyhelper.get_track_title(item))

    if len(text) == 0:
        text = title + "\nNo items in queue."
    else:
        text = title + "\nUpcoming tracks:\n" + text
            
    message = await context.bot.send_message(chat_id=update.effective_chat.id,text=text)    
    context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})

@debounce
@adminonly
async def service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This command sends a service message to all Jukebot group owners, that is all users marked as owner of a group
    """
    userid: int = update.effective_user.id

    if update.message.chat.type != "private":
        return

    if userid not in settings.superadmins:
        logging.info(f"User {userid} is not a superadmin. Access to stats denied")
        return

    result = re.search("^/service \S.*",update.message.text)
    if result is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
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

            
    
    
    
# connect a spotify player to the bot, the setclient secret and set client id commands
@debounce
async def spotify_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        bot_me = await context.bot.get_me()
               
        # direct the user to their private chat
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Like keeping your mnenomic seedphrase offline, it is better to perform these actions in a private chat with me.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
            ]))

        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return
    
    # get spotify settings for the user
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
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if update.message.chat.type == "private":
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Execute the /history command in the group instead of the private chat.")
        return
    
    # get an auth managher, if no auth manager is available, dump a message
    auth_manager = await spotifyhelper.get_auth_manager(update.effective_chat.id)
    if auth_manager is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Bot not connected to player. The admin should perform the /couple command to authorize the bot.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return

    # create spotify instance
    sp = spotipy.Spotify(auth_manager=auth_manager)
        
    text = "Track history:\n"
    history = await spotifyhelper.get_history(update.effective_chat.id,20)
    for title in history:
        text += f"{title}\n"
        
    message = await context.bot.send_message(chat_id=update.effective_chat.id,text=text)    
    context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
    
# get lndhub link for user
@debounce
async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # create a message tyo do this in a private chat
    if update.message.chat.type != "private":
        bot_me = await context.bot.get_me()
    
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Like keeping your mnenomic seedphrase offline, it is better to request your lndhub link in a private chat with me.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
            ]))

        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return

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
@debounce
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This function searches for tracks in spotify and createas a list of tracks to play
    If a playlist URL is provided, that playlist is used
    This function only works in a group chat
    """
    
    if update.message.chat.type == "private":
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Execute the /add command in the group instead of the private chat.")
        return
    
    # get an auth manager, if no auth manager is available, dump a message
    auth_manager = await spotifyhelper.get_auth_manager(update.effective_chat.id)
    if auth_manager is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Bot not connected to player. The admin should perform the /couple command to authorize the bot.")
        return

    # create spotify instance
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    # validate the search string
    searchstr = update.message.text.split(' ',1)
    if len(searchstr) > 1:
        searchstr = searchstr[1]
    else:
        message = await context.bot.send_message(chat_id=update.effective_chat.id,text=jukeboxtexts.add_command_help)
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
        return

    # check if the search string is a spotify URL
    match = re.search('https://open.spotify.com/playlist/([A-Za-z0-9]+).*$',searchstr)
    if (match):
        playlistid = match.groups()[0]
        result = sp.playlist(playlistid,fields=['name'])
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"@{update.effective_user.username} suggests to play tracks from the '{result['name']}' playlist.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Pay {await spotifyhelper.get_price(update.effective_chat.id)} sats for a random track", callback_data = telegramhelper.add_command(TelegramCommand(0,telegramhelper.playrandom,playlistid)))
            ]]))

        # start a job to kill the message  after 30 seconds if not used
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_long, data={'message':message})
        return

    # search for tracks
    numtries: int = 3
    while numtries > 0:
        try:
            result = sp.search(searchstr)
        except spotipy.oauth2.SpotifyOauthError:
            # spotify not properly authenticated
            logging.info("Spotify Oauth error")
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Music player not available, search aborted.")
            return
            
        except spotipy.exceptions.SpotifyException:
            numtries -= 1
            if numtries == 0:
                # spotify still triggers an exception
                logging.error("Spotify returned and exception, not returning search result")
                message = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Music player unavailable, search aborted.")
                return
            logging.warning("Spotify returned and exception, retrying")
            continue
        
        break

    # create a list of max five buttons, each with a unique song title
    if len(result['tracks']['items']) > 0:
        tracktitles  = {}
        button_list = []
        for item in result['tracks']['items']:            
            title = spotifyhelper.get_track_title(item)
            if title not in tracktitles:
                tracktitles[title] = 1
                button_list.append([InlineKeyboardButton(title, callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.add,item['uri'])))])
                
                # max five suggestions
                if len(tracktitles) == 5:
                    break

        # Add a cancel button to the list
        button_list.append([InlineKeyboardButton('Cancel', callback_data = telegramhelper.add_command(TelegramCommand(update.effective_user.id,telegramhelper.cancel,None)))])

        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Results for '{searchstr}'",
            reply_markup=InlineKeyboardMarkup(button_list))

        # start a job to kill the search window after 30 seconds if not used
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_medium, data={'message':message})
    else:
        message = await context.bot.send_message(chat_id=update.effective_chat.id,text=f"No results for '{searchstr}'")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})


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
    logging.info("callback_paid_invoice")
    logging.info(invoice.toJson())
    
    if invoice.chat_id is None:
        logging.error("Invoice chat_id is None")
        return

    if await invoicehelper.delete_invoice(invoice.payment_hash) == False:
        logging.info("invoicehelper.delete_invoice returned False")
        return

    
    auth_manager = await spotifyhelper.get_auth_manager(invoice.chat_id)
    if auth_manager is None:
        logging.error("No auth manager after succesfull payment")
        return


    
    try:
        logging.info(f"Trying to delete chat_id {invoice.chat_id}, messageid {invoice.message_id}")
        await application.bot.delete_message(invoice.chat_id,invoice.message_id)            
    except:
        pass


    # add to the queue and inform others
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    spotifyhelper.add_to_queue(sp, invoice.spotify_uri_list)
    try:
        await application.bot.send_message(
            chat_id=invoice.chat_id,
            parse_mode='HTML',
            text=f"'{invoice.title}' was added to the queue.")
    except:
        logging.error("Could not  send message to the group that track was added to the queue")
    
    try:
        async with aiomqtt.Client("localhost") as client:
            await client.publish(f"{invoice.chat_id}/added_to_queue", payload=invoice.title)
    except:
        logging.error("Exception when publishing queue add to mqtt")
        pass
        

    if False:
        try:
            await application.bot.send_message(
                chat_id=invoice.user.userid,
                parse_mode='HTML',
                text=f"You paid {invoice.amount_to_pay} sats for {invoice.title}.")
        except:
            logging.info("Could not send individual message to user that")
    
     # make donation to the bot
    jukeboxbot = await userhelper.get_or_create_user(settings.bot_id)
    donator = await userhelper.get_or_create_user(invoice.recipient.userid)
    donation_amount : int = await spotifyhelper.get_donation_fee(invoice.chat_id)
    donation_amount = min(donation_amount,invoice.amount_to_pay)
    donation_invoice = await invoicehelper.create_invoice(jukeboxbot, donation_amount, "donation to the bot")
    result = await invoicehelper.pay_invoice(donator, donation_invoice)

    return


@debounce
async def web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    return the Web URL where tracks can be requested using a browser
    """
    userid: int = update.effective_user.id

    if update.message.chat.type == "private":
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
    for chatid in list(now_playing_message.keys()):
        del now_playing_message[chatid]

    telegramhelper.purge_commands()


async def callback_spotify(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This function creates a message of the current playing track. It reschedules itself depending on the remaining time
    for the current playing track. Basically two seconds after the time, the first track has finished playing
    """

    interval = 300
    try:
        for key in settings.rds.scan_iter("group:*"):
            #logging.info(f"callback_spotify for group {key}")
            chat_id = key.decode('utf-8').split(':')[1]
            auth_manager = await spotifyhelper.get_auth_manager(chat_id)
            if auth_manager is None:
                #logging.warning("Auth manager is None in callback_spotify")
                continue

            currenttrack = None
            try:
                sp = spotipy.Spotify(auth_manager=auth_manager)
                currenttrack = sp.current_user_playing_track()
            except:
                #logging.info("Exception while querying the current playing track at spotify")
                continue
            
            title = "Nothing playing at the moment"
            if currenttrack is not None and 'item' in currenttrack and currenttrack['item'] is not None:
                title = spotifyhelper.get_track_title(currenttrack['item'])

                # update history
                await spotifyhelper.update_history(chat_id, title)

                newinterval  = ( currenttrack['item']['duration_ms'] - currenttrack['progress_ms'] ) / 1000 + 2
                if newinterval < interval:
                    interval = newinterval
            elif currenttrack is not None:
                logging.info(json.dumps(currenttrack))

            # update the title
            if chat_id in now_playing_message:
                [message_id, prev_title] = now_playing_message[chat_id]
                if prev_title != title:                    
                    try:
                        await context.bot.editMessageText(title,chat_id=chat_id,message_id=message_id)
                        now_playing_message[chat_id] = [ message_id, title ]
                        logging.info(f"Now playing {title} in chat {chat_id}")
                    except:
                        #logging.error("Exception when refreshing now playing")
                        pass

                    try:
                        async with aiomqtt.Client("localhost") as client:
                            await client.publish(f"{chat_id}/now_playing", payload=title)
                    except:
                        logging.error("Exception when publishing current track to mqtt")
                        pass

            else:
                logging.info("Creating new pinned message")
                try:
                    message = await context.bot.send_message(text=title,chat_id=chat_id)
                    await context.bot.pin_chat_message(chat_id=chat_id, message_id=message.id)
                    now_playing_message[chat_id] = [ message.id, title ]
                except:
                    logging.error("Exception when sending message to group")
    except:
       logging.error("Unhandled exception in callback_spotify")             
    finally:
        if interval < 30 or interval > 300:
            interval = 30
        logging.info(f"Next run in {interval} seconds")
        context.job_queue.run_once(callback_spotify, interval, job_kwargs = {'misfire_grace_time':None})
                                                               
        
#callback for button presses
async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This functions handles all button presses that are fed back into the application
    """
    key = update.callback_query.data

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
    
    if command.command == telegramhelper.cancelinvoice:
        await update.callback_query.delete_message()

        invoice = command.data
        if invoice is not None:
            await invoicehelper.delete_invoice(invoice.payment_hash)
        return


    # the commands from here on modify a list of tracks to be queue
    # and we have to check hat we have spotify available
    # get an auth managher, if no auth manager is available, dump a message    
    auth_manager = await spotifyhelper.get_auth_manager(update.effective_chat.id)
    if auth_manager is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Bot not connected to player. The admin should perform the /couple command to authorize the bot.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
        return

    # create spotify instance
    sp = spotipy.Spotify(auth_manager=auth_manager)

    # verify that player is available, otherwise it has no use to queue a track
    track = sp.current_user_playing_track()
    if track is None:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text="Player is not active at the moment. Payment aborted.")
        context.job_queue.run_once(delete_message, settings.delete_message_timeout_short, data={'message':message})        
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
    else:
        logging.error(f"Unknown command: {command.command}")
        return

    # validate payment conditions
    payment_required = True
    amount_to_pay = int((await spotifyhelper.get_price(update.effective_chat.id)) * len(spotify_uri_list))    
    logging.info(f"Amount to pay = {amount_to_pay}")
    if amount_to_pay == 0:
        payment_required = False
            
    # if no payment required, add the tracks to the queue one by one
    if payment_required == False:
        spotifyhelper.add_to_queue(sp, spotify_uri_list)

        for uri in spotify_uri_list:

            tracktitle = spotifyhelper.get_track_title(sp.track(uri))
            
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    parse_mode='HTML',
                    text=f"@{update.effective_user.username} added '{tracktitle}' to the queue.")
            except:
                pass

            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    parse_mode='HTML',
                    text=f"You added '{tracktitle}' to the queue for {amount_to_pay} sats.")
            except:
                pass

            try:
                async with aiomqtt.Client("localhost") as client:
                    await client.publish(f"{update.effective_chat.id}/added_to_queue", payload=tracktitle)
            except:
                logging.error("Exception when publishing queue add to mqtt")
                pass
        

            
        # return 
        return
        
    # create an invoice title
    invoice_title = f"'{spotifyhelper.get_track_title(sp.track(spotify_uri_list[0]))}'"
    for i in range(1,len(spotify_uri_list)):
        title += f",'{spotifyhelper.get_track_title(sp.track(spotify_uri_list[0]))}'"

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

    # pay the invoice
    payment_result = await invoicehelper.pay_invoice(invoice.user, invoice)

    # if payment success
    if payment_result['result'] == True:
        spotifyhelper.add_to_queue(sp, spotify_uri_list)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            parse_mode='HTML',
            text=f"@{update.effective_user.username} added {invoice_title} to the queue.")
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                parse_mode='HTML',
                text=f"You paid {amount_to_pay} sats for {invoice_title}.")
        except:
            logging.info("Could not send message to user")

            
        try:
            async with aiomqtt.Client("localhost") as client:
                await client.publish(f"{update.effective_chat.id}/added_to_queue", payload=invoice_title)
        except:
            logging.error("Exception when publishing queue add to mqtt")
            pass

            
        # make donation to the bot
        jukeboxbot = await userhelper.get_or_create_user(settings.bot_id)
        donation_amount : int = await spotifyhelper.get_donation_fee(invoice.chat_id)
        donation_amount = min(donation_amount,invoice.amount_to_pay)
        donation_invoice = await invoicehelper.create_invoice(jukeboxbot, donation_amount, "donation to the bot")
        result = await invoicehelper.pay_invoice(recipient, donation_invoice)

        return

    # store the invoice in a list of open invoices        
    # add extra data 
    
    # we failed paying the invoice, popup the lnurlp
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"@{update.effective_user.username} add '{invoice_title}' to the queue?\n\nClick to pay below or fund the bot with /fund@Jukebox_Lightning_bot.",       
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
    application = (
        Application.builder().token(settings.bot_token).updater(None).build()
    )
 
    # register handlers
    application.add_handler(CommandHandler('add', search))  # search for a track
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
    application.add_handler(CommandHandler('web', web))  # display the web URL

    application.add_handler(CallbackQueryHandler(callback_button))
    application.job_queue.run_repeating(regular_cleanup, 12 * 3600)
    application.job_queue.run_once(callback_spotify, 2)



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
</body>
</html>
""")
    
    async def jukebox_status(request: Request) -> PlainTextResponse:
        if 'chat_id' not in request.query_params:
            return Response("{}",media_type="application/json")
        
        chat_id = request.query_params["chat_id"]

        auth_manager = await spotifyhelper.get_auth_manager(chat_id)                               
        if auth_manager is None:
            return PlainTextResponse("""{
                "title":"Nothing is playing at the moment."
            }""", media_type="application/json")

        # create spotify instance
        sp = spotipy.Spotify(auth_manager=auth_manager)

        # get the current track
        track = sp.current_user_playing_track()
        title = "Nothing is playing at the moment"
        if track:
            title = spotifyhelper.get_track_title(track['item'])
        return PlainTextResponse(f'{{"title":"{title}"}}',media_type="application/json")

    async def spotify_callback(request: Request) -> PlainTextResponse:
        """
        This function handles the callback from spotify when authorizing request to an account
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

        # get spotify auth manager 
        auth_manager = await spotifyhelper.get_auth_manager(chat_id)                               
        if auth_manager is None:
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
        auth_manager = await spotifyhelper.get_auth_manager(chat_id)                               
        if auth_manager is None:
            return JSONResponse({"status":400,"message":"Incomplete request auth manager is None"})
            
        # create spotify instance
        sp = spotipy.Spotify(auth_manager=auth_manager)
        if sp is None:
            return JSONResponse({"status":400,"message":"Incomplete response sp is none"})

        # search for tracks
        numtries: int = 3
        while numtries > 0:
            try:
                result = sp.search(query)
            except spotipy.exceptions.SpotifyException:
                numtries -= 1
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
            title = spotifyhelper.get_track_title(item)
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
        auth_manager = await spotifyhelper.get_auth_manager(chat_id)                               
        if auth_manager is None:
            logging.warning("Auth_manager is NULL")
            return JSONResponse({"status":400,"message":"Incomplete request"})

       
        # create spotify instance
        sp = spotipy.Spotify(auth_manager=auth_manager)

        if sp is None:
            logging.warning("sp is None")
            return JSONResponse({"status":400,"message":"Incomplete request"})


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
        invoice_title = f"'{spotifyhelper.get_track_title(track)}'"
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
            Route("/jukebox/web/{chat_id}/add",web_add, methods=["GET"])
            
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
