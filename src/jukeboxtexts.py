def init():
    # when the help, start command is given
    global help
    help = """
You can use one of the following commands:
    
/start and /faq both result in this output
/add to search for tracks. Searches can be refined using statements such as 'artist:'.
/queue to view the list of upcoming tracks. 
/history view the list of tracks that were played recently
/dj <amount> to share some of your sats balance with another user. Use this command in a reply to them.

You can also chat in private with me to do stuff like viewing your balance.

/stack shows your balance.
/fund can be used to add sats to your balance. While you can pay for each track, you can also add some sats in advance.
/link provides an lndhub URL so that you can connect BlueWallet or Zeus to your wallet in the bot. That also makes it possible to withdraw/add funds.
/refund <lightning invoice> pays a lightning invoice so you can get your sats back.

The /couple, /decouple, /setclientid and /setclientsecret are commands to connect your own music player to this bot and start your own Jukebox!

If you like this bot, send a donation to herovk@ln.tips or simply /dj the bot.
"""           

    # when someone executes the balance command in a group
    global balance_in_group
    balance_in_group = "Like keeping your mnenomic seedphrase offline, it is better to query your balance in a private chat with me."

    # when someone executes the disconnect command in a private chat
    global disconnect_in_private_chat
    disconnect_in_private_chat = "Execute the /decouple command in a group where you want the player disconnected"

    # when someone tries to perform a command that requires admin permissions
    global you_are_not_admin
    you_are_not_admin = "You are not an admin in this chat."

    # when the spotify authorisation is removed
    global spotify_authorisation_removed
    spotify_authorisation_removed = "Removed player from group. To reconnect, an admin should perform the /couple command to authorize the bot."

    global spotify_authorisation_removed_error
    spotify_authorisation_removed_error = "Removed player from group failed. Sorry. retry or dm the captain."

    global no_client_id_set
    no_client_id_set = "No spotify ClientID set Use the /setclientid command to enter this id"

    global client_id_set
    client_id_set = "Spotify ClientID is set to {}"
    
    global no_client_secret_set
    no_client_secret_set = "No Spotify Client Secret set. Use the /setclientsecret command to enter this secret"        

    global client_secret_set
    client_secret_set = "Spotify Client Secret is set"

    global everything_set_now_do_connect
    everything_set_now_do_connect = "Both client_id and client_secret are set. Execute the /couple command in the group that you want to connect to the bot"

    global instructions_in_private_chat
    instructions_in_private_chat = "I'm sending instructions in the private chat."

    global button_to_private_chat
    button_to_private_chat = "Take me there"

    global click_the_button_to_authorize
    click_the_button_to_authorize = "Clicking on the button will take you to Spotify where you can grant the bot the authorisation to control the player."

    global add_command_help
    add_command_help = "Use the /add command to search for tracks and add them to the playlist. Enter the name of the artist and/or the song title after the /add command and select a track from the results. For example: \n/add rage against the machine killing in the name\n/add 7th element\n"
