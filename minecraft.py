# Minecraft Mod Finder Bot — FULL Pydroid 3 version
# Install once in Pydroid 3 Terminal:
# pip install -U python-telegram-bot requests beautifulsoup4
#
# Then put your Telegram bot token from @BotFather in BOT_TOKEN below.
#
# Commands:
# /start       - start menu
# /help        - instructions
# /search NAME - search public web pages for a Minecraft addon/mod
# /cancel      - cancel current search (best-effort)
# /about       - bot information
#
# You can also send a mod page URL. The bot checks for publicly available
# direct .mcaddon/.mcpack/.mcworld/.zip files and sends them if found.
#
# Limitations: no bot can guarantee finding every mod or the exact same file.
# Marketplace purchases, login-protected pages, Java-only mods, CAPTCHA,
# and sites that block automated requests may not be downloadable this way.
# Use official/free downloads and respect each site's rules.

import asyncio
import html
import logging
import os
import re
import tempfile
from urllib.parse import quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)

BOT_TOKEN = "8968208942:AAEIorThOytkboh0tPZcxc-TZHgITpQlUb0"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("minecraft_mod_finder")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
    )
}
FILE_EXTS = (".mcaddon", ".mcpack", ".mcworld", ".zip")
MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024  # Keep below Telegram's usual bot upload limit.
TIMEOUT = (10, 25)


def valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def safe_name(name, fallback="minecraft_addon"):
    name = unquote(name or fallback)
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")
    return (name or fallback)[:120]


def ext_from_url(url):
    return os.path.splitext(unquote(urlparse(url).path))[1].lower()


def extract_direct_link(page_url, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    for tag in soup.find_all(["a", "link"]):
        href = tag.get("href")
        if not href:
            continue
        full = urljoin(page_url, href.strip())
        ext = ext_from_url(full)
        label = " ".join(tag.get_text(" ", strip=True).lower().split())
        if ext in FILE_EXTS:
            score = 10
            if any(x in label for x in ("download", "addon", "mcpack", "mcaddon")):
                score += 5
            candidates.append((score, full))
    if not candidates:
        # Some sites use a button with a data URL.
        for tag in soup.find_all(True):
            for attr in ("data-download", "data-url", "data-href"):
                value = tag.get(attr)
                if value:
                    full = urljoin(page_url, value.strip())
                    if ext_from_url(full) in FILE_EXTS:
                        candidates.append((8, full))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1] if candidates else None


def title_from_html(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        if title:
            return title[:180]
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True)[:180] if h1 else "Minecraft addon"


def web_search(query, limit=6):
    """Search DuckDuckGo's public HTML results. Results can vary by region/site."""
    search_url = "https://html.duckduckgo.com/html/?q=" + quote_plus(
        query + " Minecraft Bedrock addon mcaddon mcpack download"
    )
    with requests.Session() as session:
        session.headers.update(HEADERS)
        response = session.get(search_url, timeout=TIMEOUT)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    seen = set()
    for item in soup.select(".result"):
        a = item.select_one(".result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        # DDG may wrap the destination in a redirect URL.
        if "uddg=" in href:
            from urllib.parse import parse_qs
            try:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            except Exception:
                pass
        title = a.get_text(" ", strip=True)
        snippet_node = item.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        if valid_url(href) and href not in seen:
            seen.add(href)
            results.append({"title": title[:180], "url": href, "snippet": snippet[:260]})
        if len(results) >= limit:
            break
    return results


def download_file(url):
    """Download a public addon file, enforcing a size limit."""
    with requests.Session() as session:
        session.headers.update(HEADERS)
        with session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as response:
            response.raise_for_status()
            final_url = response.url
            ext = ext_from_url(final_url)
            content_type = response.headers.get("Content-Type", "").lower()
            if ext not in FILE_EXTS:
                # Do not rename an arbitrary HTML/login page into an addon.
                if "zip" not in content_type and "octet-stream" not in content_type:
                    return None
                ext = ".mcaddon"
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Fayl juda katta (45 MB dan oshadi).")
            fd, temp_path = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            size = 0
            try:
                with open(temp_path, "wb") as out:
                    for chunk in response.iter_content(128 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Fayl 45 MB limitidan katta.")
                        out.write(chunk)
                if size == 0:
                    os.remove(temp_path)
                    return None
                return {
                    "path": temp_path,
                    "filename": safe_name(os.path.basename(unquote(urlparse(final_url).path)), "minecraft_addon" + ext),
                    "size": size,
                }
            except Exception:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise


def inspect_page(url):
    with requests.Session() as session:
        session.headers.update(HEADERS)
        with session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as response:
            response.raise_for_status()
            final_url = response.url
            ext = ext_from_url(final_url)
            content_type = response.headers.get("Content-Type", "").lower()
            # Direct file URL
            if ext in FILE_EXTS or "application/zip" in content_type or "octet-stream" in content_type:
                # Download again in download_file so size limits are enforced.
                return {"kind": "direct", "url": final_url, "title": os.path.basename(urlparse(final_url).path)}
            # Only read a bounded HTML response.
            data = bytearray()
            for chunk in response.iter_content(32 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    break
            page_html = bytes(data).decode(response.encoding or "utf-8", errors="replace")
            direct = extract_direct_link(final_url, page_html)
            return {
                "kind": "page",
                "url": final_url,
                "title": title_from_html(page_html),
                "download_url": direct,
            }


def human_size(n):
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def translate_text(text, source_lang, target_lang):
    """Translate text using Google's public translate endpoint."""
    endpoint = "https://translate.googleapis.com/translate_a/single"
    response = requests.get(
        endpoint,
        params={
            "client": "gtx",
            "sl": source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text,
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return "".join(part[0] for part in data[0] if part and part[0])


async def show_translation_menu(message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 Inglizcha ➜ O‘zbekcha", callback_data="trans:en:uz")],
        [InlineKeyboardButton("🇺🇿 O‘zbekcha ➜ Inglizcha", callback_data="trans:uz:en")],
        [InlineKeyboardButton("🇷🇺 Ruscha ➜ O‘zbekcha", callback_data="trans:ru:uz")],
        [InlineKeyboardButton("🇺🇿 O‘zbekcha ➜ Ruscha", callback_data="trans:uz:ru")],
        [InlineKeyboardButton("❌ Tarjimadan chiqish", callback_data="trans:exit")],
    ])
    await message.reply_text(
        "🌐 Tarjimon bo‘limi\\n\\nKerakli tarjima yo‘nalishini tanlang. "
        "Keyin tarjima qilinadigan matnni yuboring.",
        reply_markup=keyboard,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Qidirish", callback_data="search_help"),
         InlineKeyboardButton("📖 Yordam", callback_data="help")],
        [InlineKeyboardButton("🌐 Tarjimon", callback_data="translate_menu")],
        [InlineKeyboardButton("🧩 Mod saytlar", callback_data="mod_sites")],
        [InlineKeyboardButton("⭐ Eng yaxshi modlar kanali", callback_data="mod_channel")],
        [InlineKeyboardButton("🪟 Bepul Microsoft akkaunt kerakmi? Shu yerga bosing", callback_data="microsoft_channel")],
    ])
    await update.effective_message.reply_text(
        "🎮 Minecraft Mod Finder botiga xush kelibsan!\n\n"
        "🔗 Mod sahifasining havolasini yubor — ochiq faylini tekshiraman.\n"
        "🔎 Nomi bo‘yicha qidirish: /search Ender Rooms\n\n"
        "🌐 Tarjimon bo‘limi ham mavjud: inglizcha/ruscha va o‘zbekcha orasida tarjima qiladi.\n"
        "🧩 Mod saytlarini topish uchun ‘Mod saytlar’ tugmasini bosing.\n"
        "⭐ Tavsiya etilgan modlar kanali uchun ‘Eng yaxshi modlar kanali’ tugmasini bosing.\n"
        "🪟 Microsoft akkaunt kanali uchun tegishli tugmani bosing.\n\n"
        "Buyruqlar: /start, /help, /search, /cancel, /about",
        reply_markup=keyboard,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📌 Qanday ishlatish:\n\n"
        "• Havola yubor: bot sahifadagi ochiq .mcaddon/.mcpack/.mcworld/.zip faylini tekshiradi.\n"
        "• Nomi bo‘yicha qidir: /search Ender Rooms\n"
        "• /cancel — qidiruv holatini tozalash.\n"
        "• /about — bot imkoniyatlari.\n\n"
        "⚠️ Hamma sayt ishlayvermaydi. Marketplace xaridi, login, CAPTCHA yoki "
        "sayt himoyasini aylanib o‘tmayman. Bunday modlarni rasmiy usulda olish kerak."
    )


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 Minecraft Mod Finder — Pydroid 3 uchun Python bot.\n\n"
        "Imkoniyatlar: nom bo‘yicha ochiq web qidiruv, sahifa havolasini tekshirish, "
        "ochiq mod faylini Telegram orqali yuborish.\n\n"
        "Cheklovlar: qidiruv natijalari kafolatlanmaydi; barcha saytlardan fayl "
        "yuklab bo‘lmaydi; Marketplace pullik/himoyalangan fayllar ochilmasligi mumkin."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("last_results", None)
    await update.effective_message.reply_text("✅ Joriy qidiruv natijalari tozalandi.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await query.message.reply_text("Yordam: /help buyrug‘ini yubor.")
    elif query.data == "search_help":
        await query.message.reply_text("Qidirish uchun masalan: /search Ender Rooms")
    elif query.data == "translate_menu":
        await show_translation_menu(query.message)
    elif query.data == "mod_sites":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟠 CurseForge — Minecraft Bedrock", url="https://www.curseforge.com/minecraft-bedrock")],
            [InlineKeyboardButton("🟢 MCPEDL", url="https://mcpedl.com/")],
            [InlineKeyboardButton("🔵 Planet Minecraft", url="https://www.planetminecraft.com/")],
            [InlineKeyboardButton("⬅️ Menyuga qaytish", callback_data="back_start")],
        ])
        await query.message.reply_text(
            "🧩 Minecraft mod va addon saytlar\n\n"
            "Quyidagi tugmalardan birini bosing — sayt Telegram ichida yoki brauzerda ochiladi.\n"
            "CurseForge va MCPEDL’dan Bedrock/PE uchun mos modlarni tanlang; yuklab olishdan oldin versiya mosligini tekshiring.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    elif query.data == "mod_channel":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 @Minecraft_modlar_bedrock_java kanalini ochish", url="https://t.me/Minecraft_modlar_bedrock_java")],
            [InlineKeyboardButton("⬅️ Menyuga qaytish", callback_data="back_start")],
        ])
        await query.message.reply_text(
            "⭐ Tavsiya etilgan Minecraft modlar kanali\n\n"
            "📢 @Minecraft_modlar_bedrock_java\n"
            "Kanalni ochish uchun quyidagi tugmani bosing. Kanal mazmuni va xavfsizligini yuklab olishdan oldin o‘zingiz tekshirib chiqing.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    elif query.data == "microsoft_channel":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 @Microsoft_accaunt_UZB kanalini ochish", url="https://t.me/Microsoft_accaunt_UZB")],
            [InlineKeyboardButton("⬅️ Menyuga qaytish", callback_data="back_start")],
        ])
        await query.message.reply_text(
            "🪟 Microsoft akkaunt kanali\n\n"
            "📢 @Microsoft_accaunt_UZB\n"
            "Kanalni ochish uchun quyidagi tugmani bosing. Akkauntlardan foydalanishdan oldin "
            "ularning qonuniyligi va xavfsizligini tekshiring; parolingiz yoki tasdiqlash kodlaringizni hech kimga bermang.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    elif query.data == "back_start":
        await start(update, context)
    elif query.data.startswith("trans:"):
        parts = query.data.split(":")
        if parts[1] == "exit":
            context.user_data.pop("translation_mode", None)
            await query.message.reply_text("Tarjimon bo‘limidan chiqdingiz. Menyuni ochish uchun /start yuboring.")
        elif len(parts) == 3 and parts[1] in ("en", "ru", "uz") and parts[2] in ("en", "ru", "uz"):
            context.user_data["translation_mode"] = (parts[1], parts[2])
            names = {"en": "inglizcha", "ru": "ruscha", "uz": "o‘zbekcha"}
            await query.message.reply_text(
                f"✅ Tanlandi: {names[parts[1]]} ➜ {names[parts[2]]}.\n"
                "Endi tarjima qilinadigan matnni yuboring. Chiqish uchun /start yuboring."
            )


async def send_public_file(message, file_url, caption="✅ Ochiq mod fayli topildi!"):
    info = await asyncio.to_thread(download_file, file_url)
    if not info:
        await message.reply_text("⚠️ Havola faylga emas, sahifaga olib boryapti. Uni avtomatik yubora olmadim.")
        return
    try:
        with open(info["path"], "rb") as f:
            await message.reply_document(
                document=f,
                filename=info["filename"],
                caption=f"{caption}\n📦 Hajmi: {human_size(info['size'])}",
                read_timeout=60,
                write_timeout=60,
            )
    finally:
        try:
            os.remove(info["path"])
        except OSError:
            pass


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = " ".join(context.args).strip()
    if not query_text:
        await update.effective_message.reply_text(
            "Qidirish uchun mod nomini yoz:\n/search Ender Rooms"
        )
        return
    status = await update.effective_message.reply_text("🔎 Ochiq web sahifalaridan qidiryapman...")
    try:
        results = await asyncio.to_thread(web_search, query_text, 6)
        context.user_data["last_results"] = results
        if not results:
            await status.edit_text(
                "Natija topilmadi yoki qidiruv sayti so‘rovni blokladi. "
                "Boshqa nom bilan urinib ko‘r yoki mod sahifasining havolasini yubor."
            )
            return
        lines = [f"🔍 “{query_text}” bo‘yicha natijalar:\n"]
        keyboard_rows = []
        for i, item in enumerate(results, 1):
            snippet = item["snippet"]
            lines.append(f"{i}. {item['title']}\n{item['url']}\n{snippet}\n")
            keyboard_rows.append([InlineKeyboardButton(
                f"📥 {i}-natijani tekshirish", callback_data=f"result:{i-1}"
            )])
        await status.edit_text(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(keyboard_rows[:6]),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Search failed")
        await status.edit_text(
            "❌ Qidiruv ishlamadi. Internetni tekshir yoki mod sahifasining to‘g‘ridan-to‘g‘ri havolasini yubor."
        )


async def result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("result:"):
        return
    try:
        idx = int(query.data.split(":", 1)[1])
        results = context.user_data.get("last_results", [])
        item = results[idx]
    except (ValueError, IndexError):
        await query.message.reply_text("Natija eskirgan. Qayta qidir: /search mod nomi")
        return
    status = await query.message.reply_text("🔍 Tanlangan sahifani tekshiryapman...")
    try:
        info = await asyncio.to_thread(inspect_page, item["url"])
        if info["kind"] == "direct":
            await status.delete()
            await send_public_file(query.message, info["url"])
        elif info.get("download_url"):
            await status.edit_text("📦 Ochiq yuklab olish havolasi topildi. Faylni olayapman...")
            await send_public_file(query.message, info["download_url"])
            await status.delete()
        else:
            await status.edit_text(
                "⚠️ Sahifada ochiq, to‘g‘ridan-to‘g‘ri mod fayli topilmadi.\n"
                f"Saytni brauzerda ochib tekshir:\n{info['url']}\n\n"
                "Agar Marketplace yoki login talab qilsa, rasmiy ilova/sahifadan foydalan."
            )
    except Exception as exc:
        log.exception("Result check failed")
        await status.edit_text("❌ Bu sahifani avtomatik tekshirib bo‘lmadi. Boshqa natijani sinab ko‘r.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effec