     import json
     import os
     import re
     import random
     import logging
     import asyncio
     from dotenv import load_dotenv
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
     from firebase_admin import credentials, firestore, initialize_app
     from fastapi import FastAPI, Request
     import uvicorn
     import telegram

     # Load environment variables from .env file
     load_dotenv()

     # Logging setup
     logging.basicConfig(
         level=logging.INFO,
         format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
     )
     logger = logging.getLogger(__name__)

     # Configuration from Environment Variables
     TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
     CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS")
     INFURA_URL = os.environ.get("INFURA_URL")
     PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
     GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", 0))
     CREATOR_ID = int(os.environ.get("CREATOR_ID", 0))
     WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
     PORT = int(os.environ.get("PORT", 10000))  # Default to 10000 if PORT not set
     FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")

     # Validate essential environment variables
     if not all([TELEGRAM_TOKEN, CONTRACT_ADDRESS, INFURA_URL, PRIVATE_KEY, GROUP_CHAT_ID, CREATOR_ID, WEBHOOK_URL, FIREBASE_CREDENTIALS_JSON]):
         logger.critical("Missing essential environment variables. Check TELEGRAM_TOKEN, CONTRACT_ADDRESS, INFURA_URL, PRIVATE_KEY, GROUP_CHAT_ID, CREATOR_ID, WEBHOOK_URL, FIREBASE_CREDENTIALS_JSON.")
         exit(1)

     # Web3 setup
     try:
         w3 = Web3(Web3.HTTPProvider(INFURA_URL))
         if not w3.is_connected():
             raise Exception("Failed to connect to Web3 provider.")
         account = Account.from_key(PRIVATE_KEY)
         
         # Load contract ABI from file
         try:
             with open("contract_abi.json") as f:
                 CONTRACT_ABI = json.load(f)
         except FileNotFoundError:
             logger.critical("contract_abi.json not found in project root.")
             exit(1)
         except json.JSONDecodeError as e:
             logger.critical(f"Invalid contract_abi.json format: {e}")
             exit(1)
         
         contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=CONTRACT_ABI)
         logger.info("Web3 and contract initialized successfully.")
     except Exception as e:
         logger.critical(f"Web3 or Contract initialization failed: {e}")
         exit(1)

     # Firebase setup
     try:
         cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
         initialize_app(cred)
         db = firestore.client()
         logger.info("Firebase initialized successfully.")
     except json.JSONDecodeError as e:
         logger.critical(f"Invalid FIREBASE_CREDENTIALS_JSON format: {e}")
         exit(1)
     except Exception as e:
         logger.critical(f"Firebase initialization failed: {e}")
         exit(1)

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

     async def process_pvp_payouts(context, chat_id, winners, bet_amount, player_bets):
         if not winners or bet_amount == "0":
             return
         total_pool = float(bet_amount) * len(player_bets)
         fee = total_pool * 0.05
         prize_pool = total_pool - fee
         payout_per_winner = prize_pool / len(winners) if winners else 0
         try:
             for user_id, _ in winners:
                 wallet = await get_wallet(int(user_id))
                 if not wallet:
                     logger.error(f"Wallet not found for user {user_id} during payout.")
                     await context.bot.send_message(user_id, "❌ Payout failed: Wallet not found. Contact support.")
                     continue
                 address, private_key = wallet
                 user_account = Account.from_key(private_key)
                 nonce = w3.eth.get_transaction_count(account.address)
                 
                 contract_balance = contract.functions.balances(account.address).call()
                 if contract_balance < w3.to_wei(payout_per_winner, "ether"):
                     logger.error(f"Contract balance too low for payout: {contract_balance} < {w3.to_wei(payout_per_winner, 'ether')}")
                     await context.bot.send_message(user_id, "❌ Payout failed: Insufficient contract funds. Contact support.")
                     continue

                 tx = contract.functions.withdraw(w3.to_wei(payout_per_winner, "ether")).build_transaction({
                     "from": account.address,
                     "nonce": nonce,
                     "gas": 200000,
                     "gasPrice": w3.to_wei("20", "gwei"),
                 })
                 signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                 try:
                     tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                     receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                     if receipt.status == 0:
                         logger.error(f"Payout transaction failed for user {user_id}: Tx {tx_hash.hex()}")
                         await context.bot.send_message(user_id, "❌ Payout transaction failed. Contact support.")
                         continue
                     logger.info(f"Processed payout for user {user_id}: {payout_per_winner} ETH. Tx: {tx_hash.hex()}")
                     await context.bot.send_message(user_id, f"🎉 Payout of {payout_per_winner:.4f} ETH processed! Tx: {tx_hash.hex()}")
                 except Exception as tx_e:
                     logger.error(f"Payout transaction failed for user {user_id}: {tx_e}")
                     await context.bot.send_message(user_id, f"❌ Payout failed: {str(tx_e)}. Contact support.")
             
             if not winners:
                 for user_id_str in player_bets:
                     user_id = int(user_id_str)
                     wallet = await get_wallet(user_id)
                     if not wallet:
                         logger.error(f"Wallet not found for user {user_id} during refund.")
                         await context.bot.send_message(user_id, "❌ Refund failed: Wallet not found. Contact support.")
                         continue
                     address, private_key = wallet
                     user_account = Account.from_key(private_key)
                     nonce = w3.eth.get_transaction_count(account.address)
                     refund_amount = float(bet_amount) * 0.95
                     
                     contract_balance = contract.functions.balances(account.address).call()
                     if contract_balance < w3.to_wei(refund_amount, "ether"):
                         logger.error(f"Contract balance too low for refund: {contract_balance} < {w3.to_wei(refund_amount, 'ether')}")
                         await context.bot.send_message(user_id, "❌ Refund failed: Insufficient contract funds. Contact support.")
                         continue

                     tx = contract.functions.withdraw(w3.to_wei(refund_amount, "ether")).build_transaction({
                         "from": account.address,
                         "nonce": nonce,
                         "gas": 200000,
                         "gasPrice": w3.to_wei("20", "gwei"),
                     })
                     signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                     try:
                         tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                         receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                         if receipt.status == 0:
                             logger.error(f"Refund transaction failed for user {user_id}: Tx {tx_hash.hex()}")
                             await context.bot.send_message(user_id, "❌ Refund transaction failed. Contact support.")
                             continue
                         logger.info(f"Processed refund for user {user_id}: {refund_amount} ETH. Tx: {tx_hash.hex()}")
                         await context.bot.send_message(user_id, f"✅ Refund of {refund_amount:.4f} ETH processed! Tx: {tx_hash.hex()}")
                     except Exception as tx_e:
                         logger.error(f"Refund transaction failed for user {user_id}: {tx_e}")
                         await context.bot.send_message(user_id, f"❌ Refund failed: {str(tx_e)}. Contact support.")
         except Exception as e:
             logger.error(f"Error in process_pvp_payouts: chat_id={chat_id}, error={e}")
             await context.bot.send_message(chat_id, "❌ Error processing payouts. Contact support.")

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

     # Firestore Helper functions
     async def get_game(chat_id):
         try:
             doc_ref = db.collection("games").document(str(chat_id))
             doc = doc_ref.get()
             if doc.exists:
                 game_data = doc.to_dict()
                 if 'players' in game_data and isinstance(game_data['players'], str):
                     game_data['players'] = json.loads(game_data['players'])
                 if 'player_bets' in game_data and isinstance(game_data['player_bets'], str):
                     game_data['player_bets'] = json.loads(game_data['player_bets'])
                 if 'game_state' in game_data and isinstance(game_data['game_state'], str):
                     game_data['game_state'] = json.loads(game_data['game_state'])
                 if 'card_choices' in game_data and isinstance(game_data['card_choices'], str):
                     game_data['card_choices'] = json.loads(game_data['card_choices'])
                 logger.debug(f"get_game: chat_id={chat_id}, game={game_data}")
                 return game_data
             logger.debug(f"get_game: chat_id={chat_id}, game=None (not found)")
             return None
         except Exception as e:
             logger.error(f"Error in get_game: chat_id={chat_id}, error={e}")
             return None

     async def update_game(chat_id, **kwargs):
         try:
             doc_ref = db.collection("games").document(str(chat_id))
             for k, v in kwargs.items():
                 if isinstance(v, (list, dict)):
                     kwargs[k] = json.dumps(v)
             doc_ref.set(kwargs, merge=True)
             logger.debug(f"update_game: chat_id={chat_id}, kwargs={kwargs}")
         except Exception as e:
             logger.error(f"Error in update_game: chat_id={chat_id}, kwargs={kwargs}, error={e}")

     async def delete_game(chat_id):
         try:
             db.collection("games").document(str(chat_id)).delete()
             pending_bets_ref = db.collection("pending_bets")
             query = pending_bets_ref.where("chat_id", "==", chat_id).stream()
             for doc in query:
                 doc.reference.delete()
             job_name = f"timeout_{chat_id}"
             for job in application.job_queue.get_jobs_by_name(job_name):
                 job.schedule_removal()
             logger.info(f"delete_game: chat_id={chat_id}, cleaned up jobs")
         except Exception as e:
             logger.error(f"Error in delete_game: chat_id={chat_id}, error={e}")

     async def get_wallet(user_id):
         try:
             doc_ref = db.collection("wallets").document(str(user_id))
             doc = doc_ref.get()
             if doc.exists:
                 wallet_data = doc.to_dict()
                 return wallet_data.get("address"), wallet_data.get("private_key")
             return None
         except Exception as e:
             logger.error(f"Error in get_wallet: user_id={user_id}, error={e}")
             return None, None

     async def create_wallet(user_id):
         try:
             acct = Account.create()
             doc_ref = db.collection("wallets").document(str(user_id))
             doc_ref.set({
                 "user_id": user_id,
                 "address": acct.address,
                 "private_key": acct.key.hex()
             })
             return acct.address, acct.key.hex()
         except Exception as e:
             logger.error(f"Error in create_wallet: user_id={user_id}, error={e}")
             return None, None

     async def add_pending_bet(chat_id, user_id, amount):
         try:
             db.collection("pending_bets").add({
                 "chat_id": chat_id,
                 "user_id": user_id,
                 "amount": amount
             })
         except Exception as e:
             logger.error(f"Error in add_pending_bet: chat_id={chat_id}, user_id={user_id}, error={e}")

     async def process_pending_bets(context, chat_id):
         try:
             pending_bets_ref = db.collection("pending_bets")
             query = pending_bets_ref.where("chat_id", "==", chat_id).stream()
             
             bets_to_delete = []
             for doc in query:
                 bet = doc.to_dict()
                 user_id = bet["user_id"]
                 amount = bet["amount"]
                 
                 wallet = await get_wallet(user_id)
                 if not wallet:
                     logger.warning(f"Wallet not found for user {user_id} during pending bet processing.")
                     await context.bot.send_message(user_id, "❌ Bet failed: Wallet not found. Use /start to create one.")
                     bets_to_delete.append(doc.reference)
                     continue

                 address, private_key = wallet
                 user_account = Account.from_key(private_key)
                 user_balance_wei = w3.eth.get_balance(user_account.address)
                 required_wei = w3.to_wei(amount, "ether")
                 
                 if user_balance_wei < required_wei:
                     logger.warning(f"User {user_id} (address: {user_account.address}) has insufficient balance. Required: {w3.from_wei(required_wei, 'ether')} ETH, Has: {w3.from_wei(user_balance_wei, 'ether')} ETH")
                     await context.bot.send_message(user_id, f"❌ Insufficient funds for bet of {amount} ETH. Fund your wallet with Sepolia ETH.")
                     bets_to_delete.append(doc.reference)
                     continue

                 try:
                     nonce = w3.eth.get_transaction_count(user_account.address)
                     tx = contract.functions.deposit().build_transaction({
                         "from": user_account.address,
                         "value": required_wei,
                         "nonce": nonce,
                         "gas": 200000,
                         "gasPrice": w3.to_wei("20", "gwei"),
                     })
                     signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                     tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                     receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                     if receipt.status == 0:
                         logger.error(f"Deposit transaction failed for user {user_id}: Tx {tx_hash.hex()}")
                         await context.bot.send_message(user_id, "❌ Deposit transaction failed. Contact support.")
                         continue
                     logger.info(f"Processed deposit for user {user_id}: {amount} ETH. Tx: {tx_hash.hex()}")
                     bets_to_delete.append(doc.reference)
                 except Exception as tx_e:
                     logger.error(f"Deposit transaction failed for user {user_id}, amount {amount}: {tx_e}")
                     await context.bot.send_message(user_id, f"❌ Deposit failed: {str(tx_e)}. Contact support.")
                     bets_to_delete.append(doc.reference)

             batch = db.batch()
             for ref in bets_to_delete:
                 batch.delete(ref)
             batch.commit()
             logger.info(f"Deleted {len(bets_to_delete)} pending bets for chat_id={chat_id}")
         except Exception as e:
             logger.error(f"Error in process_pending_bets: chat_id={chat_id}, error={e}")
             await context.bot.send_message(chat_id, "❌ Error processing bets. Contact support.")

     async def get_username(user_id):
         try:
             doc_ref = db.collection("users").document(str(user_id))
             doc = doc_ref.get()
             if doc.exists:
                 user_data = doc.to_dict()
                 return user_data.get("username") or user_data.get("first_name") or f"User{user_id}"
             return f"User{user_id}"
         except Exception as e:
             logger.error(f"Error in get_username: user_id={user_id}, error={e}")
             return f"User{user_id}"

     async def update_user_info(user_id, username, first_name, last_name):
         try:
             doc_ref = db.collection("users").document(str(user_id))
             doc_ref.set({
                 "user_id": user_id,
                 "username": username,
                 "first_name": first_name,
                 "last_name": last_name
             }, merge=True)
         except Exception as e:
             logger.error(f"Error in update_user_info: user_id={user_id}, error={e}")

     async def get_support_username():
         try:
             doc_ref = db.collection("config").document("bot_config")
             doc = doc_ref.get()
             if doc.exists:
                 config_data = doc.to_dict()
                 return config_data.get("support_username", "@arbacenco")
             return "@arbacenco"
         except Exception as e:
             logger.error(f"Error in get_support_username: error={e}")
             return "@arbacenco"

     async def set_support_username(username):
         try:
             doc_ref = db.collection("config").document("bot_config")
             doc_ref.set({"support_username": username}, merge=True)
         except Exception as e:
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
         await set_support_username(username)
         await update.message.reply_text(f"✅ Support username set to {username}!")

     async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
         chat_id = update.effective_chat.id
         user_id = update.effective_user.id
         user = update.effective_user
         await update_user_info(user_id, user.username, user.first_name, user.last_name)
         logger.info(f"/start called: chat_id={chat_id}, user_id={user_id}")

         if chat_id == GROUP_CHAT_ID:
             game = await get_game(chat_id)
             if game and game.get("status") not in ["waiting", "finished"]:
                 logger.debug(f"Active game found: chat_id={chat_id}, status={game.get('status')}")
                 await update.message.reply_text(
                     "❌ A game is already running! Please wait for the next round. 🎲",
                     parse_mode="Markdown"
                 )
                 return
             if game and game.get("status") == "waiting":
                 logger.debug(f"Waiting game found: chat_id={chat_id}")
                 await update.message.reply_text("🎲 A game is already in progress! Join now! 🚀")
                 return
             if game:
                 logger.info(f"Cleaning up old game: chat_id={chat_id}, status={game.get('status')}")
                 if game.get("message_id"):
                     try:
                         await context.bot.delete_message(chat_id, game["message_id"])
                     except Exception as e:
                         logger.warning(f"Failed to delete old message: message_id={game['message_id']}, error={e}")
                 await delete_game(chat_id)

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
                     "🎰 **Welcome to Bakuchi-ba** 🌟\n"
                     "🔥 **Ready to Play?** No game running yet! \n"
                     "💰 **Bet**: 0 ETH\n"
                     "👥 **Players**: 0/8\n"
                     "🎲 Start a game or check the rules below! 🏆",
                     parse_mode="Markdown",
                     reply_markup=reply_markup,
                 )
                 await context.bot.pin_chat_message(chat_id, message.message_id)
                 
                 game_data = {
                     "chat_id": chat_id,
                     "message_id": message.message_id,
                     "creator_id": user_id,
                     "bet_amount": "0",
                     "players": json.dumps([]),
                     "player_count": 0,
                     "test_mode": 0,
                     "status": "waiting",
                     "player_bets": json.dumps({}),
                     "game_state": json.dumps({}),
                     "card_choices": json.dumps({}),
                     "game_mode": "interactive",
                     "target_number": 0
                 }
                 await update_game(chat_id, **game_data)
                 logger.info(f"Game created: chat_id={chat_id}, message_id={message.message_id}")
             except Exception as e:
                 logger.error(f"Error sending start message or creating game: chat_id={chat_id}, error={e}")
                 await update.message.reply_text("❌ Failed to start game! Try again or contact support.")
         else:
             wallet = await get_wallet(user_id)
             support_username = await get_support_username()
             text = (
                 "🌟 **Welcome to Bakuchi-ba!** 🎰\n"
                 " 11:11. Hotel Okitsu. Don’t be late\n\n"
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
                 address, private_key = await create_wallet(user_id)
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
         game = await get_game(chat_id)
         if not game:
             await update.message.reply_text("❌ No game to cancel!")
             return
         if user_id != game.get("creator_id"):
             await update.message.reply_text("❌ Only the game creator can cancel!")
             return
         if game.get("message_id"):
             try:
                 await context.bot.delete_message(chat_id, game["message_id"])
             except Exception as e:
                 logger.warning(f"Failed to delete message: message_id={game['message_id']}, error={e}")
         await delete_game(chat_id)
         await update.message.reply_text("🛑 Game cancelled! Ready for a new round? 🎲")

     async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
         user_id = update.effective_user.id
         chat_id = update.effective_chat.id
         if user_id != CREATOR_ID:
             await update.message.reply_text("❌ Only the bot owner can reset games!")
             return
         game = await get_game(chat_id)
         if game and game.get("message_id"):
             try:
                 await context.bot.delete_message(chat_id, game["message_id"])
             except Exception as e:
                 logger.warning(f"Failed to delete message: message_id={game['message_id']}, error={e}")
         await delete_game(chat_id)
         await update.message.reply_text("🔄 Game state reset! Start a new game with /start.")

     async def who_made_the_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
         await update.message.reply_text("🎨 This bot was crafted by @nakatroll! 🚀")

     async def timeout_card_selection(context: ContextTypes.DEFAULT_TYPE):
         job_data = context.job.data
         chat_id = job_data
         game = await get_game(chat_id)
         if not game or game.get("status") != "card_selection":
             logger.debug(f"Timeout check: No game or not in card_selection for chat_id={chat_id}")
             return
         logger.warning(f"Card selection timeout for chat_id={chat_id}")
         if game.get("message_id"):
             try:
                 await context.bot.delete_message(chat_id, game["message_id"])
             except Exception as e:
                 logger.warning(f"Failed to delete message on timeout: message_id={game['message_id']}, error={e}")
         await delete_game(chat_id)
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
         await update_user_info(user_id, user.username, user.first_name, user.last_name)
         data = query.data
         logger.debug(f"Button pressed: data={data}, chat_id={chat_id}, user_id={user_id}")

         if data in ["view_wallet", "how_to_play", "tutorial_interactive"]:
             if data == "view_wallet":
                 wallet = await get_wallet(user_id)
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
                     "ℹ️ **How to Play Baccarat** 🎰\n"
                     "1. Use /start in the group to begin.\n"
                     "2. Choose '🎮 Start Game', pick '🎲 Simple' or '🃏 Interactive' mode.\n"
                     "3. Select '💰 Set Bet' (Sepolia ETH) or '🎉 Free Play' (no ETH).\n"
                     "4. Join with '➕ Join Game'.\n"
                     "5. **Simple Mode**: Up to 8 players bet on Player, Banker, or Tie; cards are dealt randomly. Payouts: Player (1:1), Banker (1:1, 5% commission), Tie (8:1).\n"
                     "6. **Interactive Mode**: Up to 4 players pick a card (A–K). A secret target number (1–9) is revealed after selections. The player(s) closest to the target win(s) the prize pool (minus 5% fee).\n"
                     "7. Wait for 2 (test mode) or 4/8 players to join.\n"
                     "8. Check private chat for bet/card prompts; winners are tagged in the group! 🏆\n"
                     "💡 Free Play is ETH-free. For betting, fund your wallet with Sepolia ETH.\n"
                     "📖 Use the 'Tutorial' button for Interactive Mode details!",
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
                     "   - Example: Target=7, Player1 picks 8 (total=8), Player2 picks 5 (total=5). Player1 wins (distance=1 vs. 2).\n"
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
                 _, bet_type, game_chat_id_str = parts
                 game_chat_id = int(game_chat_id_str)
                 logger.debug(f"Bet callback: bet_type={bet_type}, game_chat_id={game_chat_id}, user_id={user_id}")
             except (ValueError, IndexError) as e:
                 logger.error(f"Error parsing bet callback: data={data}, error={e}")
                 await query.message.reply_text("❌ Invalid bet action! Please try joining again.")
                 return

             game = await get_game(game_chat_id)
             if not game:
                 logger.error(f"No game found for chat_id={game_chat_id}")
                 await query.message.reply_text("❌ No active game! Start one with /start in the group.")
                 return
             if game.get("status") != "betting":
                 logger.warning(f"Game not in betting state: status={game.get('status')}, chat_id={game_chat_id}")
                 await query.message.reply_text("❌ Betting phase is over or game is not ready!")
                 return
             
             player_bets = game.get("player_bets", {})
             if str(user_id) in player_bets:
                 logger.warning(f"User already bet: user_id={user_id}, chat_id={game_chat_id}")
                 await query.message.reply_text("❌ You've already placed a bet!")
                 return

             player_bets[str(user_id)] = {"choice": bet_type.capitalize() if bet_type != "none" else "None", "amount": game.get("bet_amount")}
             await update_game(game_chat_id, player_bets=player_bets)
             logger.info(f"Bet placed: user_id={user_id}, bet_type={bet_type}, amount={game.get('bet_amount')}, chat_id={game_chat_id}")
             await query.message.reply_text(
                 f"✅ Bet placed{' on **' + bet_type.capitalize() + '**' if bet_type != 'none' else ''} for {game.get('bet_amount')} ETH! Please wait for {'card selection' if game.get('game_mode') == 'interactive' else 'cards to be dealt'}...",
                 parse_mode="Markdown"
             )

             players = game.get("players", [])
             if len(player_bets) == len(players):
                 logger.info(f"All players bet: moving to {'card selection' if game.get('game_mode') == 'interactive' else 'playing'}, chat_id={game_chat_id}")
                 if game.get("game_mode") == "interactive":
                     target_number = random.randint(1, 9)
                     await update_game(game_chat_id, status="card_selection", target_number=target_number)
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
                             f"🎰 **Bakuchi-ba** 🎲\n"
                             f"🔥 Choose your card (target number is secret):\n"
                             f"💰 Your bet: {game.get('bet_amount')} ETH\n"
                             f"🃏 Pick one card to get closest to the target!",
                             parse_mode="Markdown",
                             reply_markup=reply_markup,
                         )
                     context.job_queue.run_once(timeout_card_selection, 30, data=game_chat_id, name=f"timeout_{game_chat_id}")
                 else:
                     await update_game(game_chat_id, status="playing")
                     player_cards = [deal_card(), deal_card()]
                     banker_cards = [deal_card(), deal_card()]
                     game_state = {"player_cards": player_cards, "banker_cards": banker_cards}
                     player_cards, banker_cards, player_draw, banker_draw = baccarat_third_card(player_cards, banker_cards)
                     game_state["player_cards"] = player_cards
                     game_state["banker_cards"] = banker_cards
                     await update_game(game_chat_id, game_state=game_state)
                     await proceed_to_results(context, game_chat_id, game, players, player_bets, player_cards, banker_cards)
             return

         if data.startswith("card_select_"):
             logger.debug(f"Raw card callback data: {data}")
             try:
                 parts = data.split("_")
                 if len(parts) != 4 or parts[0] != "card" or parts[1] != "select":
                     raise ValueError("Invalid callback data format for card selection")
                 card = parts[2]
                 game_chat_id = int(parts[3])
             except (ValueError, IndexError) as e:
                 logger.error(f"Error parsing card select callback: data={data}, error={e}")
                 await query.message.reply_text("❌ Invalid card selection action! Please try again.")
                 return

             game = await get_game(game_chat_id)
             if not game:
                 logger.error(f"No game found for chat_id={game_chat_id}")
                 await query.message.reply_text("❌ No active game! Start one with /start in the group.")
                 return
             if game.get("status") != "card_selection":
                 logger.warning(f"Game not in card_selection state: status={game.get('status')}, chat_id={game_chat_id}")
                 await query.message.reply_text("❌ Card selection phase is over or game is not ready! Try starting a new game.")
                 if game.get("message_id"):
                     try:
                         await context.bot.delete_message(game_chat_id, game["message_id"])
                     except Exception as e:
                         logger.warning(f"Failed to delete message: message_id={game['message_id']}, error={e}")
                 await delete_game(game_chat_id)
                 return
             card_choices = game.get("card_choices", {})
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
             await update_game(game_chat_id, card_choices=card_choices)
             logger.info(f"Card chosen: user_id={user_id}, card={card}, value={card_value}, chat_id={game_chat_id}")

             await query.message.reply_text(
                 f"✅ Card chosen: **{card}**! Waiting for other players...",
                 parse_mode="Markdown"
             )
             
             job_name = f"timeout_{game_chat_id}"
             current_jobs = context.job_queue.get_jobs_by_name(job_name)
             for job in current_jobs:
                 job.schedule_removal()
                 logger.info(f"Removed timeout job {job_name} for chat_id={game_chat_id}")

             players = game.get("players", [])
             player_bets = game.get("player_bets", {})
             if all(str(p) in card_choices for p in players):
                 logger.info(f"All players chose cards, chat_id={game_chat_id}")
                 await proceed_to_results(context, game_chat_id, game, players, player_bets, None, None)
             return

         game = await get_game(chat_id)
         if not game:
             logger.error(f"No game found for group chat_id={chat_id}")
             await query.message.reply_text("❌ No active game! Start one with /start.")
             return
         players = game.get("players", [])
         test_mode = game.get("test_mode", 0)
         max_players = 2 if test_mode else (4 if game.get("game_mode") == "interactive" else 8)

         if data == "start_game":
             if game.get("status") != "waiting":
                 logger.warning(f"Game not in waiting state for start_game: status={game.get('status')}, chat_id={chat_id}")
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
             await update_game(chat_id, game_mode=game_mode)
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
             if game.get("status") != "waiting":
                 await query.message.reply_text("🎲 Game already started! Join now! 🚀")
                 return
             await query.message.reply_text("💰 Enter bet amount (ETH, e.g., 0.01):")
             await update_game(chat_id, status="setting_bet")

         elif data == "free_play":
             if game.get("status") != "waiting":
                 await query.message.reply_text("🎲 Game already started! Join now! 🚀")
                 return
             await update_game(chat_id, bet_amount="0", status="waiting")
             keyboard = [
                 [InlineKeyboardButton("➕ Join Game", callback_data="join")],
                 [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
                 [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
             ]
             reply_markup = InlineKeyboardMarkup(keyboard)
             old_message_id = game.get("message_id")
             new_message = await context.bot.send_message(
                 chat_id,
                 f"🎰 **Bakuchi-ba** 🌟\n"
                 f"🔥 **Free Play Mode!** {'🧪 Test Mode! ' if test_mode else ''}🚀\n"
                 f"💰 **Bet**: 0 ETH\n"
                 f"👥 **Players**: 0/{max_players}\n"
                 f"🎲 Join now",
                 parse_mode="Markdown",
                 reply_markup=reply_markup,
             )
             await context.bot.pin_chat_message(chat_id, new_message.message_id)
             if old_message_id:
                 try:
                     await context.bot.delete_message(chat_id, old_message_id)
                 except Exception as e:
                     logger.warning(f"Failed to delete message: message_id={old_message_id}, error={e}")
             await update_game(chat_id, message_id=new_message.message_id)

         elif data == "tournament":
             await query.message.reply_text("🏆 Tournament mode not implemented yet! Stay tuned! 🎉")

         elif data == "test_mode" and user_id == CREATOR_ID:
             await update_game(chat_id, test_mode=1)
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
             await update_game(chat_id, status="waiting")

         elif data == "join":
             if user_id in players:
                 await query.message.reply_text("❌ You're already in the game!")
                 return
             if game.get("status") != "waiting":
                 await query.message.reply_text("❌ Game already started!")
                 return
             if len(players) >= max_players:
                 await query.message.reply_text(f"❌ Game is full! Wait for the next round. 🎲")
                 return
             wallet = await get_wallet(user_id)
             if not wallet and game.get("bet_amount") != "0":
                 await query.message.reply_text("❌ Create a wallet with /start in private chat!")
                 return
             
             players.append(user_id)
             await update_game(chat_id, players=players, player_count=len(players))
             
             old_message_id = game.get("message_id")
             keyboard = [
                 [InlineKeyboardButton("➕ Join Game", callback_data="join")],
                 [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
                 [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
             ]
             reply_markup = InlineKeyboardMarkup(keyboard)
             new_message = await context.bot.send_message(
                 chat_id,
                 f"🎰 **Bakuchi-ba** 🌟\n"
                 f"🔥 **{'Free Play' if game.get('bet_amount') == '0' else 'Betting'} Mode!** {'🧪 Test Mode! ' if test_mode else ''}🚀\n"
                 f"💰 **Bet**: {game.get('bet_amount')} ETH\n"
                 f"👥 **Players**: {len(players)}/{max_players}\n"
                 f"🎲 Join now.",
                 parse_mode="Markdown",
                 reply_markup=reply_markup,
             )
             await context.bot.pin_chat_message(chat_id, new_message.message_id)
             if old_message_id:
                 try:
                     await context.bot.delete_message(chat_id, old_message_id)
                 except Exception as e:
                     logger.warning(f"Failed to delete message: message_id={old_message_id}, error={e}")
             await update_game(chat_id, message_id=new_message.message_id)
             
             if game.get("game_mode") == "simple":
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
                     f"🎰 **Bakuchi-ba** 🎲\n"
                     f"🔥 You're in the game! Choose your bet:\n"
                     f"💰 **Amount**: {game.get('bet_amount')} ETH\n"
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
                     f"🎰 **Bakuchi-ba** 🎲\n"
                     f"🔥 You're in the game! Confirm your bet:\n"
                     f"💰 **Amount**: {game.get('bet_amount')} ETH\n"
                     f"🎯 You'll pick a card after joining!",
                     parse_mode="Markdown",
                     reply_markup=reply_markup,
                 )
             if len(players) == max_players:
                 await update_game(chat_id, status="betting")
                 await context.bot.send_message(
                     chat_id,
                     f"🎮 **Game Ready!** {len(players)} players joined! Waiting for bets... 🃏",
                     parse_mode="Markdown",
                 )

         elif data == "group_rules":
             await query.message.reply_text(
                 "ℹ️ **Baccarat** 🎰\n"
                 "1. Use /start in the group to begin a game.\n"
                 "2. Select '🎮 Start Game', then choose '🎲 Simple' or '🃏 Interactive' mode.\n"
                 "3. Pick '💰 Set Bet' (Sepolia ETH) or '🎉 Free Play' (no ETH).\n"
                 "4. Join with '➕ Join Game'.\n"
                 "5. **Simple Mode**: Up to 8 players bet on Player, Banker, or Tie; cards are dealt randomly. Payouts: Player (1:1), Banker (1:1, 5% commission), Tie (8:1).\n"
                 "6. **Interactive Mode**: Up to 4 players pick a card (A–K). A secret target number (1–9) is revealed after all players choose. The player(s) closest to the target (card total mod 10) win(s) the prize pool (minus 5% fee). Card values: A=1, 2–9=face value, 10/J/Q/K=0.\n"
                 "7. Wait for 2 (test mode) or 4 (interactive) or 8 (simple) players to join.\n"
                 "8. Check private chat for bet/card prompts; winners are tagged in the group! 🏆\n"
                 "💡 Free Play is ETH-free. For betting, fund your wallet with Sepolia ETH.\n"
                 "📖 Use the 'Tutorial' button for Interactive Mode details!",
                 parse_mode="Markdown",
             )

         elif data == "group_stats":
             await query.message.reply_text(
                 "📊 **Game Stats** 🎰\n"
                 "🔥 Coming soon! Track wins, bets, and more! 🏆",
                 parse_mode="Markdown",
             )

     async def proceed_to_results(context, game_chat_id, game, players, player_bets, player_cards, banker_cards):
         logger.info(f"Proceeding to results: chat_id={game_chat_id}, game_mode={game.get('game_mode')}")
         await update_game(game_chat_id, status="playing")

         if game.get("game_mode") == "simple":
             player_str = ", ".join(card_to_string(c) for c in player_cards)
             banker_str = ", ".join(card_to_string(c) for c in banker_cards)
             await context.bot.send_message(
                 game_chat_id,
                 f"🎰 **Bakuchi-ba** 🌟\n"
                 f"🔥 **Final Hands!** {'🧪 Test Mode! ' if game.get('test_mode') else ''}🚀\n"
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
                     f"💰 Your bet: **{player_bets.get(str(player_id), {}).get('choice', 'None')}** ({game.get('bet_amount')} ETH)",
                     parse_mode="Markdown",
                 )
             result = determine_winner(player_cards, banker_cards)
             winners = [(uid, bet) for uid, bet in player_bets.items() if bet["choice"] == result]
             
             if game.get("bet_amount") != "0":
                 await process_pending_bets(context, game_chat_id)
                 for user_id_str, bet in winners:
                     user_id = int(user_id_str)
                     wallet = await get_wallet(user_id)
                     if not wallet:
                         logger.error(f"Wallet not found for user {user_id} during payout.")
                         await context.bot.send_message(user_id, "❌ Payout failed: Wallet not found. Contact support.")
                         continue
                     address, private_key = wallet
                     user_account = Account.from_key(private_key)
                     nonce = w3.eth.get_transaction_count(account.address)
                     payout = 0.0
                     if bet["choice"] == "Banker":
                         payout = float(bet["amount"]) * 1.95
                     elif bet["choice"] == "Player":
                         payout = float(bet["amount"]) * 2
                     elif bet["choice"] == "Tie":
                         payout = float(bet["amount"]) * 9
                     
                     contract_balance = contract.functions.balances(account.address).call()
                     if contract_balance < w3.to_wei(payout, "ether"):
                         logger.error(f"Contract balance too low for payout: {contract_balance} < {w3.to_wei(payout, 'ether')}")
                         await context.bot.send_message(user_id, "❌ Payout failed: Insufficient contract funds. Contact support.")
                         continue

                     tx = contract.functions.withdraw(w3.to_wei(payout, "ether")).build_transaction({
                         "from": account.address,
                         "nonce": nonce,
                         "gas": 200000,
                         "gasPrice": w3.to_wei("20", "gwei"),
                     })
                     signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                     try:
                         tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                         receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                         if receipt.status == 0:
                             logger.error(f"Payout transaction failed for user {user_id}: Tx {tx_hash.hex()}")
                             await context.bot.send_message(user_id, "❌ Payout transaction failed. Contact support.")
                             continue
                         logger.info(f"Processed payout for user {user_id}: {payout} ETH. Tx: {tx_hash.hex()}")
                         await context.bot.send_message(user_id, f"🎉 Payout of {payout:.4f} ETH processed! Tx: {tx_hash.hex()}")
                     except Exception as tx_e:
                         logger.error(f"Payout transaction failed for user {user_id}: {tx_e}")
                         await context.bot.send_message(user_id, f"❌ Payout failed: {str(tx_e)}. Contact support.")
         else:
             card_choices = game.get("card_choices", {})
             target_number = game.get("target_number")
             winners, totals = determine_pvp_winner(card_choices, target_number)
             
             result_text_lines = []
             for uid in card_choices:
                 username = await get_username(int(uid))
                 result_text_lines.append(f"👤 @{username} picked {card_to_string(card_choices[uid])} (Total: {totals[uid]})")
             result_text = "\n".join(result_text_lines)

             await context.bot.send_message(
                 game_chat_id,
                 f"🎰 **Bakuchi-ba** 🌟\n"
                 f"🔥 **Results!** {'🧪 Test Mode! ' if game.get('test_mode') else ''}🚀\n"
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
                     f"💰 Your bet: {game.get('bet_amount')} ETH",
                     parse_mode="Markdown",
                 )
             if game.get("bet_amount") != "0":
                 await process_pending_bets(context, game_chat_id)
                 await process_pvp_payouts(context, game_chat_id, winners, game.get("bet_amount"), player_bets)
             result = "No winners" if not winners else "Winners determined"

         winner_tags = []
         for uid, _ in winners:
             username = await get_username(int(uid))
             winner_tags.append(f"@{username}")
         winner_tags_str = ", ".join(winner_tags) if winner_tags else "No winners"

         prize_text = ""
         if game.get("bet_amount") != "0" and winners:
             total_pool = float(game.get("bet_amount")) * len(player_bets)
             fee = total_pool * 0.05
             prize_pool = total_pool - fee
             payout_per_winner = prize_pool / len(winners) if winners else 0
             prize_text = f"🏆 **Prize**: {payout_per_winner:.4f} ETH each"

         await context.bot.send_message(
             game_chat_id,
             f"🎰 **Game Over!** 🌟\n"
             f"🔥 **Result**: {result}! 🏆\n"
             f"🎉 **Winners**: {winner_tags_str}\n"
             f"{prize_text}\n"
             f"🚀 Ready for another round? Use /start!",
             parse_mode="Markdown",
         )
         if game.get("message_id"):
             try:
                 await context.bot.delete_message(game_chat_id, game["message_id"])
             except Exception as e:
                 logger.warning(f"Failed to delete message: message_id={game['message_id']}, error={e}")
         await delete_game(game_chat_id)
         
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
                 "🎰 **Bakuchi-ba** 🌟\n"
                 "🔥 **Ready to Play?** No game running yet! 🚀\n"
                 "💰 **Bet**: 0 ETH\n"
                 f"👥 **Players**: 0/{4 if game.get('game_mode') == 'interactive' else 8}\n"
                 "🎲 Start a game or check the rules below! 🏆",
                 parse_mode="Markdown",
                 reply_markup=reply_markup,
             )
             await context.bot.pin_chat_message(game_chat_id, new_message.message_id)
             
             new_game_data = {
                 "chat_id": game_chat_id,
                 "message_id": new_message.message_id,
                 "creator_id": CREATOR_ID,
                 "bet_amount": "0",
                 "players": json.dumps([]),
                 "player_count": 0,
                 "test_mode": 0,
                 "status": "waiting",
                 "player_bets": json.dumps({}),
                 "game_state": json.dumps({}),
                 "card_choices": json.dumps({}),
                 "game_mode": "interactive",
                 "target_number": 0
             }
             await update_game(game_chat_id, **new_game_data)
             logger.info(f"New game created after results: chat_id={game_chat_id}, message_id={new_message.message_id}")
         except Exception as e:
             logger.error(f"Error creating new game message after results: chat_id={game_chat_id}, error={e}")

     async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
         chat_id = update.effective_chat.id
         user_id = update.effective_user.id
         user = update.effective_user
         await update_user_info(user_id, user.username, user.first_name, user.last_name)
         text = update.message.text
         game = await get_game(chat_id)
         if not game:
             logger.debug(f"No game found for message handling: chat_id={chat_id}")
             return
         if game.get("status") == "setting_bet" and user_id == game.get("creator_id"):
             try:
                 bet_amount = float(text)
                 if bet_amount <= 0 or bet_amount > 100:
                     await update.message.reply_text("❌ Bet amount must be between 0.001 and 100 ETH!")
                     return
                 bet = str(bet_amount)
                 logger.info(f"Bet amount set: {bet} ETH, chat_id={chat_id}, user_id={user_id}")
                 await update_game(chat_id, bet_amount=bet, status="waiting")
                 max_players = 2 if game.get("test_mode") else (4 if game.get("game_mode") == "interactive" else 8)
                 keyboard = [
                     [InlineKeyboardButton("➕ Join Game", callback_data="join")],
                     [InlineKeyboardButton("ℹ️ Rules", callback_data="group_rules")],
                     [InlineKeyboardButton("📖 Tutorial", callback_data="tutorial_interactive")],
                 ]
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 old_message_id = game.get("message_id")
                 new_message = await context.bot.send_message(
                     chat_id,
                     f"🎰 **Bakuchi-ba** 🌟\n"
                     f"🔥 **Betting Mode!** {'🧪 Test Mode! ' if game.get('test_mode') else ''}🚀\n"
                     f"💰 **Bet**: {bet} ETH\n"
                     f"👥 **Players**: {game.get('player_count')}/{max_players}\n"
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
                 await update_game(chat_id, message_id=new_message.message_id)
             except ValueError:
                 await update.message.reply_text("❌ Invalid bet amount! Enter a number (e.g., 0.01).")

     # Setup the Telegram Application
     try:
         application = Application.builder().token(TELEGRAM_TOKEN).build()
         logger.info("Telegram Application initialized successfully.")
     except Exception as e:
         logger.critical(f"Failed to initialize Telegram Application: {e}")
         exit(1)

     # Add handlers
     application.add_handler(CommandHandler("start", start, filters=filters.ChatType.GROUPS))
     application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
     application.add_handler(CommandHandler("cancel", cancel))
     application.add_handler(CommandHandler("reset", reset))
     application.add_handler(CommandHandler("setsupport", setsupport))
     application.add_handler(CommandHandler("whomadethebot", who_made_the_bot))
     application.add_handler(CallbackQueryHandler(button))
     application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

     # FastAPI app for webhook
     app = FastAPI()

     @app.on_event("startup")
     async def on_startup():
         logger.info(f"python-telegram-bot version: {telegram.__version__}")
         logger.info("Setting webhook...")
         try:
             await application.bot.set_webhook(url=WEBHOOK_URL)
             logger.info(f"Webhook set to: {WEBHOOK_URL}")
         except Exception as e:
             logger.critical(f"Failed to set webhook: {e}")
             exit(1)

     @app.post("/")
     async def telegram_webhook(request: Request):
         update_json = await request.json()
         update = Update.de_json(update_json, application.bot)
         await application.process_update(update)
         return {"message": "OK"}

     if __name__ == "__main__":
         logger.info(f"Starting Uvicorn on port {PORT}")
         uvicorn.run(app, host="0.0.0.0", port=PORT)
