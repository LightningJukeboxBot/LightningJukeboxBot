def init():
    # when the help, start command is given
    global help
    help = """
/add artist and track \(select best option\)
/queue \(upcoming tracks\)
/history \(recent playlist\)
/fund \(add sats to your Jukebox\)
/stack \(check Jukebox balance\)
/refund \(send sats from your /stack to another wallet\)
/dj [amount] \(tip a user in a reply e\.g\. /dj 21\)

Find all commands [here](https://github.com/LightningJukeboxBot/LightningJukeboxBot?tab=readme-ov-file#tg-commands-for-the-bot)
Learn how to setup your own Jukebox [here](https://github.com/LightningJukeboxBot/LightningJukeboxBot?tab=readme-ov-file#how-do-i-couple-the-bot-to-my-own-spotify-premium-account)
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
