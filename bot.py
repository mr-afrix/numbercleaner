import io
import os
import asyncio
import logging
import threading
import pandas as pd
import phonenumbers
import pycountry
from phonenumbers import region_code_for_number, country_code_for_region
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.WARNING)

TOKEN = "8305923328:AAEoD2u5kyGe5mMWA9cF-GZcvf3DozRljNA"
PORT  = int(os.environ.get("PORT", 8080))

CHANNELS = [
    {"id": -1003937355179, "slug": "sage_xd",    "label": "sᴀɢᴇ"},
    {"id": -1003081031970, "slug": "mrafrixtech", "label": "ᴀғʀɪxᴛᴇᴄʜ"},
    {"id": "@mr_afrix",    "slug": "mr_afrix",    "label": "ᴍʀᴀғʀɪx"},
]
OTP = {"slug": "mrafrix_bot", "label": "ᴏᴛᴘ ʙᴏᴛ"}

_SC = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢ"
)

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
    rows = []
    for ch in CHANNELS:
        try:
            m      = await bot.get_chat_member(ch["id"], uid)
            joined = m.status not in ("left", "kicked", "banned")
        except TelegramError:
            joined = False
        dot = "🟢" if joined else "🔴"
        rows.append([InlineKeyboardButton(f"{dot}  {ch['label']}", url=f"https://t.me/{ch['slug']}")])
    rows.append([InlineKeyboardButton(f"🟣  {OTP['label']}", url=f"https://t.me/{OTP['slug']}")])
    rows.append([InlineKeyboardButton(sc("tap here after joining"), callback_data="recheck")])
    return InlineKeyboardMarkup(rows)

def fmt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(sc("with  +"), callback_data="send:plus"),
        InlineKeyboardButton(sc("no  +"),   callback_data="send:bare"),
    ]])

def build_summary(result: dict) -> str:
    groups  = result["groups"]
    unknown = result["unknown"]
    clean   = sum(len(v["numbers"]) for v in groups.values()) + len(unknown)
    lines   = [
        f"*{sc('file processed')}*\n",
        f"{sc('total received')}     ›  `{result['total']}`",
        f"{sc('duplicates')}           ›  `{result['dupes']}`",
        f"{sc('clean numbers')}     ›  `{clean}`",
        f"{sc('countries found')}   ›  `{len(groups)}`\n",
    ]
    for iso, v in sorted(groups.items(), key=lambda x: -len(x[1]["numbers"])):
        lines.append(
            f"{iso_to_flag(iso)}  *{sc(v['name'].upper())}*"
            f"   `+{v['dial']}`   ·   `{len(v['numbers'])}` {sc('numbers')}"
        )
    if unknown:
        lines.append(f"🏳  *{sc('UNKNOWN')}*   ·   `{len(unknown)}` {sc('numbers')}")
    lines.append(f"\n{sc('choose format below')}")
    return "\n".join(lines)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    missing = await membership(ctx.bot, uid)
    if missing:
        await update.message.reply_text(
            sc("join all channels below to unlock access."),
            reply_markup=await gate_keyboard(ctx.bot, uid)
        )
        return
    await update.message.reply_text(
        f"*{sc('ready')}* ✦\n\n"
        f"{sc('send a')} `.csv` {sc('or')} `.xlsx` {sc('file and i will handle the rest.')}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    missing = await membership(ctx.bot, uid)
    if missing:
        await update.message.reply_text(
            sc("join all channels below to unlock access."),
            reply_markup=await gate_keyboard(ctx.bot, uid)
        )
        return

    doc  = update.message.document
    name = doc.file_name or "file.csv"
    ext  = name.lower().rsplit(".", 1)[-1]
    if ext not in ("csv", "xlsx", "xls"):
        await update.message.reply_text(sc("only .csv and .xlsx files are accepted."))
        return

    msg = await update.message.reply_text(sc("reading file..."))

    try:
        tg     = await ctx.bot.get_file(doc.file_id)
        raw    = bytes(await tg.download_as_bytearray())
        await msg.edit_text(sc("detecting countries..."))
        result = await asyncio.get_event_loop().run_in_executor(None, process, raw, name)
        ctx.user_data["result"] = result
        await msg.edit_text(
            build_summary(result),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=fmt_keyboard()
        )
    except Exception as e:
        logging.exception(e)
        await msg.edit_text(sc("failed to process file. check that it has a phone number column."))

async def cmd_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()

    if q.data == "recheck":
        missing = await membership(ctx.bot, uid)
        if missing:
            await q.edit_message_reply_markup(reply_markup=await gate_keyboard(ctx.bot, uid))
        else:
            await q.edit_message_text(sc("access granted. send /start to begin."))
        return

    if q.data.startswith("send:"):
        missing = await membership(ctx.bot, uid)
        if missing:
            await q.edit_message_text(
                sc("join all channels below to unlock access."),
                reply_markup=await gate_keyboard(ctx.bot, uid)
            )
            return

        result = ctx.user_data.get("result")
        if not result:
            await q.edit_message_text(sc("session expired. please re-send your file."))
            return

        use_plus = q.data == "send:plus"
        await q.edit_message_reply_markup(reply_markup=None)
        prog = await ctx.bot.send_message(uid, sc("sending files..."))

        groups  = result["groups"]
        unknown = result["unknown"]
        sent    = 0

        for iso, v in sorted(groups.items(), key=lambda x: -len(x[1]["numbers"])):
            nums    = [f"+{n}" if use_plus else n for n in v["numbers"]]
            buf     = io.BytesIO("\n".join(nums).encode())
            fname   = f"{v['name'].upper().replace(' ', '_')}.txt"
            caption = (
                f"{iso_to_flag(iso)} {sc(v['name'].upper())}\n"
                f"{sc('country code')}  ›  +{v['dial']}\n"
                f"{sc('total numbers')}  ›  {len(nums)}"
            )
            await ctx.bot.send_document(chat_id=uid, document=buf, filename=fname, caption=caption)
            sent += 1
            await asyncio.sleep(0.25)

        if unknown:
            nums    = [f"+{n}" if use_plus else n for n in unknown]
            buf     = io.BytesIO("\n".join(nums).encode())
            caption = f"🏳 {sc('UNKNOWN')}\n{sc('total numbers')}  ›  {len(nums)}"
            await ctx.bot.send_document(chat_id=uid, document=buf, filename="UNKNOWN.txt", caption=caption)

        ctx.user_data.pop("result", None)
        await prog.edit_text(f"✦  {sc('done')}  ·  {sent} {sc('file(s) sent')}")

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def main():
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), _Health).serve_forever(),
        daemon=True
    ).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, cmd_file))
    app.add_handler(CallbackQueryHandler(cmd_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
