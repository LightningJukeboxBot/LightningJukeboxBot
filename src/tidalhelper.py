import redis
from redis import RedisError
import asyncio
import settings
import json
import logging
import tidalapi
from time import time

class TidalSettings:
    def __init__(self, tguserid):
        self.userid = tguserid
        self.userkey = f"user:{self.userid}"        
        self.client_secret = None
        self.client_id = None

    def toJson(self):
        data = {
            'telegram_userid': self.userid,
            'client_secret': self.client_secret,
            'client_id': self.client_id
        }
        return json.dumps(data)

    def loadJson(self, data):
        assert(data is not None)
        obj = json.loads(data)
        assert(obj is not None)
        assert(obj['telegram_userid'] == self.userid)

        if 'client_secret' in obj:
            self.client_secret = obj['client_secret']

        if 'client_id' in obj:
            self.client_id = obj['client_id']
            
class CacheJukeboxTidalHandler:
    """
    This cache handler keeps track of Tidal auth data and is stored in the redis database per group so that multiple authorisations can be active at the same time
    """    
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.rediskey = f"tidal_token:{self.chat_id}"
        self.session = None

    def get_cached_session(self):
        logging.debug("Obtain cached Tidal session")
        session_info = None
    
        try:
            session_info = settings.rds.get(self.rediskey)
            if session_info:
                return json.loads(session_info)
        except RedisError as e:
            logging.warning('Error getting Tidal session from cache: ' + str(e))       

        return session_info

    def save_session_to_cache(self, session_info):
        logging.info("saving Tidal session to cache")
        try:
            settings.rds.set(self.rediskey, json.dumps(session_info))
        except RedisError as e:
            logging.warning('Error saving Tidal session to cache: ' + str(e))

def add_to_queue(session, track_ids):
    """
    Add a list of tracks to the queue
    """
    for track_id in track_ids:
        try:
            session.user.add_to_queue(track_id)
        except Exception as e:
            logging.error(f"Error adding track {track_id} to Tidal queue: {str(e)}")

def get_track_title(track):
    """
    Get a readable version of the track title from Tidal track object
    """
    if track is None:
        return "No track item"
    if not hasattr(track, 'artist') or track.artist is None:
        return "No artist"
    if not hasattr(track, 'name') or track.name is None:
        return "No track name"

    artist = track.artist.name if hasattr(track.artist, 'name') else str(track.artist)
    track_name = track.name

    return f"{artist} - {track_name}"

async def get_price(chat_id):
    """
    Gets the price for tracks in this group. Defaults to the initial price of 21 sats
    """
    rediskey = f"group:{chat_id}"
    price = settings.rds.hget(rediskey,"price")
    if price is None:
        price = settings.price
    return int(price)

async def set_price(chat_id, price):
    """
    Set the price in a group
    """
    rediskey = f"group:{chat_id}"
    price = settings.rds.hset(rediskey,"price",price)

async def create_tidal_session(chat_id):
    """
    Create a new Tidal session with OAuth authentication
    """
    logging.debug("create Tidal session")
    cache_handler = CacheJukeboxTidalHandler(chat_id)
    
    # Check for cached session first
    cached_session = cache_handler.get_cached_session()
    if cached_session:
        session = tidalapi.Session()
        session.token_type = cached_session.get('token_type')
        session.access_token = cached_session.get('access_token')
        session.refresh_token = cached_session.get('refresh_token')
        session.expiry_time = cached_session.get('expiry_time')
        
        # Verify session is still valid
        try:
            session.user  # This will trigger auth check
            return session
        except:
            logging.info("Cached Tidal session expired, creating new one")
    
    # Create new session
    session = tidalapi.Session()
    return session
            
async def init_tidal_session(chat_id):
    """
    Initialize a Tidal session for a specific group
    """
    logging.info("init Tidal session")
    session = await create_tidal_session(chat_id)
    
    # Store session info in Redis
    session_data = {
        'chat_id': chat_id,
        'created_at': time()
    }
    settings.rds.hset(f"group:{chat_id}", "tidal_session", json.dumps(session_data))
    
    return session
        
async def get_tidal_session(chat_id):
    """
    Get a Tidal session for a specific group
    """
    logging.debug("Get Tidal Session")
    session_data = settings.rds.hget(f"group:{chat_id}","tidal_session")
    if session_data is None:
        return None
    
    return await create_tidal_session(chat_id)

async def delete_tidal_session(chat_id):
    """
    Removes a Tidal session from our local store
    """
    session_data = settings.rds.hget(f"group:{chat_id}","tidal_session")
    if session_data is None:
        return True

    # delete the Tidal token as well 
    settings.rds.delete(f"tidal_token:{chat_id}")
    
    settings.rds.hdel(f"group:{chat_id}","tidal_session")
    return True

async def save_tidal_settings(tps):
    """
    Store Tidal settings in Redis
    """
    settings.rds.hset(tps.userkey,"tidal",tps.toJson())
    
async def get_tidal_settings(userid):
    """
    Get the Tidal settings for this user
    """
    tps = TidalSettings(userid)    
    data = settings.rds.hget(tps.userkey,"tidal")
    if data is not None:
        tps.loadJson(data)
    return tps

async def search_tidal_tracks(query, limit=10):
    """
    Search for tracks on Tidal
    """
    try:
        session = tidalapi.Session()
        results = session.search(query, [tidalapi.Track], limit)
        
        tracks = []
        if results and 'tracks' in results:
            for track in results['tracks']:
                track_info = {
                    'id': track.id,
                    'title': track.name,
                    'artist': track.artist.name if track.artist else 'Unknown Artist',
                    'album': track.album.name if track.album else 'Unknown Album',
                    'duration': track.duration,
                    'uri': f"tidal:track:{track.id}"
                }
                tracks.append(track_info)
        
        return tracks
    except Exception as e:
        logging.error(f"Error searching Tidal tracks: {str(e)}")
        return []

async def get_history(chat_id, maxlen):
    rediskey = f"tidal_history:{chat_id}"
    titles = []
    for i in range(0, min(maxlen,settings.rds.llen(rediskey))):
        titles.append(settings.rds.lindex(rediskey, i).decode('utf-8'))
    return titles

async def update_history(chat_id: int, title: str) -> None:
    rediskey = f"tidal_history:{chat_id}"
    currenttitle = settings.rds.lindex(rediskey,0)

    if currenttitle is None:
        settings.rds.lpush(rediskey,title)
    else:
        currenttitle = currenttitle.decode('utf-8')
        if currenttitle != title:
            settings.rds.lpush(rediskey,title)
            
        if settings.rds.llen(rediskey) > 100:
            settings.rds.rpop(rediskey)
                    
    # update last played entry
    settings.rds.hset(f"tidal_lastplayed:{chat_id}",title,int(time()))

async def get_donation_fee(chat_id: int) -> int:
    """
    Gets the donation fee for Tidal
    """
    rediskey = f"group:{chat_id}"
    fee = settings.rds.hget(rediskey,"tidal_donation_fee")    
    if fee is None:
        fee = settings.donation_fee
    fee = int(fee)
    if fee < 0:
        fee = settings.donation_fee
    return int(fee)

async def set_donation_fee(chat_id: int, fee: int) -> None:
    """
    Sets the donation fee for Tidal
    """
    rediskey = f"group:{chat_id}"
    settings.rds.hset(rediskey,"tidal_donation_fee",fee)

async def get_active_player(chat_id: int) -> str:
    """
    Get the active music player for this group
    """
    rediskey = f"group:{chat_id}"
    player = settings.rds.hget(rediskey, "active_player")
    if player is None:
        return "spotify"  # default to Spotify for backward compatibility
    return player.decode('utf-8')

async def set_active_player(chat_id: int, player: str) -> None:
    """
    Set the active music player for this group
    Options: 'spotify', 'tidal', 'both'
    """
    rediskey = f"group:{chat_id}"
    settings.rds.hset(rediskey, "active_player", player)

async def get_tidal_quality(chat_id: int) -> str:
    """
    Get the Tidal playback quality setting for this group
    """
    rediskey = f"group:{chat_id}"
    quality = settings.rds.hget(rediskey, "tidal_quality")
    if quality is None:
        return "HIGH"  # default quality
    return quality.decode('utf-8')

async def set_tidal_quality(chat_id: int, quality: str) -> None:
    """
    Set the Tidal playback quality for this group
    Options: 'MASTER', 'LOSSLESS', 'HIGH', 'NORMAL'
    """
    rediskey = f"group:{chat_id}"
    settings.rds.hset(rediskey, "tidal_quality", quality)