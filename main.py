import json
import sqlite3
import re
import random
import logging
import os
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from web3 import Web3
from eth_account import Account

# Logging setup
logging.basicConfig(
    filename="baccarat_bot.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "0xabc...")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1002588406897"))
CREATOR_ID = int(os.getenv("CREATOR_ID", "0"))
INFURA_URL = os.getenv("INFURA_URL", "https://sepolia.infura.io/v3/06b4b139092f4025b1c4f7e463b69b15")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://baccarat-bot-1e1e.onrender.com/webhook")

# Web3 setup
try:
    w3 = Web3(Web3.HTTPProvider(INFURA_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to Ethereum node")
        raise Exception("Web3 connection failed")
    account = Account.from_key(PRIVATE_KEY) if PRIVATE_KEY else None
    with open("contract_abi.json", "r") as f:
        CONTRACT_ABI = json.load(f)
    contract = w3.eth.contract(address=w3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)
except Exception as e:
    logger.error(f"Web3 initialization failed: {e}")
    raise Exception(f"Web3 initialization failed: {e}")

# FastAPI app
application = FastAPI()

# Telegram application
telegram_app = None

# Database setup
def init_db():
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("PRAGMA table_info(games)")
        columns = [col[1] for col in c.fetchall()]
        expected_columns = [
            "chat_id", "message_id", "creator_id", "bet_amount", "players",
            "player_count", "test_mode", "status", "player_bets", "game_state",
            "card_choices", "game_mode", "target_number"
        ]
        if not all(col in columns for col in expected_columns):
            logger.warning("Games table schema outdated, recreating...")
            c.execute("DROP TABLE IF EXISTS games")
            c.execute(
                """CREATE TABLE games (
                    chat_id INTEGER,
                    message_id INTEGER,
                    creator_id INTEGER,
                    bet_amount TEXT,
                    players TEXT,
                    player_count INTEGER,
                    test_mode INTEGER,
                    status TEXT,
                    player_bets TEXT,
                    game_state TEXT,
                    card_choices TEXT,
                    game_mode TEXT,
                    target_number INTEGER
                )"""
            )
        c.execute(
            """CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                address TEXT,
                private_key TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS pending_bets (
                chat_id INTEGER,
                user_id INTEGER,
                amount TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        c.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            ("support_username", "@arbacenco"),
        )
        conn.commit()
        logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error(f"Database initialization failed: {e}")
    finally:
        conn.close()

init_db()

# Baccarat logic (Simple Mode)
def deal_card():
    return random.randint(1, 13)  # 1=Ace, 2-9, 10/J/Q/K=10

def card_value(card):
    if card == 1:  # Ace
        return 1
    elif card >= 10:  # 10, J, Q, K
        return 0
    return card  # 2-9

def hand_total(cards):
    total = sum(card_value(card) for card in cards) % 10
    return total

def baccarat_third_card(player_cards, banker_cards):
    player_total = hand_total(player_cards)
    banker_total = hand_total(banker_cards)

    if player_total >= 8 or banker_total >= 8:
        return player_cards, banker_cards, False, False

    player_draw = player_total <= 5
    if player_draw:
        player_cards.append(deal_card())

    banker_draw = False
    if len(player_cards) == 3:
        player_third = card_value(player_cards[2])
        if banker_total <= 2:
            banker_draw = True
        elif banker_total == 3 and player_third != 8:
            banker_draw = True
        elif banker_total == 4 and player_third in [2, 3, 4, 5, 6, 7]:
            banker_draw = True
        elif banker_total == 5 and player_third in [4, 5, 6, 7]:
            banker_draw = True
        elif banker_total == 6 and player_third in [6, 7]:
            banker_draw = True
    elif banker_total <= 5:
        banker_draw = True

    if banker_draw:
        banker_cards.append(deal_card())

    return player_cards, banker_cards, player_draw, banker_draw

def determine_winner(player_cards, banker_cards):
    player_total = hand_total(player_cards)
    banker_total = hand_total(banker_cards)
    if player_total > banker_total:
        return "Player"
    elif banker_total > player_total:
        return "Banker"
    else:
        return "Tie"

# PvP logic (Interactive Mode)
def determine_pvp_winner(card_choices, target_number):
    totals = {}
    for user_id, card in card_choices.items():
        total = card_value(card) % 10
        totals[user_id] = total
    if not totals or all(total == 0 for total in totals.values()):
        return [], totals
    distances = {uid: abs(total - target_number) for uid, total in totals.items()}
    min_distance = min(distances.values())
    winners = [(uid, {"total": totals[uid]}) for uid, dist in distances.items() if dist == min_distance]
    return winners, totals

def process_pvp_payouts(chat_id, winners, bet_amount, player_bets):
    if not winners or bet_amount == "0":
        return
    total_pool = float(bet_amount) * len(player_bets)
    fee = total_pool * 0.05
    prize_pool = total_pool - fee
    payout_per_winner = prize_pool / len(winners) if winners else 0
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        for user_id, _ in winners:
            wallet = get_wallet(user_id)
            if wallet:
                address, private_key = wallet
                nonce = w3.eth.get_transaction_count(account.address)
                tx = contract.functions.withdraw(w3.to_wei(payout_per_winner, "ether")).build_transaction({
                    "from": account.address,
                    "nonce": nonce,
                    "gas": 200000,
                    "gasPrice": w3.to_wei("20", "gwei"),
                })
                signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
        if not winners:
            for user_id in player_bets:
                wallet = get_wallet(user_id)
                if wallet:
                    address, private_key = wallet
                    nonce = w3.eth.get_transaction_count(account.address)
                    refund = float(bet_amount) * 0.95
                    tx = contract.functions.withdraw(w3.to_wei(refund, "ether")).build_transaction({
                        "from": account.address,
                        "nonce": nonce,
                        "gas": 200000,
                        "gasPrice": w3.to_wei("20", "gwei"),
                    })
                    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    w3.eth.wait_for_transaction_receipt(tx_hash)
        conn.close()
    except (sqlite3.Error, Web3.exceptions.Web3Exception) as e:
        logger.error(f"Error in process_pvp_payouts: chat_id={chat_id}, error={e}")

def card_to_string(card):
    if card == 1:
        return "A"
    elif card == 11:
        return "J"
    elif card == 12:
        return "Q"
    elif card == 13:
        return "K"
    return str(card)

# Helper functions
def get_game(chat_id):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("SELECT * FROM games WHERE chat_id = ?", (chat_id,))
        game = c.fetchone()
        conn.close()
        logger.debug(f"get_game: chat_id={chat_id}, game={game}")
        return game
    except sqlite3.Error as e:
        logger.error(f"Error in get_game: chat_id={chat_id}, error={e}")
        return None

def update_game(chat_id, **kwargs):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [chat_id]
        c.execute(f"UPDATE games SET {fields} WHERE chat_id = ?", values)
        conn.commit()
        logger.debug(f"update_game: chat_id={chat_id}, kwargs={kwargs}")
    except sqlite3.Error as e:
        logger.error(f"Error in update_game: chat_id={chat_id}, kwargs={kwargs}, error={e}")
    finally:
        conn.close()

def delete_game(chat_id):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("DELETE FROM games WHERE chat_id = ?", (chat_id,))
        c.execute("DELETE FROM pending_bets WHERE chat_id = ?", (chat_id,))
        conn.commit()
        logger.info(f"delete_game: chat_id={chat_id}")
    except sqlite3.Error as e:
        logger.error(f"Error in delete_game: chat_id={chat_id}, error={e}")
    finally:
        conn.close()

def get_wallet(user_id):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("SELECT address, private_key FROM wallets WHERE user_id = ?", (user_id,))
        wallet = c.fetchone()
        conn.close()
        return wallet
    except sqlite3.Error as e:
        logger.error(f"Error in get_wallet: user_id={user_id}, error={e}")
        return None

def create_wallet(user_id):
    try:
        acct = Account.create()
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO wallets (user_id, address, private_key) VALUES (?, ?, ?)",
            (user_id, acct.address, acct.key.hex()),
        )
        conn.commit()
        conn.close()
        return acct.address, acct.key.hex()
    except sqlite3.Error as e:
        logger.error(f"Error in create_wallet: user_id={user_id}, error={e}")
        return None, None

def add_pending_bet(chat_id, user_id, amount):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO pending_bets (chat_id, user_id, amount) VALUES (?, ?, ?)",
            (chat_id, user_id, amount),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error in add_pending_bet: chat_id={chat_id}, user_id={user_id}, error={e}")

def process_pending_bets(chat_id):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("SELECT user_id, amount FROM pending_bets WHERE chat_id = ?", (chat_id,))
        bets = c.fetchall()
        for user_id, amount in bets:
            wallet = get_wallet(user_id)
            if wallet:
                address, private_key = wallet
                user_account = Account.from_key(private_key)
                nonce = w3.eth.get_transaction_count(user_account.address)
                tx = contract.functions.deposit().build_transaction({
                    "from": user_account.address,
                    "value": w3.to_wei(amount, "ether"),
                    "nonce": nonce,
                    "gas": 200000,
                    "gasPrice": w3.to_wei("20", "gwei"),
                })
                signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
        c.execute("DELETE FROM pending_bets WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
    except (sqlite3.Error, Web3.exceptions.Web3Exception) as e:
        logger.error(f"Error in process_pending_bets: chat_id={chat_id}, error={e}")

def get_username(user_id):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result and result[0] else f"User{user_id}"
    except sqlite3.Error as e:
        logger.error(f"Error in get_username: user_id={user_id}, error={e}")
        return f"User{user_id}"

def update_user_info(user_id, username, first_name, last_name):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, last_name),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error in update_user_info: user_id={user_id}, error={e}")

def get_support_username():
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key = ?", ("support_username",))
        result = c.fetchone()
        conn.close()
        return result[0] if result else "@arbacenco"
    except sqlite3.Error as e:
        logger.error(f"Error in get_support_username: error={e}")
        return "@arbacenco"

def set_support_username(username):
    try:
        conn = sqlite3.connect("baccarat.db")
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("support_username", username),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error in set_support_username: username={username}, error={e}")

# Telegram handlers
async def setsupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != CREATOR_ID:
        await update.message.reply_text("❌ Only the bot owner can set the support username!")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /setsupport @username")
        return
    username = context.args[0]
    if not re.match(r"^@[A-Za-z0-9_]{5,32}$", username):
        await update.message.reply_text(
            "❌ Invalid username! Must start with @, be 5-32 characters, and contain only letters, numbers, or underscores."
        )
        return
    set_support_username(username)
    await update.message.reply_text(f"✅ Support username set to {username}!")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user = update.effective_user
    update_user_info(user_id, user.username, user.first_name, user.last_name)
    logger.info(f"/start called: chat_id={chat_id}, user_id={user_id}")

    if chat_id == GROUP_CHAT_ID:
        game = get_game(chat_id)
        if game and game[7] not in ["waiting", "finished"]:
            logger.debug(f"Active game found: chat_id={chat_id}, status={game[7]}")
            await update.message.reply_text(
                "❌ A game is already running! Please wait for the next round. 🎲",
                parse_mode="Markdown"
            )
            return
        if game and game[7] == "waiting":
            logger.debug(f"Waiting game found: chat_id={chat_id}")
            await update.message.reply_text("🎲 A game is already in progress! Join now! 🚀")
            return
        if game:
            logger.info(f"Cleaning up old game: chat_id={chat_id}, status={game[7]}")
            if game[1]:
                try:
                    await context.bot.delete_message(chat_id, game[1])
                except Exception as e:
                    logger.warning(f"Failed to delete old message: message_id={game[1]}, error={e}")
            delete_game(chat_id)

        keyboard = [
            [
                InlineKeyboardButton("🎮 Start Game", callback_data="start_game"),
                InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules"),
            ],
            [
                InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive"),
                InlineKeyboardButton("📊 Stats", callback_data="group_stats"),
            ],
        ]
        if user_id == CREATOR_ID:
            keyboard.append([InlineKeyboardButton("🧪 Test Mode", callback_data="test_mode")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            message = await update.message.reply_text(
                "🎰 **Baccarat Bonanza** 🌟\n"
                "🔥 **Ready to Play?** No game running yet! 🚀\n"
                "💰 **Bet**: 0 ETH\n"
                "👥 **Players**: 0/8\n"
                "🎲 Start a game or check the rules below! 🏆",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            await context.bot.pin_chat_message(chat_id, message.message_id)
            try:
                conn = sqlite3.connect("baccarat.db")
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO games (chat_id, message_id, creator_id, bet_amount, players, player_count, test_mode, status, player_bets, game_state, card_choices, game_mode, target_number) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, message.message_id, user_id, "0", "[]", 0, 0, "waiting", "{}", "{}", "{}", "interactive", 0),
                )
                conn.commit()
                logger.info(f"Game created: chat_id={chat_id}, message_id={message.message_id}")
            except sqlite3.Error as e:
                logger.error(f"Failed to create game: chat_id={chat_id}, error={e}")
                await update.message.reply_text("❌ Failed to start game! Try again or contact support.")
                return
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Error sending start message: chat_id={chat_id}, error={e}")
            await update.message.reply_text("❌ Failed to start game! Try again or contact support.")
    else:
        wallet = get_wallet(user_id)
        support_username = get_support_username()
        text = (
            "🌟 **Welcome to Baccarat Bonanza!** 🎰\n"
            "Get ready for thrilling games and big wins! 🏆\n\n"
        )
        keyboard = [
            [InlineKeyboardButton("💼 View Wallet", callback_data="view_wallet")],
            [InlineKeyboardButton("ℹ️ How to Play", callback_data="how_to_play")],
            [
                InlineKeyboardButton("🌐 Fund Wallet", url="https://sepoliafaucet.com"),
                InlineKeyboardButton("📞 Support", url=f"https://t.me/{support_username[1:]}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if not wallet:
            address, private_key = create_wallet(user_id)
            if address:
                text += (
                    f"🎉 **New Wallet Created!**\n"
                    f"📍 **Address**: `{address}`\n"
                    f"🔑 **Private Key**: `{private_key}`\n"
                    f"⚠️ Save your private key securely!\n\n"
                    f"💧 Fund your wallet with Sepolia ETH to join paid games!"
                )
            else:
                text += "❌ Failed to create wallet! Try again or contact support."
        else:
            text += (
                f"💼 **Your Wallet**:\n"
                f"📍 **Address**: `{wallet[0]}`\n"
                f"💧 Fund it with Sepolia ETH to join paid games!"
            )
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if chat_id != GROUP_CHAT_ID:
        await update.message.reply_text("❌ Use /cancel in the group chat!")
        return
    game = get_game(chat_id)
    if not game:
        await update.message.reply_text("❌ No game to cancel!")
        return
    if user_id != game[2]:
        await update.message.reply_text("❌ Only the game creator can cancel!")
        return
    if game[1]:
        try:
            await context.bot.delete_message(chat_id, game[1])
        except Exception as e:
            logger.warning(f"Failed to delete message: message_id={game[1]}, error={e}")
    delete_game(chat_id)
    await update.message.reply_text("🛑 Game cancelled! Ready for a new round? 🎲")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != CREATOR_ID:
        await update.message.reply_text("❌ Only the bot owner can reset games!")
        return
    game = get_game(chat_id)
    if game and game[1]:
        try:
            await context.bot.delete_message(chat_id, game[1])
        except Exception as e:
            logger.warning(f"Failed to delete message: message_id={game[1]}, error={e}")
    delete_game(chat_id)
    await update.message.reply_text("🔄 Game state reset! Start a new game with /start.")

async def who_made_the_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎨 This bot was crafted by @nakatroll! 🚀")

async def timeout_card_selection(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if not game or game[7] != "card_selection":
        logger.debug(f"Timeout check: No game or not in card_selection for chat_id={chat_id}")
        return
    logger.warning(f"Card selection timeout for chat_id={chat_id}")
    if game[1]:
        try:
            await context.bot.delete_message(chat_id, game[1])
        except Exception as e:
            logger.warning(f"Failed to delete message on timeout: message_id={game[1]}, error={e}")
    delete_game(chat_id)
    await context.bot.send_message(
        chat_id,
        "⏰ Card selection timed out! Game cancelled. Start a new game with /start.",
        parse_mode="Markdown"
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user = query.from_user
    update_user_info(user_id, user.username, user.first_name, user.last_name)
    data = query.data
    logger.debug(f"Button pressed: data={data}, chat_id={chat_id}, user_id={user_id}")

    if data in ["view_wallet", "how_to_play", "tutorial_interactive"]:
        if data == "view_wallet":
            wallet = get_wallet(user_id)
            if wallet:
                await query.message.reply_text(
                    f"💼 **Your Wallet**:\n"
                    f"📍 **Address**: `{wallet[0]}`\n"
                    f"💧 Fund it with Sepolia ETH to join paid games!",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text("❌ No wallet found! Use /start to create one.")
        elif data == "how_to_play":
            await query.message.reply_text(
                "ℹ️ **How to Play Baccarat Bonanza** 🎰\n"
                "1. Use /start in the group to begin.\n"
                "2. Choose '🎮 Start Game', pick '🎲 Simple' or '🃏 Interactive' mode.\n"
                "3. Select '💰 Set Bet' (Sepolia ETH) or '🎉 Free Play' (no ETH).\n"
                "4. Join with '➕ Join Game'.\n"
                "5. **Simple Mode**: Up to 8 players bet on Player, Banker, or Tie; cards are dealt randomly. Payouts: Player (1:1), Banker (1:1, 5% commission), Tie (8:1).\n"
                "6. **Interactive Mode**: Up to 4 players pick a card (A–K). A secret target number (1–9) is revealed after selections. The player(s) closest to the target win(s) the prize pool (minus 5% fee).\n"
                "7. Wait for 2 (test mode) or 4/8 players to join.\n"
                "8. Check private chat for bet/card prompts; winners are tagged in the group! 🏆\n"
                "💡 Free Play is ETH-free. For betting, fund your wallet with Sepolia ETH.\n"
                "📖 Use the 'Tutorial' button for more.",
                parse_mode="Markdown",
            )
        elif data == "tutorial_interactive":
            await query.message.reply_text(
                "📖 **Interactive Mode Tutorial** 🃏\n"
                "Welcome to Interactive Mode in Baccarat Bonanza! Here's how to play:\n\n"
                "1. **Start the Game**: Use /start in the group, then click '🎮 Start Game' and select '🃏 Interactive'.\n"
                "2. **Set Bet**: Choose '💰 Set Bet' (e.g., 0.01 ETH) or '🎉 Free Play'. The creator sets the bet amount.\n"
                "3. **Join**: Click '➕ Join Game'. Up to 4 players can join (2 in test mode).\n"
                "4. **Confirm Bet**: In private chat, confirm your bet (no choice needed, just the amount).\n"
                "5. **Pick a Card**: Once all players join, you'll get a private message to pick a card (A, 2–9, 10, J, Q, K). Card values: A=1, 2–9=face value, 10/J/Q/K=0.\n"
                "6. **Secret Target**: A target number (1–9) is set but kept secret until all players pick their cards.\n"
                "7. **Results**: After everyone picks, the bot reveals the target number and each player's card. The player(s) whose card total (mod 10) is closest to the target wins the prize pool (minus 5% fee).\n"
                "8. **Payouts**: Winners get ETH (if betting) and are tagged in the group. Ties split the prize.\n"
                "9. **Next Round**: Use /start to play again!\n\n"
                "💡 **Tips**: Pick strategically, but it's a game of chance! Free Play is great for practice. Check 'ℹ️ Rules' for more.",
                parse_mode="Markdown",
            )
        return

    if data.startswith("bet_"):
        try:
            parts = data.split("_")
            if len(parts) != 3:
                raise ValueError("Invalid callback data format")
            _, bet_type, game_chat_id = parts
            game_chat_id = int(game_chat_id)
            logger.debug(f"Bet callback: bet_type={bet_type}, game_chat_id={game_chat_id}, user_id={user_id}")
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing bet callback: data={data}, error={e}")
            await query.message.reply_text("❌ Invalid bet action! Please try joining again.")
            return

        game = get_game(game_chat_id)
        if not game:
            logger.error(f"No game found for chat_id={game_chat_id}")
            await query.message.reply_text("❌ No active game! Start one with /start in the group.")
            return
        if game[7] != "betting":
            logger.warning(f"Game not in betting state: status={game[7]}, chat_id={game_chat_id}")
            await query.message.reply_text("❌ Betting phase is over or game is not ready!")
            return
        player_bets = json.loads(game[8]) if game[8] else {}
        if str(user_id) in player_bets:
            logger.warning(f"User already bet: user_id={user_id}, chat_id={game_chat_id}")
            await query.message.reply_text("❌ You've already placed a bet!")
            return

        player_bets[str(user_id)] = {"choice": bet_type.capitalize() if bet_type != "none" else "None", "amount": game[3]}
        update_game(game_chat_id, player_bets=json.dumps(player_bets))
        logger.info(f"Bet placed: user_id={user_id}, bet_type={bet_type}, amount={game[3]}, chat_id={game_chat_id}")
        await query.message.reply_text(
            f"✅ Bet placed{' on **' + bet_type.capitalize() + '**' if bet_type != 'none' else ''} for {game[3]} ETH! Please wait for {'card selection' if game[11] == 'interactive' else 'cards to be dealt'}...",
            parse_mode="Markdown"
        )

        players = json.loads(game[4])
        if len(player_bets) == len(players):
            logger.info(f"All players bet: moving to {'card selection' if game[11] == 'interactive' else 'playing'}, chat_id={game_chat_id}")
            if game[11] == "interactive":
                target_number = random.randint(1, 9)
                update_game(game_chat_id, status="card_selection", target_number=target_number)
                await context.bot.send_message(
                    game_chat_id,
                    f"🎮 **Bets Placed!** {len(players)} players ready! 🃏\n"
                    f"🔥 Now picking cards (target number is secret until all choose)!",
                    parse_mode="Markdown",
                )
                card_options = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
                for player_id in players:
                    keyboard = [
                        [InlineKeyboardButton(card, callback_data=f"card_select_{card}_{game_chat_id}") for card in card_options[:4]],
                        [InlineKeyboardButton(card, callback_data=f"card_select_{card}_{game_chat_id}") for card in card_options[4:8]],
                        [InlineKeyboardButton(card, callback_data=f"card_select_{card}_{game_chat_id}") for card in card_options[8:]],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        player_id,
                        f"🎰 **Baccarat Bonanza** 🎲\n"
                        f"🔥 Choose your card (target number is secret):\n"
                        f"💰 Your bet: {game[3]} ETH\n"
                        f"🃏 Pick one card to get closest to the target!",
                        parse_mode="Markdown",
                        reply_markup=reply_markup,
                    )
                context.job_queue.run_once(timeout_card_selection, 30, data=game_chat_id, name=f"timeout_{game_chat_id}")
            else:
                update_game(game_chat_id, status="playing")
                player_cards = [deal_card(), deal_card()]
                banker_cards = [deal_card(), deal_card()]
                game_state = {"player_cards": player_cards, "banker_cards": banker_cards}
                player_cards, banker_cards, player_draw, banker_draw = baccarat_third_card(player_cards, banker_cards)
                game_state["player_cards"] = player_cards
                game_state["banker_cards"] = banker_cards
                update_game(game_chat_id, game_state=json.dumps(game_state))
                await proceed_to_results(context, game_chat_id, game, players, player_bets, player_cards, banker_cards)
        return

    if data.startswith("card_select_"):
        logger.debug(f"Raw card callback data: {data}")
        try:
            logger.debug(f"Attempting to split callback data: {data}")
            parts = data.split("_")
            logger.debug(f"Split result: parts={parts}")
            if len(parts) != 4 or parts[0] != "card" or parts[1] != "select":
                raise ValueError(f"Invalid callback data format: parts={parts}")
            card = parts[2]
            game_chat_id_str = parts[3]
            logger.debug(f"Extracted: card={card}, game_chat_id_str={game_chat_id_str}")
            if not game_chat_id_str.lstrip('-').isdigit():
                raise ValueError(f"Invalid game_chat_id: {game_chat_id_str}")
            game_chat_id = int(game_chat_id_str)
            card = card.upper()
            logger.debug(f"Parsed card callback: card={card}, game_chat_id={game_chat_id}, user_id={user_id}")
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing card callback: data={data}, parts={parts if 'parts' in locals() else 'not split'}, error={e}")
            await query.message.reply_text(f"❌ Invalid card action! Please try again or contact {get_support_username()}.")
            return

        game = get_game(game_chat_id)
        if not game:
            logger.error(f"No game found for chat_id={game_chat_id}")
            await query.message.reply_text("❌ No active game! Start one with /start in the group.")
            return
        if game[7] != "card_selection":
            logger.warning(f"Game not in card_selection state: status={game[7]}, chat_id={game_chat_id}, game={game}")
            await query.message.reply_text("❌ Card selection phase is over or game is not ready! Try starting a new game.")
            if game[1]:
                try:
                    await context.bot.delete_message(game_chat_id, game[1])
                except Exception as e:
                    logger.warning(f"Failed to delete message on reset: message_id={game[1]}, error={e}")
            delete_game(game_chat_id)
            return
        card_choices = json.loads(game[10]) if game[10] else {}
        if str(user_id) in card_choices:
            logger.warning(f"User already chose card: user_id={user_id}, chat_id={game_chat_id}")
            await query.message.reply_text("❌ You've already chosen a card!")
            return

        valid_cards = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        if card not in valid_cards:
            logger.error(f"Invalid card selected: card={card}, valid_cards={valid_cards}, user_id={user_id}, chat_id={game_chat_id}")
            await query.message.reply_text(f"❌ Invalid card '{card}'! Choose from: A, 2, 3, 4, 5, 6, 7, 8, 9, 10, J, Q, K.")
            return

        card_map = {"A": 1, "J": 11, "Q": 12, "K": 13}
        card_value = int(card_map.get(card, card))
        card_choices[str(user_id)] = card_value
        update_game(game_chat_id, card_choices=json.dumps(card_choices))
        logger.info(f"Card chosen: user_id={user_id}, card={card}, value={card_value}, chat_id={game_chat_id}")

        await query.message.reply_text(
            f"✅ Card chosen: **{card}**! Waiting for other players...",
            parse_mode="Markdown"
        )

        players = json.loads(game[4])
        player_bets = json.loads(game[8]) if game[8] else {}
        if all(str(p) in card_choices for p in players):
            logger.info(f"All players chose cards, chat_id={game_chat_id}")
            await proceed_to_results(context, game_chat_id, game, players, player_bets, None, None)
        return

    game = get_game(chat_id)
    if not game:
        logger.error(f"No game found for group chat_id={chat_id}")
        await query.message.reply_text("❌ No active game! Start one with /start.")
        return
    players = json.loads(game[4])
    test_mode = game[6]
    max_players = 2 if test_mode else (4 if game[11] == "interactive" else 8)

    if data == "start_game":
        if game[7] != "waiting":
            logger.warning(f"Game not in waiting state for start_game: status={game[7]}, chat_id={chat_id}")
            await query.message.reply_text("🎲 Game already started! Join now! 🚀")
            return
        keyboard = [
            [
                InlineKeyboardButton("🎲 Simple", callback_data="game_mode_simple"),
                InlineKeyboardButton("🃏 Interactive", callback_data="game_mode_interactive"),
            ],
            [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "🎰 **Choose Your Game Mode!** 🚀\n"
            "🎲 **Simple**: Cards dealt randomly, bet on Player/Banker/Tie (up to 8 players).\n"
            "🃏 **Interactive**: Pick a card to match a secret target number (up to 4 players)!\n"
            "📖 Check the tutorial for Interactive Mode!",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    elif data in ["game_mode_simple", "game_mode_interactive"]:
        game_mode = "simple" if data == "game_mode_simple" else "interactive"
        logger.debug(f"Game mode selected: {game_mode}, chat_id={chat_id}, user_id={user_id}")
        update_game(chat_id, game_mode=game_mode)
        keyboard = [
            [
                InlineKeyboardButton("💰 Set Bet", callback_data="set_bet"),
                InlineKeyboardButton("🎉 Free Play", callback_data="free_play"),
            ],
            [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            f"🎰 **{game_mode.capitalize()} Mode Selected!** 🚀\n"
            "💰 **Set Bet**: Play with real ETH!\n"
            "🎉 **Free Play**: Just for fun, no wallet needed!\n"
            "📖 Check the tutorial for Interactive Mode!",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    elif data == "set_bet":
        logger.debug(f"Set bet initiated: chat_id={chat_id}, user_id={user_id}")
        if game[7] != "waiting":
            await query.message.reply_text("🎲 Game already started! Join now! 🚀")
            return
        await query.message.reply_text("💰 Enter bet amount (ETH, e.g., 0.01):")
        update_game(chat_id, status="setting_bet")

    elif data == "free_play":
        if game[7] != "waiting":
            await query.message.reply_text("🎲 Game already started! Join now! 🚀")
            return
        update_game(chat_id, bet_amount="0", status="waiting")
        keyboard = [
            [InlineKeyboardButton("➕ Join Game", callback_data="join")],
            [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
            [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        old_message_id = game[1]
        new_message = await context.bot.send_message(
            chat_id,
            f"🎰 **Baccarat Bonanza** 🌟\n"
            f"🔥 **Free Play Mode!** {'🧪 Test Mode! ' if test_mode else ''}🚀\n"
            f"💰 **Bet**: 0 ETH\n"
            f"👥 **Players**: 0/{max_players}\n"
            f"🎲 Join now for fun! 🏆",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        await context.bot.pin_chat_message(chat_id, new_message.message_id)
        if old_message_id:
            try:
                await context.bot.delete_message(chat_id, old_message_id)
            except Exception as e:
                logger.warning(f"Failed to delete message: message_id={old_message_id}, error={e}")
        update_game(chat_id, message_id=new_message.message_id)

    elif data == "tournament":
        await query.message.reply_text("🏆 Tournament mode not implemented yet! Stay tuned! 🎉")

    elif data == "test_mode" and user_id == CREATOR_ID:
        update_game(chat_id, test_mode=1)
        keyboard = [
            [
                InlineKeyboardButton("🎲 Simple", callback_data="game_mode_simple"),
                InlineKeyboardButton("🃏 Interactive", callback_data="game_mode_interactive"),
            ],
            [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "🧪 **Test Mode Enabled!** Choose your game mode: 🚀",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        update_game(chat_id, status="waiting")

    elif data == "join":
        if user_id in players:
            await query.message.reply_text("❌ You're already in the game!")
            return
        if game[7] != "waiting":
            await query.message.reply_text("❌ Game already started!")
            return
        if len(players) >= max_players:
            await query.message.reply_text(f"❌ Game is full! Wait for the next round. 🎲")
            return
        wallet = get_wallet(user_id)
        if not wallet and game[3] != "0":
            await query.message.reply_text("❌ Create a wallet with /start in private chat!")
            return
        players.append(user_id)
        update_game(chat_id, players=json.dumps(players), player_count=len(players))
        old_message_id = game[1]
        keyboard = [
            [InlineKeyboardButton("➕ Join Game", callback_data="join")],
            [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
            [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        new_message = await context.bot.send_message(
            chat_id,
            f"🎰 **Baccarat Bonanza** 🌟\n"
            f"🔥 **{'Free Play' if game[3] == '0' else 'Betting'} Mode!** {'🧪 Test Mode! ' if test_mode else ''}🚀\n"
            f"💰 **Bet**: {game[3]} ETH\n"
            f"👥 **Players**: {len(players)}/{max_players}\n"
            f"🎲 Join now to win big! 🏆",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        await context.bot.pin_chat_message(chat_id, new_message.message_id)
        if old_message_id:
            try:
                await context.bot.delete_message(chat_id, old_message_id)
            except Exception as e:
                logger.warning(f"Failed to delete message: message_id={old_message_id}, error={e}")
        update_game(chat_id, message_id=new_message.message_id)
        if game[11] == "simple":
            keyboard = [
                [
                    InlineKeyboardButton("👤 Player", callback_data=f"bet_player_{chat_id}"),
                    InlineKeyboardButton("🏦 Banker", callback_data=f"bet_banker_{chat_id}"),
                ],
                [InlineKeyboardButton("🤝 Tie", callback_data=f"bet_tie_{chat_id}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                user_id,
                f"🎰 **Baccarat Bonanza** 🎲\n"
                f"🔥 You're in the game! Choose your bet:\n"
                f"💰 **Amount**: {game[3]} ETH\n"
                f"👤 **Player**: 1:1 payout\n"
                f"🏦 **Banker**: 1:1 (5% commission)\n"
                f"🤝 **Tie**: 8:1 payout",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        else:
            keyboard = [
                [InlineKeyboardButton("✅ Confirm Bet", callback_data=f"bet_none_{chat_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                user_id,
                f"🎰 **Baccarat Bonanza** 🎲\n"
                f"🔥 You're in the game! Confirm your bet:\n"
                f"💰 **Amount**: {game[3]} ETH\n"
                f"🎯 You'll pick a card after joining!",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        if len(players) == max_players:
            update_game(chat_id, status="betting")
            await context.bot.send_message(
                chat_id,
                f"🎮 **Game Ready!** {len(players)} players joined! Waiting for bets... 🃏",
                parse_mode="Markdown",
            )

    elif data == "group_rules":
        await query.message.reply_text(
            "ℹ️ **Baccarat Bonanza Rules** 🎰\n"
            "1. Use /start in the group to begin a game.\n"
            "2. Select '🎮 Start Game', then choose '🎲 Simple' or '🃏 Interactive' mode.\n"
            "3. Pick '💰 Set Bet' (Sepolia ETH) or '🎉 Free Play' (no ETH).\n"
            "4. Join with '➕ Join Game'.\n"
            "5. **Simple Mode**: Up to 8 players bet on Player, Banker, or Tie; cards are dealt randomly. Payouts: Player (1:1), Banker (1:1, 5% commission), Tie (8:1).\n"
            "6. **Interactive Mode**: Up to 4 players pick a card (A–K). A secret target number (1–9) is revealed after all players choose. The player(s) closest to the target (card total mod 10) win(s) the prize pool (minus 5% fee). Card values: A=1, 2–9=face value, 10/J/Q/K=0.\n"
            "7. Wait for 2 (test mode) or 4 (interactive) or 8 (simple) players to join.\n"
            "8. Check private chat for bet/card prompts; winners are tagged in the group! 🏆\n"
            "💡 Free Play is ETH-free. For betting, fund your wallet with Sepolia ETH.\n"
            "📖 Use the 'Tutorial' button for more!",
            parse_mode="Markdown",
        )

    elif data == "group_stats":
        await query.message.reply_text(
            "📊 **Game Stats** 🎰\n"
            "🔥 Coming soon! Track wins, bets, and more! 🏆",
            parse_mode="Markdown",
        )

async def proceed_to_results(context, game_chat_id, game, players, player_bets, player_cards, banker_cards):
    logger.info(f"Proceeding to results: chat_id={game_chat_id}, game_mode={game[11]}")
    update_game(game_chat_id, status="playing")

    if game[11] == "simple":
        player_str = ", ".join(card_to_string(c) for c in player_cards)
        banker_str = ", ".join(card_to_string(c) for c in banker_cards)
        await context.bot.send_message(
            game_chat_id,
            f"🎰 **Baccarat Bonanza** 🌟\n"
            f"🔥 **Final Hands!** {'🧪 Test Mode! ' if game[6] else ''}🚀\n"
            f"👤 **Player Hand**: {player_str} (Total: {hand_total(player_cards)})\n"
            f"🏦 **Banker Hand**: {banker_str} (Total: {hand_total(banker_cards)})\n"
            f"🎲 Calculating results... 🏆",
            parse_mode="Markdown",
        )
        for player_id in players:
            await context.bot.send_message(
                player_id,
                f"🎰 **Game Update** 🎲\n"
                f"👤 **Player Hand**: {player_str} (Total: {hand_total(player_cards)})\n"
                f"🏦 **Banker Hand**: {banker_str} (Total: {hand_total(banker_cards)})\n"
                f"💰 Your bet: **{player_bets.get(str(player_id), {}).get('choice', 'None')}** ({game[3]} ETH)",
                parse_mode="Markdown",
            )
        result = determine_winner(player_cards, banker_cards)
        winners = [(uid, bet) for uid, bet in player_bets.items() if bet["choice"] == result]
        if game[3] != "0":
            process_pending_bets(game_chat_id)
            for user_id, bet in winners:
                wallet = get_wallet(user_id)
                if wallet:
                    address, private_key = wallet
                    nonce = w3.eth.get_transaction_count(account.address)
                    payout = float(bet["amount"]) * 1.95 if bet["choice"] == "Banker" else float(bet["amount"]) * 2
                    if bet["choice"] == "Tie":
                        payout = float(bet["amount"]) * 9
                    tx = contract.functions.withdraw(w3.to_wei(payout, "ether")).build_transaction({
                        "from": account.address,
                        "nonce": nonce,
                        "gas": 200000,
                        "gasPrice": w3.to_wei("20", "gwei"),
                    })
                    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    w3.eth.wait_for_transaction_receipt(tx_hash)
    else:
        card_choices = json.loads(game[10]) if game[10] else {}
        target_number = game[12]
        winners, totals = determine_pvp_winner(card_choices, target_number)
        result_text = "\n".join(
            f"👤 @{get_username(int(uid))} picked {card_to_string(card_choices[uid])} (Total: {totals[uid]})"
            for uid in card_choices
        )
        await context.bot.send_message(
            game_chat_id,
            f"🎰 **Baccarat Bonanza** 🌟\n"
            f"🔥 **Results!** {'🧪 Test Mode! ' if game[6] else ''}🚀\n"
            f"🎯 **Target Number**: {target_number}\n"
            f"{result_text}\n"
            f"🎲 Calculating winners... 🏆",
            parse_mode="Markdown",
        )
        for player_id in players:
            await context.bot.send_message(
                player_id,
                f"🎰 **Game Update** 🎲\n"
                f"🎯 **Target Number**: {target_number}\n"
                f"👤 Your card: {card_to_string(card_choices.get(str(player_id), 0))} (Total: {totals.get(str(player_id), 0)})\n"
                f"💰 Your bet: {game[3]} ETH",
                parse_mode="Markdown",
            )
        if game[3] != "0":
            process_pending_bets(game_chat_id)
            process_pvp_payouts(game_chat_id, winners, game[3], player_bets)
        result = "No winners" if not winners else "Winners determined"

    winner_tags = ", ".join(f"@{get_username(int(uid))}" for uid, _ in winners) if winners else "No winners"
    prize_text = f"🏆 **Prize**: {(float(game[3]) * len(player_bets) * 0.95 / len(winners) if winners else 0):.4f} ETH each" if game[3] != "0" and winners else ""
    await context.bot.send_message(
        game_chat_id,
        f"🎰 **Game Over!** 🌟\n"
        f"🔥 **Result**: {result}! 🏆\n"
        f"🎉 **Winners**: {winner_tags}\n"
        f"{prize_text}\n"
        f"🚀 Ready for another round? Use /start!",
        parse_mode="Markdown",
        )
    if game[1]:
        try:
            await context.bot.delete_message(game_chat_id, game[1])
        except Exception as e:
            logger.warning(f"Failed to delete message: message_id={game[1]}, error={e}")
    delete_game(game_chat_id)
    keyboard = [
        [
            InlineKeyboardButton("🎮 Start Game", callback_data="start_game"),
            InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules"),
        ],
        [
            InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive"),
            InlineKeyboardButton("📊 Stats", callback_data="group_stats"),
        ],
    ]
    if CREATOR_ID in players:
        keyboard.append([InlineKeyboardButton("🧪 Test Mode", callback_data="test_mode")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        new_message = await context.bot.send_message(
            game_chat_id,
            "🎰 **Baccarat Bonanza** 🌟\n"
            "🔥 **Ready to Play?** No game running yet! 🚀\n"
            "💰 **Bet**: 0 ETH\n"
            "👥 **Players**: 0/{4 if game[11] == 'interactive' else 8}\n"
            "🎲 Start a game or check the rules below! 🏆",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        await context.bot.pin_chat_message(game_chat_id, new_message.message_id)
        try:
            conn = sqlite3.connect("baccarat.db")
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO games (chat_id, message_id, creator_id, bet_amount, players, player_count, test_mode, status, player_bets, game_state, card_choices, game_mode, target_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game_chat_id, new_message.message_id, CREATOR_ID, "0", "[]", 0, 0, "waiting", "{}", "{}", "{}", "interactive", 0),
            )
            conn.commit()
            logger.info(f"New game created after results: chat_id={game_chat_id}, message_id={new_message.message_id}")
        except sqlite3.Error as e:
            logger.error(f"Failed to create new game after results: chat_id={game_chat_id}, error={e}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error creating new game message: chat_id={game_chat_id}, error={e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user = update.effective_user
    update_user_info(user_id, user.username, user.first_name, user.last_name)
    text = update.message.text
    game = get_game(chat_id)
    if not game:
        logger.debug(f"No game found for message handling: chat_id={chat_id}")
        return
    if game[7] == "setting_bet" and user_id == game[2]:
        try:
            bet = str(float(text))
            logger.info(f"Bet amount set: {bet} ETH, chat_id={chat_id}, user_id={user_id}")
            update_game(chat_id, bet_amount=bet, status="waiting")
            max_players = 2 if game[6] else (4 if game[11] == "interactive" else 8)
            keyboard = [
                [InlineKeyboardButton("➕ Join Game", callback_data="join")],
                [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
                [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            old_message_id = game[1]
            new_message = await context.bot.send_message(
                chat_id,
                f"🎰 **Baccarat Bonanza** 🌟\n"
                f"🔥 **Betting Mode!** {'🧪 Test Mode! ' if game[6] else ''}🚀\n"
                f"💰 **Bet**: {bet} ETH\n"
                f"👥 **Players**: {game[5]}/{max_players}\n"
                f"🎲 Join now to win big! 🏆",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            await context.bot.pin_chat_message(chat_id, new_message.message_id)
            if old_message_id:
                try:
                    await context.bot.delete_message(chat_id, old_message_id)
                except Exception as e:
                    logger.warning(f"Failed to delete message: message_id={old_message_id}, error={e}")
            update_game(chat_id, message_id=new_message.message_id)
        except ValueError:
            await update.message.reply_text("❌ Invalid bet amount! Enter a number (e.g., 0.01).")

# FastAPI routes
@application.get("/")
async def root():
    """Root endpoint for Render health checks."""
    return {"message": "Baccarat Bot API is running!", "status": "connected to Ethereum"}

@application.post("/")
async def root_post():
    """Handle misdirected POST requests to root."""
    logger.warning("Received POST request to root instead of /webhook")
    return {"error": "Please use /webhook for Telegram updates"}

@application.post("/webhook")
async def webhook(request: Request):
    """Handle Telegram webhook updates."""
    global telegram_app
    if telegram_app is None:
        logger.error("Telegram application not initialized")
        raise HTTPException(status_code=500, detail="Telegram application not initialized")
    update = await request.json()
    update_obj = Update.de_json(update, telegram_app.bot)
    await telegram_app.process_update(update_obj)
    return {"status": "ok"}

# Initialize Telegram application and set webhook
async def init_telegram_app():
    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start, filters=filters.ChatType.GROUPS))
    telegram_app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(CommandHandler("reset", reset))
    telegram_app.add_handler(CommandHandler("setsupport", setsupport))
    telegram_app.add_handler(CommandHandler("whomadethebot", who_made_the_bot))
    telegram_app.add_handler(CallbackQueryHandler(button))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        try:
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
            logger.info(f"Webhook set to {WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.error("WEBHOOK_URL not set, cannot configure webhook")

@application.on_event("startup")
async def startup_event():
    await init_telegram_app()
