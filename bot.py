import io
import os
import json
import asyncio
import logging
import threading
import pandas as pd
import phonenumbers
import pycountry
from phonenumbers import region_code_for_number, country_code_for_region
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands, MenuButtonDefault
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.WARNING)

TOKEN   = "8305923328:AAEoD2u5kyGe5mMWA9cF-GZcvf3DozRljNA"
PORT    = int(os.environ.get("PORT", 8080))
ADMIN   = 6914909019
DB_FILE = "users.json"

CHANNELS = [
    {"id": -1003937355179, "slug": "sage_xd",    "label": "sᴀɢᴇ",      "num": "①"},
    {"id": -1003081031970, "slug": "mrafrixtech", "label": "ᴀғʀɪxᴛᴇᴄʜ", "num": "②"},
    {"id": "@mr_afrix",    "slug": "mr_afrix",    "label": "ᴍʀᴀғʀɪx",   "num": "③"},
]
OTP = {"slug": "mrafrix_bot", "label": "ᴏᴛᴘ ʙᴏᴛ"}

_SC = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢ"
)

NUMS = ["①","②","③","④","⑤","⑥","⑦","⑧","⑨","⑩"]

def sc(t: str) -> str:
    return t.translate(_SC)

def iso_to_flag(iso: str) -> str:
    try:
        return "".join(chr(ord(c) + 127397) for c in iso.upper()[:2])
    except Exception:
        return "🏳"

def iso_to_name(iso: str) -> str:
    try:
        return pycountry.countries.get(alpha_2=iso).name
    except Exception:
        return iso

def pbar(step: int, total: int = 10) -> str:
    filled = round(step / total * 10)
    return "█" * filled + "░" * (10 - filled)

def load_db() -> dict:
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def db_get(uid: int) -> dict:
    return load_db().get(str(uid), {})

def db_set(uid: int, data: dict):
    db = load_db()
    db[str(uid)] = data
    save_db(db)

def db_update(uid: int, **kwargs):
    db  = load_db()
    key = str(uid)
    rec = db.get(key, {})
    rec.update(kwargs)
    db[key] = rec
    save_db(db)

def parse_number(raw: str):
    clean = "".join(filter(str.isdigit, raw))
    if not (7 <= len(clean) <= 15):
        return None, None, None, None
    try:
        p   = phonenumbers.parse("+" + clean)
        iso = region_code_for_number(p)
        if not iso:
            return clean, None, None, None
        return clean, iso, iso_to_name(iso), country_code_for_region(iso)
    except Exception:
        return clean, None, None, None

def detect_number_column(df: pd.DataFrame) -> str:
    priority = {"number", "phone", "phone number", "msisdn", "mobile", "tel", "telephone"}
    for col in df.columns:
        if col.strip().lower() in priority:
            return col
    for col in df.columns:
        sample = df[col].dropna().astype(str).head(20)
        ratio  = sample.str.replace(r"\D", "", regex=True).str.len().between(7, 15).mean()
        if ratio >= 0.6:
            return col
    raise ValueError("no phone column found")

def load_file(data: bytes, name: str) -> pd.DataFrame:
    n = name.lower()
    if n.endswith(".csv"):
        return pd.read_csv(io.BytesIO(data))
    if n.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(data), engine="openpyxl")
    if n.endswith(".xls"):
        return pd.read_excel(io.BytesIO(data), engine="xlrd")
    raise ValueError("unsupported format")

def process(data: bytes, name: str) -> dict:
    df      = load_file(data, name)
    col     = detect_number_column(df)
    raw     = df[col].dropna().astype(str).tolist()
    total   = len(raw)
    seen    = set()
    groups  = {}
    unknown = []
    dupes   = 0
    for r in raw:
        num, iso, cname, dial = parse_number(r)
        if num is None:
            continue
        if num in seen:
            dupes += 1
            continue
        seen.add(num)
        if iso:
            if iso not in groups:
                groups[iso] = {"name": cname, "dial": dial, "numbers": []}
            groups[iso]["numbers"].append(num)
        else:
            unknown.append(num)
    return {"groups": groups, "unknown": unknown, "dupes": dupes, "total": total}

async def membership(bot, uid: int) -> list:
    out = []
    for ch in CHANNELS:
        try:
            m = await bot.get_chat_member(ch["id"], uid)
            if m.status in ("left", "kicked", "banned"):
                out.append(ch)
        except TelegramError:
            out.append(ch)
    return out

async def gate_keyboard(bot, uid: int) -> InlineKeyboardMarkup:
    statuses = []
    joined   = 0
    total    = len(CHANNELS)

    for ch in CHANNELS:
        try:
            m  = await bot.get_chat_member(ch["id"], uid)
            ok = m.status not in ("left", "kicked", "banned")
        except TelegramError:
            ok = False
        if ok:
            joined += 1
        statuses.append((ch, ok))

    rows = []

    # ── channels: pair them side-by-side, last one alone if odd count ──
    i = 0
    while i < len(statuses):
        ch1, ok1 = statuses[i]
        dot1 = "✅" if ok1 else "🔴"
        btn1 = InlineKeyboardButton(
            f"{dot1} {ch1['num']} {ch1['label']}",
            url=f"https://t.me/{ch1['slug']}"
        )
        if i + 1 < len(statuses):
            ch2, ok2 = statuses[i + 1]
            dot2 = "✅" if ok2 else "🔴"
            btn2 = InlineKeyboardButton(
                f"{dot2} {ch2['num']} {ch2['label']}",
                url=f"https://t.me/{ch2['slug']}"
            )
            rows.append([btn1, btn2])
            i += 2
        else:
            rows.append([btn1])
            i += 1

    # ── OTP bot: full row, own space ──
    rows.append([InlineKeyboardButton(
        f"🟣 {OTP['label']}",
        url=f"https://t.me/{OTP['slug']}"
    )])

    # ── verify: last row, alone, no sharing ──
    bar = pbar(joined, total)
    rows.append([InlineKeyboardButton(
        f"🔵 [{bar}] {joined}/{total}  ·  {sc('verify')} ✦",
        callback_data="recheck"
    )])

    return InlineKeyboardMarkup(rows)

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 " + sc("send file"),         callback_data="menu:info")],
        [
            InlineKeyboardButton("🟢 " + sc("add channel"),    callback_data="menu:addch"),
            InlineKeyboardButton("🔴 " + sc("remove channel"), callback_data="menu:rmch"),
        ],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ " + sc("back"), callback_data="menu:back")
    ]])

def retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 " + sc("try again"), callback_data="menu:back")
    ]])

def fmt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 " + sc("with +"), callback_data="send:plus"),
         InlineKeyboardButton("🔴 " + sc("no +"),   callback_data="send:bare")],
    ])

def again_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 " + sc("process another file"), callback_data="menu:back")
    ]])

def build_summary(result: dict) -> str:
    groups  = result["groups"]
    unknown = result["unknown"]
    clean   = sum(len(v["numbers"]) for v in groups.values()) + len(unknown)
    lines   = [f"*{sc('file processed')}* ✦\n"]
    lines.append(f"{sc('received')}    ›  `{result['total']}`")
    lines.append(f"{sc('duplicates')}  ›  `{result['dupes']}`")
    lines.append(f"{sc('clean')}       ›  `{clean}`")
    lines.append(f"{sc('countries')}   ›  `{len(groups)}`\n")
    for iso, v in sorted(groups.items(), key=lambda x: -len(x[1]["numbers"])):
        lines.append(
            f"{iso_to_flag(iso)} *{sc(v['name'].upper())}*"
            f"  `+{v['dial']}`  ·  `{len(v['numbers'])}` {sc('numbers')}"
        )
    if unknown:
        lines.append(f"🏳 *{sc('UNKNOWN')}*  ·  `{len(unknown)}` {sc('numbers')}")
    lines.append(f"\n{sc('choose format')}")
    return "\n".join(lines)

async def show_main_menu(target, uid: int, edit: bool = False):
    rec  = db_get(uid)
    ch   = rec.get("channel")
    line = f"\n{sc('linked channel')}  ›  `{ch}`" if ch else f"\n{sc('no channel linked')}"
    text = f"*{sc('menu')}* ✦{line}"
    kb   = main_menu_kb()
    if edit:
        await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or "user"
    rec  = db_get(uid)
    if not rec:
        db_set(uid, {"banned": False, "channel": None})
    rec = db_get(uid)
    if rec.get("banned"):
        await update.message.reply_text(
            sc("you are banned from using this bot."),
            reply_markup=InlineKeyboardMarkup([[]])
        )
        return
    missing = await membership(ctx.bot, uid)
    if missing:
        total  = len(CHANNELS)
        joined = total - len(missing)
        bar    = pbar(joined, total)
        text   = (
            f"*{sc('access required')}* 🔒\n\n"
            f"{sc('join all channels to unlock')}\n\n"
            f"`[{bar}]` {joined}/{total}\n"
            f"🟢 {sc('joined')}   🔴 {sc('not joined')}\n\n"
            f"{sc('tap')} 🔴 {sc('to join then tap verify')}"
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await gate_keyboard(ctx.bot, uid)
        )
        return
    await show_main_menu(update.message, uid)

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rec = db_get(uid)
    if rec.get("banned"):
        await update.message.reply_text(sc("you are banned from using this bot."))
        return
    missing = await membership(ctx.bot, uid)
    if missing:
        await update.message.reply_text(
            sc("join all channels to use this bot."),
            reply_markup=await gate_keyboard(ctx.bot, uid)
        )
        return
    await show_main_menu(update.message, uid)

async def cmd_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rec = db_get(uid)
    if rec.get("banned"):
        await update.message.reply_text(sc("you are banned."), reply_markup=retry_kb())
        return
    missing = await membership(ctx.bot, uid)
    if missing:
        total  = len(CHANNELS)
        joined = total - len(missing)
        bar    = pbar(joined, total)
        text   = (
            f"*{sc('access required')}* 🔒\n\n"
            f"`[{bar}]` {joined}/{total}\n"
            f"{sc('tap')} 🔴 {sc('to join then tap verify')}"
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await gate_keyboard(ctx.bot, uid)
        )
        return

    doc  = update.message.document
    name = doc.file_name or "file.csv"
    ext  = name.lower().rsplit(".", 1)[-1]
    if ext not in ("csv", "xlsx", "xls"):
        await update.message.reply_text(
            sc("only .csv and .xlsx files are accepted."),
            reply_markup=retry_kb()
        )
        return

    msg = await update.message.reply_text(
        f"`[{pbar(0)}]` {sc('reading file...')}",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        tg  = await ctx.bot.get_file(doc.file_id)
        raw = bytes(await tg.download_as_bytearray())

        await msg.edit_text(
            f"`[{pbar(3)}]` {sc('parsing numbers...')}",
            parse_mode=ParseMode.MARKDOWN
        )

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, process, raw, name)

        await msg.edit_text(
            f"`[{pbar(8)}]` {sc('grouping countries...')}",
            parse_mode=ParseMode.MARKDOWN
        )

        ctx.user_data["result"] = result

        await msg.edit_text(
            f"`[{pbar(10)}]` {sc('done')}\n\n" + build_summary(result),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=fmt_keyboard()
        )
    except Exception as e:
        logging.exception(e)
        await msg.edit_text(
            sc("failed to process file. make sure it has a phone number column."),
            reply_markup=retry_kb()
        )

async def cmd_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rec = db_get(uid)
    if not rec.get("awaiting_channel"):
        return
    fwd = update.message.forward_origin
    if not fwd:
        await update.message.reply_text(
            sc("forward a message from your channel."),
            reply_markup=back_kb()
        )
        return
    try:
        chat_id = fwd.chat.id
        title   = fwd.chat.title or str(chat_id)
        db_update(uid, channel=chat_id, channel_title=title, awaiting_channel=False)
        await update.message.reply_text(
            f"✅ {sc('channel linked')}  ›  `{title}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb()
        )
    except Exception:
        await update.message.reply_text(
            sc("could not read channel from that message. make sure you forwarded from a channel."),
            reply_markup=back_kb()
        )

async def cmd_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    d   = q.data

    if d == "recheck":
        missing = await membership(ctx.bot, uid)
        if missing:
            total  = len(CHANNELS)
            joined = total - len(missing)
            bar    = pbar(joined, total)
            text   = (
                f"*{sc('access required')}* 🔒\n\n"
                f"`[{bar}]` {joined}/{total}\n"
                f"🟢 {sc('joined')}   🔴 {sc('not joined')}\n\n"
                f"{sc('tap')} 🔴 {sc('to join then tap verify')}"
            )
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=await gate_keyboard(ctx.bot, uid)
            )
        else:
            await q.edit_message_text(
                f"✅ *{sc('access granted')}*",
                parse_mode=ParseMode.MARKDOWN
            )
            await asyncio.sleep(0.4)
            await show_main_menu(q, uid, edit=True)
        return

    if d == "menu:back":
        await show_main_menu(q, uid, edit=True)
        return

    if d == "menu:info":
        await q.edit_message_text(
            f"*{sc('send file')}* ✦\n\n"
            f"{sc('send a')} `.csv` {sc('or')} `.xlsx` {sc('and i will clean it.')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb()
        )
        return

    if d == "menu:addch":
        db_update(uid, awaiting_channel=True)
        await q.edit_message_text(
            f"*{sc('add channel')}* ✦\n\n"
            f"{sc('forward any message from your channel here.')}\n"
            f"{sc('make sure this bot is an admin in that channel.')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb()
        )
        return

    if d == "menu:rmch":
        db_update(uid, channel=None, channel_title=None, awaiting_channel=False)
        await q.edit_message_text(
            f"✅ {sc('channel removed.')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb()
        )
        return

    if d.startswith("send:"):
        missing = await membership(ctx.bot, uid)
        if missing:
            await q.edit_message_text(
                sc("join all channels to use this bot."),
                reply_markup=await gate_keyboard(ctx.bot, uid)
            )
            return

        result = ctx.user_data.get("result")
        if not result:
            await q.edit_message_text(
                sc("session expired. re-send your file."),
                reply_markup=retry_kb()
            )
            return

        use_plus = d == "send:plus"
        await q.edit_message_reply_markup(reply_markup=None)

        total_files = len(result["groups"]) + (1 if result["unknown"] else 0)
        prog        = await ctx.bot.send_message(
            uid,
            f"`[{pbar(0)}]` {sc('preparing files...')}",
            parse_mode=ParseMode.MARKDOWN
        )

        groups  = result["groups"]
        unknown = result["unknown"]
        sent    = 0
        rec     = db_get(uid)
        linked  = rec.get("channel")

        sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]["numbers"]))

        # build full list of files to send: country groups + unknown
        all_files = []
        for iso, v in sorted_groups:
            nums  = [f"+{n}" if use_plus else n for n in v["numbers"]]
            buf   = io.BytesIO("\n".join(nums).encode())
            fname = f"{v['name'].upper().replace(' ', '_')}.txt"
            cap   = (
                f"{iso_to_flag(iso)} {sc(v['name'].upper())}\n"
                f"{sc('country code')}  ›  +{v['dial']}\n"
                f"{sc('total numbers')}  ›  {len(nums)}"
            )
            all_files.append((buf, fname, cap))

        if unknown:
            nums  = [f"+{n}" if use_plus else n for n in unknown]
            buf   = io.BytesIO("\n".join(nums).encode())
            cap   = f"🏳 {sc('UNKNOWN')}\n{sc('total numbers')}  ›  {len(nums)}"
            all_files.append((buf, "UNKNOWN.txt", cap))

        last_idx = len(all_files) - 1
        for i, (buf, fname, cap) in enumerate(all_files):
            is_last = (i == last_idx)
            kb      = again_kb() if is_last else None
            await ctx.bot.send_document(
                chat_id=uid,
                document=buf,
                filename=fname,
                caption=cap,
                reply_markup=kb
            )
            if linked:
                buf.seek(0)
                try:
                    await ctx.bot.send_document(chat_id=linked, document=buf, filename=fname, caption=cap)
                except TelegramError:
                    pass
            sent += 1
            step  = round(sent / total_files * 10)
            await prog.edit_text(
                f"`[{pbar(step)}]` {sc('sending')}  {sent}/{total_files}",
                parse_mode=ParseMode.MARKDOWN
            )
            await asyncio.sleep(0.25)

        ctx.user_data.pop("result", None)
        await prog.edit_text(
            f"`[{pbar(10)}]` ✦ {sc('done')}  ·  {sent} {sc('file(s) sent')}",
            parse_mode=ParseMode.MARKDOWN
        )

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("menu", "open menu")])
    await app.bot.set_chat_menu_button()

def main():
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), _Health).serve_forever(),
        daemon=True
    ).start()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(MessageHandler(filters.Document.ALL, cmd_file))
    app.add_handler(MessageHandler(filters.FORWARDED, cmd_forward))
    app.add_handler(CallbackQueryHandler(cmd_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
