import redis
from redis import RedisError
import asyncio
from lnbits import LNbits
from userhelper import User
import settings
import json
import logging
import qrcode
import os
from time import time

class Invoice:
    def __init__(self, payment_hash, payment_request = None):
        self.payment_hash = payment_hash
        self.payment_request = payment_request
        self.rediskey = f"invoice:{self.payment_hash}"
        self.recipient = None
        self.user = None
        self.amount_to_pay = None
        self.spotify_uri_list = None
        self.title = None
        self.chat_id = None
        self.message_id = None
        self.ttl = 300
    
    def toJson(self):
        userdata = {
            'payment_hash':self.payment_hash,
            'payment_request':self.payment_request,
            'amount_to_pay': self.amount_to_pay,
            'recipient': {
                'userid': self.recipient.userid,
                'username': self.recipient.username
            },
            'user': {
                'userid': self.user.userid,
                'username': self.user.username
            },
            'spotify_uri_list': self.spotify_uri_list,
            'title': self.title,
            'chat_id': self.chat_id,
            'message_id': self.message_id
    
        }
        return json.dumps(userdata)
        
    def loadJson(self, data):
        assert(data is not None)
        data = json.loads(data)
        assert(data is not None)
        assert(data['payment_hash'] == self.payment_hash)

        if self.payment_request is not None:
            assert(self.payment_request == data['payment_request'])
        else:
            self.payment_request = data['payment_request']

        udata = data['recipient']
        self.recipient = User(udata['userid'],udata['username'])
        udata = data['user']
        self.user = User(udata['userid'],udata['username'])
        
        self.amount_to_pay = data['amount_to_pay']
        self.spotify_uri_list = data['spotify_uri_list']
        self.title = data['title']
        self.chat_id = data['chat_id']
        self.message_id = data['message_id']

# Get/Create a QR code and store in filename
async def create_invoice(user: User, amount: int, memo: str) -> Invoice:
    lnbits_invoice = await settings.lnbits.createInvoice(user.invoicekey,amount,memo, None)
    invoice = Invoice(lnbits_invoice['payment_hash'],lnbits_invoice['payment_request'])
    return invoice


async def pay_invoice(user: User, invoice: Invoice):
    assert(user is not None)
    assert(invoice is not None)
    result = await settings.lnbits.payInvoice(invoice.payment_request,user.adminkey)

    if result['result'] == True:
        return {
            'result': True,
            'detail': 'Payment success'
        }
    else:
        retval = {
            'result': False,
            'detail': result['detail']
        }
        return retval

async def save_invoice(invoice: Invoice) -> None:
    settings.rds.set(invoice.rediskey,invoice.toJson())
            
async def delete_invoice(payment_hash: str) -> bool:
    if payment_hash is None:
        logging.error("Delete invoice called with None payment_hash")
        return False
    rediskey = f"invoice:{payment_hash}"
    if settings.rds.delete(rediskey) == 0:
        logging.info("Invoice already deleted")
        return False    
    return True
    
async def invoice_paid(invoice: Invoice) -> bool:
    result = await settings.lnbits.checkInvoice(invoice.recipient.invoicekey,invoice.payment_hash)
    if result == True:
        return True
    else:
        return False


async def get_invoice(payment_hash: str) -> Invoice:
    """
    load invoice from redis
    """
    rediskey = f"invoice:{payment_hash}"
    data = settings.rds.get(rediskey)
    if data is None:
        return None

    invoice = Invoice(payment_hash, None)
    invoice.loadJson(data)
    print(invoice)
    return invoice

