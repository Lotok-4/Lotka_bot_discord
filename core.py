import discord
from discord.ext import commands, tasks
import datetime
import sqlite3
import asyncio
import yt_dlp
import os
import aiohttp
import io
import json
import shutil
import tempfile
import re
import random
from PIL import Image
from playwright.async_api import async_playwright

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents, heartbeat_timeout=60.0)

# --- НАСТРОЙКИ ---
WELCOME_CHANNEL_ID = 1461697172733956179
ALLOWED_ROLE_NAME = "Участник Жижернии"
NEW_VOICE_CATEGORY_ID = 1461414329181278270
CREATE_CHANNEL_ID = 1461416653827870772
STATS_MEMBERS_CH_ID = 1567221094031823049
MUSIC_FOLDER = "ss14_music"
SETTINGS_FILE = "settings.json"

# Настройки для поиска
YTDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'default_search': 'ytsearch',
    'quiet': True,
    'extract_flat': True,
    'remote_components': 'ejs:github',
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtubepot-bgutilhttp': {'base_url': ['http://127.0.0.1:4416']},
        'youtube': {'player_client': ['tv', 'mweb']}
    },
}

# Настройки для СКАЧИВАНИЯ (не стриминга!)
YTDL_DOWNLOAD_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'remote_components': 'ejs:github',
    'source_address': '0.0.0.0',
    'outtmpl': '', # задается динамически
    'extractor_args': {
        'youtubepot-bgutilhttp': {'base_url': ['http://127.0.0.1:4416']},
        'youtube': {'player_client': ['tv', 'mweb']}
    },
}

FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 'options': '-vn'}
LOCAL_FFMPEG_OPTIONS = {'options': '-vn'}

ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS)

music_queues = {}
current_tracks = {}
loop_playlist_data = {} 

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {"fast_play": False}

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, channel_name TEXT, user_limit INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS active_channels (channel_id INTEGER PRIMARY KEY, user_id INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS warns (user_id INTEGER PRIMARY KEY, warn_count INTEGER DEFAULT 0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS economy (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 1000)")
    conn.commit()
    conn.close()
init_db()

# --- СОБЫТИЯ ---
@bot.event
async def on_ready():
    print(f"Lotka bot v18 | {bot.user.name} | Готов к работе!")
    await bot.change_presence(activity=discord.Game(name="!help"))
    if STATS_MEMBERS_CH_ID and not update_server_stats.is_running():
        update_server_stats.start()
        await update_server_stats()

@tasks.loop(minutes=10)
async def update_server_stats():
    if not bot.guilds: return
    guild = bot.guilds[0]
    ch_members = bot.get_channel(STATS_MEMBERS_CH_ID)
    if ch_members:
        try: await ch_members.edit(name=f"👥 Участников империи: {guild.member_count}")
        except: pass

@bot.event
async def on_command_completion(ctx):
    try: await ctx.message.delete()
    except: pass

@bot.event
async def on_voice_state_update(member, before, after):
    if after.channel and after.channel.id == CREATE_CHANNEL_ID:
        category = discord.utils.get(member.guild.categories, id=NEW_VOICE_CATEGORY_ID)
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT channel_name, user_limit FROM user_settings WHERE user_id = ?", (member.id,))
        saved = cursor.fetchone()
        
        if saved: name, limit = saved
        else:
            name, limit = f"🏰 Комната {member.display_name}", 0
            cursor.execute("INSERT INTO user_settings VALUES (?, ?, ?)", (member.id, name, limit))
            conn.commit()
            
        new_channel = await member.guild.create_voice_channel(name=name, category=category, user_limit=limit)
        cursor.execute("INSERT INTO active_channels VALUES (?, ?)", (new_channel.id, member.id))
        conn.commit()
        conn.close()
        await member.move_to(new_channel)

    if before.channel:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM active_channels WHERE channel_id = ?", (before.channel.id,))
        if cursor.fetchone() and len(before.channel.members) == 0:
            cursor.execute("DELETE FROM active_channels WHERE channel_id = ?", (before.channel.id,))
            conn.commit()
            try: await before.channel.delete()
            except: pass
        conn.close()

# --- МУЗЫКАЛЬНЫЙ ДВИЖОК (СКАЧИВАНИЕ) ---
async def download_and_play(ctx, track, voice_client):
    guild_id = ctx.guild.id
    
    if track.get('is_local'):
        voice_client.play(discord.FFmpegPCMAudio(track['url'], **LOCAL_FFMPEG_OPTIONS), after=lambda e: play_next(ctx, e))
        return

    video_id = track.get('id')
    if not video_id:
        match = re.search(r'v=([^&]+)', track.get('webpage', ''))
        if match: video_id = match.group(1)

    if not video_id:
        print("[ОШИБКА] Нет ID видео")
        play_next(ctx)
        return

    # Скачиваем во временный файл
    temp_dir = tempfile.gettempdir()
    safe_title = re.sub(r'[\\/*?:"<>|]', "", track['title']).strip()[:50]
    safe_title = safe_title if safe_title else "track"
    file_path = os.path.join(temp_dir, f"{safe_title}.webm")

    download_opts = YTDL_DOWNLOAD_OPTIONS.copy()
    download_opts['outtmpl'] = file_path

    try:
        loop = asyncio.get_event_loop()
        def _download():
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        
        await loop.run_in_executor(None, _download)
        
        track['local_path'] = file_path
        voice_client.play(discord.FFmpegPCMAudio(file_path), after=lambda e: play_next(ctx, e))
        
    except Exception as e:
        print(f"[ОШИБКА СКАЧИВАНИЯ] {e}")
        play_next(ctx)

def play_next(ctx, error=None):
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client

    if error: print(f"[FFMPEG] {error}")
    
    if guild_id in music_queues and music_queues[guild_id].get('current'):
        old_track = music_queues[guild_id]['current']
        if old_track.get('local_path') and os.path.exists(old_track['local_path']):
            try: os.remove(old_track['local_path'])
            except: pass

    if not voice_client or not voice_client.is_connected():
        if guild_id in music_queues:
            music_queues[guild_id]['current'] = None
            music_queues[guild_id]['queue'].clear()
        bot.loop.create_task(bot.change_presence(activity=discord.Game(name="!help")))
        return

    if guild_id in music_queues and music_queues[guild_id].get('loop') and music_queues[guild_id].get('current'):
        track = music_queues[guild_id]['current']
        bot.loop.create_task(download_and_play(ctx, track, voice_client))
        return

    if guild_id in music_queues and len(music_queues[guild_id]['queue']) > 0:
        next_track = music_queues[guild_id]['queue'].pop(0)
        music_queues[guild_id]['current'] = next_track
        
        bot.loop.create_task(bot.change_presence(activity=discord.Game(name=f"🎵 {next_track['title']}")))
        
        embed = discord.Embed(title="🎶 Играет следующий трек", description=f"**{next_track['title']}**", color=discord.Color.from_rgb(220, 20, 60))
        if next_track.get('thumbnail'):
            embed.set_thumbnail(url=next_track['thumbnail'])
        embed.add_field(name="⏳ Длительность", value=f"`{next_track.get('duration', '???')}`", inline=True)
        embed.add_field(name="🎬 Автор", value=f"`{next_track.get('uploader', 'Неизвестно')}`", inline=True)
        embed.add_field(name="👤 Заказал", value=next_track.get('author', 'Неизвестно'), inline=True)
        bot.loop.create_task(ctx.send(embed=embed))

        bot.loop.create_task(download_and_play(ctx, next_track, voice_client))
    else:
        if guild_id in loop_playlist_data:
            pl_name = loop_playlist_data[guild_id]['name']
            pl_tracks = loop_playlist_data[guild_id]['tracks']
            if pl_tracks:
                music_queues[guild_id]['queue'] = pl_tracks.copy()
                next_track = music_queues[guild_id]['queue'].pop(0)
                music_queues[guild_id]['current'] = next_track
                bot.loop.create_task(bot.change_presence(activity=discord.Game(name=f"🎵 {next_track['title']}")))
                
                embed = discord.Embed(title="🔂 Плейлист зациклен", description=f"Начинаю заново: **{pl_name}**", color=discord.Color.from_rgb(220, 20, 60))
                bot.loop.create_task(ctx.send(embed=embed))
                
                bot.loop.create_task(download_and_play(ctx, next_track, voice_client))
                return
        
        if guild_id in music_queues: music_queues[guild_id]['current'] = None
        bot.loop.create_task(bot.change_presence(activity=discord.Game(name="!help")))

async def ensure_voice(ctx):
    if not ctx.author.voice: return None
    voice_client = ctx.voice_client
    if not voice_client:
        voice_client = await ctx.author.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
    elif voice_client.channel != ctx.author.voice.channel:
        await voice_client.move_to(ctx.author.voice.channel)
    return voice_client

async def add_to_queue(ctx, track, user_id=None):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        music_queues[guild_id] = {'queue': [], 'loop': False, 'current': None}

    if user_id: current_tracks[user_id] = track
    elif hasattr(ctx, 'author'): current_tracks[ctx.author.id] = track

    track['author'] = f"<@{user_id}>" if user_id else (ctx.author.mention if hasattr(ctx, 'author') else "Неизвестно")

    voice_client = ctx.voice_client
    if voice_client.is_playing() or voice_client.is_paused():
        music_queues[guild_id]['queue'].append(track)
        if hasattr(ctx, 'send'): await ctx.send(f"⏳ В очередь добавлен: **{track['title']}**")
        return
    
    music_queues[guild_id]['current'] = track
    
    embed = discord.Embed(title="🎶 Сейчас играет", description=f"**{track['title']}**", color=discord.Color.from_rgb(220, 20, 60))
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    embed.add_field(name="⏳ Длительность", value=f"`{track.get('duration', '???')}`", inline=True)
    embed.add_field(name="🎬 Автор", value=f"`{track.get('uploader', 'Неизвестно')}`", inline=True)
    embed.add_field(name="👤 Заказал", value=track.get('author', 'Неизвестно'), inline=True)
    if hasattr(ctx, 'send'): await ctx.send(embed=embed)

    await bot.change_presence(activity=discord.Game(name=f"🎵 {track['title']}"))
    bot.loop.create_task(download_and_play(ctx, track, voice_client))

# --- ФАЙЛЫ СОХРАНЕНИЯ ---
def load_favorites():
    if os.path.exists("favorites.json"):
        with open("favorites.json", "r", encoding="utf-8") as f: return json.load(f)
    return {}

def save_favorites(data):
    with open("favorites.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def load_playlists():
    if os.path.exists("playlists.json"):
        with open("playlists.json", "r", encoding="utf-8") as f: return json.load(f)
    return {}

def save_playlists(data):
    with open("playlists.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# --- МЕНЮ ВЫБОРА ТРЕКА ---
class YouTubeSearchView(discord.ui.View):
    def __init__(self, results, ctx):
        super().__init__(timeout=60)
        self.results = results
        self.ctx = ctx
        for i, entry in enumerate(results[:5]):
            title = entry.get('title', 'Без названия')[:50]
            btn = discord.ui.Button(label=f"{i+1}. {title}", style=discord.ButtonStyle.secondary, row=i // 2)
            btn.callback = self.make_callback(entry)
            self.add_item(btn)
            
    def make_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            for child in self.children: child.disabled = True
            await interaction.response.edit_message(content=f"✅ Выбрано: **{entry['title']}**. Скачиваю...", embed=None, view=self)
            
            track = {
                'id': entry.get('id'),
                'url': entry.get('url'),
                'title': entry.get('title'),
                'webpage': entry.get('webpage_url', ''),
                'thumbnail': entry.get('thumbnail', None),
                'duration': entry.get('duration', 0),
                'uploader': entry.get('uploader', 'Неизвестно'),
                'author': interaction.user.mention,
                'is_local': False
            }
            await add_to_queue(self.ctx, track, user_id=interaction.user.id)
        return callback

class PlaylistTrackSelect(discord.ui.View):
    def __init__(self, results, ctx, playlist_name):
        super().__init__(timeout=60)
        self.results = results
        self.ctx = ctx
        self.playlist_name = playlist_name
        for i, entry in enumerate(results[:5]):
            title = entry.get('title', 'Без названия')[:50]
            btn = discord.ui.Button(label=f"{i+1}. {title}", style=discord.ButtonStyle.secondary, row=i // 2)
            btn.callback = self.make_callback(entry)
            self.add_item(btn)
            
    def make_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            for child in self.children: child.disabled = True
            await interaction.response.edit_message(content=f"✅ Добавлено: **{entry['title']}**", embed=None, view=self)
            
            track = {
                'id': entry.get('id'),
                'url': entry.get('url'),
                'title': entry.get('title'),
                'webpage': entry.get('webpage_url', ''),
                'thumbnail': entry.get('thumbnail', None),
                'duration': entry.get('duration', 0),
                'uploader': entry.get('uploader', 'Неизвестно'),
                'author': interaction.user.mention,
                'is_local': False
            }
            playlists = load_playlists()
            uid = str(interaction.user.id)
            if uid not in playlists: playlists[uid] = {}
            if self.playlist_name not in playlists[uid]: playlists[uid][self.playlist_name] = []
            playlists[uid][self.playlist_name].append(track)
            save_playlists(playlists)
        return callback

class AddCurrentToPlaylistView(discord.ui.View):
    def __init__(self, user_id, track):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.track = track
        playlists = load_playlists()
        user_pl = playlists.get(str(user_id), {})
        
        if not user_pl:
            self.add_item(discord.ui.Button(label="У вас нет плейлистов", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for i, name in enumerate(list(user_pl.keys())[:10]):
                btn = discord.ui.Button(label=f"🎵 {name}", style=discord.ButtonStyle.primary, row=i // 5)
                btn.callback = self.make_callback(name)
                self.add_item(btn)

    def make_callback(self, playlist_name):
        async def callback(interaction: discord.Interaction):
            playlists = load_playlists()
            uid = str(interaction.user.id)
            if uid in playlists and playlist_name in playlists[uid]:
                if any(t['title'] == self.track['title'] for t in playlists[uid][playlist_name]):
                    return await interaction.response.send_message("❌ Этот трек уже есть в плейлисте.", ephemeral=True)
                playlists[uid][playlist_name].append(self.track)
                save_playlists(playlists)
                await interaction.response.send_message(f"✅ Трек добавлен в плейлист **{playlist_name}**!", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Плейлист не найден.", ephemeral=True)
        return callback

class DeleteTrackView(discord.ui.View):
    def __init__(self, playlist_name, tracks):
        super().__init__(timeout=60)
        self.playlist_name = playlist_name
        for i, track in enumerate(tracks[:10]):
            btn = discord.ui.Button(label=track['title'][:50], style=discord.ButtonStyle.danger, row=i // 5)
            btn.callback = self.make_callback(i, track['title'])
            self.add_item(btn)

    def make_callback(self, index, track_title):
        async def callback(interaction: discord.Interaction):
            playlists = load_playlists()
            uid = str(interaction.user.id)
            if uid in playlists and self.playlist_name in playlists[uid]:
                tracks = playlists[uid][self.playlist_name]
                for i, t in enumerate(tracks):
                    if t['title'] == track_title:
                        tracks.pop(i)
                        break
                save_playlists(playlists)
                await interaction.response.send_message(f"✅ Трек **{track_title}** удален из плейлиста!", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ошибка при удалении.", ephemeral=True)
        return callback

class PlaylistTracksView(discord.ui.View):
    def __init__(self, user_id, playlist_name, tracks, ctx):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.playlist_name = playlist_name
        self.tracks = tracks
        self.ctx = ctx
        
        play_all_btn = discord.ui.Button(label="▶️ Играть плейлист", style=discord.ButtonStyle.success, row=0)
        play_all_btn.callback = self.play_all_callback
        self.add_item(play_all_btn)
        
        loop_playlist_btn = discord.ui.Button(label="🔂 Зациклить плейлист", style=discord.ButtonStyle.primary, row=0)
        loop_playlist_btn.callback = self.loop_playlist_callback
        self.add_item(loop_playlist_btn)
        
        for i, track in enumerate(tracks[:8]):
            btn = discord.ui.Button(label=track['title'][:45], style=discord.ButtonStyle.secondary, row=(i // 4) + 1)
            btn.callback = self.make_callback(track)
            self.add_item(btn)

        del_btn = discord.ui.Button(label="🗑️ Удалить трек", style=discord.ButtonStyle.danger, row=4)
        del_btn.callback = self.delete_track_callback
        self.add_item(del_btn)
        
        add_current_btn = discord.ui.Button(label="➕ Добавить текущий", style=discord.ButtonStyle.success, row=4)
        add_current_btn.callback = self.add_current_callback
        self.add_item(add_current_btn)

    async def play_all_callback(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            if interaction.user.voice: voice_client = await interaction.user.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
            else: return await interaction.response.send_message("❌ Зайдите в голосовой канал!", ephemeral=True)
        
        await interaction.response.send_message(f"▶️ Включаю плейлист **{self.playlist_name}**...", ephemeral=True)
        
        if interaction.guild.id in loop_playlist_data:
            del loop_playlist_data[interaction.guild.id]
            
        class FakeCtx:
            def __init__(self, guild, author):
                self.guild = guild; self.author = author; self.voice_client = guild.voice_client
            async def send(self, *args, **kwargs): pass
        
        fake_ctx = FakeCtx(interaction.guild, interaction.user)
        guild_id = interaction.guild.id
        
        if guild_id in music_queues:
            music_queues[guild_id]['queue'].clear()
            
        for i, track in enumerate(self.tracks):
            if i == 0:
                await add_to_queue(fake_ctx, track, user_id=interaction.user.id)
            else:
                music_queues[guild_id]['queue'].append(track)

    async def loop_playlist_callback(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            if interaction.user.voice: voice_client = await interaction.user.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
            else: return await interaction.response.send_message("❌ Зайдите в голосовой канал!", ephemeral=True)
        
        await interaction.response.send_message(f"🔂 Плейлист **{self.playlist_name}** зациклен!", ephemeral=True)
        
        loop_playlist_data[interaction.guild.id] = {
            'name': self.playlist_name,
            'tracks': self.tracks.copy()
        }
        
        class FakeCtx:
            def __init__(self, guild, author):
                self.guild = guild; self.author = author; self.voice_client = guild.voice_client
            async def send(self, *args, **kwargs): pass
        
        fake_ctx = FakeCtx(interaction.guild, interaction.user)
        guild_id = interaction.guild.id
        
        if guild_id in music_queues:
            music_queues[guild_id]['queue'].clear()
            
        for i, track in enumerate(self.tracks):
            if i == 0:
                await add_to_queue(fake_ctx, track, user_id=interaction.user.id)
            else:
                music_queues[guild_id]['queue'].append(track)

    def make_callback(self, track):
        async def callback(interaction: discord.Interaction):
            voice_client = interaction.guild.voice_client
            if not voice_client:
                if interaction.user.voice: voice_client = await interaction.user.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
                else: return await interaction.response.send_message("❌ Зайдите в голосовой канал!", ephemeral=True)
            
            await interaction.response.send_message(f"🎶 Включаю из плейлиста: **{track['title']}**", ephemeral=True)
            
            if interaction.guild.id in loop_playlist_data:
                del loop_playlist_data[interaction.guild.id]
                
            class FakeCtx:
                def __init__(self, guild, author):
                    self.guild = guild; self.author = author; self.voice_client = guild.voice_client
                async def send(self, *args, **kwargs): pass
            await add_to_queue(FakeCtx(interaction.guild, interaction.user), track, user_id=interaction.user.id)
        return callback

    async def delete_track_callback(self, interaction: discord.Interaction):
        if not self.tracks:
            return await interaction.response.send_message("📂 Плейлист пуст.", ephemeral=True)
        embed = discord.Embed(title=f"🗑️ Удалить трек из {self.playlist_name}", description="Нажмите на трек, который хотите удалить.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, view=DeleteTrackView(self.playlist_name, self.tracks), ephemeral=True)

    async def add_current_callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        current_track = current_tracks.get(interaction.user.id) or (music_queues.get(gid, {}).get('current'))
        
        if not current_track:
            return await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            
        playlists = load_playlists()
        uid = str(interaction.user.id)
        if uid in playlists and self.playlist_name in playlists[uid]:
            if any(t['title'] == current_track['title'] for t in playlists[uid][self.playlist_name]):
                return await interaction.response.send_message("❌ Этот трек уже есть в плейлисте.", ephemeral=True)
            playlists[uid][self.playlist_name].append(current_track)
            save_playlists(playlists)
            await interaction.response.send_message(f"✅ Текущий трек **{current_track['title']}** добавлен в плейлист **{self.playlist_name}**!", ephemeral=True)

class DeletePlaylistView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        playlists = load_playlists()
        user_pl = playlists.get(str(user_id), {})
        
        if not user_pl:
            self.add_item(discord.ui.Button(label="Нет плейлистов для удаления", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for i, name in enumerate(list(user_pl.keys())[:10]):
                btn = discord.ui.Button(label=f"❌ {name}", style=discord.ButtonStyle.danger, row=i // 5)
                btn.callback = self.make_callback(name, user_id)
                self.add_item(btn)

    def make_callback(self, playlist_name, user_id):
        async def callback(interaction: discord.Interaction):
            playlists = load_playlists()
            uid = str(user_id)
            if uid in playlists and playlist_name in playlists[uid]:
                del playlists[uid][playlist_name]
                if not playlists[uid]: del playlists[uid]
                save_playlists(playlists)
                await interaction.response.send_message(f"✅ Плейлист **{playlist_name}** удален!", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Плейлист не найден.", ephemeral=True)
        return callback

class PlaylistsMenuView(discord.ui.View):
    def __init__(self, user_id, ctx):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.ctx = ctx
        playlists = load_playlists()
        user_pl = playlists.get(str(user_id), {})
        
        if not user_pl:
            self.add_item(discord.ui.Button(label="У вас нет плейлистов", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for i, name in enumerate(list(user_pl.keys())[:10]):
                btn = discord.ui.Button(label=f"🎵 {name}", style=discord.ButtonStyle.success, row=i // 5)
                btn.callback = self.make_callback(name)
                self.add_item(btn)
                
        del_btn = discord.ui.Button(label="🗑️ Удалить плейлист", style=discord.ButtonStyle.danger, row=4)
        del_btn.callback = self.delete_callback
        self.add_item(del_btn)

    def make_callback(self, playlist_name):
        async def callback(interaction: discord.Interaction):
            playlists = load_playlists()
            tracks = playlists.get(str(interaction.user.id), {}).get(playlist_name, [])
            if not tracks:
                return await interaction.response.send_message("📂 Этот плейлист пуст.", ephemeral=True)
                
            embed = discord.Embed(title=f"🎵 Плейлист: {playlist_name}", description=f"Треков: **{len(tracks)}**.", color=discord.Color.from_rgb(220, 20, 60))
            await interaction.response.send_message(embed=embed, view=PlaylistTracksView(interaction.user.id, playlist_name, tracks, self.ctx), ephemeral=True)
        return callback
        
    async def delete_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Выберите плейлист для удаления:", view=DeletePlaylistView(interaction.user.id), ephemeral=True)

class FavoritesView(discord.ui.View):
    def __init__(self, user_id, bot_ctx, guild):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.bot_ctx = bot_ctx
        self.guild = guild
        favs = load_favorites()
        user_favs = favs.get(str(user_id), [])
        
        if not user_favs:
            self.add_item(discord.ui.Button(label="Пусто... Добавьте треки кнопкой ⭐", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            for i, track in enumerate(user_favs[:10]):
                btn = discord.ui.Button(label=track['title'][:50], style=discord.ButtonStyle.primary, row=i // 5)
                btn.callback = self.make_callback(track)
                self.add_item(btn)

    def make_callback(self, track):
        async def callback(interaction: discord.Interaction):
            voice_client = interaction.guild.voice_client
            if not voice_client:
                if interaction.user.voice: voice_client = await interaction.user.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
                else: return await interaction.response.send_message("❌ Зайдите в голосовой канал!", ephemeral=True)
            
            await interaction.response.send_message(f"🎶 Включаю из избранного: **{track['title']}**", ephemeral=True)
            class FakeCtx:
                def __init__(self, guild, author):
                    self.guild = guild; self.author = author; self.voice_client = guild.voice_client
                async def send(self, *args, **kwargs): pass
            await add_to_queue(FakeCtx(interaction.guild, interaction.user), track, user_id=interaction.user.id)
        return callback

class MusicView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Пауза/Продолжить", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_resume(self, interaction, button):
        vc = self.ctx.voice_client
        if not vc: return await interaction.response.send_message("❌ Бот не в канале.", ephemeral=True)
        if vc.is_playing(): vc.pause(); await interaction.response.send_message("⏸️ Пауза", ephemeral=True)
        elif vc.is_paused(): vc.resume(); await interaction.response.send_message("▶️ Продолжаю", ephemeral=True)

    @discord.ui.button(label="Пропустить", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction, button):
        vc = self.ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop(); await interaction.response.send_message("⏭️ Пропущено", ephemeral=True)
        else: await interaction.response.send_message("❌ Ничего не играет.", ephemeral=True)

    @discord.ui.button(label="Повтор", style=discord.ButtonStyle.secondary, emoji="🔂")
    async def loop(self, interaction, button):
        gid = self.ctx.guild.id
        if gid not in music_queues: return await interaction.response.send_message("❌ Пусто.", ephemeral=True)
        music_queues[gid]['loop'] = not music_queues[gid]['loop']
        await interaction.response.send_message(f"🔂 Повтор: {'Вкл' if music_queues[gid]['loop'] else 'Выкл'}", ephemeral=True)

    @discord.ui.button(label="Очередь", style=discord.ButtonStyle.secondary, emoji="📜")
    async def queue(self, interaction, button):
        gid = self.ctx.guild.id
        if gid not in music_queues or not music_queues[gid]['queue']: return await interaction.response.send_message("📜 Очередь пуста.", ephemeral=True)
        q_list = "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(music_queues[gid]['queue'][:10])])
        await interaction.response.send_message(f"**Очередь:**\n{q_list}", ephemeral=True)

    @discord.ui.button(label="⭐ В избранное", style=discord.ButtonStyle.success, emoji="⭐")
    async def add_fav(self, interaction, button):
        track = current_tracks.get(interaction.user.id) or (music_queues.get(interaction.guild.id, {}).get('current'))
        if not track: return await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            
        favs = load_favorites()
        user_id_str = str(interaction.user.id)
        if user_id_str not in favs: favs[user_id_str] = []
        if any(t['title'] == track['title'] for t in favs[user_id_str]):
            return await interaction.response.send_message("⭐ Этот трек уже есть в вашем избранном!", ephemeral=True)
            
        favs[user_id_str].append(track)
        save_favorites(favs)
        await interaction.response.send_message(f"✅ Трек **{track['title']}** добавлен в ваше избранное!", ephemeral=True)

    @discord.ui.button(label="Моё избранное", style=discord.ButtonStyle.secondary, emoji="🎧", row=1)
    async def my_favs(self, interaction, button):
        favs = load_favorites()
        user_favs = favs.get(str(interaction.user.id), [])
        embed = discord.Embed(title="🎧 Ваше избранное", description=f"У вас сохранено треков: **{len(user_favs)}**.", color=discord.Color.from_rgb(220, 20, 60))
        await interaction.response.send_message(embed=embed, view=FavoritesView(interaction.user.id, self.ctx, interaction.guild), ephemeral=True)

    @discord.ui.button(label="📂 Плейлисты", style=discord.ButtonStyle.primary, emoji="📂", row=1)
    async def my_playlists(self, interaction, button):
        playlists = load_playlists()
        user_pl = playlists.get(str(interaction.user.id), {})
        embed = discord.Embed(title="📂 Ваши плейлисты", description=f"Создано: **{len(user_pl)}**.", color=discord.Color.from_rgb(220, 20, 60))
        await interaction.response.send_message(embed=embed, view=PlaylistsMenuView(interaction.user.id, self.ctx), ephemeral=True)

    @discord.ui.button(label="➕ Добавить в плейлист", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add_to_pl(self, interaction, button):
        track = current_tracks.get(interaction.user.id) or (music_queues.get(interaction.guild.id, {}).get('current'))
        if not track: return await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
        
        embed = discord.Embed(title="Выберите плейлист", description=f"Куда добавить трек **{track['title']}**?", color=discord.Color.from_rgb(220, 20, 60))
        await interaction.response.send_message(embed=embed, view=AddCurrentToPlaylistView(interaction.user.id, track), ephemeral=True)

    @discord.ui.button(label="💾 Скачать текущий", style=discord.ButtonStyle.primary, emoji="💾", row=1)
    async def download_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = self.ctx.guild.id
        if gid not in music_queues or not music_queues[gid].get('current'):
            return await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            
        track = music_queues[gid]['current']
        await interaction.response.send_message(f"⏳ Скачиваю трек **{track['title']}** и отправляю в чат...", ephemeral=True)
        
        temp_dir = tempfile.gettempdir()
        safe_title = re.sub(r'[\\/*?:"<>|]', "", track['title']).strip()[:100]
        safe_title = safe_title if safe_title else "downloaded_track"
        temp_file_path = os.path.join(temp_dir, safe_title)
        
        try:
            if track.get('is_local'):
                shutil.copy(track['url'], f"{temp_file_path}.mp3")
                final_path = f"{temp_file_path}.mp3"
            else:
                video_url = track.get('webpage') or f"ytsearch1:{track['title']}"
                download_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': temp_file_path, 
                    'quiet': True,
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
                }
                loop = asyncio.get_event_loop()
                def _download():
                    with yt_dlp.YoutubeDL(download_opts) as ydl:
                        ydl.download([video_url])
                await loop.run_in_executor(None, _download)
                final_path = f"{temp_file_path}.mp3" 
                
            await interaction.followup.send(content=f"✅ Вот ваш файл: **{safe_title}.mp3**", file=discord.File(final_path), ephemeral=True)
            
        except Exception as e:
            print(f"[ОШИБКА СКАЧИВАНИЯ] {e}")
            await interaction.followup.send("❌ Ошибка при скачивании.", ephemeral=True)
        
        finally:
            if os.path.exists(final_path):
                try: os.remove(final_path)
                except: pass

    @discord.ui.button(label="Выйти", style=discord.ButtonStyle.danger, emoji="🚪", row=2)
    async def stop(self, interaction, button):
        vc = self.ctx.voice_client
        if vc:
            gid = self.ctx.guild.id
            if gid in music_queues:
                music_queues[gid]['queue'].clear()
                music_queues[gid]['current'] = None
                music_queues[gid]['loop'] = False
            if gid in loop_playlist_data:
                del loop_playlist_data[gid]
            await vc.disconnect()
            await bot.change_presence(activity=discord.Game(name="!help"))
            await interaction.response.send_message("👋 Вышел из канала.", ephemeral=True)
        else: await interaction.response.send_message("❌ Я не в канале.", ephemeral=True)

    @discord.ui.button(label="Список файлов", style=discord.ButtonStyle.success, emoji="📁", row=2)
    async def local_list_btn(self, interaction, button):
        if not os.path.exists(MUSIC_FOLDER): return await interaction.response.send_message(f"❌ Папка `{MUSIC_FOLDER}` не найдена!", ephemeral=True)
        files = [f for f in os.listdir(MUSIC_FOLDER) if os.path.isfile(os.path.join(MUSIC_FOLDER, f))]
        if not files: return await interaction.response.send_message("📂 Папка пуста.", ephemeral=True)
        file_list = "\n".join([f"• `{f}`" for f in files[:15]])
        if len(files) > 15: file_list += f"\n*...и еще {len(files) - 15} файлов*"
        embed = discord.Embed(title="🎵 Локальная музыка", description=f"Файлов: **{len(files)}**\n\n{file_list}", color=discord.Color.from_rgb(220, 20, 60))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Скачать файл", style=discord.ButtonStyle.success, emoji="📥", row=2)
    async def local_download_btn(self, interaction, button):
        await interaction.response.send_message("📥 Напишите в чат: `!local_download [название]`.", ephemeral=True)

@bot.command()
async def music(ctx):
    if not await ensure_voice(ctx): return await ctx.send("❌ Зайдите в голосовой канал!", delete_after=5)
    embed = discord.Embed(title="🎵 Плеер Лотка™", description="Управление кнопками ниже:", color=discord.Color.from_rgb(220, 20, 60))
    await ctx.send(embed=embed, view=MusicView(ctx))

@bot.command()
async def play(ctx, *, search: str):
    if not await ensure_voice(ctx): return await ctx.send("❌ Зайдите в канал!", delete_after=5)
    
    settings = load_settings()
    fast_mode = settings.get("fast_play", False)
    
    msg = await ctx.send(f"🔍 Ищу **{search}**...")
    
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(f"ytsearch5:{search}", download=False))
        results = data.get('entries', [])
        
        if not results:
            return await msg.edit(content="❌ Ничего не найдено.")
            
        if fast_mode:
            entry = results[0]
            await msg.edit(content=f"⏳ Включаю: **{entry['title']}**...")
            track = {
                'id': entry.get('id'),
                'url': entry.get('url'),
                'title': entry.get('title'),
                'webpage': entry.get('webpage_url', ''),
                'thumbnail': entry.get('thumbnail', None),
                'duration': entry.get('duration', 0),
                'uploader': entry.get('uploader', 'Неизвестно'),
                'author': ctx.author.mention,
                'is_local': False
            }
            await msg.delete()
            await add_to_queue(ctx, track, user_id=ctx.author.id)
        else:
            embed = discord.Embed(title="Выберите трек:", color=discord.Color.from_rgb(220, 20, 60))
            await msg.edit(content=None, embed=embed, view=YouTubeSearchView(results, ctx))
    except Exception as e:
        print(e)
        await msg.edit(content="❌ Ошибка поиска на YouTube.")

@bot.command()
async def play_config(ctx, mode: str = None):
    settings = load_settings()
    if mode == "default":
        settings["fast_play"] = True
        save_settings(settings)
        await ctx.send("✅ Включен быстрый поиск. Бот будет включать первый трек без меню выбора.", delete_after=5)
    elif mode == "menu":
        settings["fast_play"] = False
        save_settings(settings)
        await ctx.send("✅ Включен режим выбора. Бот будет показывать 5 вариантов.", delete_after=5)
    else:
        status = "Быстрый (1 трек)" if settings.get("fast_play") else "Меню (5 треков)"
        await ctx.send(f"⚙️ Текущий режим: **{status}**.\nИспользуй: `!play_config default` (1 трек) или `!play_config menu` (5 треков).", delete_after=10)

@bot.group(name="pl", invoke_without_command=True)
async def pl(ctx):
    await ctx.send("📂 Используйте: `!pl create [имя]`, `!pl add [имя] [запрос]`, `!pl del [имя]`, `!pl rmtrack [имя] [трек]`, `!pl list`")

@pl.command(name="create")
async def pl_create(ctx, *, name: str):
    playlists = load_playlists()
    uid = str(ctx.author.id)
    if uid not in playlists: playlists[uid] = {}
    if name in playlists[uid]:
        return await ctx.send(f"❌ Плейлист **{name}** уже существует!", delete_after=5)
    playlists[uid][name] = []
    save_playlists(playlists)
    await ctx.send(f"✅ Плейлист **{name}** создан!", delete_after=5)

@pl.command(name="del")
async def pl_del(ctx, *, name: str):
    playlists = load_playlists()
    uid = str(ctx.author.id)
    if uid in playlists and name in playlists[uid]:
        del playlists[uid][name]
        if not playlists[uid]: del playlists[uid]
        save_playlists(playlists)
        await ctx.send(f"✅ Плейлист **{name}** удален!", delete_after=5)
    else:
        await ctx.send(f"❌ Плейлист **{name}** не найден.", delete_after=5)

@pl.command(name="rmtrack")
async def pl_rmtrack(ctx, playlist_name: str, *, track_name: str):
    playlists = load_playlists()
    uid = str(ctx.author.id)
    if uid not in playlists or playlist_name not in playlists[uid]:
        return await ctx.send(f"❌ Плейлист **{playlist_name}** не найден.", delete_after=5)
        
    tracks = playlists[uid][playlist_name]
    for i, t in enumerate(tracks):
        if track_name.lower() in t['title'].lower():
            removed = tracks.pop(i)
            save_playlists(playlists)
            return await ctx.send(f"✅ Трек **{removed['title']}** удален из плейлиста **{playlist_name}**!", delete_after=5)
            
    await ctx.send(f"❌ Трек **{track_name}** не найден в плейлисте.", delete_after=5)

@pl.command(name="add")
async def pl_add(ctx, playlist_name: str, *, search: str):
    playlists = load_playlists()
    uid = str(ctx.author.id)
    if uid not in playlists or playlist_name not in playlists[uid]:
        return await ctx.send(f"❌ Плейлист **{playlist_name}** не найден! Создайте его: `!pl create {playlist_name}`", delete_after=5)
        
    msg = await ctx.send(f"🔍 Ищу **{search}** для плейлиста **{playlist_name}**...")
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(f"ytsearch5:{search}", download=False))
        results = data.get('entries', [])
        if not results:
            return await msg.edit(content="❌ Ничего не найдено.")
            
        embed = discord.Embed(title=f"Добавить в {playlist_name}:", color=discord.Color.from_rgb(220, 20, 60))
        await msg.edit(content=None, embed=embed, view=PlaylistTrackSelect(results, ctx, playlist_name))
    except Exception as e:
        print(e)
        await msg.edit(content="❌ Ошибка поиска.")

@pl.command(name="addcurrent")
async def pl_addcurrent(ctx):
    gid = ctx.guild.id
    track = current_tracks.get(ctx.author.id) or (music_queues.get(gid, {}).get('current'))
    
    if not track:
        return await ctx.send("❌ Сейчас ничего не играет!", delete_after=5)
        
    embed = discord.Embed(title="Выберите плейлист", description=f"Куда добавить трек **{track['title']}**?", color=discord.Color.from_rgb(220, 20, 60))
    await ctx.send(embed=embed, view=AddCurrentToPlaylistView(ctx.author.id, track))

@pl.command(name="list")
async def pl_list(ctx):
    playlists = load_playlists()
    user_pl = playlists.get(str(ctx.author.id), {})
    if not user_pl:
        return await ctx.send("📂 У вас нет плейлистов. Создайте: `!pl create [имя]`.", delete_after=5)
        
    embed = discord.Embed(title="📂 Ваши плейлисты", color=discord.Color.from_rgb(220, 20, 60))
    for name, tracks in user_pl.items():
        embed.add_field(name=f"🎵 {name}", value=f"Треков: {len(tracks)}", inline=False)
    await ctx.send(embed=embed, delete_after=15)

def get_card():
    cards = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'Валет', 'Дама', 'Король', 'Туз']
    suits = ['♠️', '♥️', '♦️', '♣️']
    return f"{random.choice(cards)} {random.choice(suits)}"

def calc_score(hand):
    score = 0
    aces = 0
    for card in hand:
        val = card.split()[0]
        if val in ['Валет', 'Дама', 'Король']: score += 10
        elif val == 'Туз': aces += 1; score += 11
        else: score += int(val)
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score

class BlackjackView(discord.ui.View):
    def __init__(self, ctx, bet):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.player_hand = [get_card(), get_card()]
        self.dealer_hand = [get_card(), get_card()]

    def get_embed(self, reveal_dealer=False):
        p_score = calc_score(self.player_hand)
        d_score = calc_score(self.dealer_hand) if reveal_dealer else calc_score([self.dealer_hand[0]])
        
        embed = discord.Embed(title="🃏 Блэкджек", color=discord.Color.from_rgb(220, 20, 60))
        embed.add_field(name=f"Ваши карты ({p_score})", value="\n".join(self.player_hand), inline=True)
        embed.add_field(name=f"Карты дилера ({d_score})", value="\n".join(self.dealer_hand) if reveal_dealer else f"{self.dealer_hand[0]}\n[Скрыто]", inline=True)
        embed.set_footer(text=f"Ставка: {self.bet} монет")
        return embed

    @discord.ui.button(label="Взять карту", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction, button):
        self.player_hand.append(get_card())
        score = calc_score(self.player_hand)
        
        if score > 21:
            await self.end_game(interaction, "Вы проиграли! Перебор.", 0)
        elif score == 21:
            await self.end_game(interaction, "Блэкджек! Вы выиграли!", self.bet * 2)
        else:
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Хватит", style=discord.ButtonStyle.secondary, emoji="🛑")
    async def stand(self, interaction, button):
        d_score = calc_score(self.dealer_hand)
        while d_score < 17:
            self.dealer_hand.append(get_card())
            d_score = calc_score(self.dealer_hand)
            
        p_score = calc_score(self.player_hand)
        
        if d_score > 21:
            await self.end_game(interaction, "Дилер перебрал! Вы выиграли!", self.bet * 2)
        elif d_score > p_score:
            await self.end_game(interaction, "Дилер выиграл!", 0)
        elif d_score < p_score:
            await self.end_game(interaction, "Вы выиграли!", self.bet * 2)
        else:
            await self.end_game(interaction, "Ничья! Ставка возвращается.", self.bet)

    async def end_game(self, interaction, text, win_amount):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(embed=self.get_embed(reveal_dealer=True), view=self)
        await interaction.followup.send(text, ephemeral=True)
        
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        if win_amount > 0:
            cursor.execute("UPDATE economy SET balance = balance + ? WHERE user_id = ?", (win_amount, self.ctx.author.id))
        elif win_amount == 0:
            cursor.execute("UPDATE economy SET balance = balance - ? WHERE user_id = ?", (self.bet, self.ctx.author.id))
        conn.commit()
        conn.close()

@bot.command()
async def blackjack(ctx, bet: int = 100):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM economy WHERE user_id = ?", (ctx.author.id,))
    res = cursor.fetchone()
    
    if not res:
        cursor.execute("INSERT INTO economy VALUES (?, ?)", (ctx.author.id, 1000))
        conn.commit()
        balance = 1000
    else:
        balance = res[0]

    if balance < bet:
        conn.close()
        return await ctx.send("❌ У вас недостаточно монет!")

    cursor.execute("UPDATE economy SET balance = balance - ? WHERE user_id = ?", (bet, ctx.author.id))
    conn.commit()
    conn.close()

    view = BlackjackView(ctx, bet)
    embed = view.get_embed()
    await ctx.send(embed=embed, view=view)

@bot.command()
async def balance(ctx):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM economy WHERE user_id = ?", (ctx.author.id,))
    res = cursor.fetchone()
    conn.close()
    
    if not res:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO economy VALUES (?, ?)", (ctx.author.id, 1000))
        conn.commit()
        conn.close()
        balance = 1000
    else:
        balance = res[0]
        
    await ctx.send(f"💰 Ваш баланс: **{balance}** монет.")

@bot.command()
async def roll(ctx, min_val: int = 1, max_val: int = 100):
    if min_val >= max_val:
        return await ctx.send("❌ Минимум должен быть меньше максимума!")
    result = random.randint(min_val, max_val)
    await ctx.send(f"🎲 Выпало число: **{result}** (от {min_val} до {max_val})")

@bot.command()
async def local_play(ctx, *, filename: str):
    if not await ensure_voice(ctx): return await ctx.send("❌ Зайдите в канал!", delete_after=5)
    if not os.path.exists(MUSIC_FOLDER): return await ctx.send(f"❌ Папка `{MUSIC_FOLDER}` не найдена.", delete_after=5)
    all_files = os.listdir(MUSIC_FOLDER)
    found_file = next((f for f in all_files if filename.lower() in f.lower()), None)
    if not found_file: return await ctx.send("❌ Файл не найден.", delete_after=5)
    await add_to_queue(ctx, {'url': os.path.abspath(os.path.join(MUSIC_FOLDER, found_file)), 'title': found_file, 'is_local': True})

@bot.command()
async def local_download(ctx, *, filename: str):
    if not os.path.exists(MUSIC_FOLDER): return await ctx.send("❌ Папка не найдена.", delete_after=5)
    all_files = os.listdir(MUSIC_FOLDER)
    found_file = next((f for f in all_files if filename.lower() in f.lower()), None)
    if not found_file: return await ctx.send("❌ Файл не найден.", delete_after=5)
    try: await ctx.send(f"📥 Вот ваш файл: **{found_file}**", file=discord.File(os.path.join(MUSIC_FOLDER, found_file)))
    except: await ctx.send("❌ Ошибка отправки.", delete_after=5)

@bot.command()
async def voice_name(ctx, *, name: str):
    if ctx.author.voice and ctx.author.voice.channel:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM active_channels WHERE channel_id = ?", (ctx.author.voice.channel.id,))
        result = cursor.fetchone()
        if result and result[0] == ctx.author.id:
            await ctx.author.voice.channel.edit(name=name)
            cursor.execute("UPDATE user_settings SET channel_name = ? WHERE user_id = ?", (name, ctx.author.id))
            conn.commit()
        conn.close()

@bot.command()
async def voice_limit(ctx, limit: int):
    if ctx.author.voice and ctx.author.voice.channel:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM active_channels WHERE channel_id = ?", (ctx.author.voice.channel.id,))
        result = cursor.fetchone()
        if result and result[0] == ctx.author.id and 0 <= limit <= 99:
            await ctx.author.voice.channel.edit(user_limit=limit)
            cursor.execute("UPDATE user_settings SET user_limit = ? WHERE user_id = ?", (limit, ctx.author.id))
            conn.commit()
        conn.close()

class AdminClearView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx
    @discord.ui.button(label="10 сообщений", style=discord.ButtonStyle.secondary)
    async def clear_10(self, interaction, button):
        await interaction.response.send_message("🧹 Удаляю 10 сообщений...", ephemeral=True)
        await interaction.channel.purge(limit=10)
    @discord.ui.button(label="50 сообщений", style=discord.ButtonStyle.secondary)
    async def clear_50(self, interaction, button):
        await interaction.response.send_message("🧹 Удаляю 50 сообщений...", ephemeral=True)
        await interaction.channel.purge(limit=50)
    @discord.ui.button(label="100 сообщений", style=discord.ButtonStyle.danger)
    async def clear_100(self, interaction, button):
        await interaction.response.send_message("🧹 Удаляю 100 сообщений...", ephemeral=True)
        await interaction.channel.purge(limit=100)

class AdminView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=180)
        self.ctx = ctx
    @discord.ui.button(label="Очистить", style=discord.ButtonStyle.secondary, emoji="🧹")
    async def clear(self, interaction, button):
        await interaction.response.send_message("Выберите, сколько удалить:", view=AdminClearView(self.ctx), ephemeral=True)
    @discord.ui.button(label="Кикнуть", style=discord.ButtonStyle.danger, emoji="👢")
    async def kick_btn(self, interaction, button): await interaction.response.send_message("👢 Введите в чат: `!kick @юзер причина`", ephemeral=True)
    @discord.ui.button(label="Забанить", style=discord.ButtonStyle.danger, emoji="🔨")
    async def ban_btn(self, interaction, button): await interaction.response.send_message("🔨 Введите в чат: `!ban @юзер причина`", ephemeral=True)
    @discord.ui.button(label="Замутить", style=discord.ButtonStyle.danger, emoji="🤐")
    async def mute_btn(self, interaction, button): await interaction.response.send_message("🤐 Введите в чат: `!mute @юзер минуты`", ephemeral=True)
    @discord.ui.button(label="Снять мут", style=discord.ButtonStyle.success, emoji="🔊")
    async def unmute_btn(self, interaction, button): await interaction.response.send_message("🔊 Введите в чат: `!unmute @юзер`", ephemeral=True)
    @discord.ui.button(label="Статистика", style=discord.ButtonStyle.primary, emoji="📊", row=1)
    async def stats_btn(self, interaction, button):
        guild = interaction.guild
        embed = discord.Embed(title="📊 Статистика сервера", color=discord.Color.from_rgb(220, 20, 60))
        embed.add_field(name="Участников", value=str(guild.member_count), inline=True)
        embed.add_field(name="Каналов", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Ролей", value=str(len(guild.roles)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def admin(ctx):
    embed = discord.Embed(title="⚔️ Панель Мажора", description="Быстрые действия для управления сервером.\n*Панель видна только вам.*", color=discord.Color.from_rgb(220, 20, 60))
    await ctx.send(embed=embed, view=AdminView(ctx), ephemeral=True)

@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def warn(ctx, member: discord.Member, *, reason="Нарушение правил"):
    if member.bot: return
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT warn_count FROM warns WHERE user_id = ?", (member.id,))
    res = cursor.fetchone()
    warns = (res[0] + 1) if res else 1
    if res: cursor.execute("UPDATE warns SET warn_count = ? WHERE user_id = ?", (warns, member.id))
    else: cursor.execute("INSERT INTO warns VALUES (?, ?)", (member.id, warns))
    conn.commit(); conn.close()
    await ctx.send(f"⚠️ **{member.display_name}** получил варн! [{warns}/3]", delete_after=10)
    if warns >= 3: await member.timeout(datetime.timedelta(days=1)); await ctx.send(f"🔒 **{member.display_name}** отправлен в мут на 24 часа!", delete_after=10)

@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def unwarn(ctx, member: discord.Member):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT warn_count FROM warns WHERE user_id = ?", (member.id,))
    res = cursor.fetchone()
    if res and res[0] > 0:
        warns = res[0] - 1
        if warns == 0: cursor.execute("DELETE FROM warns WHERE user_id = ?", (member.id,))
        else: cursor.execute("UPDATE warns SET warn_count = ? WHERE user_id = ?", (warns, member.id))
        conn.commit()
        await ctx.send(f"✅ Снят варн с **{member.display_name}**. Осталось: [{warns}/3]", delete_after=10)
    conn.close()

@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def kick(ctx, member: discord.Member, *, reason=None): await member.kick(reason=reason); await ctx.send(f"👢 {member.mention} кикнут.", delete_after=10)
@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def ban(ctx, member: discord.Member, *, reason=None): await member.ban(reason=reason); await ctx.send(f"🔨 {member.mention} забанен.", delete_after=10)
@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def mute(ctx, member: discord.Member, minutes: int): await member.timeout(datetime.timedelta(minutes=minutes)); await ctx.send(f"🤐 {member.mention} замучен на {minutes} мин.", delete_after=10)
@bot.command()
@commands.has_role(ALLOWED_ROLE_NAME)
async def unmute(ctx, member: discord.Member): await member.timeout(None); await ctx.send(f"🔊 {member.mention} размучен.", delete_after=10)

class WikiSelectView(discord.ui.View):
    def __init__(self, results: list):
        super().__init__(timeout=60)
        self.results = results
        for i, result in enumerate(results[:5]):
            btn = discord.ui.Button(label=f"{i+1}. {result['title'][:50]}", style=discord.ButtonStyle.secondary)
            btn.callback = self.make_callback(i)
            self.add_item(btn)
    def make_callback(self, index):
        async def callback(interaction: discord.Interaction):
            for child in self.children: child.disabled = True
            await interaction.response.edit_message(content=f"✅ Выбрана статья: **{self.results[index]['title']}**", embed=None, view=self)
            await process_wiki(interaction, self.results[index]['title'])
        return callback

class WikiPaginator(discord.ui.View):
    def __init__(self, pages, title, url):
        super().__init__(timeout=300)
        self.pages, self.title, self.url, self.cur = pages, title, url, 0
        self.update_btns()
    def update_btns(self):
        self.prev.disabled = self.cur == 0
        self.next.disabled = self.cur == len(self.pages) - 1
    def get_embed(self):
        embed = discord.Embed(title=f"📖 {self.title}", url=self.url, description=f"Стр. {self.cur+1}/{len(self.pages)}", color=discord.Color.from_rgb(220, 20, 60))
        embed.set_image(url="attachment://page.jpg")
        return embed
    async def update_msg(self, interaction):
        self.update_btns()
        file = discord.File(fp=io.BytesIO(self.pages[self.cur]), filename="page.jpg")
        await interaction.response.edit_message(embed=self.get_embed(), attachments=[file], view=self)
    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction, button): self.cur -= 1; await self.update_msg(interaction)
    @discord.ui.button(label="➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction, button): self.cur += 1; await self.update_msg(interaction)

@bot.command()
async def wiki(ctx, *, query: str):
    api = "https://wiki.adventurestation.ru/api.php"
    msg = await ctx.send(f"🔍 Ищу **{query}**...")
    async with aiohttp.ClientSession() as session:
        async with session.get(api, params={"action": "query", "list": "search", "srsearch": query, "srlimit": 5, "format": "json"}) as r:
            data = await r.json()
            results = data.get('query', {}).get('search', [])
            if not results: return await msg.edit(content="❌ Ничего не найдено.")
            await msg.edit(content=None, embed=discord.Embed(title="Выберите статью:", color=discord.Color.from_rgb(220, 20, 60)), view=WikiSelectView(results))

async def process_wiki(interaction, title):
    url = f"https://wiki.adventurestation.ru/{title.replace(' ', '_')}"
    msg = await interaction.followup.send(f"📸 Делаю скриншоты **{title}**...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1400, 'height': 1000})
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        elem = await page.query_selector('#mw-content-text')
        img_bytes = await elem.screenshot(type='jpeg', quality=90) if elem else await page.screenshot(full_page=True, type='jpeg', quality=90)
        await browser.close()
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    chunks = []
    for i in range(0, h, 1500):
        buf = io.BytesIO()
        img.crop((0, i, w, min(i+1500, h))).save(buf, format='JPEG', quality=95)
        chunks.append(buf.getvalue())
        
    await msg.delete()
    file = discord.File(fp=io.BytesIO(chunks[0]), filename="page.jpg")
    view = WikiPaginator(chunks, title, url)
    await interaction.channel.send(embed=view.get_embed(), file=file, view=view)

bot.remove_command('help')

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="🏰 Справочник", color=discord.Color.from_rgb(220, 20, 60))
    embed.add_field(name="🎵 Музыка", value="`!music` — Панель управления\n`!play [ссылка]` — Поиск трека\n`!play_config menu/default` — Режим поиска (меню или сразу)\n`!local_play [файл]` — Локальный файл\n`!local_download [файл]` — Скачать файл", inline=False)
    embed.add_field(name="📂 Плейлисты", value="`!pl create [имя]` — Создать\n`!pl add [имя] [запрос]` — Добавить трек\n`!pl addcurrent` — Добавить играющий трек\n`!pl del [имя]` — Удалить плейлист\n`!pl rmtrack [имя] [трек]` — Удалить трек\n`!pl list` — Список плейлистов", inline=False)
    embed.add_field(name="💰 Экономика и Игры", value="`!balance` — Баланс монет\n`!blackjack [ставка]` — Блэкджек\n`!roll [мин] [макс]` — Рандомайзер", inline=False)
    embed.add_field(name="🔊 Комнаты", value="`!voice_name [имя]` — Название комнаты\n`!voice_limit [число]` — Лимит мест", inline=False)
    embed.add_field(name="📚 Вики", value="`!wiki [запрос]` — Поиск статей", inline=False)
    if ALLOWED_ROLE_NAME in [r.name for r in ctx.author.roles]:
        embed.add_field(name="⚔️ Админ", value="`!admin` — Панель управления", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    welcome_channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if welcome_channel:
        embed = discord.Embed(title="🏰 Новое прибытие!", description=f"Приветствуем, {member.mention}!", color=discord.Color.from_rgb(220, 20, 60))
        embed.set_thumbnail(url=member.display_avatar.url)
        await welcome_channel.send(embed=embed)

bot.run('')