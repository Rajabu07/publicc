import time
import logging
import motor.motor_asyncio
import datetime
from pymongo.errors import DuplicateKeyError
from helpers.utils import fill_daily_stats

log = logging.getLogger(__name__)

# Counters tracked per day in the `stats` collection
STAT_FIELDS = ("approved", "dm_sent", "dm_failed", "followups_sent", "byes_sent", "broadcast_sent")

class Database:

    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.channel = self.db.channel
        self.pending = self.db.pending
        self.settings = self.db.settings
        self.posts = self.db.posts
        self.stats = self.db.stats
        self.followups = self.db.followups

    async def clean_duplicates(self):
        try:
            await self.col.delete_many({"id": None})
            await self.channel.delete_many({"channel_id": None})
            await self.pending.delete_many({"user_id": None})

            # Create fast unique indexes
            await self.col.create_index("id", unique=True)
            await self.channel.create_index("channel_id", unique=True)
            await self.pending.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
            await self.posts.create_index([("channel_id", 1), ("message_id", 1)], unique=True)
            await self.posts.create_index("created_at")
            await self.posts.create_index("auto_delete_at")
            await self.stats.create_index("date", unique=True)
            await self.followups.create_index("user_id", unique=True)
            await self.followups.create_index("due_ts")
        except Exception as e:
            log.error(f"Failed to clean duplicates / set indexes: {e}")

    async def new_channel(self, channel_id, admin_id=None, title=None, link=None):
        channel = {
            'channel_id': int(channel_id),
            'admin_id': int(admin_id) if admin_id else None,
            'title': title,
            'link': link,
            'mode': 'auto', # 'auto' or 'manual'
            'text': None,
            'buttons': None
        }
        try:
            await self.channel.insert_one(channel)
        except DuplicateKeyError:
            update_data = {}
            if admin_id:
                update_data['admin_id'] = int(admin_id)
            if title:
                update_data['title'] = title
            if link:
                update_data['link'] = link
            if update_data:
                await self.channel.update_one({'channel_id': int(channel_id)}, {'$set': update_data})

    async def update_channel(self, channel_id, title=None, link=None, admin_id=None):
        update_data = {}
        if title:
            update_data['title'] = title
        if link:
            update_data['link'] = link
        if admin_id:
            update_data['admin_id'] = int(admin_id)
        if update_data:
            await self.channel.update_one({'channel_id': int(channel_id)}, {'$set': update_data}, upsert=True)

    async def set_channel_mode(self, channel_id, mode):
        await self.channel.update_one({'channel_id': int(channel_id)}, {'$set': {'mode': mode}}, upsert=True)

    async def get_channel_mode(self, channel_id):
        cid = int(channel_id)
        doc = await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        return doc.get('mode', 'auto') if doc else 'auto'

    async def get_channel(self, channel_id):
        cid = int(channel_id)
        return await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})

    async def get_user_channels(self, user_id):
        uid = int(user_id)
        cursor = self.channel.find({'$or': [{'admin_id': uid}, {'admin_id': str(uid)}]})
        return await cursor.to_list(length=100)

    async def delete_channel(self, channel_id):
        cid = int(channel_id)
        await self.channel.delete_many({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        await self.pending.delete_many({'$or': [{'chat_id': cid}, {'chat_id': str(cid)}]})

    async def get_welcome(self, channel_id):
        cid = int(channel_id)
        if cid == 0:
            doc = await self.settings.find_one({'key': 'global_welcome'})
            if doc and doc.get('text'):
                return doc.get('text'), doc.get('buttons')

        channel = await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        if not channel:
            await self.new_channel(channel_id)
            channel = await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        if not channel:
            return None, None
        return channel.get('text', None), channel.get('buttons', None)

    async def update_welcome(self, channel_id, text, buttons):
        cid = int(channel_id)
        await self.channel.update_one(
            {'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]},
            {'$set': {'channel_id': cid, 'text': text, 'buttons': buttons}},
            upsert=True
        )
        if cid == 0:
            await self.settings.update_one(
                {'key': 'global_welcome'},
                {'$set': {'text': text, 'buttons': buttons}},
                upsert=True
            )

    async def get_global_welcome(self):
        doc = await self.settings.find_one({'key': 'global_welcome'})
        if doc and doc.get('text'):
            return doc.get('text'), doc.get('buttons')
        return await self.get_welcome(0)

    async def update_global_welcome(self, text, buttons):
        await self.settings.update_one(
            {'key': 'global_welcome'},
            {'$set': {'text': text, 'buttons': buttons}},
            upsert=True
        )
        cid = 0
        await self.channel.update_one(
            {'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]},
            {'$set': {'channel_id': cid, 'text': text, 'buttons': buttons}},
            upsert=True
        )

    # ==========================================
    # PENDING REQUEST QUEUE METHODS
    # ==========================================
    async def add_pending(self, chat_id, user_id, name=""):
        doc = {
            'chat_id': int(chat_id),
            'user_id': int(user_id),
            'name': name or "",
            'created_at': datetime.datetime.utcnow()
        }
        try:
            await self.pending.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    async def get_pending_requests(self, chat_id, limit=100):
        cid = int(chat_id)
        cursor = self.pending.find({
            '$or': [{'chat_id': cid}, {'chat_id': str(cid)}]
        }).sort('created_at', 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def remove_pending(self, chat_id, user_id):
        cid = int(chat_id)
        uid = int(user_id)
        await self.pending.delete_many({
            '$or': [
                {'chat_id': cid, 'user_id': uid},
                {'chat_id': str(cid), 'user_id': uid},
                {'chat_id': cid, 'user_id': str(uid)},
                {'chat_id': str(cid), 'user_id': str(uid)}
            ]
        })

    async def total_pending_count(self, chat_id):
        cid = int(chat_id)
        return await self.pending.count_documents({
            '$or': [{'chat_id': cid}, {'chat_id': str(cid)}]
        })

    # ==========================================
    # USER & STATS METHODS
    # ==========================================
    async def add_user(self, id):
        user = dict(id=int(id), joined_at=datetime.datetime.utcnow())
        try:
            await self.col.insert_one(user)
        except DuplicateKeyError:
            pass

    async def is_user_exist(self, id):
        user = await self.col.find_one({'id': int(id)})
        return True if user else False

    async def total_users_count(self):
        return await self.col.count_documents({})

    async def get_all_channels(self):
        return self.channel.find({}).batch_size(1000)

    async def get_all_users(self):
        return self.col.find({}).batch_size(1000)

    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})

    async def get_setting(self, key, default=None):
        doc = await self.settings.find_one({'key': str(key)})
        return doc.get('value', default) if doc else default

    async def set_setting(self, key, value):
        await self.settings.update_one({'key': str(key)}, {'$set': {'value': value}}, upsert=True)

    # ==========================================
    # DIRECT INVITE LINK METHODS
    # ==========================================
    async def update_channel_direct_link(self, channel_id, direct_link):
        cid = int(channel_id)
        await self.channel.update_one(
            {'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]},
            {'$set': {'direct_link': direct_link}}
        )

    async def get_channel_direct_link(self, channel_id):
        cid = int(channel_id)
        doc = await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        return doc.get('direct_link') if doc else None

    # ==========================================
    # BOT SENT POSTS & AUTO-DELETE METHODS
    # ==========================================
    async def save_bot_post(self, channel_id, message_id, channel_title="", post_type="text", text_snippet="", auto_delete_at=None, auto_delete_ts=None, direct_link=None):
        if auto_delete_at and not auto_delete_ts:
            try:
                auto_delete_ts = auto_delete_at.timestamp()
            except Exception:
                pass
        doc = {
            'channel_id': int(channel_id),
            'message_id': int(message_id),
            'channel_title': channel_title or f"Channel {channel_id}",
            'post_type': post_type,
            'text_snippet': text_snippet[:100] if text_snippet else "",
            'created_at': datetime.datetime.utcnow(),
            'auto_delete_at': auto_delete_at,
            'auto_delete_ts': auto_delete_ts,
            'deleted': False
        }
        if direct_link:
            doc['direct_link'] = direct_link
        await self.posts.update_one(
            {'channel_id': int(channel_id), 'message_id': int(message_id)},
            {'$set': doc},
            upsert=True
        )

    async def get_bot_posts(self, limit=10, skip=0, include_deleted=False):
        filter_query = {} if include_deleted else {'deleted': {'$ne': True}}
        cursor = self.posts.find(filter_query).sort('created_at', -1).skip(skip).limit(limit)
        return await cursor.to_list(length=limit)

    async def count_bot_posts(self, include_deleted=False):
        filter_query = {} if include_deleted else {'deleted': {'$ne': True}}
        return await self.posts.count_documents(filter_query)

    async def get_bot_post(self, channel_id, message_id):
        cid = int(channel_id)
        mid = int(message_id)
        return await self.posts.find_one({
            '$or': [
                {'channel_id': cid, 'message_id': mid},
                {'channel_id': str(cid), 'message_id': mid}
            ]
        })

    async def mark_post_deleted(self, channel_id, message_id):
        cid = int(channel_id)
        mid = int(message_id)
        await self.posts.update_one(
            {'$or': [
                {'channel_id': cid, 'message_id': mid},
                {'channel_id': str(cid), 'message_id': mid}
            ]},
            {'$set': {'deleted': True, 'deleted_at': datetime.datetime.utcnow()}}
        )

    async def get_pending_auto_delete_posts(self):
        import time
        now_dt = datetime.datetime.utcnow()
        now_ts = time.time()
        cursor = self.posts.find({
            'deleted': {'$ne': True},
            '$or': [
                {'auto_delete_ts': {'$ne': None, '$lte': now_ts}},
                {'auto_delete_at': {'$ne': None, '$lte': now_dt}}
            ]
        }).limit(50)
        return await cursor.to_list(length=50)


    # ==========================================
    # PER-CHANNEL WELCOME MEDIA
    # ==========================================
    async def set_welcome_media(self, channel_id, media_type, file_id):
        cid = int(channel_id)
        await self.channel.update_one(
            {'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]},
            {'$set': {'channel_id': cid, 'media_type': media_type, 'media_file_id': file_id}},
            upsert=True
        )

    async def get_welcome_media(self, channel_id):
        """Returns (media_type, file_id) or (None, None)."""
        cid = int(channel_id)
        doc = await self.channel.find_one({'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]})
        if doc and doc.get('media_type') and doc.get('media_file_id'):
            return doc['media_type'], doc['media_file_id']
        return None, None

    async def clear_welcome_media(self, channel_id):
        cid = int(channel_id)
        await self.channel.update_one(
            {'$or': [{'channel_id': cid}, {'channel_id': str(cid)}]},
            {'$set': {'media_type': None, 'media_file_id': None}}
        )

    # ==========================================
    # DAILY STATS COUNTERS (dashboard)
    # ==========================================
    async def inc_stat(self, field, amount=1):
        """Never raises: stats must not break the main flow."""
        if not amount:
            return
        try:
            day = datetime.datetime.utcnow().date().isoformat()
            await self.stats.update_one({'date': day}, {'$inc': {field: int(amount)}}, upsert=True)
        except Exception as e:
            log.debug(f"inc_stat({field}) failed: {e}")

    async def get_stats_days(self, days=7):
        today = datetime.datetime.utcnow().date()
        wanted = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days)]
        rows = await self.stats.find({'date': {'$in': wanted}}).to_list(length=days + 1)
        return fill_daily_stats(rows, days, today)

    async def get_stats_totals(self):
        pipeline = [{'$group': dict({'_id': None}, **{f: {'$sum': f'${f}'} for f in STAT_FIELDS})}]
        rows = await self.stats.aggregate(pipeline).to_list(length=1)
        row = rows[0] if rows else {}
        return {f: int(row.get(f, 0) or 0) for f in STAT_FIELDS}

    async def get_dashboard_stats(self):
        now = datetime.datetime.utcnow()
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_7d = start_today - datetime.timedelta(days=6)
        real_channels = {'channel_id': {'$nin': [0, '0']}}

        days = await self.get_stats_days(7)
        totals = await self.get_stats_totals()
        return {
            'users_total': await self.col.count_documents({}),
            'users_today': await self.col.count_documents({'joined_at': {'$gte': start_today}}),
            'users_7d': await self.col.count_documents({'joined_at': {'$gte': start_7d}}),
            'channels_total': await self.channel.count_documents(real_channels),
            'channels_manual': await self.channel.count_documents(dict(real_channels, mode='manual')),
            'pending_total': await self.pending.count_documents({}),
            'followups_pending': await self.followups.count_documents({}),
            'posts_live': await self.count_bot_posts(),
            'days': days,
            'today': days[-1] if days else {},
            'totals': totals,
        }

    # ==========================================
    # FOLLOW-UP QUEUE (one pending follow-up per user)
    # ==========================================
    async def add_followup(self, user_id, name, chat_title, delay_seconds):
        """Schedules a follow-up DM. If the user already has one pending, keep the earlier one."""
        try:
            await self.followups.update_one(
                {'user_id': int(user_id)},
                {'$setOnInsert': {
                    'user_id': int(user_id),
                    'name': name or "",
                    'chat_title': chat_title or "",
                    'due_ts': time.time() + float(delay_seconds),
                    'attempts': 0,
                    'created_at': datetime.datetime.utcnow(),
                }},
                upsert=True
            )
            return True
        except Exception as e:
            log.warning(f"add_followup failed for {user_id}: {e}")
            return False

    async def get_due_followups(self, limit=50):
        cursor = self.followups.find({'due_ts': {'$lte': time.time()}}).sort('due_ts', 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def delete_followup(self, user_id):
        await self.followups.delete_many({'user_id': int(user_id)})

    async def retry_followup_later(self, user_id, delay_seconds=900):
        await self.followups.update_one(
            {'user_id': int(user_id)},
            {'$inc': {'attempts': 1}, '$set': {'due_ts': time.time() + delay_seconds}}
        )
