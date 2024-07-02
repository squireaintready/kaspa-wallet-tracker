import requests
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext, JobQueue
import sqlite3
from sqlite3 import Error
from datetime import datetime, timezone
import pytz
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get the Telegram bot token from the environment variable
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Replace with your bot token
TOKEN = TELEGRAM_TOKEN
# Replace with the Kaspa API base URL
KASPA_API_BASE_URL = 'https://api.kaspa.org/addresses'
KASPA_PRICE_API_URL = 'https://api.kaspa.org/info/price'
# Polling interval in seconds
POLLING_INTERVAL = 60
# Eastern Timezone
TIMEZONE = 'US/Eastern'

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize SQLite database
def create_connection(db_file):
    """ create a database connection to the SQLite database specified by db_file """
    conn = None
    try:
        conn = sqlite3.connect(db_file)
    except Error as e:
        logger.error(f"Error creating connection to database: {e}")
    return conn

def create_table(conn):
    """ create tables if they don't exist """
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS wallets (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        wallet_address TEXT NOT NULL
                    )''')
        conn.commit()
    except Error as e:
        logger.error(f"Error creating table: {e}")

conn = create_connection("wallets.db")
create_table(conn)

last_transactions = {}
last_transaction_counts = {}

async def start(update: Update, context: CallbackContext) -> None:
    logger.info("Received /start command")
    await help_command(update, context)

async def help_command(update: Update, context: CallbackContext) -> None:
    logger.info("Received /help command")
    help_text = (
        "Welcome! Here are the available commands:\n"
        "/start - Display this help message\n"
        "/track <wallet_address> - Track a new wallet\n"
        "/delete_wallet <wallet_address> - Stop tracking a wallet\n"
        "/edit_wallet <old_wallet_address> <new_wallet_address> - Edit a tracked wallet\n"
        "/list_wallets - List all tracked wallets with their balance\n"
        "/history <wallet_address> - Show the 10 most recent transactions\n"
        "/help - Display this help message"
    )
    await update.message.reply_text(help_text)

async def track_wallet(update: Update, context: CallbackContext) -> None:
    try:
        if len(context.args) != 1:
            await update.message.reply_text('Usage: /track <wallet_address>')
            return

        wallet_address = context.args[0]
        user_id = update.message.from_user.id

        # Check if the wallet already exists for the user
        with conn:
            c = conn.cursor()
            c.execute("SELECT * FROM wallets WHERE user_id=? AND wallet_address=?", (user_id, wallet_address))
            result = c.fetchone()
            if result:
                await update.message.reply_text(f"You are already tracking wallet: {wallet_address}")
                return

        await update.message.reply_text(f"Tracking wallet: {wallet_address}")

        # Save wallet to database
        with conn:
            c = conn.cursor()
            c.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (?, ?)", (user_id, wallet_address))
            conn.commit()

        # Fetch initial balance and transactions
        balance = get_wallet_balance(wallet_address)
        price = get_kas_price()
        balance_in_usd = float(balance) * price
        await update.message.reply_text(f'Current balance: {balance} KAS (~${balance_in_usd:.2f})')

        transactions = get_wallet_transactions(wallet_address)
        last_transactions[wallet_address] = transactions

        transaction_count = get_transaction_count(wallet_address)
        last_transaction_counts[wallet_address] = transaction_count

        await update.message.reply_text(f'Initial transactions:\n{format_transactions(transactions[:10])}')

        # Schedule periodic checks
        job_queue = context.job_queue
        job_queue.run_repeating(check_transactions, interval=POLLING_INTERVAL, data={'chat_id': update.message.chat_id, 'wallet_address': wallet_address})
        logger.info(f"Scheduled job to check transactions for wallet: {wallet_address}")

    except Exception as e:
        logger.error(f"Error in track_wallet command: {str(e)}")

async def delete_wallet(update: Update, context: CallbackContext) -> None:
    try:
        if len(context.args) != 1:
            await update.message.reply_text('Usage: /delete_wallet <wallet_address>')
            return

        wallet_address = context.args[0]
        user_id = update.message.from_user.id

        with conn:
            c = conn.cursor()
            c.execute("DELETE FROM wallets WHERE user_id=? AND wallet_address=?", (user_id, wallet_address))
            conn.commit()

        await update.message.reply_text(f"Stopped tracking wallet: {wallet_address}")

    except Exception as e:
        logger.error(f"Error in delete_wallet command: {str(e)}")

async def edit_wallet(update: Update, context: CallbackContext) -> None:
    try:
        if len(context.args) != 2:
            await update.message.reply_text('Usage: /edit_wallet <old_wallet_address> <new_wallet_address>')
            return

        old_wallet_address = context.args[0]
        new_wallet_address = context.args[1]
        user_id = update.message.from_user.id

        with conn:
            c = conn.cursor()
            c.execute("UPDATE wallets SET wallet_address=? WHERE user_id=? AND wallet_address=?", (new_wallet_address, user_id, old_wallet_address))
            conn.commit()

        await update.message.reply_text(f"Updated wallet from {old_wallet_address} to {new_wallet_address}")

    except Exception as e:
        logger.error(f"Error in edit_wallet command: {str(e)}")

async def list_wallets(update: Update, context: CallbackContext) -> None:
    try:
        user_id = update.message.from_user.id
        with conn:
            c = conn.cursor()
            c.execute("SELECT wallet_address FROM wallets WHERE user_id=?", (user_id,))
            wallets = c.fetchall()

        if wallets:
            wallet_list = []
            price = get_kas_price()
            for wallet in wallets:
                wallet_address = wallet[0]
                balance = get_wallet_balance(wallet_address)
                balance_in_usd = float(balance) * price
                wallet_list.append(f"{wallet_address} (Balance: {balance} KAS (~${balance_in_usd:.2f}))")

            await update.message.reply_text(f"Tracked wallets:\n" + "\n".join(wallet_list))
        else:
            await update.message.reply_text("You are not tracking any wallets.")
    except Exception as e:
        logger.error(f"Error in list_wallets command: {str(e)}")

async def history(update: Update, context: CallbackContext) -> None:
    try:
        if len(context.args) != 1:
            await update.message.reply_text('Usage: /history <wallet_address>')
            return

        wallet_address = context.args[0]

        # Fetch the 10 most recent transactions
        transactions = get_wallet_transactions(wallet_address)
        if transactions:
            await update.message.reply_text(f'10 Most Recent Transactions:\n{format_transactions(transactions)}')
        else:
            await update.message.reply_text(f"No transactions found for wallet: {wallet_address}")
    except Exception as e:
        logger.error(f"Error in history command: {str(e)}")

def get_wallet_balance(wallet_address: str) -> str:
    response = requests.get(f'{KASPA_API_BASE_URL}/{wallet_address}/balance')
    if response.status_code == 200:
        data = response.json()
        balance = data.get('balance', '0')
        balance_with_decimal = format_balance(balance)
        return balance_with_decimal
    else:
        logger.error(f"Error fetching balance for wallet {wallet_address}: {response.status_code}")
        return 'Error fetching balance'

def get_kas_price() -> float:
    response = requests.get(KASPA_PRICE_API_URL)
    if response.status_code == 200:
        data = response.json()
        return data.get('price', 0.0)
    else:
        logger.error(f"Error fetching KAS price: {response.status_code}")
        return 0.0

def format_balance(balance: str) -> str:
    balance = int(balance)
    balance_with_decimal = f"{balance / 1_0000_0000:.8f}"
    balance_with_decimal = balance_with_decimal[:-2]
    return balance_with_decimal

def get_wallet_transactions(wallet_address: str):
    response = requests.get(f'{KASPA_API_BASE_URL}/{wallet_address}/full-transactions?limit=10&offset=0&resolve_previous_outpoints=no')
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error fetching transactions for wallet {wallet_address}: {response.status_code}")
        return []

def get_transaction_count(wallet_address: str) -> int:
    response = requests.get(f'{KASPA_API_BASE_URL}/{wallet_address}/transactions-count')
    if response.status_code == 200:
        data = response.json()
        return data.get('total', 0)  # Ensure we use the correct key
    else:
        logger.error(f"Error fetching transaction count for wallet {wallet_address}: {response.status_code}")
        return 0

def format_transactions(transactions):
    formatted_transactions = ""
    price = get_kas_price()
    for i, tx in enumerate(transactions):
        amount = format_balance(sum(output['amount'] for output in tx['outputs']))
        amount_in_usd = float(amount) * price
        try:
            time = datetime.fromtimestamp(tx['block_time'] / 1000, tz=timezone.utc).astimezone(pytz.timezone(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            logger.error(f"Error converting time for transaction {tx['transaction_id']}: {str(e)}")
            time = 'Invalid timestamp'
        formatted_transactions += (
            f"{i+1}. Transaction ID: {tx['transaction_id']}\n"
            f"   Amount: {amount} KAS (~${amount_in_usd:.2f})\n"
            f"   Time: {time}\n\n"
        )
    return formatted_transactions

def check_transactions(context: CallbackContext) -> None:
    job = context.job
    chat_id = job.data['chat_id']
    wallet_address = job.data['wallet_address']

    logger.info(f"Checking transactions for wallet: {wallet_address}")

    # Fetch the current transaction count
    current_transaction_count = get_transaction_count(wallet_address)
    logger.info(f"Current transaction count for wallet {wallet_address}: {current_transaction_count}")

    # Compare with the last known transaction count
    if wallet_address in last_transaction_counts and current_transaction_count != last_transaction_counts[wallet_address]:
        logger.info(f"Transaction count changed for wallet {wallet_address}")
        # Transaction count has changed, fetch the latest transactions
        current_transactions = get_wallet_transactions(wallet_address)
        new_transactions = current_transactions[:1]  # Get the most recent transaction

        if new_transactions:
            logger.info(f"New transaction detected for wallet {wallet_address}: {new_transactions}")
            context.bot.send_message(chat_id=chat_id, text=f'New transaction detected:\n{format_transactions(new_transactions)}')
            last_transactions[wallet_address] = current_transactions

        last_transaction_counts[wallet_address] = current_transaction_count
    else:
        logger.info(f"No new transactions for wallet {wallet_address}")
        last_transaction_counts[wallet_address] = current_transaction_count

def main() -> None:
    logger.info("Starting bot")
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('track', track_wallet))
    application.add_handler(CommandHandler('delete_wallet', delete_wallet))
    application.add_handler(CommandHandler('edit_wallet', edit_wallet))
    application.add_handler(CommandHandler('list_wallets', list_wallets))
    application.add_handler(CommandHandler('history', history))

    application.run_polling()
    logger.info("Bot started with polling")

if __name__ == '__main__':
    main()
