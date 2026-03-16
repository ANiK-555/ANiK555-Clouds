import asyncio
import logging
import os
import re
import uuid
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from config import (
    BOT_TOKEN, BOT_USERNAME, BOT_NAME, CHANNEL_URL, CHANNEL_BTN, OWNER_ID,
    DB_URI, DB_NAME, DB_CHANNEL, AUTO_DEL,
    START_PHOTO, FSUB_PHOTO, DEFAULT_MESSAGES,
)

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")

_client = AsyncIOMotorClient(DB_URI)
_db = _client[DB_NAME]
files_col = _db["files"]
users_col = _db["users"]
settings_col = _db["settings"]
fsub_col = _db["fsub_channels"]
banned_col = _db["banned"]


def _make_token(fname):
    base = re.sub(r'\.[^.]+$', '', fname)
    slug = re.sub(r'[^\w]', '_', base).strip('_')
    slug = re.sub(r'_+', '_', slug)[:40]
    return f"{slug}_{uuid.uuid4().hex[:5]}" if slug else uuid.uuid4().hex[:12]


def _fmt_size(b):
    if b >= 1073741824: return f"{b/1073741824:.1f} GB"
    if b >= 1048576: return f"{b/1048576:.1f} MB"
    if b >= 1024: return f"{b/1024:.1f} KB"
    return f"{b} B"


def _fmt_del(s):
    if s >= 3600: return f"{s//3600}h"
    if s >= 60: return f"{s//60}m"
    return f"{s}s"


def _back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_back")]])


async def db_store_file(file_id, file_type, file_name, message_id, token):
    await files_col.insert_one({"_id": token, "file_id": file_id, "file_type": file_type, "file_name": file_name, "message_id": message_id})


async def db_get_file(token):
    return await files_col.find_one({"_id": token})


async def db_del_file(token):
    await files_col.delete_one({"_id": token})


async def db_list_files(limit=15):
    return await files_col.find().sort("_id", -1).limit(limit).to_list(None)


async def db_add_user(uid, first, username):
    await users_col.update_one({"_id": uid}, {"$set": {"first_name": first, "username": username}}, upsert=True)


async def db_all_users():
    return await users_col.find({}, {"_id": 1}).to_list(None)


async def db_user_count():
    return await users_col.count_documents({})


async def db_file_count():
    return await files_col.count_documents({})


async def db_is_banned(uid):
    return await banned_col.find_one({"_id": uid}) is not None


async def db_ban(uid):
    await banned_col.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)


async def db_unban(uid):
    await banned_col.delete_one({"_id": uid})


async def db_get_setting(key, default=None):
    doc = await settings_col.find_one({"_id": key})
    return doc["value"] if doc else default


async def db_set_setting(key, value):
    await settings_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


async def db_get_msg(key):
    val = await db_get_setting(f"msg_{key}")
    return val if val is not None else DEFAULT_MESSAGES.get(key, "")


async def db_save_msg(key, value):
    await db_set_setting(f"msg_{key}", value)


async def db_get_auto_del():
    return int(await db_get_setting("auto_del", AUTO_DEL))


async def db_add_fsub(ch_id, title, invite_link=None):
    await fsub_col.update_one(
        {"_id": ch_id},
        {"$set": {"title": title, "invite_link": invite_link}},
        upsert=True,
    )


async def db_remove_fsub(ch_id):
    await fsub_col.delete_one({"_id": ch_id})


async def db_get_fsub():
    return await fsub_col.find().to_list(None)


async def get_channel_invite(context, ch_id):
    try:
        chat = await context.bot.get_chat(ch_id)
        if chat.username:
            return f"https://t.me/{chat.username}", chat.title
        link = chat.invite_link
        if not link:
            link = await context.bot.export_chat_invite_link(ch_id)
        return link, chat.title
    except Exception as e:
        return None, str(ch_id)


async def check_fsub(uid, context):
    not_joined = []
    for ch in await db_get_fsub():
        try:
            m = await context.bot.get_chat_member(ch["_id"], uid)
            if m.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    if await db_is_banned(user.id):
        await update.message.reply_text(await db_get_msg("BANNED"))
        return
    await db_add_user(user.id, user.first_name, user.username or "")
    if context.args:
        token = context.args[0]
        not_joined = await check_fsub(user.id, context)
        if not_joined:
            await send_fsub(update, context, not_joined, token, user.first_name)
            return
        await deliver_file(update, context, token)
        return
    msg = (await db_get_msg("START")).replace("{first}", user.first_name)
    btn = [[InlineKeyboardButton(CHANNEL_BTN, url=CHANNEL_URL)]]
    try:
        await update.message.reply_photo(photo=START_PHOTO, caption=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btn))
    except Exception:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btn))


async def send_fsub(update, context, channels, token, first_name):
    msg = (await db_get_msg("FSUB")).replace("{first}", first_name)
    btns = []
    for ch in channels:
        invite = ch.get("invite_link")
        title = ch.get("title", str(ch["_id"]))
        if not invite:
            invite, title = await get_channel_invite(context, ch["_id"])
            if invite:
                await db_add_fsub(ch["_id"], title, invite)
        if invite:
            btns.append([InlineKeyboardButton(f"➕ Join {title}", url=invite)])
    btns.append([InlineKeyboardButton("✅ Try Again", url=f"https://t.me/{BOT_USERNAME}?start={token}")])
    markup = InlineKeyboardMarkup(btns)
    try:
        await update.message.reply_photo(photo=FSUB_PHOTO, caption=msg, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=markup)


async def deliver_file(update, context, token):
    doc = await db_get_file(token)
    if not doc:
        await update.message.reply_text(await db_get_msg("NO_FILE"))
        return
    auto_del = await db_get_auto_del()
    caption = (
        (await db_get_msg("FILE_CAPTION"))
        .replace("{file_name}", doc.get("file_name", "File"))
        .replace("{auto_del}", _fmt_del(auto_del))
    )
    ftype, fid = doc["file_type"], doc["file_id"]
    try:
        if ftype == "document":
            sent = await update.message.reply_document(fid, caption=caption, parse_mode="HTML")
        elif ftype == "video":
            sent = await update.message.reply_video(fid, caption=caption, parse_mode="HTML")
        elif ftype == "audio":
            sent = await update.message.reply_audio(fid, caption=caption, parse_mode="HTML")
        elif ftype == "photo":
            sent = await update.message.reply_photo(fid, caption=caption, parse_mode="HTML")
        else:
            sent = await update.message.reply_document(fid, caption=caption, parse_mode="HTML")

        chat_id = update.effective_chat.id
        msg_id = sent.message_id

        async def _auto_del():
            await asyncio.sleep(auto_del)
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            try:
                await context.bot.send_message(chat_id, await db_get_msg("FILE_EXPIRED"), parse_mode="HTML")
            except Exception:
                pass

        asyncio.create_task(_auto_del())
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


def _is_admin(uid):
    return uid == OWNER_ID


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not _is_admin(update.effective_user.id):
        return
    auto_del = await db_get_auto_del()
    fsub = await db_get_fsub()
    users = await db_user_count()
    files = await db_file_count()
    text = (
        f"⚙️ <b>{BOT_NAME} Admin Panel</b>\n\n"
        f"👥 Users: <b>{users}</b>  |  📦 Files: <b>{files}</b>\n"
        f"🔒 Force Sub: <b>{len(fsub)}</b>  |  ⏳ Auto Del: <b>{_fmt_del(auto_del)}</b>"
    )
    btns = [
        [InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
         InlineKeyboardButton("📋 List FSub", callback_data="adm_listfsub")],
        [InlineKeyboardButton("📝 Edit Messages", callback_data="adm_listmsgs"),
         InlineKeyboardButton("📦 List Files", callback_data="adm_listfiles")],
        [InlineKeyboardButton("➕ Add FSub", callback_data="adm_addfsub_help"),
         InlineKeyboardButton("➖ Remove FSub", callback_data="adm_removefsub_help")],
        [InlineKeyboardButton("⏳ Auto Delete", callback_data="adm_autodel_help"),
         InlineKeyboardButton("📡 Broadcast", callback_data="adm_broadcast_help")],
        [InlineKeyboardButton("🚫 Ban", callback_data="adm_ban_help"),
         InlineKeyboardButton("✅ Unban", callback_data="adm_unban_help")],
        [InlineKeyboardButton("🗑 Delete File", callback_data="adm_delfile_help"),
         InlineKeyboardButton("🍃 MongoDB", callback_data="adm_mongo")],
    ]
    markup = InlineKeyboardMarkup(btns)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    await q.answer()
    if not _is_admin(q.from_user.id):
        return
    d = q.data

    if d == "adm_stats":
        text = (
            f"📊 <b>Stats</b>\n\n"
            f"👥 Users: <b>{await db_user_count()}</b>\n"
            f"📦 Files: <b>{await db_file_count()}</b>\n"
            f"🔒 FSub: <b>{len(await db_get_fsub())}</b>\n"
            f"⏳ Auto Del: <b>{_fmt_del(await db_get_auto_del())}</b>"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_mongo":
        try:
            await _client.admin.command("ping")
            users = await db_user_count()
            files = await db_file_count()
            text = (
                f"🍃 <b>MongoDB Status</b>\n\n"
                f"✅ <b>Connected</b>\n"
                f"🗄 DB: <code>{DB_NAME}</code>\n"
                f"👥 Users: <b>{users}</b>\n"
                f"📦 Files: <b>{files}</b>\n\n"
                f"<i>All data persists across deployments</i>"
            )
        except Exception as e:
            text = f"🍃 <b>MongoDB Status</b>\n\n❌ <b>Error:</b>\n<code>{e}</code>"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfsub":
        channels = await db_get_fsub()
        text = "📋 <b>Force Sub Channels:</b>\n\n" if channels else "No force sub channels.\n\nUse /addfsub &lt;id1&gt; &lt;id2&gt; ..."
        for ch in channels:
            text += f"• <b>{ch.get('title','?')}</b>\n  <code>{ch['_id']}</code>\n\n"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listmsgs":
        text = "📝 <b>Editable Keys:</b>\n\n"
        for key in DEFAULT_MESSAGES:
            text += f"<code>/setmsg {key} text</code>\n"
        text += "\n<b>Placeholders:</b> <code>{first}</code> <code>{auto_del}</code> <code>{file_name}</code> <code>{count}</code>"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfiles" or d.startswith("adm_listfiles_p"):
        page = int(d.split("_p")[1]) if "_p" in d else 0
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await q.edit_message_text("No files stored.", reply_markup=_back_btn())
            return
        text = f"📦 <b>Files</b> ({page * per_page + 1}–{min((page + 1) * per_page, total)} of {total})\n\n"
        text += "<i>🔗 = Get Link  🗑 = Delete</i>\n"
        btns = []
        for f in chunk:
            fname = f.get("file_name", "?")
            short = fname[:26] + "…" if len(fname) > 26 else fname
            token = f["_id"]
            link = f"https://t.me/{BOT_USERNAME}?start={token}"
            btns.append([
                InlineKeyboardButton(f"📄 {short}", callback_data=f"adm_fileinfo_{token}"),
                InlineKeyboardButton("🔗", url=link),
                InlineKeyboardButton("🗑", callback_data=f"adm_qdel_{token}|{page}"),
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_listfiles_p{page - 1}"))
        if (page + 1) * per_page < total:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_listfiles_p{page + 1}"))
        if nav:
            btns.append(nav)
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("adm_fileinfo_"):
        token = d[len("adm_fileinfo_"):]
        doc = await db_get_file(token)
        if not doc:
            await q.edit_message_text("❌ File not found.", reply_markup=_back_btn())
            return
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        text = (
            f"📄 <b>File Info</b>\n\n"
            f"📦 <b>Name:</b> {doc.get('file_name','?')}\n"
            f"🗂 <b>Token:</b> <code>{token}</code>\n"
            f"🔗 <b>Link:</b>\n<code>{link}</code>"
        )
        btns = [
            [InlineKeyboardButton("🔗 Open Link", url=link)],
            [InlineKeyboardButton("🗑 Delete File", callback_data=f"adm_qdel_{token}|0")],
            [InlineKeyboardButton("⬅️ Back to Files", callback_data="adm_listfiles")],
        ]
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("adm_qdel_"):
        payload = d[len("adm_qdel_"):]
        token, page_str = payload.rsplit("|", 1)
        page = int(page_str)
        doc = await db_get_file(token)
        if doc:
            try:
                await context.bot.delete_message(DB_CHANNEL, doc["message_id"])
            except Exception:
                pass
            await db_del_file(token)
        await q.answer("🗑 Deleted!", show_alert=False)
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk and page > 0:
            page = max(0, page - 1)
            chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await q.edit_message_text("No files stored.", reply_markup=_back_btn())
            return
        text = f"📦 <b>Files</b> ({page * per_page + 1}–{min((page + 1) * per_page, total)} of {total})\n\n<i>🔗 = Get Link  🗑 = Delete</i>\n"
        btns = []
        for f in chunk:
            fname = f.get("file_name", "?")
            short = fname[:26] + "…" if len(fname) > 26 else fname
            tok = f["_id"]
            link = f"https://t.me/{BOT_USERNAME}?start={tok}"
            btns.append([
                InlineKeyboardButton(f"📄 {short}", callback_data=f"adm_fileinfo_{tok}"),
                InlineKeyboardButton("🔗", url=link),
                InlineKeyboardButton("🗑", callback_data=f"adm_qdel_{tok}|{page}"),
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_listfiles_p{page - 1}"))
        if (page + 1) * per_page < total:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_listfiles_p{page + 1}"))
        if nav:
            btns.append(nav)
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d == "adm_addfsub_help":
        await q.edit_message_text(
            "➕ <b>Add Force Sub</b>\n\n"
            "1. Add bot as <b>admin</b> to channel\n"
            "2. Run (supports multiple):\n\n"
            "<code>/addfsub -100xxx</code>\n"
            "<code>/addfsub -100xxx -100yyy -100zzz</code>\n\n"
            "<i>Get ID via @userinfobot</i>",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_removefsub_help":
        channels = await db_get_fsub()
        if not channels:
            text = "No channels to remove."
        else:
            text = "➖ <b>Remove FSub — copy & run:</b>\n\n"
            for ch in channels:
                text += f"<code>/removefsub {ch['_id']}</code> — {ch.get('title','?')}\n"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_autodel_help":
        s = await db_get_auto_del()
        await q.edit_message_text(
            f"⏳ <b>Auto Delete</b>\n\nCurrent: <b>{_fmt_del(s)}</b> ({s}s)\n\n"
            f"<code>/setautodel 300</code> — 5m\n"
            f"<code>/setautodel 3600</code> — 1h\n"
            f"<code>/setautodel 86400</code> — 24h",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_broadcast_help":
        await q.edit_message_text(
            f"📡 <b>Broadcast</b>\n\n{await db_user_count()} users will receive it.\n\n<code>/broadcast your text</code>",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_ban_help":
        await q.edit_message_text("🚫 <b>Ban</b>\n\n<code>/ban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_unban_help":
        await q.edit_message_text("✅ <b>Unban</b>\n\n<code>/unban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_delfile_help":
        await q.edit_message_text(
            "🗑 <b>Delete File</b>\n\n<code>/delfile token</code>\n\nDeletes from channel + DB.",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_back":
        await admin_panel(update, context)


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not _is_admin(update.effective_user.id):
        return
    msg = update.effective_message
    if msg.document:
        fid, ftype, fname, fsize = msg.document.file_id, "document", msg.document.file_name or "file", msg.document.file_size or 0
    elif msg.video:
        fid, ftype, fname, fsize = msg.video.file_id, "video", f"video_{msg.video.file_unique_id}.mp4", msg.video.file_size or 0
    elif msg.audio:
        fid, ftype, fname, fsize = msg.audio.file_id, "audio", msg.audio.file_name or "audio.mp3", msg.audio.file_size or 0
    elif msg.photo:
        fid, ftype, fname, fsize = msg.photo[-1].file_id, "photo", "photo.jpg", msg.photo[-1].file_size or 0
    else:
        return
    try:
        if ftype == "document":
            fwd = await context.bot.send_document(chat_id=DB_CHANNEL, document=fid)
        elif ftype == "video":
            fwd = await context.bot.send_video(chat_id=DB_CHANNEL, video=fid)
        elif ftype == "audio":
            fwd = await context.bot.send_audio(chat_id=DB_CHANNEL, audio=fid)
        elif ftype == "photo":
            fwd = await context.bot.send_photo(chat_id=DB_CHANNEL, photo=fid)
        token = _make_token(fname)
        await db_store_file(fid, ftype, fname, fwd.message_id, token)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        auto_del = await db_get_auto_del()
        text = (
            f"✅ <b>File Uploaded Successfully!</b>\n\n"
            f"🗂 <b>Token:</b> <code>{token}</code>\n"
            f"📦 <b>File:</b> {fname}\n"
            f"💾 <b>Size:</b> {_fmt_size(fsize)}\n"
            f"⏰ <b>Auto-delete:</b> {_fmt_del(auto_del)}\n\n"
            f"🔗 <b>Share Link:</b>\n<code>https://t.me/{BOT_USERNAME}?start={token}</code>"
        )
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Share Link", url=link)]]))
    except Exception as e:
        await msg.reply_text(f"❌ Error: {e}")


async def cmd_addfsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /addfsub -100xxx -100yyy ...")
        return
    results = []
    for arg in context.args:
        try:
            ch_id = int(arg)
            if ch_id > 0: ch_id = int(f"-100{ch_id}")
            invite, title = await get_channel_invite(context, ch_id)
            await db_add_fsub(ch_id, title, invite)
            results.append(f"✅ <b>{title}</b> <code>{ch_id}</code>")
        except Exception as e:
            results.append(f"❌ <code>{arg}</code> — {e}")
    await update.message.reply_text("\n".join(results), parse_mode="HTML")


async def cmd_removefsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /removefsub <channel_id>")
        return
    await db_remove_fsub(int(context.args[0]))
    await update.message.reply_text(f"✅ Removed <code>{context.args[0]}</code>", parse_mode="HTML")


async def cmd_listfsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    channels = await db_get_fsub()
    if not channels:
        await update.message.reply_text("No force sub channels.")
        return
    text = "📋 <b>Force Sub Channels:</b>\n\n"
    for ch in channels:
        text += f"• <b>{ch.get('title','?')}</b>\n  <code>{ch['_id']}</code>\n\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    text = (
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: <b>{await db_user_count()}</b>\n"
        f"📦 Files: <b>{await db_file_count()}</b>\n"
        f"🔒 FSub: <b>{len(await db_get_fsub())}</b>\n"
        f"⏳ Auto Del: <b>{_fmt_del(await db_get_auto_del())}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_mongo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    try:
        await _client.admin.command("ping")
        users = await db_user_count()
        files = await db_file_count()
        text = (
            f"🍃 <b>MongoDB Status</b>\n\n"
            f"✅ <b>Connected</b>\n"
            f"🗄 DB: <code>{DB_NAME}</code>\n"
            f"👥 Users: <b>{users}</b>\n"
            f"📦 Files: <b>{files}</b>\n\n"
            f"<i>Data persists across all deployments</i>"
        )
    except Exception as e:
        text = f"🍃 <b>MongoDB Status</b>\n\n❌ <b>Error:</b>\n<code>{e}</code>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_setautodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /setautodel <seconds>")
        return
    try:
        val = int(context.args[0])
        if val < 30:
            await update.message.reply_text("❌ Minimum 30s.")
            return
        await db_set_setting("auto_del", val)
        await update.message.reply_text(f"✅ Auto delete: <b>{_fmt_del(val)}</b> ({val}s)", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")


async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setmsg <KEY> <text>")
        return
    key = context.args[0].upper()
    if key not in DEFAULT_MESSAGES:
        await update.message.reply_text(f"❌ Invalid key. Valid: {', '.join(DEFAULT_MESSAGES.keys())}")
        return
    await db_save_msg(key, " ".join(context.args[1:]))
    await update.message.reply_text(f"✅ <b>{key}</b> updated!", parse_mode="HTML")


async def cmd_listmsgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    text = "📝 <b>Message Keys:</b>\n\n" + "\n".join(f"• <code>{k}</code>" for k in DEFAULT_MESSAGES)
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <text>")
        return
    text = " ".join(context.args)
    users = await db_all_users()
    sent = failed = 0
    prog = await update.message.reply_text(f"📡 Sending to {len(users)} users...")
    for u in users:
        try:
            await context.bot.send_message(u["_id"], text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await prog.edit_text(f"✅ Sent: {sent} | ❌ Failed: {failed}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(context.args[0])
    await db_ban(uid)
    await update.message.reply_text(f"🚫 Banned <code>{uid}</code>", parse_mode="HTML")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    await db_unban(uid)
    await update.message.reply_text(f"✅ Unbanned <code>{uid}</code>", parse_mode="HTML")


async def cmd_delfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /delfile <token>")
        return
    token = context.args[0]
    doc = await db_get_file(token)
    if not doc:
        await update.message.reply_text("❌ Token not found.")
        return
    try:
        await context.bot.delete_message(DB_CHANNEL, doc["message_id"])
    except Exception:
        pass
    await db_del_file(token)
    await update.message.reply_text(f"🗑 <code>{token}</code> deleted.", parse_mode="HTML")


async def error_handler(update, context):
    err = context.error
    logging.error(f"Update {update} caused error: {err}", exc_info=context.error)


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Get your file"),
    ])


async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("mongo", cmd_mongo))
    app.add_handler(CommandHandler("addfsub", cmd_addfsub))
    app.add_handler(CommandHandler("removefsub", cmd_removefsub))
    app.add_handler(CommandHandler("listfsub", cmd_listfsub))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("setautodel", cmd_setautodel))
    app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    app.add_handler(CommandHandler("listmsgs", cmd_listmsgs))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delfile", cmd_delfile))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        file_handler,
    ))
    app.add_error_handler(error_handler)

    async def webhook_handler(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.update_queue.put(update)
        return web.Response(text="ok")

    async def health_handler(request):
        return web.Response(text="ok")

    await app.initialize()

    if WEBHOOK_URL:
        await app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logging.info(f"Webhook set: {WEBHOOK_URL}/webhook")
    else:
        logging.warning("WEBHOOK_URL not set — webhook not registered with Telegram")

    await app.start()

    aio_app = web.Application()
    aio_app.router.add_post("/webhook", webhook_handler)
    aio_app.router.add_get("/health", health_handler)
    aio_app.router.add_get("/", health_handler)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"🌩️ {BOT_NAME} running on port {PORT}")

    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
