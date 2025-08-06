# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is the Lightning Jukebox Bot - a Bitcoin Lightning Network-enabled Telegram bot that allows users to collaboratively create music playlists by paying satoshis to add tracks. Users can connect their Spotify Premium accounts to act as the music source, while others pay small Lightning Network invoices (typically 21 sats) to add songs to a shared queue.

## Development Environment Setup

### Dependencies
- Python 3.8+
- Poetry for dependency management
- Redis for session storage
- LNbits Lightning Network wallet backend
- Spotify Premium account for music playback

### Installation and Running
```bash
# Install dependencies
cd src
poetry install

# Run the bot (requires environment variables)
python jukeboxbot.py
```

### Environment Variables Required
- `JUKEBOX_ENV` - Set to 'development' or 'production'
- `JUKEBOX_DOMAIN` - Domain where the bot web interface is hosted
- `BOT_TOKEN` - Telegram bot token
- `BOT_ID` - Telegram bot ID
- `BOT_IPADDRESS` - Server IP address
- `LNBITS_PROTOCOL` - LNbits protocol (http/https)
- `LNBITS_HOST` - LNbits host
- `LNBITS_ADMINKEY` - LNbits admin key
- `LNBITS_INVOICEKEY` - LNbits invoice key
- `LNBITS_USRKEY` - LNbits user key
- `SUPERADMINS` - Comma-separated list of Telegram user IDs with admin privileges
- `TIDAL_CLIENT_ID` - Tidal API client ID (optional, for Tidal integration)
- `TIDAL_CLIENT_SECRET` - Tidal API client secret (optional, for Tidal integration)

## Architecture

The current application is a monolithic Python bot with plans to transition to a modular WebSocket-based architecture (see `doc/design/index.md`).

### Core Components

**Main Application (`jukeboxbot.py`)**
- Central bot logic with Telegram webhook handling
- Starlette web server for payment callbacks and Spotify OAuth
- WebSocket server (planned for future modular architecture)

**Helper Modules:**
- `telegramhelper.py` - Telegram command processing and utilities
- `spotifyhelper.py` - Spotify API integration and authentication
- `tidalhelper.py` - Tidal API integration and session management
- `lnbits.py` - Lightning Network payment processing via LNbits
- `userhelper.py` - User management and wallet functions
- `invoicehelper.py` - Lightning invoice creation and tracking
- `statshelper.py` - Bot usage statistics
- `settings.py` - Configuration management and environment setup

**Web Interface (`web/` directory)**
- HTML pages for invoice payment and Spotify authentication
- JavaScript for dynamic payment status updates
- CSS styling and assets

### Key Telegram Commands
- `/add <artist> <track>` - Add music to queue (requires payment, searches both Spotify and Tidal if available)
- `/queue` - View upcoming tracks
- `/history` - Show recently played tracks  
- `/fund` - Preload wallet balance
- `/stack` - Check wallet balance
- `/couple` - Connect Spotify account
- `/tcouple` - Connect Tidal account  
- `/tsetclientid <id>` - Set Tidal client ID (private chat only)
- `/tsetclientsecret <secret>` - Set Tidal client secret (private chat only)
- `/tdecouple` - Disconnect Tidal account
- `/ttest <query>` - Test Tidal search functionality
- `/web` - Get web interface QR codes
- `/price <amount> <dev_fee>` - Set track pricing (admins only)

### Payment Flow
1. User sends `/add` command with track details
2. Bot searches available music services (Spotify/Tidal) and presents track options with service icons
3. User selects track, bot creates Lightning invoice
4. Payment opens in web interface with QR code
5. On payment confirmation, track is added to queue
6. Active music player receives instruction to queue the track

### Future Architecture (Planned)
The project plans to transition to a modular WebSocket-based system where:
- **Jukebox Controller** - Central queue and payment management
- **User Interfaces** - Telegram, Web, Nostr, Physical interfaces
- **Music Players** - Spotify, Local filesystem, other streaming services
- Communication via WebSocket with JSON messages

## Development Notes

- Uses Poetry for dependency management (`pyproject.toml` in `src/`)
- Redis for session and state storage (database 2)
- Custom Spotify client implementation in `spotipy/` directory
- Web server runs on port 7000 by default
- No formal test suite currently implemented
- Logging configured based on environment (file-based for production/development)

## Security Considerations

- Bot requires multiple API keys and tokens (Telegram, LNbits, Spotify)
- Payment processing involves real Bitcoin Lightning transactions
- Spotify OAuth flow requires precise redirect URI configuration
- Redis stores sensitive session data and user wallet information