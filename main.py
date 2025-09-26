import os
import logging
import asyncio
import hashlib
import hmac
import time
import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
import uvicorn
from fastapi import FastAPI, Request

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class DeltaExchangeAPI:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.india.delta.exchange"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
    
    def generate_signature(self, message: str) -> str:
        """Generate HMAC signature for API authentication"""
        message_bytes = bytes(message, 'utf-8')
        secret_bytes = bytes(self.api_secret, 'utf-8')
        hash_obj = hmac.new(secret_bytes, message_bytes, hashlib.sha256)
        return hash_obj.hexdigest()
    
    def make_request(self, method: str, path: str, params: Dict = None, data: str = "") -> Dict:
        """Make authenticated API request"""
        timestamp = str(int(time.time()))
        query_string = ""
        
        if params:
            query_string = "?" + "&".join([f"{k}={v}" for k, v in params.items()])
        
        signature_data = method + timestamp + path + query_string + data
        signature = self.generate_signature(signature_data)
        
        headers = {
            'api-key': self.api_key,
            'timestamp': timestamp,
            'signature': signature,
            'User-Agent': 'telegram-bot-client',
            'Content-Type': 'application/json'
        }
        
        url = f"{self.base_url}{path}"
        
        try:
            response = requests.request(
                method, url, headers=headers, params=params, data=data, timeout=(10, 30)
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
    
    def get_btc_spot_price(self) -> float:
        """Get current BTC spot price from indices"""
        try:
            response = self.make_request("GET", "/v2/indices")
            if response.get('success'):
                for index in response['result']:
                    if index['symbol'] == '.DEXBTUSD':
                        # Get current spot price via ticker
                        ticker_response = self.make_request("GET", f"/v2/tickers/{index['symbol']}")
                        if ticker_response.get('success'):
                            return float(ticker_response['result']['mark_price'])
            raise Exception("BTC spot price not found")
        except Exception as e:
            logger.error(f"Error getting BTC spot price: {e}")
            raise
    
    def get_options_chain(self, expiry_date: str) -> List[Dict]:
        """Get BTC options chain for given expiry date"""
        params = {
            'contract_types': 'call_options,put_options',
            'underlying_asset_symbols': 'BTC',
            'expiry_date': expiry_date
        }
        
        try:
            response = self.make_request("GET", "/v2/tickers", params=params)
            if response.get('success'):
                return response['result']
            return []
        except Exception as e:
            logger.error(f"Error getting options chain: {e}")
            return []
    
    def find_atm_strikes(self, spot_price: float, options_chain: List[Dict]) -> Tuple[Optional[Dict], Optional[Dict]]:
        """Find ATM call and put options closest to spot price"""
        call_option = None
        put_option = None
        min_diff = float('inf')
        
        for option in options_chain:
            strike_price = float(option.get('strike_price', 0))
            diff = abs(strike_price - spot_price)
            
            if diff < min_diff:
                min_diff = diff
                atm_strike = strike_price
        
        # Find call and put options at ATM strike
        for option in options_chain:
            if float(option.get('strike_price', 0)) == atm_strike:
                if 'call_options' in option.get('contract_type', ''):
                    call_option = option
                elif 'put_options' in option.get('contract_type', ''):
                    put_option = option
        
        return call_option, put_option
    
    def place_order(self, product_id: int, side: str, size: int = 1, order_type: str = "market_order") -> Dict:
        """Place an order"""
        order_data = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": order_type
        }
        
        try:
            response = self.make_request("POST", "/v2/orders", data=json.dumps(order_data))
            return response
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            raise
    
    def place_stop_loss_order(self, product_id: int, side: str, stop_price: str, size: int = 1) -> Dict:
        """Place a reduce-only stop loss order"""
        order_data = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": stop_price,
            "reduce_only": "true",
            "stop_trigger_method": "mark_price"
        }
        
        try:
            response = self.make_request("POST", "/v2/orders", data=json.dumps(order_data))
            return response
        except Exception as e:
            logger.error(f"Error placing stop loss order: {e}")
            raise

class TradingBot:
    def __init__(self, delta_api: DeltaExchangeAPI):
        self.delta_api = delta_api
        self.active_positions = {}
    
    def get_today_expiry_date(self) -> str:
        """Get today's date in DD-MM-YYYY format for same-day expiry"""
        return datetime.now().strftime("%d-%m-%Y")
    
    async def execute_short_straddle(self, chat_id: int, bot: Bot) -> str:
        """Execute short straddle strategy"""
        try:
            # Get current BTC spot price
            spot_price = self.delta_api.get_btc_spot_price()
            await bot.send_message(chat_id, f"📊 Current BTC Spot Price: ${spot_price:,.2f}")
            
            # Get today's expiry options chain
            expiry_date = self.get_today_expiry_date()
            options_chain = self.delta_api.get_options_chain(expiry_date)
            
            if not options_chain:
                return f"❌ No options found for today's expiry ({expiry_date})"
            
            # Find ATM call and put options
            call_option, put_option = self.delta_api.find_atm_strikes(spot_price, options_chain)
            
            if not call_option or not put_option:
                return "❌ Could not find ATM call and put options"
            
            call_strike = float(call_option['strike_price'])
            put_strike = float(put_option['strike_price'])
            
            await bot.send_message(
                chat_id, 
                f"🎯 ATM Strike Selected: ${call_strike:,.0f}\n"
                f"📞 Call Option: {call_option['symbol']}\n"
                f"📞 Put Option: {put_option['symbol']}"
            )
            
            # Execute short straddle (sell call and put)
            await bot.send_message(chat_id, "🔄 Executing Short Straddle...")
            
            # Sell call option (1 lot)
            call_order = self.delta_api.place_order(
                product_id=call_option['product_id'],
                side="sell",
                size=1
            )
            
            # Sell put option (1 lot)
            put_order = self.delta_api.place_order(
                product_id=put_option['product_id'],
                side="sell",
                size=1
            )
            
            if not call_order.get('success') or not put_order.get('success'):
                return "❌ Failed to execute short straddle orders"
            
            # Calculate premium collected (approximate)
            call_premium = float(call_option.get('mark_price', 0))
            put_premium = float(put_option.get('mark_price', 0))
            total_premium = call_premium + put_premium
            
            # Calculate 25% increase in premium for stop loss
            stop_loss_call_price = str(call_premium * 1.25)
            stop_loss_put_price = str(put_premium * 1.25)
            
            await bot.send_message(chat_id, "🛡️ Placing Stop Loss Orders...")
            
            # Place stop loss orders (buy back at 25% premium increase)
            call_sl_order = self.delta_api.place_stop_loss_order(
                product_id=call_option['product_id'],
                side="buy",
                stop_price=stop_loss_call_price,
                size=1
            )
            
            put_sl_order = self.delta_api.place_stop_loss_order(
                product_id=put_option['product_id'],
                side="buy",
                stop_price=stop_loss_put_price,
                size=1
            )
            
            # Store position info
            position_id = f"{chat_id}_{int(time.time())}"
            self.active_positions[position_id] = {
                'call_option': call_option,
                'put_option': put_option,
                'call_order_id': call_order['result']['id'],
                'put_order_id': put_order['result']['id'],
                'call_sl_order_id': call_sl_order['result']['id'] if call_sl_order.get('success') else None,
                'put_sl_order_id': put_sl_order['result']['id'] if put_sl_order.get('success') else None,
                'premium_collected': total_premium,
                'timestamp': datetime.now().isoformat()
            }
            
            result_message = (
                f"✅ Short Straddle Executed Successfully!\n\n"
                f"📊 Strike Price: ${call_strike:,.0f}\n"
                f"💰 Premium Collected: ~${total_premium:.2f}\n"
                f"📞 Call Order ID: {call_order['result']['id']}\n"
                f"📞 Put Order ID: {put_order['result']['id']}\n"
                f"🛡️ Stop Loss Orders Placed\n"
                f"⏰ Expiry: {expiry_date}\n\n"
                f"⚠️ WARNING: This is a high-risk strategy with unlimited loss potential!"
            )
            
            return result_message
            
        except Exception as e:
            logger.error(f"Error executing short straddle: {e}")
            return f"❌ Error executing short straddle: {str(e)}"

# Initialize components
DELTA_API_KEY = os.getenv('DELTA_API_KEY', 'your_api_key_here')
DELTA_API_SECRET = os.getenv('DELTA_API_SECRET', 'your_api_secret_here')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'your_telegram_bot_token_here')

delta_api = DeltaExchangeAPI(DELTA_API_KEY, DELTA_API_SECRET)
trading_bot = TradingBot(delta_api)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# FastAPI app for webhook
app = FastAPI()

async def start_handler(update: Update) -> None:
    """Start command handler"""
    keyboard = [
        [InlineKeyboardButton("🚀 Execute Short Straddle", callback_data='execute_straddle')],
        [InlineKeyboardButton("📊 Check Positions", callback_data='check_positions')],
        [InlineKeyboardButton("ℹ️ Help", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_message = (
        "🤖 *Delta Exchange Short Straddle Bot*\n\n"
        "⚠️ *HIGH RISK WARNING* ⚠️\n"
        "This bot executes short straddle strategies with unlimited loss potential.\n\n"
        "📋 *Strategy Details:*\n"
        "• Sells 1 lot ATM Call + 1 lot ATM Put\n"
        "• Same-day expiry options\n"
        "• 25% premium increase stop-loss\n"
        "• Reduce-only stop orders\n\n"
        "Choose an option below:"
    )
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback_handler(update: Update) -> None:
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'execute_straddle':
        await query.edit_message_text("🔄 Executing Short Straddle Strategy...\nThis may take a few moments.")
        
        result = await trading_bot.execute_short_straddle(query.message.chat_id, bot)
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await bot.send_message(query.message.chat_id, result, reply_markup=reply_markup)
    
    elif query.data == 'check_positions':
        if trading_bot.active_positions:
            positions_text = "📊 *Active Positions:*\n\n"
            for pos_id, pos_data in trading_bot.active_positions.items():
                positions_text += (
                    f"🎯 Strike: ${float(pos_data['call_option']['strike_price']):,.0f}\n"
                    f"💰 Premium: ${pos_data['premium_collected']:.2f}\n"
                    f"⏰ Time: {pos_data['timestamp'][:19]}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                )
        else:
            positions_text = "📊 No active positions found."
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(positions_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    elif query.data == 'help':
        help_text = (
            "ℹ️ *Help & Information*\n\n"
            "*Strategy Overview:*\n"
            "Short straddle involves selling both call and put options at the same strike price.\n\n"
            "*Risk Warning:*\n"
            "• Unlimited loss potential\n"
            "• High margin requirements\n"
            "• Time decay benefits seller\n"
            "• Profits if price stays near strike\n\n"
            "*Bot Features:*\n"
            "• Automatic ATM strike selection\n"
            "• Same-day expiry execution\n"
            "• Automatic stop-loss placement\n"
            "• Real-time position tracking\n\n"
            "*Requirements:*\n"
            "• Valid Delta Exchange API keys\n"
            "• Sufficient margin balance\n"
            "• Understanding of options risks"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    elif query.data == 'back_to_menu':
        keyboard = [
            [InlineKeyboardButton("🚀 Execute Short Straddle", callback_data='execute_straddle')],
            [InlineKeyboardButton("📊 Check Positions", callback_data='check_positions')],
            [InlineKeyboardButton("ℹ️ Help", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🤖 *Delta Exchange Short Straddle Bot*\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

@app.post(f"/{TELEGRAM_BOT_TOKEN}")
async def webhook(request: Request):
    """Handle incoming webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        
        if update.message and update.message.text == '/start':
            await start_handler(update)
        elif update.callback_query:
            await button_callback_handler(update)
            
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check():
    """Health check endpoint"""
    return {"status": "Bot is running", "timestamp": datetime.now().isoformat()}

@app.on_event("startup")
async def startup_event():
    """Set webhook on startup"""
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'your-app-name.onrender.com')}/{TELEGRAM_BOT_TOKEN}"
    try:
        await bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting bot server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
