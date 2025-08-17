# --------------------------------------------------
#  الجزء السادس عشر: تطوير الدستور الذكي
# --------------------------------------------------

import logging
import random
import json
from datetime import timedelta, datetime
from collections import deque
import os
import re
import uuid

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

# --- (1) قسم الإعدادات العامة (تم التعديل هنا) ---
# سيتم الآن قراءة المفاتيح السرية من متغيرات البيئة في الخادم
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID'))
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# بقية الملفات يتم تحديدها كما هي
BLACKLIST_FILE = "blacklist.txt"
REPLIES_FILE = "auto_replies.json"
CONSTITUTION_FILE = "constitution.txt" 
REASON_COUNTS_FILE = "reason_counts.json"

# --- (2) إعدادات الرقابة (تبقى كما هي) ---
WARN_LIMIT = 3
MUTE_DURATIONS = {1: 10, 2: 60, 3: 1440} 
MINIMUM_WORD_COUNT = 4
# لاحظ أننا لا نزال نضع WHITELISTED_USERS هنا، يمكنك تعديلها مباشرة في الكود
WHITELISTED_USERS = { OWNER_ID, 987654321 } 
VIOLATION_CACHE = deque(maxlen=100)
TRUST_SCORE_INCREMENT = 1
TRUSTED_USER_THRESHOLD = 100
TRUSTED_USER_SAMPLING_RATE = 10
REASON_TRIGGER_COUNT = 3

# --- (3) إعداد الخدمات والحالات (تبقى كما هي) ---
# التأكد من وجود المفتاح قبل استخدامه
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    logging.warning("مفتاح GEMINI_API_KEY غير موجود. ميزات الذكاء الاصطناعي ستكون معطلة.")
    model = None

(SETTING_WELCOME_MESSAGE, SETTING_CONSTITUTION, 
 ADD_REPLY_TRIGGER, ADD_REPLY_TEXT) = range(4)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- (4) دوال التعامل مع الملفات (مُحسّنة) ---
def load_from_file(filename, is_json=False, default_value=None):
    if not os.path.exists(filename):
        return default_value if default_value is not None else ({} if is_json else set())
    with open(filename, "r", encoding="utf-8") as f:
        if is_json:
            try: return json.load(f)
            except json.JSONDecodeError: return default_value or {}
        elif filename.endswith(".txt"):
            return f.read()
        else:
            return {line.strip() for line in f if line.strip()}

def save_to_file(filename, data, is_json=False):
    with open(filename, "w", encoding="utf-8") as f:
        if is_json:
            json.dump(data, f, ensure_ascii=False, indent=4)
        else:
            f.write(data)

# تحميل البيانات عند بدء التشغيل
BAD_WORDS_BLACKLIST = load_from_file(BLACKLIST_FILE)
AUTO_REPLIES = load_from_file(REPLIES_FILE, is_json=True)
CONSTITUTION = load_from_file(CONSTITUTION_FILE, default_value="لم يتم تعيين دستور لهذه المجموعة بعد.")
REASON_COUNTS = load_from_file(REASON_COUNTS_FILE, is_json=True)

# --- (5) دوال الذكاء الاصطناعي (مع إضافة جديدة) ---
# ... (بقية الكود يبقى كما هو بدون أي تغيير)
def contains_bad_word(message_text: str) -> bool:
    lower_message = message_text.lower()
    for word in BAD_WORDS_BLACKLIST:
        if word in lower_message:
            return True
    return False

async def analyze_message_with_ai(message_text: str, constitution: str) -> bool:
    if not model: return False # التأكد من أن النموذج موجود
    prompt = f"""أنت مشرف ذكاء اصطناعي. هذا هو دستور المجموعة:\n---\n{constitution}\n---\nوهذه رسالة من أحد المستخدمين: "{message_text}"\nهل هذه الرسالة تخالف الدستور؟ أجب بكلمة واحدة فقط: "نعم" أو "لا"."""
    try:
        response = await model.generate_content_async(prompt)
        decision = response.text.strip().lower()
        logging.info(f"AI decision for message '{message_text}': {decision}")
        return "نعم" in decision
    except Exception as e:
        logging.error(f"Error calling Gemini API: {e}")
        return False
        
async def extract_offensive_word_with_ai(message_text: str) -> str | None:
    if not model: return None
    prompt = f"""حلل هذه الرسالة المخالفة: "{message_text}".
    استخرج منها الكلمة **الواحدة** الأكثر إساءة والتي لا تقبل الشك وتصلح للإضافة إلى قائمة الكلمات المحظورة.
    إذا لم تجد كلمة واضحة، أجب بكلمة "لايوجد".
    أجب بالكلمة فقط دون أي مقدمات."""
    try:
        response = await model.generate_content_async(prompt)
        word = response.text.strip().lower()
        if "لايوجد" in word or len(word.split()) > 1:
            return None
        return word
    except Exception as e:
        logging.error(f"Error in offensive word extraction: {e}")
        return None

async def propose_new_rule_with_ai(reason: str, current_constitution: str) -> str | None:
    if not model: return None
    current_rule_count = len([line for line in current_constitution.splitlines() if re.match(r'^\d+\.', line.strip())])
    new_rule_number = current_rule_count + 1
    prompt = f"""
    أنت خبير في صياغة القوانين للمجموعات. هذا هو الدستور الحالي:
    ---
    {current_constitution}
    ---
    لقد تكررت مخالفة سببها: "{reason}".
    قم بصياغة قانون جديد ومناسب وواضح يبدأ بالرقم ({new_rule_number}.) لمعالجة هذه المشكلة.
    أجب بالقانون المقترح فقط دون أي مقدمات أو شروحات.
    """
    try:
        response = await model.generate_content_async(prompt)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Error proposing new rule: {e}")
        return None

# --- (6) دوال لوحة التحكم (معدلة لتستخدم الدستور من الذاكرة) ---
# ...
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! أنا بوت إدارة ذكي. أضفني لمجموعة وامنحني صلاحيات الإشراف.")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("هذا الأمر مخصص لمالك البوت فقط.")
        return
    keyboard = [
        [InlineKeyboardButton("📝 تغيير رسالة الترحيب", callback_data='set_welcome_msg')],
        [InlineKeyboardButton("📜 تعديل دستور المجموعة", callback_data='set_constitution')],
        [InlineKeyboardButton("🤖 إدارة الردود التلقائية", callback_data='manage_replies')],
        [InlineKeyboardButton("❌ إغلاق", callback_data='close_settings')],
    ]
    await update.message.reply_text("أهلاً بك في لوحة التحكم.", reply_markup=InlineKeyboardMarkup(keyboard))

# ... (بقية الدوال تبقى كما هي)
async def set_constitution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل الدستور الجديد ويحفظه في الذاكرة والملف"""
    global CONSTITUTION
    new_constitution = update.message.text
    CONSTITUTION = new_constitution
    save_to_file(CONSTITUTION_FILE, new_constitution)
    await update.message.reply_text("✅ تم حفظ الدستور الجديد بنجاح!")
    return ConversationHandler.END

def parse_duration(duration_str: str) -> timedelta | None:
    match = re.fullmatch(r"(\d+)([mhd])", duration_str.lower())
    if not match: return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm': return timedelta(minutes=value)
    if unit == 'h': return timedelta(hours=value)
    if unit == 'd': return timedelta(days=value)
    return None

async def manual_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    message = update.effective_message
    if admin.id not in WHITELISTED_USERS or not message.reply_to_message: return
    
    lines = message.text.splitlines()
    if len(lines) < 3 or not lines[0].strip().lower() == '/hi': return
    
    action = lines[1].strip()
    reason = lines[2].strip()
    
    REASON_COUNTS[reason] = REASON_COUNTS.get(reason, 0) + 1
    save_to_file(REASON_COUNTS_FILE, REASON_COUNTS, is_json=True)
    
    if REASON_COUNTS[reason] == REASON_TRIGGER_COUNT:
        proposed_rule = await propose_new_rule_with_ai(reason, CONSTITUTION)
        if proposed_rule:
            proposal_key = str(uuid.uuid4())
            context.bot_data[proposal_key] = proposed_rule
            
            keyboard = [[
                InlineKeyboardButton("✅ موافقة", callback_data=f'approve_rule:{proposal_key}'),
                InlineKeyboardButton("❌ رفض", callback_data=f'reject_rule:{proposal_key}')
            ]]
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"💡 **اقتراح لتطوير الدستور**\n\nبناءً على تكرار المخالفات، يقترح الـ AI إضافة القانون التالي:\n\n`{proposed_rule}`\n\nهل توافق على إضافته للدستور؟",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='MarkdownV2'
            )

    offender = message.reply_to_message.from_user
    duration_str = lines[3].strip() if len(lines) > 3 else None
    await message.delete()

    if action == "كتم":
        duration = parse_duration(duration_str) if duration_str else timedelta(minutes=10)
        if not duration: return
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=offender.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now() + duration
        )
        action_text, duration_text = "تم الكتم", f"{duration.days} أيام" if duration.days > 0 else f"{duration.seconds // 3600} ساعات" if duration.seconds >= 3600 else f"{duration.seconds // 60} دقائق"
    elif action == "طرد":
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=offender.id)
        action_text, duration_text = "تم الطرد", "بشكل دائم"
    else: return
    
    announcement = f"⚖️ *إجراء إداري*\n\n👤 *العضو:* {offender.mention_markdown_v2()}\n🚫 *الإجراء:* {action_text}\n⏳ *المدة:* {duration_text}\n📝 *السبب:* {reason}"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=announcement, parse_mode='MarkdownV2')


async def approve_rule_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CONSTITUTION
    query = update.callback_query
    proposal_key = query.data.split(":")[1]
    
    proposed_rule = context.bot_data.get(proposal_key)
    if not proposed_rule:
        await query.edit_message_text("⚠️ حدث خطأ أو أن هذا الاقتراح منتهي الصلاحية.")
        return

    CONSTITUTION += f"\n{proposed_rule}"
    save_to_file(CONSTITUTION_FILE, CONSTITUTION)
    
    await query.edit_message_text(f"✅ تم تحديث الدستور بنجاح بالقانون الجديد:\n\n`{proposed_rule}`", parse_mode='MarkdownV2')
    if proposal_key in context.bot_data:
        del context.bot_data[proposal_key]

async def reject_rule_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    proposal_key = query.data.split(":")[1]
    
    proposed_rule = context.bot_data.get(proposal_key, "اقتراح غير متوفر")
    await query.edit_message_text(f"❌ تم تجاهل اقتراح إضافة القانون التالي:\n\n`{proposed_rule}`", parse_mode='MarkdownV2')
    if proposal_key in context.bot_data:
        del context.bot_data[proposal_key]

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (بقية الكود تبقى كما هي)
    pass

async def apply_punishment(user, chat, message, context, violation_reason):
    # ... (بقية الكود تبقى كما هي)
    pass

def main():
    if not TELEGRAM_TOKEN or not OWNER_ID or not GEMINI_API_KEY:
        logging.error("المفاتيح السرية (TOKEN, OWNER_ID, GEMINI_API_KEY) غير موجودة. يرجى إضافتها كمتغيرات بيئة.")
        return

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # ... (بقية الكود يبقى كما هو)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler("settings", settings))
    # ...
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()