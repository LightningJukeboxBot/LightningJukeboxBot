# A Bitcoin enabled Jukebox
![ssets/20230307-Bot-logo-new.jpg](https://github.com/LightningJukeboxBot/LightningJukeboxBot/blob/main/Assets/LightningJukeboxBot.jpg)
*Our trust in DJ's has been broken, and we will make them obsolete!* ;)

## What we achieved:
- We want the crowd to be the DJ. Our Jukebox Lightning Bot* makes this possible.
Not only for Radiostations, but also for live events, venues, bars and pubs, businesess, you name it!
- You want a three channel silent disco for your #bitcoin meetup, conference of festival? We got you covered!
- It works very well for parties, pubs, or wherever else you want folks to have acces to a Lightning enabled Jukebox Bot. **GREAT FOR ORANGE PILLING FRENS & FAM!**

## Our goals
- Integrating with Wavlake with the Jukebox Lightning Bot
So musicians get their fair share of sats that flow through the Jukebox
- Making the Lightning Jukebox Bot more modular as to easily allow for plugging in/out more media-players & -libraries and to further bot integration over more platforms
- Getting the bot to nostr. This process is slowly underway, see: #npub1wqtq2k9cawq2hkwz474xdm6ef0drmdhn4fk59hpyuex5l0ewa9rsj9a4cz

## General info
 - Find the bot on Telegram as '[@Jukebox_Lightning_bot](https://t.me/Jukebox_Lightning_bot)'
 
If you want to test out the functionality, there are a few options. Tune in via our [zap.stream](https://zap.stream/naddr1qqjrswfevvukxvrr95cx2vt9956xyepn94snxdfs95urwwryxe3x2wfjvscn2q3qeaz6dwsnvwkha5sn5puwwyxjgy26uusundrm684lg3vw4ma5c2jsxpqqqpmxwe2sz27) or [radio.noderunners.org](https://radio.noderunners.org/) and use their [unique web-interface](https://jukebox.lighting/jukebox/web/-1001672416970) to add music to the queue.

You can even spin up your own Radio with our Jukebox Lightning Bot! When you do, you will get your own unique web-interface.
For now, ask for help in the [Noderunners Radio Telegram](https://t.me/noderunnersradio) chat if you need it, or just come hangout and play us your favorite pieces of music. There is a bit more functionality available in Telegram right now than compared to the web-interface.

## Ideas for new features
 - Super admin stats: interface for dedicated superadmins that can see stuff like the groups that are using the bot, sats in the bot etc. 
 - Seperate queue: When folks type /queue, and the bot shows the queue, can we differentiate between what is added by plebs and what is the background playlist? Maybe by not showing the background playlist at all? We could create our own queue that is separate from the background playlist. If we manage our own queue we could also introduce features like upvoting items in the queue. 
- Song duration and time left displayed in TG somehow
- Stats: /stats command to see the top jukebox stats, most requested track, top 10 of users adding tracks to the queue. 
 - NOSTR connectivity: Chat embedded on the website (NOSTR maybe?) Endusers can toggle this on or of to be dislplayed in their own videofeed. Preferably should not requre a login but users should be able to set a username or use nostr or twitter login to chat. How would this look like when there are multiple instances of the JukeBox running? Would the admin of a group provide a NOSTR private key for the bot to use in this group?
 - Web API: the ability to use the bot through a Web API. How should this work for multiple groups? Provide an API endpoint in the Group Admin chat? This should preferably be a REST API (search, results, pay, queue, currentplaying). Actually the current playing track is a kind of (limited) REST API already.
 - Congestion control #1: limmeting folks to add more tracks if the queue gets to a certain size and they already added 2 tracks in a row. This is dependent on the seperate queue function.  
 - Congestion control #2: Increase cost per added track per track that is still waiting in the queue. This is dependent on the seperate queue. 
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
