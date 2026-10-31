import random
import string
import logging
import settings
from time import time
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ContextTypes
)
import jukeboxtexts

arf = {}

playrandom = 'PLAYRANDOM'
add = 'ADD'
cancel = 'CANCEL'
cancelinvoice = 'CANCELINVOICE'
upvote= 'UPVOTE'

# message debouncer to prevent processing the same message twice
message_debounce = {}


class TelegramCommand:
    def __init__(self, userid, command, data = None):
        self.userid = userid
        self.command = command
        self.data = data
        self.time = time()

def add_command(command: TelegramCommand) -> str:
    key = "".join(random.sample(string.ascii_letters,12))
    arf[key] = command
    return key

def get_command(key: str) -> TelegramCommand:
    if key in arf:
        command = arf[key]
        return command
    else:
        return None
    
def purge_commands() -> None:
    now = time()
    for key in list(arf.keys()):
        if now - arf[key].time > 3600: # 60 minutes
            logging.info("Deleting command from cache")
            del arf[key]


def group_chat_only(func):
    """
    This decorator function controls that the function is only available in group chats
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:        
        if update.message.chat.type == "private":
            await send_telegram_message(
                context=context,
                chat_id=update.effective_chat.id,
                text='This command is only available in group chats',
                delete_timeout=settings.delete_message_timeout_short)
        else:
            await func(update, context)

    return wrapper

def private_chat_only(func):
    """
    This decorator function controls that the function is only available in private chats
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:        
        if update.message.chat.type != "private":
            bot_me = await context.bot.get_me()
            await send_telegram_message(
                context=context,
                chat_id=update.effective_chat.id,
                text='This command is only available in private chats',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Take me there",url=f"https://t.me/{bot_me.username}")]
                ]),
                delete_timeout=settings.delete_message_timeout_short)

        else:
            await func(update, context)

    return wrapper
            
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
        pass

def auto_delete_message(context, message, timeout):
    context.job_queue.run_once(delete_message, timeout, data={'message':message})

async def send_telegram_message(context, chat_id, text, delete_timeout=0,reply_markup=None,parse_mode='HTML'):
    message = await context.bot.send_message(
        chat_id=chat_id,
        parse_mode=parse_mode,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )
    if delete_timeout > 0:
        context.job_queue.run_once(delete_message, delete_timeout, data={'message':message})
