# The player WebSocket interface

The /player WebSocket interface is the interface that connects music players to the Jukebox. The WebSocket is exposed by the Jukebox and player connect to the Jukebox Controller by creating a WebSocket connection to:

```
/player/{jukebox identifier}
```

The *jukebox identifier* is a token that identifies the specific Jukebox.

The Jukebox Controller maintains a list of all players that are active in the group. Each player is represented by an active WebSocket connection to the /player endpoint with the correct identifier. 

## Potential issues

 * When the Spotify Player connects to the Jukebox Controller, it has to create a WebSocket connection for each jukebox it serves.
 * Due to the loose coupling, there is limited confirmation wether an action was succesfull or not. 

## Messages

A general overview of message structure is given in messages.md

### Search

Search message is sent by the Jukebox Controller to search for a specific track.

Sample message
```
{
   "topic":"search",
   "query":"rage against the machine"
   "reference":"sadkjhgdsafjkhg"
}
```

The Jukebox Controller iterates over all active players and sends the search query to each player. It then waits for all responses from all players or a certain timeout (a few seconds) before returning results. The Jukebox Controller may send updates to results, but that is typically more advanced (and not bare minimum). 

### Search response

A search response is sent by players in response to a search query. Each player should respond to a search query, even if there are no results. In case of problems, the player may include a statuscode (internal failure, not authorized, see issues in messages.md). 

The message contains an array or results with a least a title and reference. Maybe more data could be added (like duration, artist, album), but that's beyond the bare minimum.

Sample message
```
{
   "topic":"searchresponse",
   "reference":"copy of reference the search"
   "results":[
	{
		"title":"Rage Against the Machine - Killing in the Name",
		"reference":"some internal reference to this track that the player understands"
	},
	{
		"title":"Rage Against the Machine - Bombtrack",
		"reference":"some internal reference to this track that the player understands"
	},
	{
		"title":"Cypress Hill - Insane in the Brain",
		"reference":"some internal reference to this track that the player understands"
	}
   ]
}
```

### play

The play command is the instruction to play a track by a music player. This event can be sent anytime by the Jukebox Controlelr, but should be sent only a few seconds before the current playing track is finished. The play message may contain a *delay* parameter, which instructs the player to wait several seconds before playing the next track.

Let's consider an environment with two music players (A and B) connected to the same music player. 

  * Player A is not playing and Player B is not playing. When the Jukebox Controller sends a message to Player A (via its websocket), player A starts to play the track specified in the message.
  * Player A is playing and Player B is not playing. When the Jukebox Controller sends a message to Player A (via its websocket), player A starts playing the track specified in the message, right after the current track is finished.
  * Player A is not playing and Player B is playing. When the Jukebox Controller sends a message to Player A, player A starts to play the track specified in the message, wether or not player B is playing. To avoid this behaviour, the Jukebox Controller can include an attribute (delay) in the message that instructs the music player to start playing after the specified amount of time has elapsed.
  * Player A is playing and Player B is not playing. The Jukebox Controller does not send a message. Player A stops after finishing the track.
  * Player A is playing and Player B is not playing. When the Jukebox Controller sends multiple 'play' messages, only the last message is interpreted. Each message is always considered as an instruction what to do next.

Sample message
```
{
	"topic":"play",
	"reference":"reference to the track to be played",
	"delay":15000
	
}
```
### nowplaying

This message is sent at (ir)regular intervals by a player to share its current state. The Jukebox Controller learns through this message the current state of the player. This message contains a *remaining* field, which can be used by the Jukebox Controller to learn the current state of the system.

The player can also use the remaining time of the track to send this message at the start of a track, and just before the end of the track.

Sample message
```
{
	"topic":"nowplaying",
	"title":"Rage Against the Machine - Killing in the Name",
	"remaining":180.3276
}
```

If the player has a track to play next, it may include that in the nowplaying message

Sample message
```
{
	"topic":"nowplaying",
	"title":"Rage Against the Machine - Killing in the Name",
	"reference":"local reference to the track",	
	"remaining":180.3276,
	"next":{
		"title":"Rage Against the Machine - Killing in the Name",
		"reference":"local reference to the track"
	}
}
```