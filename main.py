#!/usr/bin/env python3
import json
import os
import time
import logging
import datetime
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# KONFIGURATION
# ══════════════════════════════════════════════════════════════════

BOT_TOKEN      = "8693790416:AAEq24ADNbig5x5pBsv8vuHxR_3Dz-U-pnk"
ADMIN_IDS      = [8271107366]
DATA_FILE      = "shop_data.json"
PENDING_FILE   = "pending_orders.json"
CHECK_INTERVAL = 30
ORDER_TIMEOUT  = 3600
LTC_TOLERANCE  = 0.0001

# Conversation States
(
    ADMIN_MENU, ADD_PRODUCT_NAME, ADD_PRODUCT_DESC,
    ADD_PRODUCT_PRICE, ADD_PRODUCT_STOCK,
    SET_LTC, RESTOCK_SELECT, RESTOCK_AMOUNT
) = range(8)

# ══════════════════════════════════════════════════════════════════
# DATENVERWALTUNG
# ══════════════════════════════════════════════════════════════════

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"products": {}, "ltc_address": "", "orders": []}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_pending(pending):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ══════════════════════════════════════════════════════════════════
# LTC BLOCKCHAIN CHECK
# ══════════════════════════════════════════════════════════════════

async def get_incoming_ltc(address: str, since_timestamp: int) -> list:
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?limit=50"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"BlockCypher Status: {resp.status}")
                    return []
                raw = await resp.json()

        results = []
        for tx in raw.get("txs", []):
            received_str = tx.get("received", "")
            try:
                dt = datetime.datetime.fromisoformat(received_str.replace("Z", "+00:00"))
                tx_ts = int(dt.timestamp())
            except Exception:
                tx_ts = 0

            if tx_ts < since_timestamp:
                continue

            amount_sat = 0
            for out in tx.get("outputs", []):
                if address in out.get("addresses", []):
                    amount_sat += out.get("value", 0)

            if amount_sat > 0:
                results.append({
                    "txid":          tx.get("hash", ""),
                    "amount_ltc":    round(amount_sat / 1e8, 8),
                    "confirmations": tx.get("confirmations", 0)
                })
        return results

    except Exception as e:
        logger.error(f"BlockCypher Fehler: {e}")
        return []

# ══════════════════════════════════════════════════════════════════
# HINTERGRUND-TASK: Zahlungen prüfen (Job Queue)
# ══════════════════════════════════════════════════════════════════

async def payment_checker(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    try:
        pending  = load_pending()
        data     = load_data()
        ltc_addr = data.get("ltc_address", "")

        if not pending or not ltc_addr:
            return

        now        = int(time.time())
        oldest_ts  = now - ORDER_TIMEOUT
        txs        = await get_incoming_ltc(ltc_addr, oldest_ts)
        used_txids = {o.get("txid") for o in data.get("orders", []) if o.get("txid")}
        to_remove  = []

        for order_id, order in list(pending.items()):

            # Timeout
            if now - order["created_at"] > ORDER_TIMEOUT:
                to_remove.append(order_id)
                try:
                    await app.bot.send_message(
                        order["user_id"],
                        f"⏰ *Bestellung abgelaufen*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Deine Bestellung für *{order['product_name']}* ist abgelaufen.\n\n"
                        f"Tippe /start für einen neuen Versuch.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                continue

            expected = float(order["price"])

            for tx in txs:
                if tx["txid"] in used_txids:
                    continue
                if abs(tx["amount_ltc"] - expected) > LTC_TOLERANCE:
                    continue

                pid = order["product_id"]
                p   = data["products"].get(pid)

                if not p or not p.get("items"):
                    try:
                        await app.bot.send_message(
                            order["user_id"],
                            "⚠️ *Zahlung erhalten* – Produkt leider ausverkauft!\n"
                            "Bitte kontaktiere den Support.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                    to_remove.append(order_id)
                    break

                item = p["items"].pop(0)
                data["orders"].append({
                    "user_id":   order["user_id"],
                    "username":  order.get("username", ""),
                    "product":   p["name"],
                    "price":     order["price"],
                    "item":      item,
                    "txid":      tx["txid"],
                    "timestamp": now
                })
                used_txids.add(tx["txid"])
                save_data(data)
                to_remove.append(order_id)

                try:
                    await app.bot.send_message(
                        order["user_id"],
                        f"🎉 *Zahlung bestätigt!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🛍 Produkt: *{p['name']}*\n"
                        f"💰 Betrag: `{tx['amount_ltc']} LTC`\n"
                        f"🔗 TX: `{tx['txid'][:20]}...`\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔑 *Dein Inhalt:*\n"
                        f"`{item}`\n\n"
                        f"📩 Bitte sicher aufbewahren! 🙏",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

                for aid in ADMIN_IDS:
                    try:
                        await app.bot.send_message(
                            aid,
                            f"💰 *Neuer Verkauf!*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"👤 @{order.get('username','?')}\n"
                            f"📦 *{p['name']}* – `{order['price']} LTC`\n"
                            f"📊 Restbestand: *{len(p['items'])}x*",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                break

        for oid in to_remove:
            pending.pop(oid, None)
        save_pending(pending)

    except Exception as e:
        logger.error(f"payment_checker Fehler: {e}")

# ══════════════════════════════════════════════════════════════════
# SHOP – Kundenbereich
# ══════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data     = load_data()
    products = data["products"]

    if not products:
        await update.message.reply_text(
            "🏪 *Willkommen im Shop!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "😔 Aktuell keine Produkte verfügbar.\n"
            "_Schau später wieder vorbei!_",
            parse_mode="Markdown"
        )
        return

    keyboard = []
    for pid, p in products.items():
        stock = len(p.get("items", []))
        label = f"🛍 {p['name']}  •  {p['price']} LTC  ✅ ({stock}x)" if stock > 0 else f"🛍 {p['name']}  •  {p['price']} LTC  ❌"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"product_{pid}")])

    await update.message.reply_text(
        "🏪 *Willkommen im Shop!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💎 *Sichere Zahlung via Litecoin*\n"
        "🔒 Anonym · Schnell · Zuverlässig\n\n"
        "👇 Wähle ein Produkt:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid  = query.data.replace("product_", "")
    data = load_data()
    p    = data["products"].get(pid)

    if not p:
        await query.edit_message_text("❌ Produkt nicht gefunden.")
        return

    stock        = len(p.get("items", []))
    stock_status = "✅ Verfügbar" if stock > 0 else "❌ Ausverkauft"
    text = (
        f"🛍 *{p['name']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *Beschreibung:*\n_{p['description']}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Preis: *{p['price']} LTC*\n"
        f"📦 Status: {stock_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = []
    if stock > 0 and data.get("ltc_address"):
        keyboard.append([InlineKeyboardButton("🛒 Jetzt kaufen", callback_data=f"buy_{pid}")])
    keyboard.append([InlineKeyboardButton("◀️ Zurück zum Shop", callback_data="back_shop")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid  = query.data.replace("buy_", "")
    data = load_data()
    p    = data["products"].get(pid)

    if not p or not p.get("items"):
        await query.edit_message_text("❌ Leider ausverkauft!")
        return
    ltc = data.get("ltc_address", "")
    if not ltc:
        await query.edit_message_text("❌ Keine Zahlungsadresse konfiguriert.")
        return

    user     = query.from_user
    order_id = f"{user.id}_{int(time.time())}"
    pending  = load_pending()
    pending[order_id] = {
        "user_id":      user.id,
        "username":     user.username or user.first_name,
        "product_id":   pid,
        "product_name": p["name"],
        "price":        p["price"],
        "created_at":   int(time.time())
    }
    save_pending(pending)

    text = (
        f"🧾 *Zahlungsdetails*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛍 Produkt: *{p['name']}*\n\n"
        f"💸 *Sende EXAKT:*\n`{p['price']} LTC`\n\n"
        f"📬 *An diese Adresse:*\n`{ltc}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Check alle {CHECK_INTERVAL} Sek.\n"
        f"⏳ Gültig für *60 Minuten*\n"
        f"🔔 Du wirst automatisch benachrichtigt!"
    )
    keyboard = [[InlineKeyboardButton("❌ Bestellung abbrechen", callback_data=f"cancel_{order_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    order_id = query.data.replace("cancel_", "")
    pending  = load_pending()
    pending.pop(order_id, None)
    save_pending(pending)
    await query.edit_message_text(
        "❌ *Bestellung abgebrochen*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kein Problem! Tippe /start für einen neuen Einkauf.",
        parse_mode="Markdown"
    )

async def back_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = load_data()
    keyboard = []
    for pid, p in data["products"].items():
        stock = len(p.get("items", []))
        label = f"🛍 {p['name']}  •  {p['price']} LTC  ✅ ({stock}x)" if stock > 0 else f"🛍 {p['name']}  •  {p['price']} LTC  ❌"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"product_{pid}")])
    await query.edit_message_text(
        "🏪 *Willkommen im Shop!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💎 *Sichere Zahlung via Litecoin*\n"
        "🔒 Anonym · Schnell · Zuverlässig\n\n"
        "👇 Wähle ein Produkt:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Kein Zugriff!")
        return ConversationHandler.END
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def show_admin_menu(update, context, edit=False):
    data    = load_data()
    pending = load_pending()
    ltc     = data.get("ltc_address") or "❌ Nicht gesetzt"

    text = (
        f"⚙️ *Admin Panel*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💳 *LTC Adresse:*\n`{ltc}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Produkte:           *{len(data['products'])}*\n"
        f"⏳ Offene Zahlungen:   *{len(pending)}*\n"
        f"✅ Abgeschl. Verkäufe: *{len(data['orders'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Produkt hinzufügen", callback_data="admin_add_product")],
        [InlineKeyboardButton("📦 Lager auffüllen",    callback_data="admin_restock"),
         InlineKeyboardButton("📋 Produkte",           callback_data="admin_list")],
        [InlineKeyboardButton("💳 LTC Adresse setzen", callback_data="admin_set_ltc"),
         InlineKeyboardButton("📊 Statistiken",        callback_data="admin_stats")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if edit and hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data

    if action == "admin_set_ltc":
        await query.edit_message_text("💳 Sende die neue LTC Empfangsadresse:")
        return SET_LTC

    elif action == "admin_add_product":
        await query.edit_message_text("📦 *Neues Produkt*\n\nName eingeben:", parse_mode="Markdown")
        return ADD_PRODUCT_NAME

    elif action == "admin_restock":
        data = load_data()
        if not data["products"]:
            await query.edit_message_text("❌ Keine Produkte vorhanden.")
            return ADMIN_MENU
        keyboard = [
            [InlineKeyboardButton(f"{p['name']} (📦 {len(p.get('items',[]))}x)", callback_data=f"restock_{pid}")]
            for pid, p in data["products"].items()
        ]
        keyboard.append([InlineKeyboardButton("◀️ Zurück", callback_data="admin_back")])
        await query.edit_message_text("📦 Welches Produkt auffüllen?", reply_markup=InlineKeyboardMarkup(keyboard))
        return RESTOCK_SELECT

    elif action == "admin_list":
        data = load_data()
        if not data["products"]:
            await query.edit_message_text("❌ Keine Produkte vorhanden.")
            return ADMIN_MENU
        text = "📋 *Produktübersicht*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for pid, p in data["products"].items():
            stock = len(p.get("items", []))
            dot   = "🟢" if stock > 5 else ("🟡" if stock > 0 else "🔴")
            text += f"{dot} *{p['name']}*\n   💰 {p['price']} LTC  •  📦 {stock}x\n\n"
        keyboard = [[InlineKeyboardButton("◀️ Zurück", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return ADMIN_MENU

    elif action == "admin_stats":
        data    = load_data()
        pending = load_pending()
        revenue = sum(float(o.get("price", 0)) for o in data["orders"])
        text = (
            f"📊 *Statistiken*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Einnahmen: `{revenue:.4f} LTC`\n"
            f"✅ Verkäufe:  *{len(data['orders'])}*\n"
            f"⏳ Offen:     *{len(pending)}*\n"
            f"📦 Produkte:  *{len(data['products'])}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        keyboard = [[InlineKeyboardButton("◀️ Zurück", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return ADMIN_MENU

    elif action == "admin_back":
        await show_admin_menu(update, context, edit=True)
        return ADMIN_MENU

    elif action.startswith("restock_"):
        pid = action.replace("restock_", "")
        context.user_data["restock_pid"] = pid
        data = load_data()
        p    = data["products"][pid]
        await query.edit_message_text(
            f"📦 *{p['name']}*\n"
            f"Aktueller Bestand: {len(p.get('items', []))}x\n\n"
            f"Sende Artikel – einen pro Zeile:\n"
            f"_(z.B. Lizenzkeys, Accounts, Links…)_",
            parse_mode="Markdown"
        )
        return RESTOCK_AMOUNT

async def set_ltc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ltc  = update.message.text.strip()
    data = load_data()
    data["ltc_address"] = ltc
    save_data(data)
    await update.message.reply_text(f"✅ LTC Adresse gesetzt:\n`{ltc}`", parse_mode="Markdown")
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"] = {"name": update.message.text.strip(), "items": []}
    await update.message.reply_text("📝 Beschreibung des Produkts:")
    return ADD_PRODUCT_DESC

async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text("💰 Preis in LTC (z.B. `0.05`):", parse_mode="Markdown")
    return ADD_PRODUCT_PRICE

async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_product"]["price"] = float(update.message.text.strip())
        await update.message.reply_text("📦 Erste Artikel einfügen (einen pro Zeile):")
        return ADD_PRODUCT_STOCK
    except ValueError:
        await update.message.reply_text("❌ Ungültiger Preis. Nochmal eingeben:")
        return ADD_PRODUCT_PRICE

async def add_product_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = [l.strip() for l in update.message.text.split("\n") if l.strip()]
    np    = context.user_data["new_product"]
    np["items"] = items
    data  = load_data()
    pid   = str(int(time.time()))
    data["products"][pid] = np
    save_data(data)
    await update.message.reply_text(
        f"✅ *{np['name']}* erstellt!\n💰 {np['price']} LTC | 📦 {len(items)}x",
        parse_mode="Markdown"
    )
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def restock_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid   = context.user_data.get("restock_pid")
    items = [l.strip() for l in update.message.text.split("\n") if l.strip()]
    data  = load_data()
    if pid in data["products"]:
        data["products"][pid]["items"].extend(items)
        save_data(data)
        p = data["products"][pid]
        await update.message.reply_text(
            f"✅ *{p['name']}* aufgefüllt!\n"
            f"➕ {len(items)} neue | 📦 {len(p['items'])}x gesamt",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Produkt nicht gefunden.")
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Abgebrochen.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin)],
        states={
            ADMIN_MENU:        [CallbackQueryHandler(admin_callback)],
            SET_LTC:           [MessageHandler(filters.TEXT & ~filters.COMMAND, set_ltc)],
            ADD_PRODUCT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_PRODUCT_DESC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_desc)],
            ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)],
            ADD_PRODUCT_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_stock)],
            RESTOCK_SELECT:    [CallbackQueryHandler(admin_callback)],
            RESTOCK_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, restock_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(show_product,  pattern=r"^product_"))
    app.add_handler(CallbackQueryHandler(buy_product,   pattern=r"^buy_"))
    app.add_handler(CallbackQueryHandler(cancel_order,  pattern=r"^cancel_"))
    app.add_handler(CallbackQueryHandler(back_shop,     pattern=r"^back_shop$"))

    app.job_queue.run_repeating(payment_checker, interval=CHECK_INTERVAL, first=15)

    logger.info("Bot startet...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main(