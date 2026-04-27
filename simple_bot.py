import os
import sys
import time
import random
import asyncio
import sqlite3
import logging
import signal
import re
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv
from knowledge import knowledge_base, style_tracker, qa_store
from config_loader import load_config, get, get_channels, get_instructions, get_decision_prompt, get_triggers, is_paused, set_paused

# ============ CONFIGURATION ============
PID_FILE = "/tmp/discord_bot.pid"
LOG_FILE = "/tmp/bot.log"

# ============ LOGGING ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ SIGNAL HANDLING ============
def signal_handler(signum, frame):
    if signum == signal.SIGUSR1:
        set_paused(True)
        logger.info("Bot paused via signal")
    elif signum == signal.SIGUSR2:
        set_paused(False)
        logger.info("Bot resumed via signal")
    else:
        logger.info(f"Received signal {signum}, shutting down...")
        cleanup_pid()
        sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGUSR1, signal_handler)  # Pause
signal.signal(signal.SIGUSR2, signal_handler)  # Resume

# ============ PROCESS MANAGEMENT ============
def check_existing_bot():
    # Check if bot is already running (excluding current process)
    result = os.popen("pgrep -f 'simple_bot.py'").read().strip()
    if result:
        pids = result.split('\n')
        # If there's another bot running (not us), exit
        if len(pids) > 1 or (len(pids) == 1 and int(pids[0]) != os.getpid()):
            logger.info("Bot is already running! Exiting.")
            sys.exit(1)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

def cleanup_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            logger.info("PID file cleaned up")
    except:
        pass

check_existing_bot()

# ============ LOAD ENVIRONMENT ============
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", ".env"))
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    logger.error("ERROR: DISCORD_TOKEN not found")
    cleanup_pid()
    sys.exit(1)

# ============ DISCORD CLIENT ============
import discord
client = discord.Client()

message_queue = deque(maxlen=get("behavior.max_queue", 20))
last_reply = 0
total_replies = 0
replied_set = set()
processing_set = set()

recent_message_hashes = deque(maxlen=20)
recent_sent_responses = deque(maxlen=10)

last_bot_response = ""
last_bot_words = set()

conversation_history = {}
pre_found_answers = {}

last_reset_date = datetime.utcnow().date()

def check_daily_reset():
    """Check if it's a new day (UTC) and reset conversation history"""
    global last_reset_date, conversation_history
    
    current_date = datetime.utcnow().date()
    
    if current_date > last_reset_date:
        # New day - clear conversation history but keep knowledge
        conversation_history.clear()
        last_reset_date = current_date
        logger.info("🌙 New day (UTC) - conversation history cleared, knowledge kept")

def get_channel_history(channel_id):
    max_history = get("knowledge.max_conversation_history", 100)
    if channel_id not in conversation_history:
        conversation_history[channel_id] = deque(maxlen=max_history)
    return conversation_history[channel_id]

# Lock created lazily in event loop
processing_lock = None

# ============ DATABASE ============
DB_FILE = "config/bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS replied (msg_id TEXT PRIMARY KEY, time REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS responses (id INTEGER PRIMARY KEY, channel_id INTEGER, response_text TEXT, created_at REAL)")
    conn.commit()
    conn.close()

def is_replied(msg_id):
    if str(msg_id) in replied_set:
        return True
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM replied WHERE msg_id=?", (str(msg_id),))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_replied(msg_id):
    replied_set.add(str(msg_id))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO replied VALUES (?, ?)", (str(msg_id), time.time()))
    conn.commit()
    conn.close()

def cleanup_old_replies():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM replied WHERE time < ?", (time.time() - 86400,))
    c.execute("DELETE FROM responses WHERE created_at < ?", (time.time() - 86400,))
    conn.commit()
    conn.close()

def get_recent_responses(channel_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT response_text FROM responses WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?", (channel_id, limit))
    results = c.fetchall()
    conn.close()
    return [r[0] for r in results if r[0]]

init_db()
cleanup_old_replies()

# ============ RESPONSE VALIDATION ============
def is_bad_response(response):
    bad_patterns = ["haha, nope", "sorry, i can't", "i am an ai", "as an ai"]
    return any(p in response.lower() for p in bad_patterns)

def is_duplicate_response(channel_id, response):
    recent = get_recent_responses(channel_id, limit=10)
    response_lower = response.lower().strip()
    for r in recent:
        if r and (response_lower in r.lower() or r.lower() in response_lower):
            return True
    return False

def store_response(channel_id, response):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO responses (channel_id, response_text, created_at) VALUES (?, ?, ?)",
                  (channel_id, response[:100], time.time()))
        conn.commit()
        conn.close()
    except:
        pass

def get_message_hash(content):
    """Simple hash to track recent message contents"""
    return hash(content.lower().strip()[:50])

def is_duplicate_message_content(content):
    """Check if we've recently seen a very similar message"""
    msg_hash = get_message_hash(content)
    for h in recent_message_hashes:
        if abs(h - msg_hash) < 5:  # Similar hash = similar content
            return True
    return False

def is_similar_response(response):
    """Check if we've recently sent a very similar response"""
    resp_lower = response.lower().strip()
    for resp in recent_sent_responses:
        if resp_lower == resp:
            return True
        # Check for very similar (90%+ same)
        if len(resp_lower) > 10 and len(resp) > 10:
            if resp_lower in resp or resp in resp_lower:
                return True
    return False

def add_message_hash(content):
    recent_message_hashes.append(get_message_hash(content))

def add_sent_response(response):
    recent_sent_responses.append(response.lower().strip())

def extract_key_words(text):
    """Extract key words from response for self-awareness"""
    stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                  'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
                  'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
                  'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from', 'as',
                  'into', 'through', 'during', 'before', 'after', 'above', 'below',
                  'between', 'under', 'again', 'further', 'then', 'once', 'here',
                  'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few',
                  'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only',
                  'own', 'same', 'so', 'than', 'too', 'very', 'just', 'and', 'but',
                  'if', 'or', 'because', 'until', 'while', 'about', 'against', 'this',
                  'that', 'these', 'those', 'am', 'your', 'yours', 'you', 'i', 'me',
                  'my', 'we', 'our', 'they', 'their', 'she', 'it', 'what', 'he',
                  'which', 'who', 'whom', 'its', 'lol', 'like', 'yeah', 'yes', 'nah',
                  'ngl', 'fr', 'bro', 'man', 'oi', 'hey', 'hi', 'gm', 'gn', 'yo',
                  'wsup', 'wassup', 'get', 'got', 'go', 'going', 'come', 'coming',
                  'see', 'seen', 'say', 'said', 'know', 'knew', 'think', 'thought',
                  'feel', 'felt', 'want', 'wanted', 'make', 'made', 'give', 'gave',
                  'tell', 'told', 'let'}
    
    words = text.lower().split()
    key_words = set()
    for word in words:
        word = word.strip('.,!?;:()"\'-')
        if len(word) > 2 and word not in stop_words:
            key_words.add(word)
    return key_words

def check_self_awareness(response):
    """Check if response overlaps too much with what bot said before"""
    global last_bot_response, last_bot_words
    
    if not last_bot_response:
        last_bot_response = response
        last_bot_words = extract_key_words(response)
        return True
    
    new_words = extract_key_words(response)
    overlap = new_words.intersection(last_bot_words)
    
    if len(overlap) > 2:
        logger.warning(f"[SELF-AWARE] Response repeats too much: {overlap}")
        return False
    
    last_bot_response = response
    last_bot_words = new_words
    return True

def build_conversation_context(channel_id):
    channel_history = get_channel_history(channel_id)
    
    if not channel_history:
        return None
    
    current_time = time.time()
    time_limit = get("behavior.conversation_time_limit", 1200)
    context_size = get("behavior.conversation_context_size", 30)
    context_messages = []
    
    for msg_data in channel_history:
        msg_time, author, content = msg_data
        if current_time - msg_time <= time_limit:
            context_messages.append(f"{author}: {content}")
    
    recent = context_messages[-context_size:] if len(context_messages) > context_size else context_messages
    
    if not recent:
        return None
    
    return {
        'count': len(recent),
        'text': '\n'.join(recent)
    }

# ============ CONTEXT STREAM ============
class ContextStream:
    def __init__(self):
        self.max = get("behavior.max_context", 30)
        self.messages = deque(maxlen=self.max)
    
    def add(self, message):
        self.messages.append(f"{message.author.name}: {message.content}")
        if len(self.messages) > self.max:
            self.messages.popleft()
    
    def format_for_ai(self, count=None):
        if count is None:
            count = len(self.messages)
        return "\n".join(list(self.messages)[-count:])

context_stream = ContextStream()

# ============ INSTRUCTIONS ============
# Instructions are loaded from config/config.yaml via get_instructions()

# ============ AI CLIENT ============
from openai import AsyncOpenAI as OpenAI

ai_client = None

def init_ai_client():
    global ai_client
    if ai_client is None:
        try:
            api_key = os.getenv("OPENROUTER_API_KEY")
            ai_client = OpenAI(
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1"
            )
            logger.info("AI client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize AI client: {e}")
            ai_client = None

def check_ai_connection():
    global ai_client
    if ai_client is None:
        return False
    try:
        loop = asyncio.new_event_loop()
        response = loop.run_until_complete(
            asyncio.wait_for(
                ai_client.chat.completions.create(
                    model=get("ai.model", "google/gemma-3-27b-it"),
                    messages=[{"role": "system", "content": get_instructions()[:100]}],
                    max_tokens=10
                ),
                timeout=5
            )
        )
        loop.close()
        return True
    except:
        return False

# ============ QUESTION HANDLING ============

def is_question(message_content):
    content_lower = message_content.lower()
    triggers = get_triggers()
    question_indicators = triggers.get("question_indicators", ['?', 'when', 'how', 'what', 'where', 'why', 'which', 'who'])
    return any(q in content_lower for q in question_indicators) or '?' in message_content

async def ai_find_answer(question, facts, history):
    """Use AI to find answer from context"""
    global ai_client
    
    if not ai_client:
        return None
    
    context_parts = []
    if facts:
        facts_text = "\n".join([f"- {f['content']}" for f in facts])
        context_parts.append(f"Relevant Facts:\n{facts_text}")
    if history and history.get('text'):
        context_parts.append(f"Recent Conversation:\n{history['text'][:500]}")
    
    context = "\n\n".join(context_parts) if context_parts else "No context available"
    
    prompt = f"""Based on the context below, find the answer to the question.

{context}

Question: "{question}"

Rules:
- If you can find a clear answer in the context, provide it
- Keep answer SHORT - 1-2 sentences max
- If no clear answer exists, respond with exactly: NO_ANSWER
- Don't make up information not in context

Provide your answer:"""
    
    try:
        response = await asyncio.wait_for(
            ai_client.chat.completions.create(
                model=get("ai.model", "google/gemma-3-27b-it"),
                messages=[
                    {"role": "system", "content": "You are finding answers from conversation context. Be precise and concise."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=get("ai.answer_max_tokens", 50),
                temperature=get("ai.answer_temperature", 0.4)
            ),
            timeout=get("ai.decision_timeout", 10)
        )
        
        if response and response.choices:
            answer = response.choices[0].message.content.strip()
            return answer if answer != "NO_ANSWER" else None
    
    except Exception as e:
        logger.warning(f"[AI_FIND] Error finding answer: {e}")
    
    return None

async def find_answer_for_question(question, channel_id):
    """Search multiple sources to find answer to question"""
    
    # Step 1: Check Q&A store (highest priority)
    qa_match = qa_store.find_answer(question, threshold=0.5, channel_id=channel_id)
    if qa_match:
        return qa_match['answer'], "qa"
    
    # Step 2: Get relevant facts
    facts = knowledge_base.get_relevant_facts(question, limit=5, channel_id=channel_id)
    
    # Step 3: Get conversation history
    history = build_conversation_context(channel_id)
    
    # Step 4: Use AI to find answer from context
    answer = await ai_find_answer(question, facts, history)
    if answer:
        return answer, "context"
    
    return None, None

async def learn_from_conversation(message):
    """Learn Q&A from conversation when others answer questions"""
    content_lower = message.content.lower()
    
    # Check if this looks like an answer (not a question, not too short)
    question_indicators = ['?', 'when', 'how', 'what', 'where', 'why', 'which', 'who']
    is_question_msg = any(q in content_lower for q in question_indicators) or '?' in message.content
    
    if is_question_msg or len(message.content) < 5:
        return
    
    # Get recent conversation history
    channel_history = get_channel_history(message.channel.id)
    if len(channel_history) < 2:
        return
    
    # Look for recent question from another user
    recent_msgs = list(channel_history)[-10:]
    for msg_time, author, content in reversed(recent_msgs):
        # Skip bot's own messages
        if author.lower() == get("bot.discord_username", "a_raphie").lower():
            continue
        # Skip same author (not a Q&A pair)
        if author.lower() == message.author.name.lower():
            continue
        # Check if it was a question
        if is_question(content):
            # Store Q&A pair
            qa_store.store_qa(content, message.content, message.author.name, channel_id=message.channel.id)
            logger.info(f"[LEARN] Q from {author}, A from {message.author.name}")
            return

# Decision prompt is loaded from config/config.yaml via get_decision_prompt()

async def should_respond(message_content, author_name, channel_id):
    global ai_client
    
    if not ai_client:
        return True
    
    conv_context = build_conversation_context(channel_id)
    context_text = conv_context['text'] if conv_context else "No recent messages"
    
    decision_prompt = get_decision_prompt()
    
    try:
        response = await asyncio.wait_for(
            ai_client.chat.completions.create(
                model=get("ai.model", "google/gemma-3-27b-it"),
                messages=[
                    {"role": "system", "content": decision_prompt.format(
                        bot_id=client.user.id,
                        context=context_text[:500],
                        message=message_content,
                        author=author_name
                    )},
                    {"role": "user", "content": "Should I respond? YES or NO?"}
                ],
                max_tokens=get("ai.decision_max_tokens", 5),
                temperature=0.3
            ),
            timeout=10
        )
        
        if response and response.choices:
            answer = response.choices[0].message.content.strip().upper()
            should_respond = 'YES' in answer
            logger.info(f"[DECISION] '{answer}' - {'responding' if should_respond else 'skipping'}")
            return should_respond
    
    except Exception as e:
        logger.warning(f"[DECISION] Error: {e}")
        return True  # Default to responding on error
    
    return True

# Settings for naturally joining conversations
# Join conversation chance is loaded from config/config.yaml

async def should_naturally_join(message_content, channel_id):
    """Decide if bot should naturally join a conversation between others"""
    global ai_client
    
    # Only consider joining if:
    # 1. There's conversation history in this channel
    # 2. At least 2 other people have been talking
    
    channel_history = get_channel_history(channel_id)
    if len(channel_history) < 3:
        return False
    
    bot_username = get("bot.discord_username", "a_raphie").lower()
    recent_msgs = list(channel_history)[-5:]
    other_users = set()
    for msg_time, author, content in recent_msgs:
        if author.lower() != bot_username:
            other_users.add(author)
    
    if len(other_users) < 2:
        return False
    
    knowledge_context = knowledge_base.build_context(message_content, channel_id)
    has_knowledge_match = knowledge_context and len(knowledge_context) > 10
    
    join_chance = get("behavior.join_conversation_chance", 0.10)
    random_chance = random.random() < join_chance
    
    should_join = (has_knowledge_match and random_chance)
    
    if should_join:
        logger.info(f"[JOIN] Naturally joining conversation (knowledge match: {has_knowledge_match}, chance: {random_chance})")
    
    return should_join

async def generate_response(prompt, channel_id, history=None, max_retries=2):
    global ai_client
    
    if not ai_client:
        logger.warning("AI client not initialized")
        return None
    
    # Get relevant knowledge context
    knowledge_context = knowledge_base.build_context(prompt, channel_id)
    if knowledge_context:
        logger.info(f"[KNOWLEDGE] Using context: {knowledge_context[:80]}...")
    
    # Get learned style prompt
    style_prompt = style_tracker.get_style_prompt()
    if style_prompt:
        logger.info(f"[STYLE] Using learned style")
    
    # Build conversation context
    conversation_context = build_conversation_context(channel_id)
    if conversation_context:
        logger.info(f"[CONTEXT] Using {conversation_context['count']} recent messages")
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Generating response (attempt {attempt}/{max_retries})")
            
            messages = []
            messages.append({"role": "system", "content": get_instructions()})
            
            if style_prompt:
                messages.append({"role": "system", "content": style_prompt})
            
            if conversation_context:
                messages.append({
                    "role": "system",
                    "content": f"[RECENT CONVERSATION]\n{conversation_context['text']}"
                })
            
            if knowledge_context:
                messages.append({
                    "role": "system",
                    "content": f"[RELEVANT CHAT HISTORY - Use this info if helpful]\n{knowledge_context}"
                })
            
            messages.append({
                "role": "system", 
                "content": "Think about how to respond. Consider: Is this directed at me? What's the vibe? What would feel natural? Keep it short and authentic."
            })
            
            messages.append({"role": "user", "content": prompt})
            
            response = await asyncio.wait_for(
                ai_client.chat.completions.create(
                    model=get("ai.model", "google/gemma-3-27b-it"),
                    messages=messages,
                    max_tokens=random.randint(get("ai.max_tokens_min", 25), get("ai.max_tokens_max", 50)),
                    temperature=random.uniform(get("ai.temperature_min", 0.7), get("ai.temperature_max", 0.9))
                ),
                timeout=get("ai.timeout", 15)
            )
            
            if response and response.choices:
                result = response.choices[0].message.content
                
                if is_bad_response(result):
                    if attempt < max_retries:
                        logger.warning(f"Bad response detected: {result[:50]}")
                        continue
                    else:
                        return None
                
                if is_duplicate_response(0, result):
                    if attempt < max_retries:
                        logger.warning(f"Duplicate detected: {result[:50]}")
                        continue
                    else:
                        return None
                
                if result and not result[0].isupper():
                    result = result[0].upper() + result[1:]
                
                logger.info(f"Response generated: {result[:50]}")
                return result
            
        except asyncio.TimeoutError:
            logger.warning(f"AI timeout (attempt {attempt})")
            if attempt < max_retries:
                continue
            else:
                return None
        except Exception as e:
            logger.error(f"AI error (attempt {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                return None
    
    return None

# ============ EVENT HANDLERS ============
@client.event
async def on_ready():
    global processing_lock, ai_client
    processing_lock = asyncio.Lock()
    
    # Check for daily reset on startup
    check_daily_reset()
    
    logger.info(f"Bot ready: {client.user}")
    logger.info(f"Model: {get('ai.model', 'google/gemma-3-27b-it')}")
    logger.info(f"Context: {get('behavior.max_context', 30)} messages")
    
    try:
        stats = knowledge_base.get_stats()
        logger.info(f"[KNOWLEDGE] Loaded {stats['total']} facts from memory")
    except Exception as e:
        logger.warning(f"[KNOWLEDGE] Could not load stats: {e}")
    
    client.loop.create_task(process_queue())
    client.loop.create_task(maintenance_loop())

@client.event
async def on_message(message):
    global message_queue, total_replies
    
    # Skip own messages FIRST
    if message.author.id == client.user.id:
        return
    if message.author.bot:
        return
    if message.content.startswith('!'):
        return
    
    if is_paused():
        return
    
    if message.channel.id not in get_channels():
        logger.info(f"[DEBUG] Ignoring message - wrong channel: {message.channel.id}")
        return
    
    # Debug: log messages AFTER filters pass
    logger.info(f"[DEBUG] Got message from {message.author.name} in channel {message.channel.id}: {message.content[:50]}")
    
    if is_replied(message.id):
        logger.info(f"[DEBUG] Already replied to {message.id}")
        return
    
    if message.id in processing_set:
        logger.info(f"[DEBUG] Already processing {message.id}")
        return
    
    # Check for duplicate/similar message content
    if is_duplicate_message_content(message.content):
        logger.info(f"[DEBUG] Duplicate content detected: {message.content[:30]}...")
        return
    
    # Check if similar message already in queue
    for msg in message_queue:
        if msg.content.lower().strip() == message.content.lower().strip():
            logger.info(f"[DEBUG] Same message already in queue: {message.content[:30]}...")
            return
    
    # Scan message for knowledge extraction
    facts = knowledge_base.scan_message(message.content, message.author.name, message.channel.id)
    if facts:
        logger.info(f"[KNOWLEDGE] Stored {len(facts)} facts from {message.author.name}")
    
    # Auto-learn Q&A pairs from conversation
    await learn_from_conversation(message)
    
    # Track message for style learning
    style_tracker.add_message(message.content)
    if style_tracker.message_count_since_update == 0:
        logger.info(f"[STYLE] Updated style after {style_tracker.total_messages} messages")
    
    # Universal question handling - search for answers
    if is_question(message.content):
        # FIRST: Check if this question is directed at us
        mentions = message.mentions
        bot_id = client.user.id
        
        # Check if bot is mentioned
        bot_mentioned = any(m.id == bot_id for m in mentions)
        
        # Check if someone else was mentioned (question directed at them, not bot)
        other_mentioned = any(m.id != bot_id for m in mentions)
        
        # If someone else was mentioned but NOT bot → skip
        if other_mentioned and not bot_mentioned:
            logger.info(f"[QUESTION] Question directed at someone else, skipping")
            return
        
        # If no one was mentioned, check if conversation is between others
        if not bot_mentioned and not other_mentioned:
            recent_msgs = list(get_channel_history(message.channel.id))[-5:]
            other_users = set(author for _, author, _ in recent_msgs if author.lower() != get("bot.discord_username", "a_raphie").lower())
            
            if len(other_users) >= 2:
                # Multiple people talking - use AI decision
                should_answer = await should_respond(message.content, message.author.name, message.channel.id)
                if not should_answer:
                    return
        
        # Proceed with answer search (bot mentioned or open question)
        answer, source = await find_answer_for_question(message.content, message.channel.id)
        if answer:
            # Found answer - will respond with it
            logger.info(f"[QUESTION] Found answer from {source}: {answer[:30]}...")
            # Store pre-found answer for queue to use
            pre_found_answers[message.id] = (answer, source)
            # Add to conversation history
            channel_history = get_channel_history(message.channel.id)
            channel_history.append((time.time(), message.author.name, message.content))
            # Add to queue to respond with the answer
            mark_replied(message.id)
            processing_set.add(message.id)
            add_message_hash(message.content)
            context_stream.add(message)
            message_queue.append(message)
            logger.info(f"[MSG] {message.author.name}: {message.content[:40]}...")
            return
        else:
            # No answer found - skip the question
            logger.info(f"[QUESTION] No answer found, skipping")
            return
    
    # Check if message should trigger a response
    content_lower = message.content.lower()
    words = message.content.split()
    
    triggers = get_triggers()
    
    mentions = message.mentions
    bot_id = client.user.id
    
    bot_mentioned = any(m.id == bot_id for m in mentions)
    
    other_users_mentioned = any(m.id != bot_id for m in mentions)
    if other_users_mentioned and not bot_mentioned:
        is_tagged = False
    else:
        is_tagged = bot_mentioned
    
    emojis = re.findall(r'[\U0001F300-\U0001F9FF]', message.content)
    is_emoji_only = len(emojis) > 0 and len(words) <= 2
    
    if is_emoji_only and len(emojis) < 2:
        logger.info(f"[DEBUG] Skipping single emoji message")
        return
    
    greetings = triggers.get("greetings", [])
    has_greeting = any(g in content_lower for g in greetings)
    
    is_short = 1 <= len(words) <= 3 and not is_emoji_only
    
    direct_words = triggers.get("direct_words", [])
    has_direct = any(w in content_lower for w in direct_words)
    
    casual_words = triggers.get("casual_words", [])
    has_casual = any(w in content_lower for w in casual_words)
    
    bot_name = get("bot.name", "Raphie").lower()
    basic_trigger = is_tagged or has_greeting or is_short or has_direct or has_casual or bot_name in content_lower
    
    # Check if should naturally join a conversation between others
    wants_to_join = False
    if not basic_trigger:
        # Try to naturally join conversation
        wants_to_join = await should_naturally_join(message.content, message.channel.id)
        if wants_to_join:
            logger.info(f"[JOIN] Attempting to naturally join conversation...")
    
    if not basic_trigger and not wants_to_join:
        logger.info(f"[DEBUG] Not responding - doesn't match triggers")
        return
    
    # Add to conversation history for AI context
    channel_history = get_channel_history(message.channel.id)
    channel_history.append((time.time(), message.author.name, message.content))
    
    # Ask AI if we should respond (smart detection)
    # Skip this check if we're naturally joining
    if wants_to_join:
        logger.info(f"[JOIN] Skipping AI decision check - joining naturally")
        ai_says_respond = True
    else:
        ai_says_respond = await should_respond(message.content, message.author.name, message.channel.id)
    
    if not ai_says_respond:
        logger.info(f"[AI DECISION] Skipping - not directed at bot")
        return
    
    mark_replied(message.id)
    processing_set.add(message.id)
    add_message_hash(message.content)
    
    context_stream.add(message)
    message_queue.append(message)
    logger.info(f"[MSG] {message.author.name}: {message.content[:40]}...")

async def process_queue():
    global last_reply, total_replies, processing_lock
    
    while True:
        try:
            await asyncio.sleep(1)
            
            if processing_lock is None:
                continue
            
            current_time = time.time()
            time_since_reply = current_time - last_reply
            cooldown = get("behavior.cooldown", 60)
            
            if time_since_reply < cooldown:
                if len(message_queue) > 0:
                    logger.info(f"[QUEUE WAIT] {cooldown - time_since_reply:.0f}s left, {len(message_queue)} queued")
                continue
            
            if len(message_queue) == 0:
                continue
            
            async with processing_lock:
                if len(message_queue) == 0:
                    continue
                
                message = message_queue.popleft()
                
                # Skip messages older than 60 seconds
                message_age = time.time() - message.created_at.timestamp()
                if message_age > get("behavior.message_max_age", 60):
                    logger.info(f"[QUEUE] Skipping old message ({message_age:.0f}s): {message.content[:30]}...")
                    processing_set.discard(message.id)
                    continue
                
                print(f"\n[QUEUE] Processing: {message.content[:40]}...")
                
                context = context_stream.format_for_ai()
                history = []
                for msg in context_stream.messages:
                    history.append({"role": "user", "content": msg})
                
                # Check if we already found an answer for this question
                if message.id in pre_found_answers:
                    response, source = pre_found_answers[message.id]
                    logger.info(f"[Q&A] Using pre-found answer from {source}: {response[:50]}...")
                    del pre_found_answers[message.id]  # Clean up
                # Check Q&A store first for matching answer
                elif qa_match := qa_store.find_answer(message.content, channel_id=message.channel.id):
                    response = qa_match['answer']
                    logger.info(f"[Q&A] Found matching answer: {response[:50]}...")
                else:
                    response = await generate_response(
                        prompt=message.content,
                        channel_id=message.channel.id,
                        history=history
                    )
                
                if response is None:
                    logger.warning("AI failed to generate response, skipping...")
                    processing_set.discard(message.id)
                    continue
                
                # Check for similar responses we've already sent
                if is_similar_response(response):
                    logger.warning(f"Similar response already sent recently, skipping: {response[:30]}...")
                    processing_set.discard(message.id)
                    continue
                
                # Check self-awareness - don't repeat what bot said before
                if not check_self_awareness(response):
                    logger.warning(f"[SELF-AWARE] Response too similar to last, regenerating...")
                    processing_set.discard(message.id)
                    continue
                
                # Remove em dashes and emojis for more natural output
                response = response.replace('—', '').replace('–', '').strip()
                response = re.sub(r'[\U0001F300-\U0001F9FF]', '', response).strip()
                
                logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] Reply: {response[:50]}...")
                
                async with message.channel.typing():
                    await asyncio.sleep(random.randint(get("behavior.typing_time_min", 3), get("behavior.typing_time_max", 12)))
                    await message.reply(response, mention_author=False)
                logger.info("Reply sent")
                
                # Track this response to prevent duplicates
                add_sent_response(response)
                processing_set.discard(message.id)
                
                store_response(message.channel.id, response)
                
                last_reply = current_time
                total_replies += 1
                
                if total_replies % 10 == 0:
                    logger.info(f"Total replies today: {total_replies}")
                
        except asyncio.CancelledError:
            logger.info("Process queue stopped")
            break
        except Exception as e:
            logger.error(f"Critical error in process_queue: {e}")
            logger.exception("Full traceback:")
            await asyncio.sleep(5)
            continue

async def maintenance_loop():
    global ai_client
    
    while True:
        try:
            await asyncio.sleep(300)
            
            # Check for daily reset (midnight UTC)
            check_daily_reset()
            
            if ai_client and not check_ai_connection():
                logger.warning("AI connection unhealthy, reinitializing...")
                init_ai_client()
            
            try:
                deleted = knowledge_base.cleanup_old_facts()
                if deleted > 0:
                    logger.info(f"[KNOWLEDGE] Cleaned up {deleted} old facts")
            except Exception as e:
                logger.error(f"Knowledge cleanup error: {e}")
        except asyncio.CancelledError:
            logger.info("Maintenance loop stopped")
        except Exception as e:
            logger.error(f"Error in maintenance_loop: {e}")

# ============ MAIN ============
if __name__ == "__main__":
    try:
        init_ai_client()
        logger.info("Connecting to Discord...")
        client.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        cleanup_pid()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.exception("Full traceback:")
        cleanup_pid()
