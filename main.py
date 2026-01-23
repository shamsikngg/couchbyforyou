import os
import asyncio
import logging
import sqlite3
import sys
import time
import datetime
import random # Added missing import for random

# DEBUG: Print environment at startup
print("DEBUG: VERSION 4.0 (FULL REWRITE) LOADED - CHECKING SYSTEM...")
print(f"DEBUG: Python version: {sys.version}")
print(f"DEBUG: Current dir: {os.getcwd()}")
print(f"DEBUG: File list: {os.listdir('.')}")

from dotenv import load_dotenv
load_dotenv()
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# GLOBAL SETTINGS
# Voice and AI settings would go here...

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
import tempfile
import edge_tts

# 1. SETUP
logging.basicConfig(level=logging.INFO)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
DB_NAME = os.getenv("DB_PATH", "bot_memory.db")
WEBAPP_URL = "https://shamsikngg.github.io/couchbyforyou/"
ANALYTICS_URL = "https://shamsikngg.github.io/couchbyforyou/analytics.html"

print(f"DEBUG: DB_PATH uses: {DB_NAME}")

if not BOT_TOKEN:
    print("FATAL ERROR: BOT_TOKEN is missing!")
    time.sleep(5)
    exit(1)

# 2. DB INIT
def init_db():
    try:
        db_dir = os.path.dirname(DB_NAME)
        if db_dir and not os.path.exists(db_dir):   
            os.makedirs(db_dir, exist_ok=True)
            
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    subscription_status TEXT DEFAULT 'free',
                    subscription_expiry TIMESTAMP,
                    subscription_start_date TEXT,
                    last_completed_day INTEGER DEFAULT 0,
                    current_self TEXT,
                    fear TEXT,
                    dream TEXT,
                    core_values TEXT,
                    vision TEXT
                )
            ''')
            
            # Daily Stats table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    user_id INTEGER,
                    date TEXT,
                    energy_level INTEGER,
                    productivity_level INTEGER
                )
            ''')
            
            # --- MIGRATOIN FIX (AUTO-HEAL) ---
            try:
                cursor.execute("ALTER TABLE stats ADD COLUMN energy_level INTEGER")
                print("DEBUG: Migrated stats table (added energy_level)")
            except Exception:
                pass # Already exists
                
            try:
                cursor.execute("ALTER TABLE stats ADD COLUMN productivity_level INTEGER")
                print("DEBUG: Migrated stats table (added productivity_level)")
            except Exception:
                pass # Already exists
            
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN subscription_start_date TEXT")
            except: pass
            
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN last_completed_day INTEGER DEFAULT 0")
            except: pass

            # FIX FOR OLD DB SCHEMA (COMPREHENSIVE HEALING)
            # Ensure all required columns exist in 'users'
            required_columns = ['username', 'full_name', 'current_self', 'fear', 'dream', 'core_values', 'vision']
            for col in required_columns:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                    print(f"DEBUG: Migrated users table (added {col})")
                except Exception:
                    pass # Already exists

            # History Table for Unique Wins
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_wins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    win_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Contracts Table (Futures Contract)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    goal TEXT,
                    deadline TEXT,
                    stake TEXT,
                    status TEXT DEFAULT 'active', -- active, completed, failed
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
            print("DEBUG: DB Initialized & Migrated.")
    except Exception as e:
        print(f"DB Init Error: {e}")
        # NUCLEAR OPTION: If syntax error persists (because of old file), delete and retry
        if "syntax error" in str(e) or "values" in str(e):
             print("CRITICAL: DETECTED BROKEN DB SCHEMA. DELETING DB FILE AND RETRYING...")
             try:
                 conn.close()
             except:
                 pass
             try:
                 if os.path.exists(DB_NAME):
                     os.remove(DB_NAME)
                     print("DEBUG: Old DB file deleted.")
                 # Recursive retry (dangerous but necessary here)
                 init_db() 
             except Exception as e2:
                 print(f"FATAL: Could not delete/recreate DB: {e2}")

# --- GLOBAL MEMORY CACHE ---
# Structure: { user_id: set(normalized_text_hashes) }
HISTORY_CACHE = {}

def normalize_text(s):
    import string
    return s.lower().strip().translate(str.maketrans('', '', string.punctuation))

def clean_format(text):
    """
    Removes hashtags and replaces markdown bolding with quotes
    as per user request (Brutal Minimalist Style).
    """
    if not text: return ""
    text = text.replace("#", "")        # Remove hashtags
    text = text.replace("**", '"')      # Replace bold with quotes
    text = text.replace("*", "")        # Remove single stars
    return text

def load_history_to_cache():
    """Load DB history into RAM on startup"""
    print("DEBUG: Loading history to RAM...")
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cur = conn.cursor()
            # Auto-heal check first
            try:
                cur.execute("SELECT user_id, win_text FROM user_wins")
            except sqlite3.OperationalError:
                 return # Table likely doesn't exist yet, empty cache is fine
                 
            rows = cur.fetchall()
            count = 0
            for uid, text in rows:
                if uid not in HISTORY_CACHE:
                    HISTORY_CACHE[uid] = set()
                HISTORY_CACHE[uid].add(normalize_text(text))
                count += 1
            print(f"DEBUG: Loaded {count} wins into RAM cache.")
    except Exception as e:
        print(f"Cache load error: {e}")

# Call on module load
load_history_to_cache()

# 3. HELPERS
def get_user_stats(user_id):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT AVG(energy_level) FROM stats WHERE user_id = ?", (user_id,))
            res = cursor.fetchone()
            return round(res[0], 1) if res and res[0] else 0.0
    except:
        return 0.0

def get_recent_stats(user_id, days=7):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            # Get last N records for graph
            cursor.execute("""
                SELECT energy_level FROM stats 
                WHERE user_id = ? 
                ORDER BY date DESC LIMIT ?
            """, (user_id, days))
            rows = cursor.fetchall()
            if not rows:
                return []
            
            # Reverse to show chronological order (Oldest -> Newest)
            # Filter out None values (treat as 5 or skip)
            data = [r[0] for r in rows if r[0] is not None][::-1]
            return data
    except Exception as e:
        print(f"Stats fetch error: {e}")
        return []

def save_daily_stat(user_id, energy):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            date_str = datetime.date.today().isoformat()
            # Add basic stat
            cursor.execute("INSERT INTO stats (user_id, date, energy_level) VALUES (?, ?, ?)", 
                          (user_id, date_str, energy))
            conn.commit()
            print(f"DEBUG: Saved energy {energy} for {user_id}")
    except Exception as e:
        print(f"DB Write Error: {e}")
        raise e 

def get_subscription_status(user_id):
    # Stub for now - always True for testing
    return True

def get_profile(user_id):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT current_self, fear, dream, core_values, vision FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()
    except:
        return None

# 4. BOT SETUP
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# PROMPTS
COACH_SYSTEM_PROMPT = "Ты — жесткий, но справедливый коуч. Твоя цель — заставить пользователя действовать. Не жалей его."
ACTION_PLAN_PROMPT = "Составь план действий на неделю, исходя из целей пользователя. 3 главных шага."

# --- HANDLERS ---

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# FSM for Perspective Shift
class PerspectiveState(StatesGroup):
    waiting_for_problem = State()

# FSM for Futures Contract
class ContractState(StatesGroup):
    waiting_for_goal = State()
    waiting_for_deadline = State()
    waiting_for_stake = State()

# FSM for Legacy Test
class LegacyState(StatesGroup):
    waiting_for_memory = State() # How to be remembered
    waiting_for_lessons = State() # 3 lessons

# FSM for Mindprint
class MindprintState(StatesGroup):
    waiting_for_q1 = State()
    waiting_for_q2 = State()
    waiting_for_q3 = State()

# --- MENUS & HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Save user to DB if new
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                        (message.from_user.id, message.from_user.username, message.from_user.full_name))
            conn.commit()
    except:
        pass

    kb = ReplyKeyboardBuilder()
    
    # 4 MAIN HUBS
    kb.button(text="🧠 ЦЕНТР УПРАВЛЕНИЯ")
    kb.button(text="⚡ ПОЛИГОН")
    kb.button(text="🏛 АРХИВ")
    kb.button(text="👤 ТЕРМИНАЛ ЛИЧНОСТИ", web_app=types.WebAppInfo(url=WEBAPP_URL))
    
    kb.adjust(2, 2)
    
    await message.answer(
        f"🧬 \"СИСТЕМА ALTER-EGO АКТИВИРОВАНА\"\n\n"
        f"Привет, {message.from_user.first_name}. Я — твой цифровой двойник.\n"
        f"Та версия тебя, которая не знает лени.\n\n"
        f"\"Что мы делаем сегодня?\"",
        reply_markup=kb.as_markup(resize_keyboard=True)
    )

# --- LEVEL 1: HUBS ---

# FSM for Personal AI
class PersonalAIState(StatesGroup):
    chatting = State()

# --- LEVEL 1: HUBS ---

@dp.message(F.text == "🧠 ЦЕНТР УПРАВЛЕНИЯ")
async def hub_brain(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Сдвиг Перспективы", callback_data="feature_perspective")
    kb.button(text="📝 План Действий", callback_data="feature_plan")
    kb.button(text="🧬 Mindprint (Скан)", callback_data="feature_mindprint")
    kb.button(text="🤖 Личный ИИ", callback_data="feature_ai_chat")
    kb.adjust(1)
    kb.adjust(1)
    await message.answer("🧠 \"Центр Управления\"\nМышление и Стратегия.", reply_markup=kb.as_markup())

# --- PERSONAL AI HANDLERS ---
@dp.callback_query(F.data == "feature_ai_chat")
async def start_personal_ai(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🤖 \"ЛИЧНЫЙ АССИСТЕНТ\"\n\n"
        "Я знаю твой профиль. Я помню твои цели.\n"
        "Спрашивай что угодно или проси совета.\n\n"
        "*(Напиши 'Стоп' чтобы выйти)*"
    )
    await state.set_state(PersonalAIState.chatting)
    await callback.answer()

@dp.message(PersonalAIState.chatting)
async def process_personal_ai(message: types.Message, state: FSMContext):
    if message.text.lower() in ["стоп", "выход", "stop", "exit"]:
        await message.answer("Сеанс завершен.")
        await state.clear()
        return

    # Fetch context
    user_id = message.from_user.id
    profile_summary = ""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT current_self, fear, dream FROM users WHERE user_id=?", (user_id,))
            row = cursor.fetchone()
            if row:
                 profile_summary = f"[User Profile -> Pain: {row[0]}, Fear: {row[1]}, Goal: {row[2]}]"
    except: pass
    
    if GIGACHAT_CREDENTIALS:
        try:
            # Context-aware prompt
            system_prompt = (
                f"ТЫ — ЛИЧНЫЙ ИИ-КОУЧ. Ты знаешь всё о пользователе.\n"
                f"{profile_summary}\n"
                "Твоя цель: Помогать ему достичь цели, используя его профиль.\n"
                "Стиль: Краткий, умный, по делу."
            )
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                 payload = Chat(
                    messages=[
                        Messages(role=MessagesRole.SYSTEM, content=system_prompt),
                        Messages(role=MessagesRole.USER, content=message.text)
                    ],
                    temperature=0.7
                )
                 answer = clean_format(giga.chat(payload).choices[0].message.content)
                 await message.answer(answer)
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
    else:
        await message.answer("🧠 Мозг не подключен.")

@dp.message(F.text == "⚡ ПОЛИГОН")
async def hub_action(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Быстрая Победа", callback_data="feature_win")
    kb.button(text="⚡ Волшебный Пинок", callback_data="feature_kick")
    kb.adjust(1)
    await message.answer("⚡ \"Полигон\"\nДействие и Энергия.", reply_markup=kb.as_markup())

@dp.message(F.text == "🏛 АРХИВ")
async def hub_archive(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Контракты", callback_data="portal_contracts")
    kb.button(text="🕯️ Наследие", callback_data="feature_legacy")
    kb.button(text="📊 Статистика", callback_data="feature_stats")
    kb.adjust(1)
    await message.answer("🏛 \"Архив\"\nИстория и Обязательства.", reply_markup=kb.as_markup())

# --- LEVEL 2: FEATURE HANDLERS ---

@dp.callback_query(F.data == "feature_perspective")
async def start_perspective(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("👁 \"Сдвиг Перспективы\"\n\nОпиши проблему, которая тебя тревожит:")
    await state.set_state(PerspectiveState.waiting_for_problem)
    await callback.answer()

@dp.callback_query(F.data == "feature_win")
async def start_win(callback: types.CallbackQuery):
    task = random.choice(QUICK_WINS)
    await callback.message.answer(f"⚔️ \"ТВОЯ ЦЕЛЬ:\"\n\n{task}\n\nСделай это. Потом возвращайся.")
    await callback.answer()

@dp.callback_query(F.data == "feature_kick")
async def start_kick(callback: types.CallbackQuery):
    await callback.answer("⚡ Генерация пинка...", show_alert=False)
    
    # 1. Fetch Data
    user_id = callback.from_user.id
    pain, fear = "Лень", "Быть никем"
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT current_self, fear FROM users WHERE user_id=?", (user_id,))
            row = cursor.fetchone()
            if row:
                if row[0]: pain = row[0]
                if row[1]: fear = row[1]
    except: pass
    
    # 2. Touch of AI
    text = "Вставай и делай. Хватит ждать."
    if GIGACHAT_CREDENTIALS:
        try:
             prompt = (
                 f"ТЫ — ГНЕВНЫЙ ТРЕНЕР. Твой ученик ноет.\n"
                 f"Его проблема: {pain}. Его страх: {fear}.\n"
                 "Наори на него. Скажи ему правду. 2-3 жестких предложения.\n"
                 "Используй 'Ты'. Заставь его двигаться."
             )
             with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                 text = clean_format(giga.chat(prompt).choices[0].message.content)
        except: pass
        
    # 3. Generate Voice
    try:
        voice_file = os.path.join(tempfile.gettempdir(), f"kick_{user_id}_{int(time.time())}.mp3")
        comm = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
        await comm.save(voice_file)
        
        await callback.message.answer_voice(
            types.FSInputFile(voice_file), 
            caption="🔊 **НЕЙРО-ПИНОК**"
        )
        # Cleanup later (or let temp dir handle it, but better remove to save space)
        # os.remove(voice_file) # Async sending might need file overlap, keeping it for now or using sleep
    except Exception as e:
        await callback.message.answer(f"Ошибка голоса: {e}\n\nТекст: {text}")

@dp.callback_query(F.data == "feature_plan")
async def start_plan(callback: types.CallbackQuery):
    await callback.message.answer("📝 **Генерация Плана**\n(В разработке).")
    await callback.answer()

@dp.callback_query(F.data == "feature_stats")
async def start_stats(callback: types.CallbackQuery):
    energy_data = get_recent_stats(callback.from_user.id)
    data_str = ",".join(map(str, energy_data))
    url = f"{ANALYTICS_URL}?energy={data_str}" if data_str else ANALYTICS_URL
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 Графики", web_app=types.WebAppInfo(url=url))
    await callback.message.answer("📊 **Статистика**", reply_markup=kb.as_markup())
    await callback.answer()

# --- LOGIC HANDLERS ---

@dp.message(PerspectiveState.waiting_for_problem)
async def process_perspective_problem(message: types.Message, state: FSMContext):
    await state.update_data(problem=message.text)
    
    # Inline keyboard for personas
    builder = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="👴 Твой 80-летний Я", callback_data="persp_old")],
        [types.InlineKeyboardButton(text="🚀 Илон Маск", callback_data="persp_elon")],
        [types.InlineKeyboardButton(text="👹 Жесткий Критик", callback_data="persp_critic")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="persp_cancel")]
    ])
    
    await message.answer(f"Принято: \"{message.text}\"\n\n**Чьими глазами посмотрим на это?**", reply_markup=builder)
    # Don't reset state yet, we need data for callback

@dp.callback_query(F.data.startswith("persp_"))
async def callback_perspective_choice(callback: types.CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[1]
    
    if choice == "cancel":
        await callback.message.delete()
        await state.clear()
        return

    data = await state.get_data()
    problem = data.get("problem", "Нет данных")
    
    persona_prompts = {
        "old": "ТЫ — 80-ЛЕТНИЙ 'Я' ЭТОГО ЧЕЛОВЕКА. Мудрый, спокойный, прожил жизнь. Ты знаешь, что важно, а что шелуха. Твоя цель — успокоить и дать совет с высоты прожитых лет. Скажи, будет ли эта проблема важна через 50 лет?",
        "elon": "ТЫ — ИЛОН МАСК. Мыслишь первыми принципами. Масштабно. Рискованно. Ты презираешь мелочность. Твоя цель — показать, как использовать эту проблему для роста или как решить её радикально.",
        "critic": "ТЫ — ЖЕСТКИЙ КРИТИК. Ты видишь все слабости. Ты не жалеешь. Ты говоришь правду в лицо. Твоя цель — разнести нытьё и показать, где человек сам виноват и как ему собрать тряпку."
    }
    
    system_prompt = persona_prompts.get(choice, "Ты — Ментор.")
    
    await callback.message.edit_text(f"⏳ **Загружаю сознание...**")
    
    if GIGACHAT_CREDENTIALS:
        try:
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                 payload = Chat(
                    messages=[
                        Messages(role=MessagesRole.SYSTEM, content=system_prompt),
                        Messages(role=MessagesRole.USER, content=f"МОЯ ПРОБЛЕМА: {problem}")
                    ],
                    temperature=1.0
                )
                 res = giga.chat(payload)
                 answer = res.choices[0].message.content
                 
                 await callback.message.edit_text(f"📝 **Мнение:**\n\n{answer}")
        except Exception as e:
            await callback.message.edit_text(f"Ошибка нейросети: {e}")
            
    await state.clear()


    await state.clear()


# --- FUTURES CONTRACT LOGIC ---

@dp.callback_query(F.data == "portal_contracts")
async def start_contracts_portal(callback: types.CallbackQuery):
    builder = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✍️ Заключить Новый", callback_data="contract_new")],
        [types.InlineKeyboardButton(text="🗂 Мои Сделки", callback_data="contract_list")]
    ])
    await callback.message.answer("⚖️ **Бюро Контрактов**", reply_markup=builder)
    await callback.answer()

# 1. Start New Contract Flow
@dp.callback_query(F.data == "contract_new")
async def cb_contract_new(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("1. **Напиши свою ЦЕЛЬ.** (Четко и конкретно)\n\n*Пример: Заработать 100к*")
    await state.set_state(ContractState.waiting_for_goal)
    await callback.answer()

# 2. List Active Contracts
@dp.callback_query(F.data == "contract_list")
async def cb_contract_list(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Removed LIMIT 5 to show ALL contracts (as requested)
            cursor.execute("SELECT goal, deadline, stake, created_at FROM contracts WHERE user_id = ? ORDER BY id DESC", (user_id,))
            rows = cursor.fetchall()
            
        if not rows:
            await callback.message.edit_text("📂 **Архив пуст.**\nТы пока никому ничего не должен.")
        else:
            text = "🗂 **ВСЕ ТВОИ КОНТРАКТЫ:**\n\n"
            for i, row in enumerate(rows, 1):
                text += (
                    f"**{i}. {row['goal']}**\n"
                    f"⏳ Срок: {row['deadline']}\n"
                    f"💀 Ставка: {row['stake']}\n"
                    f"📅 Дата: {row['created_at'][:10]}\n"
                    f"-------------------------\n"
                )
            # Basic validation for message length (Telegram limit is 4096)
            if len(text) > 4000:
                text = text[:4000] + "\n...(список обрезан, слишком много сделок)..."
            
            await callback.message.edit_text(text)
            
    except Exception as e:
        await callback.message.answer(f"Ошибка архива: {e}")
    await callback.answer()


@dp.message(ContractState.waiting_for_goal)
async def contract_goal(message: types.Message, state: FSMContext):
    await state.update_data(goal=message.text)
    await message.answer("2. **Установи ДЕДЛАЙН.** (Дата или срок, например: 'до 1 марта' или 'через неделю')")
    await state.set_state(ContractState.waiting_for_deadline)

@dp.message(ContractState.waiting_for_deadline)
async def contract_deadline(message: types.Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await message.answer(
        "3. **Назначь ЦЕНУ СЛОВА (ШТРАФ).**\n"
        "Что ты сделаешь, если провалишься? Это должно быть больно.\n"
        "Примеры:\n"
        "- 'Отправлю 5000р врагу'\n"
        "- 'Сбрею брови'\n"
        "- 'Не буду пить кофе месяц'\n\n"
        "Пиши свою ставку:"
    )
    await state.set_state(ContractState.waiting_for_stake)

@dp.message(ContractState.waiting_for_stake)
async def contract_stake(message: types.Message, state: FSMContext):
    stake = message.text
    data = await state.get_data()
    goal = data['goal']
    deadline = data['deadline']
    user_id = message.from_user.id
    
    # Save to DB
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute(
                "INSERT INTO contracts (user_id, goal, deadline, stake) VALUES (?, ?, ?, ?)",
                (user_id, goal, deadline, stake)
            )
            conn.commit()
            
        # Generate Certificate
        certificate = (
            f"📜 **КОНТРАКТ С БУДУЩИМ №{int(time.time())}**\n"
            f"-----------------------------------\n"
            f"👤 **УЧАСТНИК:** {message.from_user.first_name}\n"
            f"🎯 **ЦЕЛЬ:** {goal}\n"
            f"⏳ **СРОК:** {deadline}\n"
            f"💀 **ШТРАФ:** {stake}\n"
            f"-----------------------------------\n"
            f"✅ **ПОДПИСАНО КРОВЬЮ (цифровой).**\n\n"
            f"Я (Бот) свидетельствую.\n"
            f"Нарушишь — будешь знать, что ты трепло."
        )
        await message.answer(certificate)
        
    except Exception as e:
        await message.answer(f"Ошибка при подписании: {e}")
        
    await state.clear()



# --- LEGACY (MANIFESTO) LOGIC ---

@dp.callback_query(F.data == "feature_legacy")
async def start_legacy(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🕯️ \"Зал Наследия\"\n\n"
        "Давай представим, что твое время вышло.\n"
        "1. \"Что должны написать на твоем камне?\"\n(Одной фразой: каким тебя запомнят?)"
    )
    await state.set_state(LegacyState.waiting_for_memory)
    await callback.answer()

@dp.message(LegacyState.waiting_for_memory)
async def legacy_memory(message: types.Message, state: FSMContext):
    await state.update_data(memory=message.text)
    await message.answer(
        "2. \"Назови 3 главных урока твоей жизни.\"\n"
        "(Истины, к которым ты пришел через боль и опыт)"
    )
    await state.set_state(LegacyState.waiting_for_lessons)

# IMPORTS
from PIL import Image, ImageDraw, ImageFont
import textwrap

# --- IMAGE GENERATION ---
def create_manifesto_image(text, filename="manifesto.jpg"):
    # 1. Config - PREMIUM LUXURY
    W, H = 1080, 1350 # Instagram Portrait
    BG_COLOR = (10, 10, 12) # Deep Void Black
    GOLD_COLOR = (212, 175, 55) # Classic Gold
    WHITE_COLOR = (245, 245, 245) # Soft White
    GREY_COLOR = (100, 100, 100)
    FONT_PATH = "UniSansDemo-HeavyCAPS.otf"
    
    # 2. Canvas & Gradient (Simulate Radial Glow)
    img = Image.new('RGB', (W, H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    # Draw subtle center glow (simulated circles)
    for r in range(400, 0, -5):
        alpha = int(20 * (r/400))
        # This requires RGBA, but let's stick to simple circles on RGB for perf or just flat background
        # To keep it safe and fast, let's just do a nice border
    
    # PREMIUM BORDER (Double Gold Line)
    draw.rectangle([50, 50, W-50, H-50], outline=GOLD_COLOR, width=2)
    draw.rectangle([65, 65, W-65, H-65], outline=GOLD_COLOR, width=1)
    
    # CORNER ACCENTS (The "Luxury" Touch)
    length = 100
    w = 8
    # Top Left
    draw.line([(50, 50), (50+length, 50)], fill=GOLD_COLOR, width=w)
    draw.line([(50, 50), (50, 50+length)], fill=GOLD_COLOR, width=w)
    # Top Right
    draw.line([(W-50, 50), (W-50-length, 50)], fill=GOLD_COLOR, width=w)
    draw.line([(W-50, 50), (W-50, 50+length)], fill=GOLD_COLOR, width=w)
    # Bottom Left
    draw.line([(50, H-50), (50+length, H-50)], fill=GOLD_COLOR, width=w)
    draw.line([(50, H-50), (50, H-50-length)], fill=GOLD_COLOR, width=w)
    # Bottom Right
    draw.line([(W-50, H-50), (W-50-length, H-50)], fill=GOLD_COLOR, width=w)
    draw.line([(W-50, H-50), (W-50, H-50-length)], fill=GOLD_COLOR, width=w)
    
    # 3. Load Fonts
    try:
        font_title = ImageFont.truetype(FONT_PATH, 80)
        font_body = ImageFont.truetype(FONT_PATH, 42)
        font_footer = ImageFont.truetype(FONT_PATH, 30)
    except:
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_footer = ImageFont.load_default()

    # 4. Process Text
    lines = text.split('\n')
    title = ""
    body = ""
    footer = "Сгенерировано: @SelfForger_bot"
    
    if "МАНИФЕСТ" in lines[0]:
        title = lines[0]
        body_lines = lines[1:]
    else:
        title = "МАНИФЕСТ"
        body_lines = lines
    
    body_lines = [l for l in body_lines if "@SelfForger_bot" not in l]
    body = "\n".join(body_lines).strip()

    # 5. Draw Title (GOLD)
    bbox = draw.textbbox((0, 0), title, font=font_title)
    tw = bbox[2]-bbox[0]
    draw.text(((W-tw)/2, 180), title, font=font_title, fill=GOLD_COLOR)
    
    # Separator Line
    draw.line([(W/2 - 100, 280), (W/2 + 100, 280)], fill=WHITE_COLOR, width=3)

    # 6. Draw Body (White)
    wrapper = textwrap.TextWrapper(width=35) 
    wrapped_lines = []
    for line in body.split('\n'):
        if line.strip():
             wrapped_lines.extend(wrapper.wrap(line))
        else:
             wrapped_lines.append("")
             
    current_y = 350
    for line in wrapped_lines:
        if current_y > H - 200:
            break
        bbox = draw.textbbox((0, 0), line, font=font_body)
        lw = bbox[2]-bbox[0]
        draw.text(((W-lw)/2, current_y), line, font=font_body, fill=WHITE_COLOR)
        current_y += 65 # More Line Height for elegance
        
    # 7. Draw Footer (Grey)
    bbox = draw.textbbox((0, 0), footer, font=font_footer)
    fw = bbox[2]-bbox[0]
    draw.text(((W-fw)/2, H - 100), footer, font=font_footer, fill=GREY_COLOR)

    img.save(filename)
    return filename


@dp.message(LegacyState.waiting_for_lessons)
async def legacy_lessons(message: types.Message, state: FSMContext):
    lessons = message.text
    data = await state.get_data()
    memory = data['memory']
    user_name = message.from_user.first_name # Get real name
    
    await message.answer("⏳ **Гравирую на цифровом камне...**")
    
    prompt = (
        f"ТЫ — ФИЛОСОФ-ПИСАТЕЛЬ.\n"
        f"Задача: Оформить ответы человека в КРАСИВЫЙ, ЭПИЧНЫЙ МАНИФЕСТ.\n"
        f"Имя автора: {user_name}\n\n"
        f"Данные:\n1. Память о нем: {memory}\n2. Его уроки: {lessons}\n\n"
        f"Стиль: Лаконичный, Тезисный. ИЗБЕГАЙ ДЛИННЫХ АБЗАЦЕВ. Максимум 3-4 строки на мысль.\n"
        f"Структура:\n"
        f"- Заголовок: '📜 МАНИФЕСТ [ИМЯ АВТОРА В РОДИТЕЛЬНОМ ПАДЕЖЕ]'\n"
        f"- Текст: 3-4 емких тезиса.\n"
        f"- Эпитафия.\n"
    )
    
    if GIGACHAT_CREDENTIALS:
        try:
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                 payload = Chat(
                    messages=[Messages(role=MessagesRole.USER, content=prompt)],
                    temperature=1.0
                )
                 res = giga.chat(payload)
                 manifest_text = res.choices[0].message.content.strip()
                 
                 # Clean up markdown (User Request: No #, ** -> "")
                 clean_text = clean_format(manifest_text)
                 
                 # Generate Image
                 img_path = create_manifesto_image(clean_text)
                 
                 # Send Photo
                 photo_file = types.FSInputFile(img_path)
                 await message.answer_photo(photo_file, caption="💎 **Твое Наследие.**\nСохрани этот камень.")
                 
        except Exception as e:
             await message.answer(f"Ошибка каменотеса: {e}")
    else:
        await message.answer("Мозг не подключен. (Нет GigaChat токена)")
        
    await state.clear()


# --- MINDPRINT LOGIC ---

# 1. Image Generator (Brutal/Business Style + RADAR FINAL)
def create_mindprint_image(text, archetype_title, stats, filename="mindprint.jpg"):
    import math
    
    # Config - BRUTAL
    W, H = 1080, 1350
    BG_COLOR = (15, 15, 15) 
    ACCENT_COLOR = (255, 255, 255) 
    SEC_COLOR = (100, 100, 100)
    
    img = Image.new('RGB', (W, H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    # Fonts (Slightly Bigger as requested)
    try:
        font_header = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 90)
        font_sub = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 45) # Used for Report Headers
        font_body = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 40) # Bigger Body (was 35)
        font_tiny = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 30) # Bigger Tiny
    except:
        font_header = ImageFont.load_default()
        font_sub = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_tiny = ImageFont.load_default()

    # Clean Inputs (Remove "Grids" ###)
    text = text.replace("#", "").strip()
    archetype_title = archetype_title.replace("#", "").strip()

    # LAYOUT
    
    # Header
    draw.line([(50, 150), (W-50, 150)], fill=ACCENT_COLOR, width=8)
    draw.text((50, 80), "MINDPRINT // NEURO_ID_V3", font=font_tiny, fill=SEC_COLOR)
    
    # Archetype Title
    if len(archetype_title) > 15:
        parts = archetype_title.split()
        line1 = " ".join(parts[:len(parts)//2])
        line2 = " ".join(parts[len(parts)//2:])
        draw.text((50, 200), line1, font=font_header, fill=ACCENT_COLOR)
        draw.text((50, 300), line2, font=font_header, fill=ACCENT_COLOR)
        y_offset = 500
    else:
        draw.text((50, 200), archetype_title, font=font_header, fill=ACCENT_COLOR)
        y_offset = 400
        
    # --- RADAR CHART (Bigger) ---
    cx, cy = W/2 + 220, y_offset + 200 # Moved right and down slightly
    radius = 160 # Bigger (was 120)
    
    angles = [-90, 30, 150]
    axis_pts = []
    
    for ang in angles:
        rad = math.radians(ang)
        ex = cx + radius * math.cos(rad)
        ey = cy + radius * math.sin(rad)
        axis_pts.append((ex, ey))
        draw.line([(cx, cy), (ex, ey)], fill=SEC_COLOR, width=3)
    
    draw.polygon(axis_pts, outline=SEC_COLOR, width=2)
    
    # Labels
    draw.text((axis_pts[0][0]-25, axis_pts[0][1]-45), "RISK", font=font_tiny, fill=ACCENT_COLOR)
    draw.text((axis_pts[1][0]+15, axis_pts[1][1]), "LOGIC", font=font_tiny, fill=ACCENT_COLOR)
    draw.text((axis_pts[2][0]-80, axis_pts[2][1]), "POWER", font=font_tiny, fill=ACCENT_COLOR)
    
    # User Stats
    u_pts = []
    for i, val in enumerate(stats):
        r_val = (val / 100.0) * radius
        rad = math.radians(angles[i])
        ux = cx + r_val * math.cos(rad)
        uy = cy + r_val * math.sin(rad)
        u_pts.append((ux, uy))
    
    draw.polygon(u_pts, outline=ACCENT_COLOR, width=6) # Thicker line
    
    # TEXT REPORT
    draw.rectangle([50, y_offset, 70, y_offset+20], fill=ACCENT_COLOR)
    draw.text((90, y_offset-5), "REPORT:", font=font_tiny, fill=SEC_COLOR)
    
    # Body Text (Bigger Font 40, Width 20)
    wrapper = textwrap.TextWrapper(width=20)
    lines = wrapper.wrap(text)
    y = y_offset + 60
    for line in lines:
        if y > H - 250: break
        draw.text((50, y), line, font=font_body, fill=(220, 220, 220))
        y += 60

    # FOOTER (Centered, No Barcode)
    footer_text = "GENERATED BY @SELFFORGER_BOT"
    bbox = draw.textbbox((0, 0), footer_text, font=font_tiny)
    fw = bbox[2]-bbox[0]
    draw.text(((W-fw)/2, H-100), footer_text, font=font_tiny, fill=SEC_COLOR)
        
    img.save(filename)
    return filename

# 2. Handlers
@dp.callback_query(F.data == "feature_mindprint")
async def start_mindprint(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🧬 **Скан Интеллекта**\n\nЯ проанализирую твой паттерн мышления.\n\n1. **Стабильность или Шанс?**\n(100$ гарантированно или 50% шанс на 1.000.000$?)")
    await state.set_state(MindprintState.waiting_for_q1)
    await callback.answer()

@dp.message(MindprintState.waiting_for_q1)
async def mp_q1(message: types.Message, state: FSMContext):
    await state.update_data(q1=message.text)
    await message.answer("2. **Источник Решений?**\n(Анализ/Факты или Интуиция/Чуйка?)")
    await state.set_state(MindprintState.waiting_for_q2)

@dp.message(MindprintState.waiting_for_q2)
async def mp_q2(message: types.Message, state: FSMContext):
    await state.update_data(q2=message.text)
    await message.answer("3. **Враг повержен. Действие?**\n(Добить, Пройти мимо, Помочь?)")
    await state.set_state(MindprintState.waiting_for_q3)
    
@dp.message(MindprintState.waiting_for_q3)
async def mp_q3(message: types.Message, state: FSMContext):
    await message.answer("🧠 **Обработка нейропаттернов...**")
    data = await state.get_data()
    
    prompt = (
        f"ТЫ — КИБЕР-ПСИХОЛОГ.\n"
        f"Задача: Определить Архетип Мышления на основе ответов.\n"
        f"Ответы:\n1. Риск: {data['q1']}\n2. Логика: {data['q2']}\n3. Жестокость: {message.text}\n\n"
        f"Выведи ответ в формате:\n"
        f"TITLE: [Название Архетипа, 2-3 слова, Капсом]\n"
        f"DESC: [Описание]\n"
        f"STATS: [RISK(0-100), LOGIC(0-100), POWER(0-100)] (Например: 80, 20, 90)\n"
    )
    
    if GIGACHAT_CREDENTIALS:
        try:
             with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                 payload = Chat(
                    messages=[Messages(role=MessagesRole.USER, content=prompt)],
                    temperature=1.0
                )
                 res = giga.chat(payload)
                 content = res.choices[0].message.content
                 
                 # Parse
                 title = "UNKNOWN MIND"
                 desc = content
                 stats = [50, 50, 50] # Default
                 
                 if "TITLE:" in content:
                     try:
                        # Extract Title
                        parts = content.split("TITLE:")[1].split("DESC:")
                        title = parts[0].strip()
                        # Extract Desc & Stats
                        rest = parts[1]
                        if "STATS:" in rest:
                            desc_parts = rest.split("STATS:")
                            desc = desc_parts[0].strip()
                            stats_str = desc_parts[1].strip()
                            # Parse Stats "80, 20, 90" or "[80, 20, 90]"
                            import re
                            nums = re.findall(r'\d+', stats_str)
                            if len(nums) >= 3:
                                stats = [int(nums[0]), int(nums[1]), int(nums[2])]
                        else:
                            desc = rest.strip()
                     except:
                        pass

                 # Generate Image
                 unique_filename = f"mindprint_{message.from_user.id}.jpg"
                 img_path = create_mindprint_image(desc, title, stats, filename=unique_filename)
                 
                 await message.answer_photo(types.FSInputFile(img_path), caption=f"🧬 **Твой Mindprint:**\n{title}")
                 
        except Exception as e:
            await message.answer(f"Ошибка скана: {e}")
    else:
         await message.answer("Мозг не подключен.")
    
    await state.clear()


# --- QUICK WINS LOGIC (THE HEART OF THE FIX) ---

QUICK_WINS = [
    "Выпей стакан воды. Прямо сейчас.",
    "Сделай 10 отжиманий. Кровь должна двигаться.",
    "Удали 3 ненужных фото из галереи.",
    "Напиши одному важному человеку 'Спасибо'.",
    "Прочитай 2 страницы любой книги.",
    "Выпрями спину."
]

@dp.message(F.text == "⚔️ БЫСТРАЯ ПОБЕДА")
async def btn_win(message: types.Message):
    # Backward compatibility handler (Triggered if user clicks old menu button)
    # Redirect to new logic
    user_id = message.from_user.id
    task = random.choice(QUICK_WINS)
    await message.answer(f"⚔️ **ТВОЯ ЦЕЛЬ:**\n\n{task}\n\nСделай это. Потом возвращайся.")

# --- DOSSIER GENERATOR ---

# --- BLACK BOX GENERATOR (MONETIZATION) ---
def create_blackbox_image(user_id, filename="blackbox.jpg"):
    # Config - CYBERPUNK / ENCRYPTED
    W, H = 1080, 1350
    BG_COLOR = (5, 5, 8) # Almost black
    ACCENT_COLOR = (255, 50, 50) # Red for Alert
    LOCK_COLOR = (200, 200, 200)
    
    img = Image.new('RGB', (W, H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    try:
        font_header = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 90)
        font_sub = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 50)
        font_mono = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 35)
    except:
        font_header = ImageFont.load_default()
        font_sub = ImageFont.load_default()
        font_mono = ImageFont.load_default()

    # 1. DRAW LOCK ICON (Center)
    cx, cy = W/2, H/3 - 50
    # Shackle
    draw.arc([cx-60, cy-140, cx+60, cy-20], start=180, end=0, fill=LOCK_COLOR, width=15)
    # Body
    draw.rectangle([cx-80, cy-40, cx+80, cy+100], fill=LOCK_COLOR)
    # Keyhole
    draw.ellipse([cx-20, cy+10, cx+20, cy+50], fill=BG_COLOR)
    
    # 2. STATUS TEXT
    draw.text((W/2 - 200, cy+150), "STATUS: LOCKED", font=font_sub, fill=ACCENT_COLOR)
    
    # 3. GLITCHY / BLURRED LINES
    # Simulate hidden text bars
    start_y = cy + 300
    for i in range(5):
        # Label (Visible)
        labels = ["THREAT LEVEL", "MINDSET FLAW", "SUCCESS PROB", "HIDDEN ASSET", "CRITICAL ERROR"]
        draw.text((100, start_y), f"{labels[i]}:", font=font_mono, fill=(150, 150, 150))
        
        # Value (Blurred/Blocked)
        rect_w = random.randint(200, 400)
        # Draw a "scrambled" block
        draw.rectangle([450, start_y+5, 450+rect_w, start_y+30], fill=(30, 30, 35))
        # Add some random characters
        scramble = "".join([random.choice("!@#$%^&*01") for _ in range(10)])
        draw.text((460, start_y), scramble, font=font_mono, fill=(50, 50, 60))
        
        start_y += 80

    # 4. BIG WARNING
    draw.rectangle([50, H-400, W-50, H-250], outline=ACCENT_COLOR, width=5)
    text = "ENCRYPTED FILE"
    bbox = draw.textbbox((0, 0), text, font=font_header)
    tw = bbox[2]-bbox[0]
    draw.text(((W-tw)/2, H-360), text, font=font_header, fill=ACCENT_COLOR)

    # 5. CTA
    cta = "UNLOCK TO VIEW PROTOCOL"
    bbox_c = draw.textbbox((0, 0), cta, font=font_sub)
    tw_c = bbox_c[2]-bbox_c[0]
    draw.text(((W-tw_c)/2, H-150), cta, font=font_sub, fill=(255, 255, 255))
    
    img.save(filename)
    return filename

def create_dossier_image(data, codename, filename="dossier.jpg"):
    # Config - KGB / SECRET SERVICE
    W, H = 1080, 1350
    BG_COLOR = (20, 20, 20) 
    TEXT_COLOR = (230, 230, 230)
    ACCENT_COLOR = (200, 50, 50) # Red Stamp
    
    img = Image.new('RGB', (W, H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    try:
        font_header = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 60)
        font_mono = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 35) # Ideally Monospace, but let's stick to style
        font_stamp = ImageFont.truetype("UniSansDemo-HeavyCAPS.otf", 50)
    except:
        font_header = ImageFont.load_default()
        font_mono = ImageFont.load_default()
        font_stamp = ImageFont.load_default()

    # HEADER
    draw.text((50, 50), "TOP SECRET // PERSONAL FILE", font=font_header, fill=TEXT_COLOR)
    draw.line([(50, 130), (W-50, 130)], fill=TEXT_COLOR, width=5)
    
    # CONTENT
    y = 200
    fields = [
        ("CODENAME:", codename),
        ("REAL NAME:", data.get('full_name', 'Unknown')),
        ("THREAT (Fear):", data.get('fear', 'N/A')),
        ("MISSION (Dream):", data.get('dream', 'N/A')),
        ("VALUES:", data.get('core_values', 'N/A')),
        ("STATUS:", "ACTIVE // MONITORING")
    ]
    
    wrapper = textwrap.TextWrapper(width=30)
    
    for label, value in fields:
        draw.text((50, y), label, font=font_mono, fill=(150, 150, 150))
        y += 45
        
        # Value might need wrap
        lines = wrapper.wrap(str(value).upper())
        for line in lines:
            draw.text((70, y), line, font=font_mono, fill=TEXT_COLOR)
            y += 45
        y += 40 # Gap between fields

    # STAMP (Redesigned - "Tasty" & Level)
    stamp_x, stamp_y = W-350, 200
    stamp_w, stamp_h = 300, 120
    
    # Outer "Bracket" aesthetic
    draw.line([(stamp_x, stamp_y), (stamp_x+50, stamp_y)], fill=ACCENT_COLOR, width=8) # Top Left
    draw.line([(stamp_x, stamp_y), (stamp_x, stamp_y+50)], fill=ACCENT_COLOR, width=8) 
    
    draw.line([(stamp_x+stamp_w, stamp_y+stamp_h), (stamp_x+stamp_w-50, stamp_y+stamp_h)], fill=ACCENT_COLOR, width=8) # Bottom Right
    draw.line([(stamp_x+stamp_w, stamp_y+stamp_h), (stamp_x+stamp_w, stamp_y+stamp_h-50)], fill=ACCENT_COLOR, width=8)
    
    # Inner Solid Box
    draw.rectangle([stamp_x+20, stamp_y+20, stamp_x+stamp_w-20, stamp_y+stamp_h-20], fill=ACCENT_COLOR)
    
    # Text
    bbox = draw.textbbox((0, 0), "CLASSIFIED", font=font_mono)
    th = bbox[3]-bbox[1]
    tw = bbox[2]-bbox[0]
    # Center text in solid box
    tx = (stamp_x+20) + ((stamp_w-40) - tw) / 2
    ty = (stamp_y+20) + ((stamp_h-40) - th) / 2 - 5 # Adjust visual center
    
    draw.text((tx, ty), "CLASSIFIED", font=font_mono, fill=(20, 20, 20)) # Dark text on Red BG
    
    # ID
    draw.text((50, H-100), f"ID: {int(time.time())}", font=font_mono, fill=(80, 80, 80))
    
    img.save(filename)
    return filename

@dp.message(F.content_type == types.ContentType.WEB_APP_DATA)
async def handle_web_app_data(message: types.Message):
    import json
    import re
    try:
        data = json.loads(message.web_app_data.data)
        
        # Extraction (New Keys: pain, fear, goal, price)
        # ... (mapping logic stays same) ...
        pain = data.get('pain', 'Н/Д')
        fear = data.get('fear', 'Н/Д')
        goal = data.get('goal', 'Н/Д') 
        price = data.get('price', 'Н/Д')
        
        # Legacy/DB Mapping
        current_self = pain 
        dream = goal
        core_values = price
        vision = "N/A" 
        
        user_id = message.from_user.id
        
        # DB Update
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                        (user_id, message.from_user.username, message.from_user.full_name))
            cursor.execute("""
                UPDATE users 
                SET current_self=?, fear=?, dream=?, core_values=?, vision=?
                WHERE user_id=?
            """, (current_self, fear, dream, core_values, vision, user_id))
            conn.commit()
            
        await message.answer("📁 **Данные получены. Формирую досье...**")
        
        # AI Codename Gen
        codename = "AGENT_X"
        if GIGACHAT_CREDENTIALS:
            try:
                with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                    prompt = (
                        f"Ты — Куратор Спецслужб. Придумай КРУТОЙ ПОЗЫВНОЙ (Codename) для агента.\n"
                        f"Его проблема: {pain}\nЕго страх: {fear}\nЕго цель: {goal}\nЦену, которую готов платить: {price}\n\n"
                        f"Позывной должен быть пафосным, кратким (1-2 слова), жестким. На английском.\n"
                        f"Ответ: ТОЛЬКО ПОЗЫВНОЙ.\n"
                        f"ПРИМЕР ОТВЕТА: **IRON_WOLF** или SHADOW_HUNTER"
                    )
                    raw_response = giga.chat(prompt).choices[0].message.content.strip()
                    
                    # 1. Try to extract from ** **
                    match = re.search(r'\*\*(.*?)\*\*', raw_response)
                    if match:
                        codename = match.group(1).strip()
                    else:
                        # 2. Extract first valid English words (uppercase-ish)
                        # Remove quotes
                        clean = raw_response.replace('"', '').replace("'", "")
                        # Split and take first 2 words max
                        parts = clean.split()
                        if len(parts) > 0:
                            codename = " ".join(parts[:2]).upper()
                        
                    # Final Cleanup
                    codename = codename.replace("CODENAME:", "").strip()
                    
            except Exception as e:
                print(f"AI Error: {e}")
                pass
        
        # Generate Image
        dossier_data = {
            'full_name': message.from_user.full_name,
            'fear': fear,
            'dream': goal, # Mapped
            'core_values': price # Mapped
        }
        unique_filename = f"dossier_{user_id}.jpg"
        img_path = create_dossier_image(dossier_data, codename, filename=unique_filename)
        
        await message.answer_photo(
            types.FSInputFile(img_path), 
            caption=f"📂 **ЛИЧНОЕ ДЕЛО ОБНОВЛЕНО.**\n\n👤 Позывной: **{codename}**\n\nТвои данные в реестре. Мы следим."
        )
        
    except Exception as e:
        print(f"WebApp Error: {e}")
        await message.answer(f"❌ Ошибка досье: {e}")

@dp.message(F.text == "⚡ ПОЛУЧИТЬ ПИНОК")
async def btn_kick(message: types.Message):
    # Dummy kick for now
    await message.answer("Скоро здесь будет нейро-голос.")

# --- BLACK BOX LOGIC ---
@dp.message(Command("blackbox"))
async def cmd_blackbox(message: types.Message):
    try:
        user_id = message.from_user.id
        await message.answer("🔒 **ИНИЦИАЛИЗАЦИЯ ПРОТОКОЛА ЗАЩИТЫ...**")
        time.sleep(1)
        
        # 1. Fetch Real Data
        barrier = "НЕ ОПРЕДЕЛЕН"
        goal = "НЕ ОПРЕДЕЛЕНА"
        
        try:
            with sqlite3.connect(DB_NAME) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT fear, dream FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if row:
                    if row[0]: barrier = row[0] # Fear
                    if row[1]: goal = row[1]   # Dream/Goal
        except Exception as  db_err:
            print(f"DB Error: {db_err}")

        # 2. Logic: Calculated Risk & Gap
        # If no goal set -> Risk is ULTRA CRITICAL
        risk_level = "КРИТИЧЕСКИЙ" if (goal == "НЕ ОПРЕДЕЛЕНА") else "ВЫСОКИЙ"
        
        # 3. Construct Teaser
        teaser = (
            "⚠️ \"ОБНАРУЖЕНА КРИТИЧЕСКАЯ УЯЗВИМОСТЬ\"\n\n"
            "Система проанализировала твои паттерны.\n"
            "Результат скрыт в защищенном контейнере.\n\n"
            "\"Фрагменты отчета:\"\n"
            f"🔴 Уровень риска: \"{risk_level}\"\n"
            f"🎯 Цель под угрозой: \"{goal}\"\n"
            f"🚫 Главный ментальный барьер: \"{barrier}\"\n\n"
            "\"Внутри ящика:\"\n"
            "1. Твоя главная ошибка мышления.\n"
            "2. Точная сумма денег, которую ты теряешь ежедневно.\n"
            "3. Алгоритм взлома твоей реальности."
        )
        
        # 4. Generate Encrypted Image
        unique_filename = f"blackbox_{user_id}_{int(time.time())}.jpg"
        img_path = create_blackbox_image(user_id, unique_filename)
        
        # 5. Button
        kb = InlineKeyboardBuilder()
        kb.button(text="🔓 ОТКРЫТЬ ЯЩИК (PREMIUM)", callback_data="blackbox_unlock")
        
        await message.answer_photo(
            types.FSInputFile(img_path),
            caption=teaser,
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        await message.answer(f"❌ Сбой протокола: {e}")
        print(f"BlackBox Error: {e}")

# --- PAYMENTS LOGIC ---

PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "TEST_MODE") 
PRICE_LABEL = "BLACK BOX ACCESS"
PRICE_AMOUNT = 39000 # 390.00 RUB

@dp.callback_query(F.data == "blackbox_unlock")
async def start_payment(callback: types.CallbackQuery):
    # Fix loading spinner: Answer immediately
    await callback.answer()
    
    # DEV MODE BYPASS
    if PAYMENT_TOKEN == "TEST_MODE":
        await cb_blackbox_unlock_dev(callback)
        return

    try:
        await callback.message.answer_invoice(
            title="ДОСТУП К ЧЕРНОМУ ЯЩИКУ",
            description="Расшифровка анализа личности + План выхода из матрицы + 30 дней подписки.",
            payload="blackbox_sub_1",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[types.LabeledPrice(label=PRICE_LABEL, amount=PRICE_AMOUNT)],
            start_parameter="blackbox_sub",
            photo_url="https://i.imgur.com/v8p8G8b.jpg", 
            photo_height=512, photo_width=512, photo_size=512,
            is_flexible=False
        )
    except Exception as e:
        await callback.message.answer(f"❌ ОШИБКА ОПЛАТЫ:\n{e}\n\n(Проверь токен или фото)")

    try:
        await callback.message.answer_invoice(
            title="ДОСТУП К ЧЕРНОМУ ЯЩИКУ",
            description="Расшифровка анализа личности + План выхода из матрицы + 30 дней подписки.",
            payload="blackbox_sub_1",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[types.LabeledPrice(label=PRICE_LABEL, amount=PRICE_AMOUNT)],
            start_parameter="blackbox_sub",
            photo_url="https://i.imgur.com/v8p8G8b.jpg", 
            photo_height=512, photo_width=512, photo_size=512,
            is_flexible=False
        )
    except Exception as e:
        await callback.message.answer(f"❌ ОШИБКА ОПЛАТЫ:\n{e}\n\n(Проверь токен или фото)")

# Dev/Fallback Unlock (Renamed old function)
async def cb_blackbox_unlock_dev(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer("💳 ПЛАТЕЖ ПРИНЯТ (DEV MODE). РАСШИФРОВКА...", show_alert=True)
    msg = await callback.message.answer("🔓 \"ДОСТУП РАЗРЕШЕН.\"\n⏳ Извлечение архива...")
    
    # Fetch Data
    pain, fear, goal, price = "Неизвестно", "Неизвестно", "Неизвестно", "Неизвестно"
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT current_self, fear, dream, price FROM users WHERE user_id=?", (user_id,))
            row = cursor.fetchone()
            if row:
                pain, fear, goal, price = row[0], row[1], row[2], row[3]
            
            # UPDATE SUBSCRIPTION
            expiry = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            start_date = datetime.datetime.now().isoformat()
            cursor.execute("""
                UPDATE users 
                SET subscription_status='active', subscription_expiry=?, subscription_start_date=? 
                WHERE user_id=?
            """, (expiry, start_date, user_id))
            conn.commit()
    except: pass

    # AI Generation
    prompt = (
        f"Ты - Alter Ego. Пользователь купил доступ.\n"
        f"Данные: {pain}, {fear}, {goal}, {price}\n"
        "Сгенерируй отчет: 1. Ошибка, 2. Потери, 3. Алгоритм.\n"
        "Стиль: Жесткий, по фактам."
    )
    
    try:
        content = "Симуляция отчета (GigaChat выключен)."
        if GIGACHAT_CREDENTIALS:
             with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                content = clean_format(giga.chat(prompt).choices[0].message.content)
            
        await msg.edit_text(f"🔓 \"DECRYPTED DATA // USER: {user_id}\"\n\n{content}")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    await message.answer(
        f"💸 \"ОПЛАТА ПОЛУЧЕНА: {message.successful_payment.total_amount // 100} {message.successful_payment.currency}\"\n"
        "Подписка активирована на 30 дней."
    )
    
    # Trigger unlock manually
    # Just reusing the dev logic but passing a dummy callback object might be tricky
    # So we copy-paste the minimal unlock logic here for robustness
    
    user_id = message.from_user.id
    try:
        with sqlite3.connect(DB_NAME) as conn:
            expiry = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            start_date = datetime.datetime.now().isoformat()
            conn.execute("""
                UPDATE users 
                SET subscription_status='active', subscription_expiry=?, subscription_start_date=? 
                WHERE user_id=?
            """, (expiry, start_date, user_id))
            conn.commit()
    except: pass
    
    await message.answer("🔓 \"ГЕНЕРАЦИЯ ОТЧЕТА...\"\n(Иди в Центр Управления или нажми /blackbox)")


@dp.message(F.text)
async def handle_text(message: types.Message):
    # Ignore commands
    if message.text.startswith("/"):
        return
    
    # Generic AI chat
    if GIGACHAT_CREDENTIALS:
         with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
            msg = giga.chat(message.text).choices[0].message.content
            await message.answer(msg)

@dp.callback_query(F.data.startswith("complete_day_"))
async def cb_complete_day(callback: types.CallbackQuery):
    # complete_day_1
    try:
        user_id = callback.from_user.id
        day = int(callback.data.split("_")[-1])
        
        # Update DB
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET last_completed_day = ? WHERE user_id = ?", (day, user_id))
            conn.commit()
            
        await callback.answer(f"День {day} засчитан. Красава.")
        
        # Visual feedback - edit message
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ **ВЫПОЛНЕНО**",
            reply_markup=None
        )
        
        # Optional: AI Motivation or Next Step hint
        if day == 7:
            await callback.message.answer("🎉 ПРОТОКОЛ 7 ЗАВЕРШЕН.\nТы выжил. Теперь начинается настоящая игра.\nЖди обновлений...")
            
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)

@dp.message(Command("set_energy"))
async def cmd_set_energy(message: types.Message):
    # /set_energy 7
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Использование: /set_energy <число 1-10>")
            return
            
        val = int(args[1])
        save_daily_stat(message.from_user.id, val)
        
        # Verify
        check = get_recent_stats(message.from_user.id)
        
        await message.answer(
            f"✅ Команда выполнена.\nПопытка записи: {val}\nЕсть в базе (последние): {check}\n"
        )
    except Exception as e:
        await message.answer(f"🔥 КРИТИЧЕСКАЯ ОШИБКА БАЗЫ:\n{e}\n\n(Скинь этот текст разработчику)")

@dp.message(Command("test_day"))
async def cmd_test_day(message: types.Message):
    # /test_day 1
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Использование: /test_day <номер дня 1-7>")
            return
            
        day = int(args[1])
        content = PROTOCOL_7.get(day)
        
        if content:
             await message.answer(
                f"🧪 **ТЕСТ ПРОТОКОЛА (ДЕНЬ {day})**\n\n"
                f"☀️ \"ДЕНЬ {day}: {content['title']}\"\n\n"
                f"{content['task']}",
                reply_markup=InlineKeyboardBuilder().button(text="✅ ВЫПОЛНИЛ", callback_data=f"complete_day_{day}").as_markup()
            )
        else:
            await message.answer("❌ Нет контента для этого дня.")
            
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# --- PROTOCOL CONTENT ---
PROTOCOL_7 = {
    1: {
        "title": "ДОФАМИНОВОЕ ГОЛОДАНИЕ",
        "task": (
            "Сегодня мы перезагружаем твой мозг.\n"
            "Твои рецепторы выжжены дешевым кайфом.\n\n"
            "ЗАПРЕТЫ НА 24 ЧАСА:\n"
            "🚫 Социальные сети (Удали приложения).\n"
            "🚫 Сахар и фастфуд.\n"
            "🚫 Игры и YouTube.\n"
            "🚫 Музыка (Только тишина).\n\n"
            "Твоя задача — почувствовать скуку. Скука — это начало действий."
        )
    },
    2: {
        "title": "ЦИФРОВАЯ ТИШИНА",
        "task": (
            "Твой телефон — это поводок. Сегодня ты его снимаешь.\n\n"
            "ЗАДАНИЕ:\n"
            "1. Отключи ВСЕ уведомления (кроме звонков от близких).\n"
            "2. Переведи экран в Черно-Белый режим (Настройки -> Экрана).\n"
            "3. Не бери телефон в руки первый час после пробуждения.\n\n"
            "Послушай свои мысли, а не шум извне."
        )
    },
    3: {
        "title": "MEMENTO MORI",
        "task": (
            "Ты умрешь. Это единственная гарантия.\n"
            "Большинство живут так, будто у них в запасе вечность.\n\n"
            "ЗАДАНИЕ:\n"
            "Напиши свою эпитафию (надпись на могиле).\n"
            "Что там будет? 'Он просидел жизнь в ТикТоке'?\n"
            "Напиши один абзац: как тебя ДОЛЖНЫ запомнить.\n"
            "И сравни с тем, кто ты есть сейчас."
        )
    },
    4: {
        "title": "АУДИТ 80/20",
        "task": (
            "Закон Парето: 20% усилий дают 80% результата.\n"
            "Остальное — суета и имитация деятельности.\n\n"
            "ЗАДАНИЕ:\n"
            "Выпиши 10 дел, которые ты делал вчера.\n"
            "Вычеркни 8 из них, которые не ведут к твоей Главной Цели.\n"
            "Оставь 2. Сфокусируйся только на них сегодня."
        )
    },
    5: {
        "title": "ОХОТА НА СТРАХ",
        "task": (
            "Страх — это компас. Он показывает, куда тебе надо идти.\n\n"
            "ЗАДАНИЕ:\n"
            "Сделай сегодня ОДНО действие, которое вызывает социальный дискомфорт.\n"
            "- Попроси скидку там, где её не дают.\n"
            "- Заговори с незнакомцем.\n"
            "- Скажи 'Нет', когда привык соглашаться.\n\n"
            "Сломай шаблон."
        )
    },
    6: {
        "title": "ГЛУБОКАЯ РАБОТА",
        "task": (
            "Мир принадлежит тем, кто умеет фокусироваться.\n\n"
            "ЗАДАНИЕ:\n"
            "Выдели блок из 4 часов.\n"
            "Убери телефон в другую комнату.\n"
            "Займись только ОДНОЙ самой сложной задачей.\n"
            "Не вставай, пока не закончишь (или пока не пройдет время)."
        )
    },
    7: {
        "title": "РЕВЬЮ И КОРРЕКЦИЯ",
        "task": (
            "Неделя прошла. Ты стал лучше или просто старше?\n\n"
            "ЗАДАНИЕ:\n"
            "Оцени свой прогресс по шкале 1-10.\n"
            "Что сработало? Что мешало?\n"
            "Скорректируй план на следующую неделю.\n\n"
            "Ты в игре. Не останавливайся."
        )
    }
}

# --- DAILY LOOP (SCHEDULER) ---

async def morning_protocol():
    """07:00 AM: Goals & Wake Up"""
    print("DEBUG: Executing Morning Protocol...")
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, full_name, dream, fear, subscription_start_date, subscription_status FROM users")
            users = cursor.fetchall()
            
        for uid, name, goal, fear, start_date, status in users:
            try:
                # 1. Determine Day Number
                day_num = 1
                if start_date:
                    try:
                        start_dt = datetime.datetime.fromisoformat(start_date)
                        day_num = (datetime.datetime.now() - start_dt).days + 1
                    except: pass
                
                # 2. Get Content
                msg = ""
                content = PROTOCOL_7.get(day_num)
                
                if status == 'active' and content:
                    # PREMIUM PROTOCOL MESSAGE
                    msg = (
                        f"☀️ \"ДЕНЬ {day_num}: {content['title']}\"\n\n"
                        f"{content['task']}\n\n"
                        f"Цель: {goal}"
                    )
                else:
                    # FALLBACK / TRIAL / AI GEN
                    prompt = (
                        f"ТЫ — ВОЕННЫЙ БУДИЛЬНИК. Твой подопечный: {name}.\n"
                        f"Его цель: {goal if goal else 'Не выбрана'}.\n"
                        f"Его страх: {fear if fear else 'Быть никем'}.\n\n"
                        "Напиши ему утреннее сообщение (короткое, 2-3 строки).\n"
                        "Задача: Заставить его встать и уничтожить этот день.\n"
                        "Стиль: Агрессивная мотивация."
                    )
                    msg = "☀️ \"ВСТАВАЙ, САМУРАЙ.\"\nЦель сама себя не достигнет."
                    
                    if GIGACHAT_CREDENTIALS:
                         with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                            msg = clean_format(giga.chat(prompt).choices[0].message.content)
                    
                    if status != 'active':
                        msg += "\n\n(🔒 Доступ к Курсу закрыт. Оплати, чтобы получить задание.)"

                await bot.send_message(uid, f"{msg}\n\n👇 \"Напиши 3 главные задачи на сегодня:\"")
            except Exception as e:
                print(f"Failed to send morning to {uid}: {e}")
                
    except Exception as e:
        print(f"Morning Loop Error: {e}")

async def evening_report():
    """22:00 PM: Accountability"""
    print("DEBUG: Executing Evening Report...")
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, full_name, price FROM users")
            users = cursor.fetchall()
            
        for uid, name, price in users:
            try:
                # Dynamic Prompt
                prompt = (
                    f"ТЫ — СТРОГИЙ АУДИТОР. Пользователь: {name}.\n"
                    f"Цена провала: {price if price else 'Жизнь в нищете'}.\n\n"
                    "Спроси его, как прошел день. Ты не веришь оправданиям.\n"
                    "Стиль: Холодный, требующий правды."
                )
                msg = "🌙 **22:00. ОТЧЕТ.**\nТы сделал то, что должен был?"
                
                if GIGACHAT_CREDENTIALS:
                     with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                        msg = clean_format(giga.chat(prompt).choices[0].message.content)

                kb = InlineKeyboardBuilder()
                kb.button(text="🔥 Да, я красавчик (100%)", callback_data="report_100")
                kb.button(text="😐 Ну так... (50%)", callback_data="report_50")
                kb.button(text="💀 День в унитаз (0%)", callback_data="report_0")
                kb.adjust(1)

                await bot.send_message(uid, f"{msg}", reply_markup=kb.as_markup())
            except Exception as e:
                print(f"Failed to send evening to {uid}: {e}")
                
    except Exception as e:
        print(f"Evening Loop Error: {e}")

# Callbacks for Stats (Simple)
@dp.callback_query(F.data.startswith("report_"))
async def cb_report_log(callback: types.CallbackQuery):
    val_map = {"100": 100, "50": 50, "0": 0}
    val = val_map.get(callback.data.split("_")[1], 0)
    
    # Save to stats (Energy/Productivity field)
    # Re-using save_daily_stat but treating as productivity
    # We really should have specific productivity column logic but for MVP we log it
    save_daily_stat(callback.from_user.id, int(val/10)) # Map 100->10 scale
    
    await callback.message.edit_text(f"📉 **ДАННЫЕ ЗАПИСАНЫ:** {val}%\nАрхив помнит всё.")


# 5. RUN
async def main():
    print("DEBUG: Bot polling starting...")
    init_db()
    
    # SCHEDULER SETUP
    scheduler.add_job(morning_protocol, 'cron', hour=7, minute=0)
    scheduler.add_job(evening_report, 'cron', hour=22, minute=0)
    scheduler.start()
    print("DEBUG: Scheduler started (07:00 & 22:00).")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
