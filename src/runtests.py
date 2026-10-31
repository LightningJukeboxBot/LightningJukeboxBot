import unittest
import asyncio
import userhelper
from lnbits import LNbits
import settings


userkey1 = "user:12345678"
userkey2 = "user:87654321"


class TestJukeboxBot(unittest.TestCase):
    def test_settings(self):
        """
        Test initialising the settings
        """
        self.assertEqual(settings.init(),True)
    
    def test_no_wallets(self):
        """
        Test if there are no user/wallets present
        """
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)

    def test_extensions(self):
        """
        Test creation and deletion of a wallet
        """
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        # create a user and a wallet
        lnbitsuserid = asyncio.run(settings.lnbits.createUser(userkey1))
        self.assertIsNotNone(lnbitsuserid)

        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)

        self.assertEqual(asyncio.run(settings.lnbits.enableExtension('lnurlp',lnbitsuserid)),True)
        self.assertEqual(asyncio.run(settings.lnbits.enableExtension('lnurlp',lnbitsuserid)),True)
        self.assertEqual(asyncio.run(settings.lnbits.enableExtension('lndhub',lnbitsuserid)),True)        
        
        # delete wallet
        asyncio.run(settings.lnbits.deleteUser(lnbitsuserid))
    
        # get the wallets
        self.assertEqual( len(asyncio.run(settings.lnbits.getWallets())),0)

    def test_get_wallet(self):
        """
        Test the retrieval of some wallet
        """
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        lnbitsuserid = asyncio.run(settings.lnbits.createUser(userkey1))

        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)

        wallet = asyncio.run(settings.lnbits.getWallet(lnbitsuserid))
        self.assertIsNotNone(wallet)

        self.assertEqual(wallet['name'],userkey1)
        
        asyncio.run(settings.lnbits.deleteUser(lnbitsuserid))
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)

    def test_transaction(self):
        """
        Create a testtransaction
        """
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)

        # test is there is some sats in the balance of the admin
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)
        
        # create a user
        lnbitsuserid = asyncio.run(settings.lnbits.createUser(userkey1))
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)

        # get the wallet of a user
        wallet = asyncio.run(settings.lnbits.getWallet(lnbitsuserid))
        self.assertIsNotNone(wallet)

        # test there is no balance for the user
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(wallet['inkey'])),0)
        
        # create invoice admin -> user
        invoice = asyncio.run(settings.lnbits.createInvoice(wallet['inkey'],21,"admin -> user"))
        self.assertIsNotNone(invoice)
        
        # pay the invoice
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],settings.lnbits._admin_adminkey))
        self.assertEqual(result['result'],True)

        # pay the invoice again should fail
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],settings.lnbits._admin_adminkey))
        self.assertEqual(result['result'],False)

        # test the balance for the user
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(wallet['inkey'])),21)

        # test is there is some sats in the balance of the admin
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),0)


        # create invoice user -> admin 
        invoice = asyncio.run(settings.lnbits.createInvoice(settings.lnbits._admin_invoicekey,21,"user -> admin"))
        self.assertIsNotNone(invoice)

        # pay the invoice
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],wallet['adminkey']))
        self.assertEqual(result['result'],True)

        # pay the invoice again should fail
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],wallet['adminkey']))
        self.assertEqual(result['result'],False)

        # test there is no balance for the user
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(wallet['inkey'])),0)

        # test is there is some sats in the balance of the admin
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)

        # delete wallet
        asyncio.run(settings.lnbits.deleteUser(lnbitsuserid))
    
        # get the wallets
        self.assertEqual( len(asyncio.run(settings.lnbits.getWallets())),0)


    def test_recreating_user(self):
        """
        Test a helper function that creates a user
        """

        settings.rds.hdel("user:123456","userdata")
        settings.rds.hdel("user:654321","userdata")
                
        # test all preconditions
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)

        user1_1 = asyncio.run(userhelper.get_or_create_user(123456,"test_username1"))
        
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),1)

        # recreate the user again
        user1_2 = asyncio.run(userhelper.get_or_create_user(123456,"test_username1"))

        # check that we not created a new user and the we did not create a new wallet
        self.assertEqual(user1_1.lnbitsuserid, user1_2.lnbitsuserid)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),1)

        # delete the redis entry and create the user again
        settings.rds.hdel("user:123456","userdata")
        user1_3 = asyncio.run(userhelper.get_or_create_user(123456,"test_username1"))
                
        # check that we not created a new user and the we did not create a new wallet
        self.assertEqual(user1_1.lnbitsuserid, user1_3.lnbitsuserid)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),1)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),1)
        
        # delete the user and its wallet
        asyncio.run(settings.lnbits.deleteUser(user1_1.lnbitsuserid))
        
        # delete users from redis 
        settings.rds.hdel("user:123456","userdata")
        
        # test all preconditions
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)


        
    def test_get_or_create_user(self):
        """
        Test a helper function that creates a user
        """
        settings.rds.hdel("user:123456","userdata")
        settings.rds.hdel("user:654321","userdata")

        # test all preconditions
        self.assertEqual(settings.init(),True)
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)

        # get or create a user
        user1 = asyncio.run(userhelper.get_or_create_user(123456,"test_username1"))
        user2 = asyncio.run(userhelper.get_or_create_user(654321,"test_username2"))

        # assert wallets and users
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),2)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),2)

        # test there is no balance for the user
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user1.invoicekey)),0)        
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user2.invoicekey)),0)        

        # create invoice admin -> user
        invoice = asyncio.run(settings.lnbits.createInvoice(user1.invoicekey,21,"admin -> user1"))
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],settings.lnbits._admin_adminkey))
        self.assertEqual(result['result'],True)
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],settings.lnbits._admin_adminkey))
        self.assertEqual(result['result'],False)

        # test the balances for everyone
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user1.invoicekey)),21)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user2.invoicekey)),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),0)

        # create invoice user1 -> user2
        invoice = asyncio.run(settings.lnbits.createInvoice(user2.invoicekey,21,"user1 -> user2"))
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],user1.adminkey))
        self.assertEqual(result['result'],True)
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],user1.adminkey))
        self.assertEqual(result['result'],False)

        # test the balances for everyone
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user1.invoicekey)),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user2.invoicekey)),21)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),0)

        # create invoice user2 -> admin
        invoice = asyncio.run(settings.lnbits.createInvoice(settings.lnbits._admin_invoicekey,21,"user2 -> admin"))
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],user2.adminkey))
        self.assertEqual(result['result'],True)
        result = asyncio.run(settings.lnbits.payInvoice(invoice['payment_request'],user2.adminkey))
        self.assertEqual(result['result'],False)

        # test the balances for everyone
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user1.invoicekey)),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(user2.invoicekey)),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)

        # delete the user and its wallet
        asyncio.run(settings.lnbits.deleteUser(user1.lnbitsuserid))
        asyncio.run(settings.lnbits.deleteUser(user2.lnbitsuserid))

        # delete users from redis 
        settings.rds.hdel("user:123456","userdata")
        settings.rds.hdel("user:654321","userdata")

        # test all preconditions
        self.assertEqual(len(asyncio.run(settings.lnbits.getWallets())),0)
        self.assertEqual(len(asyncio.run(settings.lnbits.getUsers())),0)
        self.assertEqual(asyncio.run(settings.lnbits.getBalance(settings.lnbits._admin_invoicekey)),21)
        
if __name__ == '__main__':        
    asyncio.run(unittest.main())
