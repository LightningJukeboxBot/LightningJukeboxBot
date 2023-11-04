# Lightning Jukebox Bot Design

Currently, the Lightning Jukebox Bot is a monolithic python application. The goal is to change to for several reasons:

  - Enable further development
  - Support other music players to the bot than Spotify
  - Connect multiple music players to the same JukeBox
  - Advanced queuing features such as moving tracks up and down the queue
  - Enable other user interfaces (Telegram, Web, Nostr, Physical,...)
  - Simplify architecture. Move away from a monolithic application to make contributions by other easier.

## Priority

  - Now:
    - Keep stuff minimal for now, add details later
    - Messaging to get the interplay between multiple music players in one Jukebox correct
  - Later:
    - Add security
    - Support for multiple groups
    - Split user interfaces from Jukebox

## Principles

We strive to use the following design/implementation goals

 - Only use technology that is supported by many platforms: use standard such as WebSockets, HTTP, JSON.
 - Support for multiple programming languages: interface on protocol
 - Asynchronous operation. Avoid blocking/synchronous function calls as much as possible. 

## Architecture 

The structure of the intended application is to introduce more seperation between individual components (the player, the user interface). The current plan is to communicate between all components via WebSockerts. All components that user the Jukebox communicate internally via WebSocket using JSON messages. 

The general idea is that components connected to the Jukebox can send or react to messages when ever they want. For instance to query for a track, music players respond to such messages by sending a message which tracks they can play that best fit that query. The user can then select one of these tracks and a message is sent to that player to play the track. 

Some persistent storage will also be needed. That is typically left open and up to each component itself. While the goal is to only communicate via a WebSocket, a shared database should be avoided.

## Multiple groups

For now, we have to figure out how to properly separate (authenticate and authorize) messages from other Jukeboxes. At this moment, we only focus on getting it running for one Jukebox only, and then scale that up to properly support multiple Jukeboxes at the same time.

## Dumb players

In this architecture, players are dumb and not expected to play anything automatically. Players play a song, or coninue to the next (if there is one in their queue), but stop when their local play queue is empty. This behaviour is used to make it possible/easier to switch between players.

## Architecture overview

The following schematic provides an overview of the Jukebox. 

![Architecture overview](./architecture.svg)

In this architecture, each component has a distinct function.

 * The *Jukebox Controller* is the central component responsible for managing all distinct Jukeboxes, manage the queue of upcoming tracks, controlling which track to play next, creating invoices and tracking payments and sending instructions to players to play a certain track. Basically this component knows everything about all jukeboxes and is the source of truth.
 * Above the *Jukebox Controller* component are several user interface components that interface to various platforms: Telegram, Web, NOST, Physical Jukeboxes, Music Tickers, whatever. These components are responsible for interacting on their platform with users and retrieve/send instructions to the Jukebox via a WebSocket.
 * Below the *Jukebox Controller* component are the components that can play music: a player that connects to a Spotify account, Local filesystem or a Fake player that does nothing but just acts as if it is a player. Each player must implement a minimum set of functionality (like playing a track, searching for a track) that is exposed to the Jukebox. Music players can be used to control music playback in multiple groups, but also in distinct groups. Like a Spotify Player can control music playback for multiple groups, a local filesystem player is typically intended for one group.


## The player interface

The file player.md describes the communication via the /player WebSocket interface.

The file jukebox.md describes the communication via the /jukebox WebSocket interface.


