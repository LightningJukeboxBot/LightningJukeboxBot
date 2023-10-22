# A Bitcoin enabled Jukebox
*Please consider donating some sats to [our Geyser.fund](https://geyser.fund/project/jukeboxbot), thank you!*
![ssets/20230307-Bot-logo-new.jpg](https://github.com/LightningJukeboxBot/LightningJukeboxBot/blob/main/Assets/LightningJukeboxBot.jpg)

Our trust in DJ's has been broken, and we will make them obsolete! ;)
*Except for [Rootzoll](https://twitter.com/rootzoll), he the man!* 

## What we achieved:
- We want the crowd to be the DJ. Our **Jukebox Bot** makes this possible.
Not only for Radiostations, but also for live events, venues, bars and pubs, businesess, you name it!
- You want a three channel silent disco for your #bitcoin meetup, conference of festival? We got you covered!
- It works very well for parties, pubs, or wherever else you want folks to have acces to a Lightning enabled Jukebox Bot. **GREAT FOR ORANGE PILLING FRENS & FAM!**

## Our goals
- Integrating with Wavlake with the Jukebox Lightning Bot
So musicians get their fair share of sats that flow through the Jukebox
Update on this: We have someone looking into it. The way the bot is currently set up, we are not quite ready for going the podcasting 2.0 route. This will howver not be forgotten. Currently work is being done on a demo to see if we can create a Proof of Cocnept for payments over Lightning and perhaps some coördination over Nostr for this. Will update when we know more. 
- Making the Lightning Jukebox Bot more modular as to easily allow for plugging in/out more media-players & -libraries and to further bot integration over more platforms
- Getting the bot to nostr. This process is slowly underway, see: #npub1wqtq2k9cawq2hkwz474xdm6ef0drmdhn4fk59hpyuex5l0ewa9rsj9a4cz

## General info
If you want to test out the functionality, there are a few options. Tune in via our [zap.stream](https://zap.stream/naddr1qqjrswfevvukxvrr95cx2vt9956xyepn94snxdfs95urwwryxe3x2wfjvscn2q3qeaz6dwsnvwkha5sn5puwwyxjgy26uusundrm684lg3vw4ma5c2jsxpqqqpmxwe2sz27) or [radio.noderunners.org](https://radio.noderunners.org/) and use their [unique web-interface](https://jukebox.lighting/jukebox/web/-1001672416970) to add music to the queue.

You can even spin up your own Radio with our Jukebox Lightning Bot! When you do, you will get your own unique web-interface. For now, you must still chat with the bot via Telegram to accomplish this. You may find the bot on Telegram as '[@Jukebox_Lightning_bot](https://t.me/Jukebox_Lightning_bot)'
If you need help, tag @noderunnersFM or @artdesignbySF in the [Noderunners Radio Telegram](https://t.me/noderunnersradio), or contact us via other channels. Of course you are welcome to just come hangout and play us your favorite tunes. **There is a bit more functionality available in Telegram right now than compared to the web-interface.**

The Jukebox uses LNbits in the back, meaning each user that comes in via TG gets their own unique LNbits wallet which they can connect to their mobile solution. End users need not use Telegram to add tracks to the /queue, as they can just use the web-interface.

## TG commands for the bot
- /faq to show short list of options (needs updating)
- /add artist and track title (to add music to the /queue)
- /history
- /queue (see upcoming tracks (need to still differentiate between added tracks and those in the background playlist))
- /stack (takes you to PM with the bot to view your stack
- /fund (folks can pay per track as they /add, or preload their Jukebox stack with the /fund command)
- /refund invoice (allows users to send sats from their /stack to any invoice)
- /stats (for super-admins only, shows how many admins connected their instance to a TG group and have it connected to their media-player)
- /link (to link your personal /stack to your mobile lightning solution)
- /dj (used as a reply to someone to send sats. Example /dj 21 sends 21 sats)
- /couple, /decouple, /setclientid and /setclientsecret are commands to connect your own music player to this bot and start your own Jukebox! For now, there is only Sptify Premiums upport.

## FAQ
#### How do I couple the bot to my own Spotify Premium account?
- For now, you are sadly still required to use Telegram to connect the bot to your own Premium Spotify account. **Please keep in mind, that if you later want to stream the audio to a Telegram group, you must be OWNER of that group!** *(There is a difference between being owner and admin. Only owners can retrieve RTMP + streaming Key from a TG group)* If you are certain you don't want to stream the music into that TG group at a later date, you can just be admin. The bot will act within that group, only as a remote to your Spotify. 
1. Invite the [Jukebox Bot](https://t.me/Jukebox_Lightning_bot) to an existing or new Telegram Group
2. Make the bot admin
3. Send the bot a Private Message with this command: /couple
4. The bot will now give you a short list of what to do next
5. Locate the [link to your Spotify Developer account](https://developer.spotify.com/dashboard) and go there
6. In your [Spotify Developer account](https://developer.spotify.com/dashboard), click on the 'Create an app' button and give the bot a random name and description. Then click 'Create"
7. Click 'Edit Settings' and add **EXACTLY** this url https://jukebox.lighting/spotify under 'Redirect URIs'. Do not forget to click 'Add' and 'Save'
8. Copy the Client ID and give the bot this command in a Private Message and paste the client ID after it: /setclientid PasteClientID
9. Copy the Client Secret and give the bot this command in a Private Message and paste the secret after it: /setclientsecret PasteClientID
10. Now, make sure your spotify is playing music. *(We recommend setting a playlist to reteat continuously)*
11. Next, return to the group you made the bot admin of and type this command: /couple
12. The bot should give you a message with a button to click to finalize coupling your account. Click it!
13. The browser should open up, and you will see this message if all went well: *Authorisation Succesfull! You can close this window now.*
- To test if all works well (again, make sure spotify is playing music), try using some of the [commands you can use in Telegram](https://github.com/LightningJukeboxBot/LightningJukeboxBot/tree/main#tg-commands-for-the-bot)
*If you have trouble setting up, contact [@NoderunnersFM](https://t.me/noderunnersFM) or [@artdesigbysf](https://t.me/artdesigbysf) on Telegram*
- You can now plug in your pc/phone or whatever device is playing your spotify to a soundsystem and use the /web interface to /add music to the /queue!
Just give the /web command to print out QR codes with you unique web-interface link on it. 
- Additionally, you may set a /price. Standard is: /price 21 7 (Meaning 14 will go to your personal Jukeobox /stack and 7 will go to furhter development. You may choose to set /price to whatever you like. Examples: */price 0 0 (no amounts set, adding is free) /price 2100 210 (Price per track added is 2100 of which 210 go to bot dev fund and the rest to you)*
- **More functionality is in the works!**
    
#### How do I stream the music to some other location?
1. Make sure spotify is playing music
2. [Download and install OBS](https://obsproject.com/)
3. Make sure to mute your desktop audio, unless you want every sound your device makes to be streamed!
4. Make sure to set audio to 320KB for optimal quality
5. Set video bitrate to whatever works best for you
6. Add sources (select spotify exe), you may choose to add a microphone or imagery for your video feed
7. **Important**: set all audio outputs under 'Audio Mixer' to  -3 dB
8. If you are owner of a TG group, you can find the RTMP settings behind the symbol on the dopright (**TG-desktop op ONLY!**) that looks like a speech-balloon with three vertical lines in it.
9. Enter the RTMP werver of your TG group into OBS settings
10. Do the same for the key
11. Click, start streaming to begin streaming into TG
12. If you want to ouput to multiple locations, [use this plugin](https://obsproject.com/forum/resources/multiple-rtmp-outputs-plugin.964/)
13. Good luck and have fun! 

## Ideas for new features
 - Super admin stats: interface for dedicated superadmins that can see stuff like the groups that are using the bot, sats in the bot etc. 
 - Seperate queue + add mempool.space queue design: When folks type /queue, and the bot shows the queue, it would be cool it looked like the mempool.space ux and nice to see what is added by users and which tracks are just background playlist tracks.
- Function where user who added track can remove it from the queue and get auto-refund (for miss-clicks/wrong selections)
- Song duration and time left displayed in TG somehow
- Stats: /stats command to see the top jukebox stats, most requested track, top 10 of users adding tracks to the queue. 
 - NOSTR connectivity: Chat embedded on the website (NOSTR maybe?) Endusers can toggle this on or of to be dislplayed in their own videofeed. Preferably should not requre a login but users should be able to set a username or use nostr or twitter login to chat. How would this look like when there are multiple instances of the JukeBox running? Would the admin of a group provide a NOSTR private key for the bot to use in this group?
 - Web API: the ability to use the bot through a Web API. How should this work for multiple groups? Provide an API endpoint in the Group Admin chat? This should preferably be a REST API (search, results, pay, queue, currentplaying). Actually the current playing track is a kind of (limited) REST API already.
 - Congestion control #1: limmeting folks to add more tracks if the queue gets to a certain size and they already added 2 tracks in a row. This is dependent on the seperate queue function.  
 - Congestion control #2: Increase cost per added track per track that is still waiting in the queue. This is dependent on the seperate queue. 
 - Congestion control #3: Up- and/or Downvote tracks within the queue by paying extra
 - Connect the bot to NOSTR: Users can add a NOSTR private key to their private chat (which I wouldn't do). And then in a private chat with the bot enable/disable NOSTR through a command. The JukeboxBot uses that key to post a message when a track is played.  
 - Silent disco feature! 3-channels, crowd is the dj! (this works right now, but in a somewhat convoluted way)
 - Congestion control: For busy moments, a mode where only n of x requested tracks get added to the list (fair chance). This will probably increase the load even more. Just make more requests, best way is to increase the price. 
 - Audio players other than spotify
 - Improved faucet over LNtips with congestion control.
 - Alternative price for a number of tracks: the price for the next three tracks is 0 or 1 sat 

 ## Backlog:
 - Test scripts for telegram itself
 - Curated playlists: User can create a playlist and make that public to the bot. Other users can vote for the playlist by paying sats. When a threshold is reached, the playlist is submitted to the queue. A percentage of the amount payed for the playlist goes to the creator of the playlist. 
 - Create a playlist: User can create a playlist by direct communication to the the bot. Basically a version of the /add command, but without playing. Playlists managed by the bot. 
 - Custodial bot. When a user receives money from other users, the amount could be kept by the bot as a budget to request new songs. That means, when there is sufficient budget, the bot just displays a Pay xyz sats, only clickable for the user, which is then substracted from their budget
 - Liedjes faucet, ik gooi geld in de jukebox, maar jullie mogen de muziek uitzoeken. /faucet, when there is money left, /add payment comes from faucet 

## Problems
  - /fund limit is too low at 2100
  - FIXED: Multiple uses of the /fund command does not result in updated /stack balance
  - FIXED: /stack command result looks like it is not updating after /fund command, but does udpate after /dj command or when funds are subtracted
  - ?FIXED? /stats bot balance seems to only update in 21 sat increments for all groups, even though some groups have higher or lower /price settings ammount set.
  - When the player is not available, the bot keeps sending messages that the player is not available (removed for now)
  - Let updates of the price message stay, or add it to the current playing track message (including description of the most important commands). Maybe the price command should be removed at all. Variable price appears as confusing
  
## Done
 - Donation per track
 - Create a new bot with the name JukeboxBot
 - Upload code to github 
 - When a track is selected, the payment message is a new message. This requires the user to scroll. Instead the track selection message should be replaced. Fixed.
 - Reversed history in message
 - Connect JukeboxBot to noderunnersfm
 - Integrate callback script and bot script into one script
 - Change polling function to callback from telegram
 - Create a welcome message for new users
 - One-to-one communication: Allow users to have a private conversation with to bot. Search for tracks, manage their playlist, add to queue. This one is important to create as it enables to create other features as described below. 
 - Curated playlists: User can create a playlist and make that public to the bot. Other users can vote for the playlist by paying sats. When a threshold is reached, the playlist is submitted to the queue. A percentage of the amount payed for the playlist goes to the creator of the playlist. 
 - Create a playlist: User can create a playlist by direct communication to the the bot. Basically a version of the /add command, but without playing. Playlists managed by the bot. 
 - Custodial bot. When a user receives money from other users, the amount could be kept by the bot as a budget to request new songs. That means, when there is sufficient budget, the bot just displays a Pay xyz sats, only clickable for the user, which is then substracted from their budget
 - Personal budget for users. Users have personal budget where they can upload money to later spend on payments for songs -> faster payment flow
 - Create another instance of the bot so that it can be used on other groups. 
 - When a payment is not made, the request should be removed after some time (half an hour or so). Appears not to be a mayor issue
 - Create new names and groups for Jukebox_lightning_test and Jukebox_lightning_development
 - Spotify gave a new error when connecting, test invalid ID's. Error appears to be resolved
 - Create nicely designed HTML pages for payment and Spotify connection.
 - Create another instance of the bot so that it can be used on other groups. 
 - One-to-one communication: Allow users to have a private conversation with to bot. Search for tracks, manage their playlist, add to queue. This one is important to create as it enables to create other features as described below. 
 - Personal budget for users. Users have personal budget where they can upload money to later spend on payments for songs -> faster payment flow
 - Faster payment feedback
 - Payment feedback in payment HTML page
 - Create nicely designed payment page for funding

## Test
Connect bot to test group
Disconnect from spotify
queue
history
add

*We still like some DJ's don't worry. You can pauze the bot any time if you want to play dictator and ruin everyone's fun :p ;)
