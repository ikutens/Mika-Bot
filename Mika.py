import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import random
import aiohttp
import aiofiles
import asyncio
from bs4 import BeautifulSoup
import json
import re
from collections import defaultdict
import urllib.parse
import base64
import io
from rapidfuzz import process, fuzz
import atexit

# ======= Setup and Globals =======
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REVIEW_JSON_PATH = "curator_reviews.json"

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
logging.basicConfig(level=logging.WARNING, handlers=[handler])

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='h2a ', intents=intents, help_command=None)

aiohttp_session = None
message_streaks = defaultdict(lambda: {"last_user": None, "count": 0})

# ======= Preload Data =======
sosa_folder = "./Sosa"
sosa_files = [os.path.join(sosa_folder, f) for f in os.listdir(sosa_folder)
              if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]

berman_sentences = []
try:
    with open("berman.txt", "r", encoding="utf-8") as f:
        berman_sentences = f.read().splitlines()
except Exception:
    pass

gacha_games = []
fish_list = []
curator_reviews = []



# ======= Helper Functions =======
def clean_message_content(content):
    return re.sub(r"http\S+", "", content)

def word_in_text(word, text):
    return re.search(rf'\b{re.escape(word)}\b', text, re.IGNORECASE)

async def get_session():
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        aiohttp_session = aiohttp.ClientSession()
    return aiohttp_session

def clean_discogs_artist(name):
    return re.sub(r"\s*\(\d+\)", "", name).strip()

def clean_album_title(title):
    title = re.sub(r"\s*\([^)]+\)", "", title)
    title = re.sub(r"\s*\[[^\]]+\]", "", title)
    title = re.sub(r"[–:.,]", "", title)
    return re.sub(r"\s+", " ", title).strip()

async def get_spotify_token(client_id, client_secret):
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = "grant_type=client_credentials"
    session = await get_session()
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers=headers
    ) as resp:
        result = await resp.json()
        return result.get("access_token")

async def get_spotify_album_url(token, album, artist): #בגלל שדיסקוגס מגבילים את הכמות מידע שאפשר לקחת והרבה פעמים האלבום קאבר & טראקליסט די גרועים באתר, החלטתי לקחת אותם דרך ספוטיפיי
    headers = {"Authorization": f"Bearer {token}"}

    def clean_artist_for_spotify(a):
        a = re.sub(r"\s*\(\d+\)", "", a)
        a = re.split(r'feat\.|,', a, flags=re.IGNORECASE)[0]
        return a.strip().lower()

    def clean_album_for_spotify(t):
        t = re.sub(r"\s*\([^)]+\)", "", t)
        t = re.sub(r"\s*\[[^\]]+\]", "", t)
        return t.strip().lower()

    queries = [
        f"album:{album} artist:{artist}",
        f"album:{album} artist:{clean_artist_for_spotify(artist)}",
        f"album:{clean_album_for_spotify(album)} artist:{clean_artist_for_spotify(artist)}",
        f"album:{album}",
        f"album:{clean_album_for_spotify(album)}",
    ]
    session = await get_session()
    for query in queries: # מכאן הלאה אני די בטוח שהתחלתי להשתגע כבר, צ'אט רשם כמעט את כל שאר הפקודה הזאת
        search_url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=album&limit=10"
        async with session.get(search_url, headers=headers) as resp:
            result = await resp.json()
            items = result.get("albums", {}).get("items", [])
            if not items:
                continue
            # Use rapidfuzz to find the best match by both album and artist name
            cleaned_album = clean_album_for_spotify(album)
            cleaned_artist = clean_artist_for_spotify(artist)
            scored = []
            for album_data in items:
                name_score = fuzz.token_set_ratio(clean_album_for_spotify(album_data["name"]), cleaned_album)
                # Try to find primary artist
                artists = [a["name"] for a in album_data.get("artists", [])]
                main_artist = artists[0] if artists else ""
                artist_score = fuzz.token_set_ratio(clean_artist_for_spotify(main_artist), cleaned_artist)
                total_score = (name_score + artist_score) / 2
                scored.append((total_score, album_data))
            scored.sort(reverse=True, key=lambda tup: tup[0])
            # Set a reasonable threshold for "close enough" (e.g., 80+)
            best_score, best_album = scored[0]
            if best_score >= 80:
                spotify_url = best_album["external_urls"]["spotify"]
                images = best_album.get("images", [])
                image_url = images[0]["url"] if images else None
                album_id = best_album["id"]
                # Now fetch the album details for tracklist
                album_api_url = f"https://api.spotify.com/v1/albums/{album_id}"
                async with session.get(album_api_url, headers=headers) as album_resp:
                    album_info = await album_resp.json()
                    spotify_tracks = []
                    for i, track in enumerate(album_info.get("tracks", {}).get("items", []), 1):
                        name = track.get("name")
                        if name:
                            spotify_tracks.append(f"**{i}.** {name}")
                    return spotify_url, image_url, spotify_tracks
            # If no close match, continue trying other queries
    return None, None, None


async def resolve_discogs_artist_name(user_input, discogs_token):
    url = f"https://api.discogs.com/database/search?type=artist&q={urllib.parse.quote(user_input)}&per_page=5&token={discogs_token}"
    session = await get_session()
    async with session.get(url) as resp:
        if resp.status != 200:
            return user_input
        data = await resp.json()
        results = data.get("results", [])
        if not results:
            return user_input
        titles = [r.get("title", "") for r in results]
        best_match, score, idx = process.extractOne(user_input, titles, scorer=fuzz.ratio) #מנסה לקחת את האומן שהכי הגיוני
        #כאשר יש אומנים רבים עם אותו השם דיסקוגס מביא להם מספרים - אומן (1) & אומן (2)
        #אז בשביל שזה לא יביא יוצר רנדומלי עם שירים מלפני ארבע מאות שנה שרושמים שם של אומן, זה יביא את האחד ההרבה יותר מוכר
        print(f"[DEBUG] resolve_discogs_artist_name: Input: '{user_input}', Best match: '{best_match}', Score: {score}")
        if score >= 75:
            return best_match
        return titles[0] if titles else user_input



# ======= Yerkalator Goonerapist Jr. =======
async def fetch_wikipedia_gacha_games():
    url = "https://en.wikipedia.org/wiki/List_of_gacha_games"
    session = await get_session()
    async with session.get(url) as response:
        if response.status == 200:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            tables = soup.find_all('table', {'class': 'wikitable'})
            for table in tables:
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cells = row.find_all('td')
                    if cells:
                        link_tag = cells[0].find('a')
                        if link_tag and link_tag.get('href'):
                            game_name = link_tag.get_text(strip=True)
                            game_link = f"https://en.wikipedia.org{link_tag['href']}"
                            gacha_games.append(f"[Wikipedia] [{game_name}]({game_link})")

async def fetch_fandom_gacha_games():
    url = "https://gachagames.fandom.com/wiki/List_of_Gacha_Games"
    session = await get_session()
    async with session.get(url) as response:
        if response.status == 200:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            rows = soup.find_all('tr')
            for row in rows:
                link_tag = row.find('a')
                if link_tag and link_tag.get('href') and "/wiki/" in link_tag['href']:
                    game_name = link_tag.get_text(strip=True)
                    game_link = f"https://gachagames.fandom.com{link_tag['href']}"
                    gacha_games.append(f"[Fandom] [{game_name}]({game_link})")



# ======= Sata Andagi :D =======
async def fetch_fish_list():
    url = "https://mexican-fish.com/fish-alphabetical-index-by-common-name/"
    session = await get_session()
    async with session.get(url) as response:
        if response.status == 200:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            strong_tags = soup.find_all('strong')
            for tag in strong_tags:
                link_tag = tag.find('a')
                if link_tag and link_tag.get('href'):
                    fish_name = link_tag.get_text(strip=True)
                    fish_link = link_tag['href']
                    fish_list.append(f"[{fish_name}]({fish_link})")




# ======= Crack Smoking Time Reviews =======
async def fetch_and_save_curator_reviews():
    global curator_reviews
    curator_reviews.clear()
    start = 0
    count = 50
    session_url = "https://store.steampowered.com/curator/41625352-Crack-Smoking-Time/ajaxgetcuratorrecommendations"
    temp_reviews = []
    headers = { # מסתבר שסטים לא אוהבים שקליינטים לא אמיתיים מנסים לתקשר עם השרתים שלהם :)
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://store.steampowered.com/"
    }
    session = await get_session()
    while True:
        url = f"{session_url}?start={start}&count={count}"
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                break
            data = await resp.json()
            html = data.get("results_html", "")
            if not html.strip():
                break
            soup = BeautifulSoup(html, "html.parser")
            review_divs = soup.find_all("div", class_="recommendation")
            if not review_divs:
                break
            for div in review_divs:
                app_id = div.get("data-ds-appid")
                if not app_id:
                    continue
                app_url = f"https://store.steampowered.com/app/{app_id}"
                a_tag = div.select_one(".recommmendation_app_small_cap_ctn a")
                game_title = a_tag['href'].split('/')[5].replace('_', ' ') if a_tag else f"App ID {app_id}"
                desc_tag = div.find("div", class_="recommendation_desc")
                blurb = desc_tag.get_text(strip=True) if desc_tag else "No review text."
                if div.find("span", class_="color_recommended"):
                    verdict = "✅ Recommended"
                elif div.find("span", class_="color_not_recommended"):
                    verdict = "❌ Not Recommended"
                elif div.find("span", class_="color_informational"):
                    verdict = "🧠 Informational"
                else:
                    verdict = "🧠 Informational"
                temp_reviews.append({
                    "game": game_title,
                    "blurb": blurb,
                    "verdict": verdict,
                    "url": app_url
                })
            start += count
    curator_reviews.extend(temp_reviews)
    async with aiofiles.open(REVIEW_JSON_PATH, "w", encoding="utf-8") as f:
        await f.write(json.dumps(curator_reviews, indent=2, ensure_ascii=False))

async def load_curator_reviews():
    global curator_reviews
    try:
        async with aiofiles.open(REVIEW_JSON_PATH, "r", encoding="utf-8") as f:
            content = await f.read()
            curator_reviews = json.loads(content)
    except Exception:
        curator_reviews = []

# ======= On Ready =======
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    await fetch_wikipedia_gacha_games()
    await fetch_fandom_gacha_games()
    await fetch_fish_list()
    await load_curator_reviews()

# ======= Preloads for Triggers =======
media_triggers = [
    (["wow", "וואו"], "wow.jpg"), # נחשף על ידי זינאדין זידאן ב1.5
    (["מכשפה בוכרית"], "yerk.png"), # פורמן מצא את זה ב11.5
    (["noob", "נוב"], "the_noob.mp4"),
    (["you can call me deku","gosling","גוסלינג"], "deku.mp4"),
    (["you can call me miku"], "miku-final.png"),
    (["fyp", "for you", "פור יו", "tiktok", "טיקטוק"], "fyp.mp4"), #גיימינג מצא את זה ב 29.5
    (["warden", "וורדן"], "./Fish/Warden.png") # מאוד התאכזבתי שלא מצאתם את זה עד עכשיו, זה היה האיסטר אג האהוב עלי :(
]
def suicide_triggers(msg): #דניאל נמ מצא את זה (בטעות) ב10.5
    return ("kill" in msg and "self" in msg) or \
        (("הרוג" in msg or "ירה" in msg or "אדקור" in msg) and "עצמ" in msg) or \
        ("kys" in msg) or ("kms" in msg) or \
        ("תתאבד" in msg) or ("יתאבד" in msg) or ("התאבד" in msg)

#דיבאג שהאמת פשוט שכחתי להוריד מהקוד המלא
async def send_file(destination, filename, reply=False):
    if not os.path.exists(filename):
        if reply and hasattr(destination, "reply"):
            await destination.reply(f"⚠️ File `{filename}` not found.")
        elif hasattr(destination, "send"):
            await destination.send(f"⚠️ File `{filename}` not found.")
        return
    async with aiofiles.open(filename, "rb") as f:
        data = await f.read()
        file = discord.File(io.BytesIO(data), filename=os.path.basename(filename))
        if reply and hasattr(destination, "reply"):
            await destination.reply(file=file)
        elif hasattr(destination, "send"):
            await destination.send(file=file)

# ======= On Message - טריגרים להודעות =======
@bot.event
async def on_message(message):
    if message.author == bot.user or message.author.bot:
        return

    msg = clean_message_content(message.content.lower())
    channel_id = message.channel.id
    author_id = message.author.id

    # --- Yap Counter ---
    #אבירם גיימינג הפעיל את זה ב10.5, 3 דקות אחרי שעשיתי פוש לאפדייט...
    if message.content.startswith(bot.command_prefix):
        message_streaks[channel_id]["last_user"] = None
        message_streaks[channel_id]["count"] = 0
        await bot.process_commands(message)
        return

    if message_streaks[channel_id]["last_user"] == author_id:
        message_streaks[channel_id]["count"] += 1
    else:
        message_streaks[channel_id]["last_user"] = author_id
        message_streaks[channel_id]["count"] = 1
    if message_streaks[channel_id]["count"] == 7:
        async with aiofiles.open("yap.gif", "rb") as f:
            data = await f.read()
            file = discord.File(io.BytesIO(data), filename="yap.gif")
            await message.channel.send(
                content=f"{message.author.mention} Shut the hell up",
                file=file
            )
        message_streaks[channel_id]["count"] = 0
        return



    # NEVER KILL YOURSELF trigger
    if suicide_triggers(msg):
        await send_file(message, "nkyss.mp4", reply=True)
        return

    # Media triggers
    for keywords, filename in media_triggers:
        if any(word_in_text(w, msg) for w in keywords):
            await send_file(message.channel, filename)
            return

    # Custom triggers
    if word_in_text("מה אומר דוד", msg): # אני בטעות חשפתי את זה ב4.5
        await message.channel.send("https://www.the-importer.co.il/cdn-cgi/image/format=auto,metadata=none,quality=85,fit=pad/media/catalog/product/4/7/4750021000805.jpg")

    elif word_in_text("holy fuck", msg): # דניאל נמ מצא את זה ב30.4
        await message.channel.send("https://www.ginotpeershop.co.il/images/logos/2/WhatsApp9_11zon_(1).png")

    elif word_in_text("stunna", msg) or word_in_text("סטאנה", msg): #ולדי היה האחד שביקש את זה ואיכשהו עדיין לא הפעילו את זה עד עכשיו
        await message.channel.send("https://tenor.com/view/stunnaboy-stunnaboy-get-em-pretty-boy-swag-stunna-dance-stunna-gif-19367307")

    elif word_in_text("נאפו", msg):
        await message.reply("בנאפו?! מי מציג בנאפו??")

    # תיוגים
    for user in message.mentions:
        if user.id == 168329788325363712 and not message.reference: # האיסטר אג הראשון שגילו. ע"י אבירם ב25.4
            await message.reply("אה סנקי")
            return
        if user.id == 201051167084642304 and not message.reference: # אם מתייגים את דינו
            await message.reply("https://hebrew-academy.org.il/keyword/%D7%91%D6%BC%D6%B9%D7%94%D6%B6%D7%9F/")
            return
        if user.id == 343667951959932940 and not message.reference: # אם מתייגים את סוריקטה, למרות שאני די בטוח שזה משום מה לא עובד חחחחחח
            await message.channel.send("https://upload.wikimedia.org/wikipedia/en/0/03/Walter_White_S5B.png")
            return

    # שולח משחק גאצ'ה רנדומלי אם מתייגים את יאן ורושמים משהו המכיל את המילה גון או גונר בשני השפות
    if any(user.id == 280755361596702721 for user in message.mentions) and (
            'goon' in msg or 'גונ' in msg or 'גון' in msg):
        if gacha_games:
            selected_game = random.choice(gacha_games)
            await message.reply(f" קח משחק גאצ'ה רנדומלי: **{selected_game}**")
        return

    # האיסטר אג של קרליק, האחד שעבדתם עליו כל כך קשה. שולח דג רנדומלי אם מתייגים את קרליק ורושמים פיש בשני השפות
    if any(user.id == 473100047849095168 for user in message.mentions) and (
            "fish" in msg or "פיש" in msg or "דג" in msg):
        if fish_list:
            selected_fish = random.choice(fish_list)
            await message.reply(f"🐟 Here's a fish for you: {selected_fish}", suppress_embeds=True)
        return

    # אבירם מצא את זה ב29.4
    if any(user.id == 258938288684007424 for user in message.mentions) or word_in_text("howard", msg):
        await send_file(message.channel, "howard.png")
        return

    # זה אחד מיוחד - כל פעם שרפאל ברמן מתייג את דניאל נמ זה שולח משפט רנדומלי מרשימה קצרה, אבל איכשהו מאז היום שעשיתי לאפדייט פוש ברמן פשוט הפסיק
    author_id = 334649464750866433
    target_id = 290380573124591627
    if message.author.id == author_id and any(user.id == target_id for user in message.mentions):
        if berman_sentences:
            selected_sentence = random.choice(berman_sentences)
            await message.channel.send(selected_sentence)

    await bot.process_commands(message)

# ======= COMMANDS =======

@bot.command()
async def teddy(ctx):
    await send_file(ctx, "teddy.jpg")

@bot.command()
async def motivation(ctx):
    file_path = random.choice(sosa_files)
    await send_file(ctx, file_path)

@bot.command()
async def mishpat(ctx):
    with open("geo-list.txt", "r", encoding="utf-8") as f:
        content = f.read()
    entries = content.strip().split("\n\n")
    theorem_entry = random.choice(entries)
    lines = theorem_entry.strip().split("\n")
    raw_id = lines[0].replace("ID:", "").strip("[] ").zfill(3)
    if raw_id == "123": # אם יוצא משפט פיתגורס
        embed = discord.Embed(
            title="[ 123 ]",
            description="https://www.youtube.com/watch?v=40M9UJXBvIw&t=63s)",
            color=discord.Color.red()
        )
        embed.set_image(url="https://img.youtube.com/vi/40M9UJXBvIw/hqdefault.jpg")
        await ctx.send(embed=embed)
        return
    id_line = f"[ {raw_id} ]"
    text = "\n".join(lines[1:])
    embed = discord.Embed(
        title=id_line,
        description=text,
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed)

@bot.command(name="crack", aliases=["cst"])
async def crack(ctx, filter: str = None):
    if not curator_reviews:
        await ctx.send("No reviews found. Try again later.")
        return
    valid_filters = {
        "(recommended)": "✅ Recommended",
        "(not)": "❌ Not Recommended",
        "(info)": "🧠 Informational"
    }
    filtered_reviews = curator_reviews
    if filter:
        filter = filter.lower()
        if filter in valid_filters:
            filtered_reviews = [r for r in curator_reviews if r["verdict"] == valid_filters[filter]]
        else:
            await ctx.send("❌ Invalid filter. Use `(recommended)`, `(not)`, or `(info)`.")
            return
    if not filtered_reviews:
        await ctx.send("No matching reviews found.")
        return
    selected = random.choice(filtered_reviews)
    match = re.search(r'/app/(\d+)', selected["url"])
    app_id = match.group(1) if match else ""
    if "Not Recommended" in selected["verdict"]:
        color = discord.Color.red()
    elif "Recommended" in selected["verdict"]:
        color = discord.Color.green()
    else:
        color = discord.Color.gold()
    blurb = selected["blurb"].strip('"')
    description = f"### {blurb}"
    embed = discord.Embed(
        title=selected["game"],
        url=selected["url"],
        description=description,
        color=color
    )
    embed.set_author(
        name=selected["verdict"],
        icon_url="https://avatars.cloudflare.steamstatic.com/bd6df2273e04387f443475fd3217435c34da8e65_full.jpg"
    )
    if app_id:
        embed.set_image(url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg")
    await ctx.send(embed=embed)

@bot.command(name="help_crack")
async def help_crack(ctx):
    embed = discord.Embed(
        title="Crack Smoking Time Review Help",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="\u200b",
        value="You can also write `h2a cst` to trigger it.",
        inline=False
    )
    embed.add_field(
        name="📜 Usage",
        value="Use the command by itself to get a random review by Crack Smoking Time.\n\n"
              "To filter by review type, add one of the following:\n"
              "`(recommended)` – Only shows ✅ Recommended reviews\n"
              "`(not)` – Only shows ❌ Not Recommended reviews\n"
              "`(info)` – Only shows 🧠 Informational reviews",
        inline=False
    )
    embed.add_field(
        name="\u200b",
        value="For example: `h2a crack (recommended)`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
async def update_reviews(ctx):
    if ctx.author.id != 338054995209355274: # אם מישהו שהוא לא אני מנסה להשתמש בפקודה הזאת
        await send_file(ctx, "stfu.mov")
        return
    await ctx.send("🔄 Updating curator reviews...")
    await fetch_and_save_curator_reviews()
    await ctx.send(f"✅ Updated and saved {len(curator_reviews)} reviews.")

@bot.command()
async def album(ctx, *, filters: str = ""): # הפקודה הזאת עברה כל כך הרבה גרסאות ביני ובין צ'אט שבאמת אין לי מושג מה עושה מה כבר
    if not DISCOGS_TOKEN:
        await ctx.send("❌ Missing Discogs API token.")
        return

    looking_msg = await ctx.send("🔄 looking for album")

    artist = style = year = None
    match = re.search(r"\[(.*?)\]", filters)
    attempts = []
    if match:
        parts = [p.strip() for p in match.group(1).split(',') if p.strip()]
        for part in parts[:]:
            if part.isdigit() and len(part) == 4:
                year = part
                parts.remove(part)
        if len(parts) == 2:
            attempts.append({'artist': parts[0], 'style': parts[1]})
            attempts.append({'style': parts[0], 'artist': parts[1]})
        elif len(parts) == 1:
            attempts.append({'style': parts[0]})
            attempts.append({'artist': parts[0]})
    if (not attempts or not match) and year:
        attempts.append({})
    if not attempts:
        attempts.append({})

    # Fuzzy resolve artist names using Discogs
    for attempt in attempts:
        if 'artist' in attempt and attempt['artist']:
            attempt['artist'] = await resolve_discogs_artist_name(attempt['artist'], DISCOGS_TOKEN)

    params_base = []
    if year: params_base.append(f"year={year}")
    params_base.append("per_page=15")
    params_base.append("type=master")
    params_base.append(f"token={DISCOGS_TOKEN}")

    found_result = None
    master_id = None
    master_data = None
    thumb = None

    session = await get_session()
    for attempt in attempts:
        params = params_base[:]
        if 'artist' in attempt: params.append(f"artist={urllib.parse.quote(attempt['artist'])}")
        if 'style' in attempt: params.append(f"style={urllib.parse.quote(attempt['style'])}")

        url = f"https://api.discogs.com/database/search?{'&'.join(params)}"
        async with session.get(url) as resp:
            if resp.status != 200:
                continue
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                continue
            found_result = random.choice(results)
            master_id = found_result.get("id")
            master_data = found_result
            thumb = found_result.get("cover_image")
            break

    if not found_result or not master_id:
        await looking_msg.delete()
        await ctx.send("❌ No albums found for that filter.")
        return

    details_url = f"https://api.discogs.com/masters/{master_id}"
    artist_out = master_data.get("artist") or ""
    year_out = master_data.get("year") or year or "Unknown Year"
    genres = ", ".join(master_data.get("genre", []))
    styles = ", ".join(master_data.get("style", []))
    title = master_data.get("title", "Unknown Title")

    split_match = re.match(r"(.+?)[–:-]\s+(.+)", title)
    if split_match:
        possible_artist, possible_album = split_match.groups()
        if (not artist_out or re.search(r"\(\d+\)", artist_out) or artist_out.lower() in possible_artist.lower()):
            artist_out = possible_artist.strip()
            title = possible_album.strip()

    spotify_url = None
    spotify_image = None
    spotify_tracklist = None
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            token = await get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
            if token:
                spotify_url, spotify_image, spotify_tracklist = await get_spotify_album_url(token, title, clean_discogs_artist(artist_out))
        except Exception as e:
            spotify_url = None
            spotify_image = None
            spotify_tracklist = None

    track_str = ""
    if spotify_tracklist:
        track_str = "\n".join(spotify_tracklist)
    else:
        session2 = await get_session()
        async with session2.get(details_url) as resp2:
            if resp2.status == 200:
                release_data = await resp2.json()
                images = release_data.get("images")
                if images and images[0].get("uri"):
                    thumb = images[0]["uri"]
                if "title" in release_data:
                    title = release_data["title"]
                if "artists" in release_data and release_data["artists"]:
                    artist_out = ", ".join(a["name"] for a in release_data["artists"])
                if "year" in release_data:
                    year_out = release_data["year"]
                if "genres" in release_data:
                    genres = ", ".join(release_data["genres"])
                if "styles" in release_data:
                    styles = ", ".join(release_data["styles"])
                discogs_tracklist = release_data.get("tracklist", [])
                local_track_str = ""
                if discogs_tracklist:
                    for i, t in enumerate(discogs_tracklist, 1):
                        name = t.get('title', '')
                        if name:
                            local_track_str += f"**{i}.** {name}\n"
                if not local_track_str:
                    main_release_id = release_data.get("main_release")
                    if main_release_id:
                        await asyncio.sleep(1.5)
                        release_url = f"https://api.discogs.com/releases/{main_release_id}"
                        async with session2.get(release_url) as rel_resp:
                            if rel_resp.status == 200:
                                rel_data = await rel_resp.json()
                                rel_tracklist = rel_data.get("tracklist", [])
                                for i, t in enumerate(rel_tracklist, 1):
                                    name = t.get('title', '')
                                    if name:
                                        local_track_str += f"**{i}.** {name}\n"
                track_str = local_track_str or "No tracklist available."
            else:
                track_str = "No tracklist available."

    if spotify_url:
        link_url = spotify_url
        if spotify_image:
            thumb = spotify_image
    else:
        link_url = f"https://www.discogs.com/master/{master_id}"

    artist_clean = clean_discogs_artist(artist_out)
    title_header = f"[__**{title.upper()}**__]({link_url})"

    description = (
        f"{title_header}\n\n"
        f"**Artist:** {artist_clean}\n"
        f"**Year:** {year_out}\n"
        f"**Genres:** {genres}\n"
        f"**Styles:** {styles}\n\n"
        f"**Tracklist:**\n{track_str}"
    )

    embed = discord.Embed(
        description=description,
        color=discord.Color.blue()
    )
    if thumb:
        embed.set_image(url=thumb)

    await looking_msg.delete()
    await ctx.send(embed=embed)

@bot.command(name="help_album")
async def help_album(ctx):
    embed = discord.Embed(
        title="Replies with a random album from Discogs",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="📜 Usage",
        value="**Use the command by itself to get a random album from Discogs.**\n\n"
              "Pressing the album title will open it in Spotify (if available)\n",
        inline=False
    )
    embed.add_field(
        name="\u200b",
        value="You can also filter results by writing `h2a album [{Artist},{Genre},{Year}]`\n\n"
        "For example:\n"
              "`h2a album [Young Thug, Rap, 2015]`\n"
              "`h2a album [Rock, 2011]`\n"
              "`h2a album [Chief Keef, 2012]`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help(ctx):
    embed = discord.Embed(
        color=discord.Color.blurple()
    )
    embed.add_field(name="`h2a album`", value="Write 'h2a help_album' for more info on the command", inline=False)
    embed.add_field(name="`h2a crack`", value="Write 'h2a help_crack' for more info on the command", inline=False)
    embed.add_field(name="`h2a teddy`", value="🐶", inline=False)
    embed.add_field(name="`h2a mishpat`", value="180 (תכנית 141)", inline=False)
    embed.add_field(name="`h2a motivation`", value="Sends a motivational picture of Chief Keef", inline=False)
    embed.add_field(name="`h2a help`", value="Shows this list of commands", inline=False)
    await ctx.send(embed=embed)

# ======= Bot Run & Cleanup =======
@atexit.register
def cleanup():
    global aiohttp_session
    if aiohttp_session and not aiohttp_session.closed:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(aiohttp_session.close())

bot.run(TOKEN)
