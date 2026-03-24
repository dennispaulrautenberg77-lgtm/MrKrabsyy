#!/usr/bin/env python3
"""
Telegram Shop Bot mit echter LTC Blockchain-Zahlungsverifizierung
Installieren: pip install python-telegram-bot[job-queue]==20.7 aiohttp
"""

import json
import os
import time
import asyncio
import logging
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# KONFIGURATION  –  Hier anpassen!
# ══════════════════════════════════════════════════════════════════

BOT_TOKEN      = "8693790416:AAEq24ADNbig5x5pBsv8vuHxR_3Dz-U-pnk"
ADMIN_IDS      = [8271107366]
DATA_FILE      = "shop_data.json"
PENDING_FILE   = "pending_orders.json"
CHECK_INTERVAL = 30          # Sekunden zwischen Blockchain-Checks
ORDER_TIMEOUT  = 3600        # Sekunden bis Bestellung verfällt (1h)
LTC_TOLERANCE  = 0.0001      # Erlaubte Abweichung beim Betrag

# ══════════════════════════════════════════════════════════════════
# Conversation States
# ══════════════════════════════════════════════════════════════════

(
    ADMIN_MENU, ADD_PRODUCT_NAME, ADD_PRODUCT_DESC,
    ADD_PRODUCT_PRICE, ADD_PRODUCT_STOCK,
    SET_LTC, RESTOCK_SELECT, RESTOCK_AMOUNT
) = range(8)

# ──────────────────────────────────────────────────────────────────
# DATENVERWALTUNG
# ──────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────
# LTC BLOCKCHAIN CHECK (BlockCypher – kostenlos)
# ──────────────────────────────────────────────────────────────────

async def get_incoming_ltc(address: str, since_timestamp: int) -> list:
    """
    Ruft alle eingehenden LTC-Transaktionen an 'address' ab,
    die nach 'since_timestamp' empfangen wurden.
    Rueckgabe: [{"txid": str, "amount_ltc": float, "confirmations": int}]
    """
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?limit=50"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"BlockCypher Status: {resp.status}")
                    return []
                raw = await resp.json()

        import datetime
        results = []
        for tx in raw.get("txs", []):
            received_str = tx.get("received", "")
            try:
                dt    = datetime.datetime.fromisoformat(received_str.replace("Z", "+00:00"))
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

# ──────────────────────────────────────────────────────────────────
# HINTERGRUND-TASK: Zahlungen prüfen
# ──────────────────────────────────────────────────────────────────

async def payment_checker_loop(context: ContextTypes.DEFAULT_TYPE):
    """
    Wird vom Job-Queue alle CHECK_INTERVAL Sekunden aufgerufen.
    Prueft ob offene Bestellungen eine passende LTC-Zahlung erhalten haben.
    """
    app = context.application
    try:
        pending  = load_pending()
        data     = load_data()
        ltc_addr = data.get("ltc_address", "")

        if not pending or not ltc_addr:
            return

        now          = int(time.time())
        oldest_ts    = now - ORDER_TIMEOUT
        transactions = await get_incoming_ltc(ltc_addr, oldest_ts)
        used_txids   = {o.get("txid") for o in data.get("orders", []) if o.get("txid")}
        to_remove    = []

        for order_id, order in list(pending.items()):

            if now - order["created_at"] > ORDER_TIMEOUT:
                to_remove.append(order_id)
                try:
                    await app.bot.send_message(
                        order["user_id"],
                        f"⏰ *Bestellung abgelaufen*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Deine Bestellung für *{order['product_name']}* ist nach 60 Minuten abgelaufen.\n\n"
                        f"👉 Tippe /start für einen neuen Versuch.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                continue

            expected_ltc = float(order["price"])

            for tx in transactions:
                if tx["txid"] in used_txids:
                    continue
                if abs(tx["amount_ltc"] - expected_ltc) > LTC_TOLERANCE:
                    continue

                pid = order["product_id"]
                p   = data["products"].get(pid)

                if not p or not p.get("items"):
                    try:
                        await app.bot.send_message(
                            order["user_id"],
                            "⚠️ *Zahlung erhalten* – Produkt jedoch ausverkauft!\n"
                            "Bitte kontaktiere den Support.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                    to_remove.append(order_id)
                    for aid in ADMIN_IDS:
                        try:
                            await app.bot.send_message(
                                aid,
                                f"⚠️ Zahlung erhalten aber *{p['name'] if p else pid}* ausverkauft!\n"
                                f"User: @{order.get('username','?')} | {tx['amount_ltc']} LTC",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
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
                        f"┌─────────────────────\n"
                        f"│ `{item}`\n"
                        f"└─────────────────────\n\n"
                        f"📩 *Bitte sicher aufbewahren!*\n"
                        f"Danke für deinen Einkauf! 🙏",
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
                            f"👤 Käufer: @{order.get('username','?')}\n"
                            f"📦 Produkt: *{p['name']}*\n"
                            f"💸 Betrag: `{order['price']} LTC`\n"
                            f"📊 Restbestand: *{len(p['items'])}x*\n"
                            f"🔗 TX: `{tx['txid'][:20]}...`",
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

# ──────────────────────────────────────────────────────────────────
# SHOP – Kundenbereich
# ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data     = load_data()
    products = data["products"]

    if not products:
        await update.message.reply_text(
            "🏪 *Willkommen im Shop!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "😔 Aktuell sind keine Produkte verfügbar.\n\n"
            "_Schau später wieder vorbei!_",
            parse_mode="Markdown"
        )
        return

    keyboard = []
    for pid, p in products.items():
        stock = len(p.get("items", []))
        if stock > 0:
            label = f"🛍 {p['name']}  •  {p['price']} LTC  ✅ ({stock}x)"
        else:
            label = f"🛍 {p['name']}  •  {p['price']} LTC  ❌"
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
        await query.edit_message_text("\u274c Produkt nicht gefunden.")
        return

    stock = len(p.get("items", []))
    stock_status = "✅ Verfügbar" if stock > 0 else "❌ Ausverkauft"
    text  = (
        f"🛍 *{p['name']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *Beschreibung:*\n"
        f"_{p['description']}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Preis: *{p['price']} LTC*\n"
        f"📦 Status: {stock_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = []
    if stock > 0 and data.get("ltc_address"):
        keyboard.append([InlineKeyboardButton("🛒 Jetzt kaufen", callback_data=f"buy_{pid}")])
    keyboard.append([InlineKeyboardButton("◀️ Zurück zum Shop", callback_data="back_shop")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid  = query.data.replace("buy_", "")
    data = load_data()
    p    = data["products"].get(pid)

    if not p or not p.get("items"):
        await query.edit_message_text("\u274c Leider ausverkauft!")
        return
    ltc = data.get("ltc_address", "")
    if not ltc:
        await query.edit_message_text("\u274c Keine Zahlungsadresse konfiguriert.")
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
        f"💸 *Sende EXAKT diesen Betrag:*\n"
        f"┌─────────────────────\n"
        f"│ `{p['price']} LTC`\n"
        f"└─────────────────────\n\n"
        f"📬 *An diese Adresse:*\n"
        f"┌─────────────────────\n"
        f"│ `{ltc}`\n"
        f"└─────────────────────\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Blockchain-Check alle {CHECK_INTERVAL} Sek.\n"
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
        "Kein Problem! Du kannst jederzeit neu kaufen.\n\n"
        "👉 Tippe /start für den Shop.",
        parse_mode="Markdown"
    )

async def back_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    data     = load_data()
    keyboard = []
    for pid, p in data["products"].items():
        stock = len(p.get("items", []))
        if stock > 0:
            label = f"🛍 {p['name']}  •  {p['price']} LTC  ✅ ({stock}x)"
        else:
            label = f"🛍 {p['name']}  •  {p['price']} LTC  ❌"
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

# ──────────────────────────────────────────────────────────────────
# ADMIN PANEL
# ──────────────────────────────────────────────────────────────────

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Kein Zugriff!")
        return ConversationHandler.END
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def show_admin_menu(update, context, edit=False):
    data    = load_data()
    pending = load_pending()
    ltc     = data.get("ltc_address") or "\u274c Nicht gesetzt"

    text = (
        f"⚙️ *Admin Panel*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💳 *LTC Adresse:*\n`{ltc}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Produkte:          *{len(data['products'])}*\n"
        f"⏳ Offene Zahlungen:  *{len(pending)}*\n"
        f"✅ Abgeschl. Verkäufe: *{len(data['orders'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Produkt hinzufügen",  callback_data="admin_add_product")],
        [InlineKeyboardButton("📦 Lager auffüllen",     callback_data="admin_restock"),
         InlineKeyboardButton("📋 Produkte anzeigen",   callback_data="admin_list")],
        [InlineKeyboardButton("💳 LTC Adresse setzen",  callback_data="admin_set_ltc"),
         InlineKeyboardButton("📊 Statistiken",         callback_data="admin_stats")],
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
        await query.edit_message_text("\U0001f4b3 Sende die neue LTC Empfangsadresse:")
        return SET_LTC

    elif action == "admin_add_product":
        await query.edit_message_text("\U0001f4e6 *Neues Produkt*\n\nName eingeben:", parse_mode="Markdown")
        return ADD_PRODUCT_NAME

    elif action == "admin_restock":
        data = load_data()
        if not data["products"]:
            await query.edit_message_text("\u274c Keine Produkte vorhanden.\n\n/admin zurueck")
            return ADMIN_MENU
        keyboard = [
            [InlineKeyboardButton(f"{p['name']} (\U0001f4e6 {len(p.get('items',[]))})", callback_data=f"restock_{pid}")]
            for pid, p in data["products"].items()
        ]
        keyboard.append([InlineKeyboardButton("\U0001f519 Zurueck", callback_data="admin_back")])
        await query.edit_message_text(
            "\U0001f4e6 Welches Produkt auffuellen?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return RESTOCK_SELECT

    elif action == "admin_list":
        data = load_data()
        if not data["products"]:
            await query.edit_message_text("❌ Keine Produkte vorhanden.")
            return ADMIN_MENU
        text = "📋 *Produktübersicht*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for pid, p in data["products"].items():
            stock = len(p.get('items', []))
            bar = "🟢" if stock > 5 else ("🟡" if stock > 0 else "🔴")
            text += f"{bar} *{p['name']}*\n   💰 {p['price']} LTC  •  📦 {stock}x\n\n"
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
            f"💰 Gesamteinnahmen: `{revenue:.4f} LTC`\n"
            f"✅ Verkäufe gesamt: *{len(data['orders'])}*\n"
            f"⏳ Offene Zahlungen: *{len(pending)}*\n"
            f"📦 Produkte aktiv: *{len(data['products'])}*\n"
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
            f"\U0001f4e6 *{p['name']}*\n"
            f"Aktueller Bestand: {len(p.get('items', []))}\n\n"
            f"Sende Artikel \u2013 einen pro Zeile\n"
            f"_(z.B. Lizenzkeys, Accounts, Links\u2026)_",
            parse_mode="Markdown"
        )
        return RESTOCK_AMOUNT

async def set_ltc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ltc  = update.message.text.strip()
    data = load_data()
    data["ltc_address"] = ltc
    save_data(data)
    await update.message.reply_text(f"\u2705 LTC Adresse gesetzt:\n`{ltc}`", parse_mode="Markdown")
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"] = {"name": update.message.text.strip(), "items": []}
    await update.message.reply_text("\U0001f4dd Beschreibung des Produkts:")
    return ADD_PRODUCT_DESC

async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text("\U0001f4b0 Preis in LTC (z.B. `0.05`):", parse_mode="Markdown")
    return ADD_PRODUCT_PRICE

async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_product"]["price"] = float(update.message.text.strip())
        await update.message.reply_text("\U0001f4e6 Erste Artikel einfuegen (einen pro Zeile):")
        return ADD_PRODUCT_STOCK
    except ValueError:
        await update.message.reply_text("\u274c Ungultiger Preis. Erneut eingeben:")
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
        f"\u2705 *{np['name']}* erstellt!\n\U0001f4b0 {np['price']} LTC | \U0001f4e6 {len(items)} Artikel",
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
            f"\u2705 *{p['name']}* aufgefuellt!\n"
            f"\u2795 {len(items)} neue Artikel | \U0001f4e6 {len(p['items'])} gesamt",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("\u274c Produkt nicht gefunden.")
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("\u274c Abgebrochen.")
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin)],
        states={
            ADMIN_MENU: [CallbackQueryHandler(admin_callback)],
            SET_LTC:    [MessageHandler(filters.TEXT & ~filters.COMMAND, set_ltc)],
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

    app.job_queue.run_repeating(payment_checker_loop, interval=CHECK_INTERVAL, first=10)

    logger.info("Bot startet...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
