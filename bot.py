import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import API_URL, CHECK_INTERVAL, ALERT_BEFORE_END_HOURS, WATCHED_NICKS

# Настройки для автоотправки изображения
IMAGE_SEND_INTERVAL = 86400
IMAGE_PATH = Path(__file__).parent / "image.jpg"  # Путь к изображению

known_auctions = set()
last_bids = {}
first_run = True

def load_telegram_config():
    config_path = Path(__file__).parent / "telegram_key.txt"
    if not config_path.exists():
        return None, None
    
    config = {}
    for line in config_path.read_text(encoding="utf-8").strip().split("\n"):
        if "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    
    token = config.get("BOT_TOKEN", "")
    chat_id = config.get("CHAT_ID", "")
    
    if "ВСТАВЬ" in token or "ВСТАВЬ" in chat_id:
        return None, None
    
    return token, chat_id

TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = load_telegram_config()

async def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    print(f"tg error: {await resp.text()}")
    except Exception as e:
        print(f"tg error: {e}")

async def send_image():
    """Отправляет изображение в Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    if not IMAGE_PATH.exists():
        print(f"image not found: {IMAGE_PATH}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    try:
        async with aiohttp.ClientSession() as session:
            with open(IMAGE_PATH, 'rb') as image_file:
                form = aiohttp.FormData()
                form.add_field('chat_id', TELEGRAM_CHAT_ID)
                form.add_field('photo', image_file, filename='image.jpg')
                
                async with session.post(url, data=form) as resp:
                    if resp.status != 200:
                        print(f"tg image error: {await resp.text()}")
                    else:
                        print("image sent successfully")
    except Exception as e:
        print(f"tg image error: {e}")

async def image_sender():
    """Фоновая задача для автоматической отправки изображения каждый час"""
    while True:
        await asyncio.sleep(IMAGE_SEND_INTERVAL)
        await send_image()

async def fetch_auctions():
    async with aiohttp.ClientSession() as session:
        params = {
            "limit": 100,
            "offset": 0,
            "sort_type": "ENDING_SOON",
            "status_type": "ON_AUCTION",
            "item_types": "USERNAME"
        }
        async with session.get(API_URL, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

def parse_time(time_str):
    return datetime.fromisoformat(time_str + "+03:00")

def format_time_left(end_time_str):
    end_time = parse_time(end_time_str)
    now = datetime.now(timezone(timedelta(hours=3)))
    diff = end_time - now
    
    if diff.total_seconds() <= 0:
        return "ended"
    
    days = diff.days
    hours = int((diff.seconds // 3600))
    minutes = int((diff.seconds % 3600) // 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

def is_ending_soon(end_time_str):
    end_time = parse_time(end_time_str)
    now = datetime.now(timezone(timedelta(hours=3)))
    diff = end_time - now
    return timedelta(0) < diff <= timedelta(hours=ALERT_BEFORE_END_HOURS)

async def fetch_bids(item_id):
    url = f"{API_URL}/bid"
    async with aiohttp.ClientSession() as session:
        params = {"item_id": item_id, "limit": 5, "offset": 0}
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

def should_track(nick):
    if not WATCHED_NICKS:
        return True
    return nick.lower() in [n.lower() for n in WATCHED_NICKS]

async def notify_auction(item, is_new=False):
    time_left = format_time_left(item["end_time"])
    tag = "[new]" if is_new else ""
    
    print(f"{tag} {item['item_data']} | {item['current_bid']} | {time_left}")
    
    if TELEGRAM_TOKEN:
        prefix = "<b>new:</b> " if is_new else ""
        msg = f"{prefix}<b>{item['item_data']}</b>\n"
        msg += f"vim: {item['current_bid']} | min: {item['minimum_bid']}\n"
        msg += f"left: {time_left}\n"
        msg += f"https://collect.vimeworld.com/auction/{item['id']}"
        await send_telegram(msg)

async def notify_new_bid(item, new_bid, old_bid):
    time_left = format_time_left(item["end_time"])
    
    bids_data = await fetch_bids(item["id"])
    bidder = "unknown"
    if bids_data and bids_data.get("items"):
        bidder = bids_data["items"][0]["user_name"]
    
    print(f"[bid] {item['item_data']}: {bidder} -> {new_bid} vim | {time_left}")
    
    if TELEGRAM_TOKEN:
        msg = f"<b>stavka on {item['item_data']}</b>\n"
        msg += f"by: {bidder}\n"
        msg += f"vim: {old_bid} -> <b>{new_bid}</b>\n"
        msg += f"left: {time_left}\n"
        msg += f"https://collect.vimeworld.com/auction/{item['id']}"
        await send_telegram(msg)


async def monitor():
    global first_run, known_auctions, last_bids
    
    tg_status = "on" if TELEGRAM_TOKEN else "off"
    print(f"started | tg: {tg_status}")
    
    if TELEGRAM_TOKEN:
        await send_telegram("ZDAROVA PIDARASIKI")
    
    while True:
        try:
            data = await fetch_auctions()
            if data and "items" in data:
                items = data["items"]
                
                if first_run:
                    print(f"found {len(items)} auctions\n")
                    
                    for item in items:
                        if not should_track(item["item_data"]):
                            continue
                        await notify_auction(item, is_new=False)
                        known_auctions.add(item["id"])
                        if is_ending_soon(item["end_time"]):
                            last_bids[item["id"]] = item["current_bid"]
                    
                    first_run = False
                else:
                    for item in items:
                        if not should_track(item["item_data"]):
                            continue
                        
                        item_id = item["id"]
                        
                        if item_id not in known_auctions:
                            await notify_auction(item, is_new=True)
                            known_auctions.add(item_id)
                        
                        if is_ending_soon(item["end_time"]):
                            if item_id not in last_bids:
                                last_bids[item_id] = item["current_bid"]
                            elif last_bids[item_id] != item["current_bid"]:
                                await notify_new_bid(item, item["current_bid"], last_bids[item_id])
                                last_bids[item_id] = item["current_bid"]
                            
        except Exception as e:
            print(f"error: {e}")
        
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    # Запускаем обе задачи параллельно
    async def main():
        await asyncio.gather(
            monitor(),
            image_sender()
        )
    
    asyncio.run(main())





