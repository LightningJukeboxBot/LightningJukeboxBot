import random
import string
import logging
from time import time

arf = {}

playrandom = 'PLAYRANDOM'
add = 'ADD'
cancel = 'CANCEL'
cancelinvoice = 'CANCELINVOICE'

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