import json
import httpx
import logging

# TODO: verify the LNbits version
# /api/v1/status on v1
# or just an environment variable call to /openapi.json contains version field

class LNbits:
    def __init__(self, protocol, host, admin_adminkey, admin_invoicekey, admin_usrkey):
        self.protocol = protocol
        self.host = host
        self._admin_adminkey = admin_adminkey
        self._admin_invoicekey = admin_invoicekey
        self._admin_usrkey = admin_usrkey

        # do a call to /openapi.json to determine version
        with httpx.Client() as client:
            response = client.get(f"{self.protocol}://{self.host}/openapi.json")
            result = json.loads(response.text)
            version = result['info']['version']
            if version[0] == '0':
                self._pre_version_one = True
            else:
                self._pre_version_one = False
        
        print(f"LNbits initialized with pre one version: {self._pre_version_one}")

                            
    # get balance
    async def getBalance(self, invoicekey):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.protocol}://{self.host}/api/v1/wallet",
                headers = {"X-Api-Key": invoicekey})
            result = json.loads(response.text)
            return int(result['balance']/1000)
    
    # pay an invoice
    async def payInvoice(self, invoice, adminkey):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.protocol}://{self.host}/api/v1/payments",
                json = {"out": True, "bolt11": invoice },
                headers = {"X-Api-Key": adminkey})
            result = json.loads(response.text)
            if "payment_hash" in result:
                result['result'] = True
            else:            
                result['result'] = False
                if result['detail'] ==  'Insufficient balance.': 
                    pass
                elif result['detail'].startswith('(sqlite3.IntegrityError) UNIQUE constraint failed'):
                    result['detail'] = 'Duplicate invoice, payment failed.'
                else:    
                    logging.info(result)
                    result['detail'] = 'Payment failed.'


            return result
        
    # creat an lnbits invoice
    async def createInvoice(self, invoicekey, amount, memo, extra = None):
        payload ={
            "out": False,
            "amount": amount,
            "memo": memo,
            "webhook": "http://127.0.0.1:7000/jukebox/invoicecallback"
        }
        if extra is not None:
            payload['extra'] = extra
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.protocol}://{self.host}/api/v1/payments",
                headers={'X-Api-Key':invoicekey},
                json=payload)
        
            return json.loads(response.text)

    # create a user and initial wallet
    async def createUser(self, name):
        async with httpx.AsyncClient() as client:\
            # TODO: remove usermanager dependency
            # probably /users/api/v1/user
            # requires userid
            # requires to be an admin
            # external_id can be used as reference
            response = await client.post(
                f"{self.protocol}://{self.host}/usermanager/api/v1/users",
                headers={'X-Api-Key':self._admin_adminkey},
                json={
                    "admin_id": self._admin_usrkey,
                    "wallet_name": name,
                    "user_name": name                    
                })
        logging.info(response.text)
        user = json.loads(response.text)
        return user['id']

    # delete user and their wallets
    async def deleteUser(self, lnbitsuserid):
        async with httpx.AsyncClient() as client:
            # TODO: remove usermanager dependency
            # First remove all wallets of the account DELETE /users/api/v1/user/{userid}/wallets
            # Testing learns that deleting a wallet appears to be a bit problematic. Let wallets and users live forever? 
            response = await client.delete(
                f"{self.protocol}://{self.host}/usermanager/api/v1/users/{lnbitsuserid}",
                headers={'X-Api-Key':self._admin_adminkey})
                         
    # create a wallet for a user
    async def createWallet(self, lnbitsuserid, name):
        print("We should not come in the function createWallet")
        return None
        
        async with httpx.AsyncClient() as client:
            # TODO: replace this to remove usermanager dependency
            # Lijkt te werken: POST  to /users/api/v1/user/{user_id}/wallet
            # requires usr of admin in the URL. 
            response = await client.post(
                f"{self.protocol}://{self.host}/usermanager/api/v1/wallets",
                headers={'X-Api-Key':self._admin_invoicekey},
                json={
                    "user_id": lnbitsuserid,
                    "wallet_name": name,
                    "admin_id": self._admin_usrkey
                })
        wallet = json.loads(response.text)
        return wallet

    # enable extension
    async def enableExtension(self, name, lnbitsuserid):
        # enable extension for user wallet
        async with httpx.AsyncClient() as client:
            # TODO rewrite this to remove usermanager dependency: Update user to add extensions
            response = await client.post(
                f"{self.protocol}://{self.host}/usermanager/api/v1/extensions?extension={name}&userid={lnbitsuserid}&active=true",
                headers={'X-Api-Key': self._admin_invoicekey})
            if response.status_code == 200:
                return True
            else:
                return False
        
    # creates LNURLP link
    async def createLnurlp(self, adminkey, payload):
        # delete all previous paylinks, there could be conflicts
        print("replacing all paylinks")
        paylinks = []

        # get all paylinks
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.protocol}://{self.host}/lnurlp/api/v1/links",
                headers={'X-Api-Key':adminkey})            
            paylinks.extend(response.json())
    

        # delete all paylinks
        for paylink in paylinks:
            logging.info(f"Deleting old paylink with id {paylink['id']}")
            async with httpx.AsyncClient() as client:
                response = await client.delete(
                    f"{self.protocol}://{self.host}/lnurlp/api/v1/links/{paylink['id']}",
                    headers={'X-Api-Key':adminkey})            
                
        # create lnurlp link
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.protocol}://{self.host}/lnurlp/api/v1/links",
                json=payload,
                headers={'X-Api-Key':adminkey})            
            result = response.json()

            
            if not 'id' in result:
                if result['detail'].startswith("Username already exists."):
                    logging.warning(f"Username already exists {payload['username']}, retrying without username")
                    del payload['username']
                    return await self.createLnurlp(adminkey,payload)
                else:
                    logging.error(f"Could not create Lnurlpay link for payload '{json.dumps(payload)}'  the response was: '{json.dumps(result)}'")
                    return None
            else:
                return result

    # retrieves LNURLP link
    async def getLnurlp(self, baseurl, adminkey, payid):
        """
        Retrieve a Lnurlp link details
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{baseurl}lnurlp/api/v1/links/{payid}",
                headers={'X-Api-Key':adminkey})

            if response.status_code == 200:
                return response.json()
            else:
                logging.error(f"LNbits returned and error {response.status_code}")
                return None
            
            
    # check wether an invoice has been paid. Returns True if paid. Otherwise False
    async def checkInvoice(self,invoicekey,payment_hash):
        async with httpx.AsyncClient() as client:
            try:
                logging.info(payment_hash)
                logging.info(invoicekey)
                response = await client.get(
                    f"{self.protocol}://{self.host}/api/v1/payments/{payment_hash}",
                    headers = {"X-Api-Key": invoicekey})
            
                jsobj = json.loads(response.text)
                if jsobj['paid'] == True:
                    return True
            except httpx.ReadTimeout:
                logging.warning("LNbits read timeout")
                return False
        return False


    # get all wallets in lnbits
    async def getWallets(self):
        async with httpx.AsyncClient() as client:
            # TODO: replace this to remove dependency of usermanager
            # /api/v1/wallet/paginated
            # this function can also filter on external id, 
            response = await client.get(
                f"{self.protocol}://{self.host}/usermanager/api/v1/wallets",
                headers = {"X-Api-Key": self._admin_adminkey})
            return json.loads(response.text)

    # get all users in lnbits
    async def getUsers(self):
        async with httpx.AsyncClient() as client:
            # TODO: replace this to remove dependency of usermanager
            # GET /users/api/v1/user
            response = await client.get(
                f"{self.protocol}://{self.host}/usermanager/api/v1/users",
                headers = {"X-Api-Key": self._admin_adminkey})
            return json.loads(response.text)

    # return the wallet for a specific user
    async def getWallet(self, lnbitsuserid):
        async with httpx.AsyncClient() as client:
            # TODO: rewrite this to get the wallet of a specific user and remove usermanager dependency
            # /users/api/v1/user/{user_id}/wallet
            response = await client.get(
                f"{self.protocol}://{self.host}/usermanager/api/v1/wallets/{lnbitsuserid}",
                headers = {"X-Api-Key": self._admin_adminkey})
            result = json.loads(response.text)
            if len(result) > 0:
                return result[0]

        return None
        

