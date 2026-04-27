import sqlite3
import re
import time
from collections import deque
from difflib import SequenceMatcher

DB_FILE = "config/knowledge.db"
MAX_FACTS_PER_MESSAGE = 4
MIN_IMPORTANCE_SCORE = 3
MAX_CONTEXT_FACTS = 5
EXPIRY_DAYS = 7
QA_SIMILARITY_THRESHOLD = 0.6

class KnowledgeBase:
    def __init__(self):
        self.init_db()
        self.recent_mentions = deque(maxlen=50)
    
    def init_db(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY,
            content TEXT,
            category TEXT,
            source_user TEXT,
            source_message TEXT,
            created_at REAL,
            last_used REAL,
            use_count INTEGER DEFAULT 0,
            is_permanent INTEGER DEFAULT 0
        )""")
        
        c.execute("""CREATE TABLE IF NOT EXISTS qa_pairs (
            id INTEGER PRIMARY KEY,
            question TEXT,
            answer TEXT,
            source_user TEXT,
            source_message TEXT,
            created_at REAL,
            last_used REAL,
            use_count INTEGER DEFAULT 0
        )""")
        
        conn.commit()
        conn.close()
    
    def score_message(self, message_content):
        score = 0
        reasons = []
        
        content = message_content.strip()
        words = content.split()
        
        if len(words) < 3:
            return 0, []
        
        conversational = ['gm', 'gn', 'lol', 'haha', 'hey', 'hi', 'hello', 'good morning', 'good night', '👍', '👋', '🔥', '💯', '😂']
        if any(c in content.lower() for c in conversational) and len(words) < 5:
            score -= 5
            reasons.append("conversational")
        
        if re.search(r'\$[\d,]+(?:\.\d+)?', content):
            score += 3
            reasons.append("price/money")
        
        if re.search(r'\d+%', content):
            score += 2
            reasons.append("percentage")
        
        if re.search(r'0x[a-fA-F0-9]{20,}', content):
            score += 3
            reasons.append("wallet address")
        
        discord_invite = re.search(r'discord\.gg/[a-zA-Z0-9]+', content)
        if discord_invite:
            score += 4
            reasons.append("discord invite")
        
        url = re.search(r'https?://[^\s]+', content)
        if url:
            score += 2
            reasons.append("URL")
        
        social_handle = re.search(r'@[\w]{3,30}', content)
        if social_handle:
            score += 2
            reasons.append("social handle")
        
        version = re.search(r'\bv\d+\.\d+(?:\.\d+)?\b', content)
        if version:
            score += 2
            reasons.append("version number")
        
        crypto_terms = ['btc', 'eth', 'sol', 'bitcoin', 'ethereum', 'solana', 'token', 'coin', 'crypto', 'defi', 'nft', 'airdop', 'presale', 'listing', 'launch', 'pump', 'dump', 'bullish', 'bearish', 'hold', 'buy', 'sell', 'trade']
        if any(term in content.lower() for term in crypto_terms):
            score += 2
            reasons.append("crypto term")
        
        important_keywords = ['important', 'remember', 'note', 'warning', 'announcement', 'rules', 'faq', ' pinned']
        if any(kw in content.lower() for kw in important_keywords):
            score += 3
            reasons.append("important keyword")
        
        if '?' in content:
            score += 1
            reasons.append("question detected")
        
        project_patterns = [
            r'\b[A-Z][a-z]+(?:\s?[A-Z][a-z]+)*\b',
            r'\b[a-z]+\.(io|com|net|org)\b',
        ]
        for pattern in project_patterns:
            matches = re.findall(pattern, content)
            if len(matches) > 0:
                score += len(matches)
                reasons.append(f"project names: {len(matches)}")
        
        if len(content) > 20:
            score += 1
        
        if '?' not in content and len(words) > 3:
            score += 1
            reasons.append("statement not question")
        
        return max(0, score), reasons
    
    def extract_facts(self, message_content, author_name):
        score, reasons = self.score_message(message_content)
        
        if score < MIN_IMPORTANCE_SCORE:
            return []
        
        facts = []
        content = message_content.strip()
        
        price_matches = re.findall(r'\$[\d,]+(?:\.\d+)?', content)
        for match in price_matches[:2]:
            facts.append({
                'content': f"Price mentioned: {match}",
                'category': 'number',
                'score': 3
            })
        
        percentage_matches = re.findall(r'\d+%', content)
        for match in percentage_matches[:2]:
            facts.append({
                'content': f"Percentage: {match}",
                'category': 'number',
                'score': 2
            })
        
        wallet_match = re.search(r'(0x[a-fA-F0-9]{20,})', content)
        if wallet_match:
            facts.append({
                'content': f"Wallet address: {wallet_match.group(1)[:10]}...",
                'category': 'wallet',
                'score': 3
            })
        
        discord_invite = re.search(r'(discord\.gg/[a-zA-Z0-9]+)', content)
        if discord_invite:
            facts.append({
                'content': f"Discord invite: {discord_invite.group(1)}",
                'category': 'link',
                'score': 4
            })
        
        url_matches = re.findall(r'(https?://[^\s]+)', content)
        for url in url_matches[:2]:
            if 'discord' not in url.lower():
                facts.append({
                    'content': f"Link: {url[:50]}...",
                    'category': 'link',
                    'score': 2
                })
        
        social_handles = re.findall(r'(@[\w]{3,30})', content)
        for handle in social_handles[:2]:
            facts.append({
                'content': f"Social: {handle}",
                'category': 'social',
                'score': 2
            })
        
        version_matches = re.findall(r'(\bv\d+\.\d+(?:\.\d+)?\b)', content)
        for ver in version_matches[:2]:
            facts.append({
                'content': f"Version: {ver}",
                'category': 'version',
                'score': 2
            })
        
        words = content.split()
        important_words = []
        for word in words:
            if len(word) > 3 and word.lower() not in ['that', 'this', 'with', 'from', 'have', 'been', 'just', 'like', 'would', 'could', 'should', 'think', 'really', 'about']:
                important_words.append(word)
        
        if important_words:
            topic = ' '.join(important_words[:4])
            if len(topic) > 5:
                facts.append({
                    'content': topic,
                    'category': 'topic',
                    'score': score
                })
        
        return facts[:MAX_FACTS_PER_MESSAGE]
    
    def store_facts(self, facts, author_name, message_content, is_permanent=0, channel_id=None):
        if not facts:
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        for fact in facts:
            try:
                c.execute("""INSERT INTO knowledge (content, category, source_user, source_message, created_at, last_used, use_count, is_permanent, channel_id)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (fact['content'], fact['category'], author_name, message_content[:100], time.time(), time.time(), is_permanent, channel_id)
                )
            except:
                pass
        
        conn.commit()
        conn.close()
    
    def store_permanent_fact(self, content, category, source_user="system", source_message="", channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO knowledge (content, category, source_user, source_message, created_at, last_used, use_count, is_permanent, channel_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)""",
                (content, category, source_user, source_message[:100], time.time(), time.time(), channel_id)
            )
            conn.commit()
        except:
            pass
        finally:
            conn.close()
    
    def scan_message(self, message_content, author_name, channel_id=None):
        facts = self.extract_facts(message_content, author_name)
        if facts:
            self.store_facts(facts, author_name, message_content, channel_id=channel_id)
            return facts
        return []
    
    def get_relevant_facts(self, query, limit=MAX_CONTEXT_FACTS, channel_id=None):
        query_lower = query.lower()
        query_words = set(query_lower.split())
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        if channel_id:
            c.execute("""SELECT content, category, use_count FROM knowledge 
                      WHERE channel_id = ? AND (created_at > ? OR is_permanent = 1)""", 
                      (channel_id, time.time() - (EXPIRY_DAYS * 86400),))
        else:
            c.execute("""SELECT content, category, use_count FROM knowledge 
                      WHERE (created_at > ? OR is_permanent = 1)""", 
                      (time.time() - (EXPIRY_DAYS * 86400),))
        all_facts = c.fetchall()
        conn.close()
        
        scored_facts = []
        for content, category, use_count in all_facts:
            content_lower = content.lower()
            matches = sum(1 for w in query_words if w in content_lower)
            relevance_score = matches + (use_count * 0.1)
            
            if matches > 0:
                scored_facts.append({
                    'content': content,
                    'category': category,
                    'score': relevance_score
                })
        
        scored_facts.sort(key=lambda x: x['score'], reverse=True)
        return scored_facts[:limit]
    
    def build_context(self, user_message, channel_id=None):
        facts = self.get_relevant_facts(user_message, channel_id=channel_id)
        
        if not facts:
            return ""
        
        context_lines = []
        for fact in facts:
            if fact['category'] == 'number':
                context_lines.append(f"- {fact['content']}")
            elif fact['category'] == 'wallet':
                context_lines.append(f"- {fact['content']}")
            else:
                context_lines.append(f"- {fact['content']}")
        
        return "\n".join(context_lines)
    
    def cleanup_old_facts(self, channel_id=None):
        cutoff = time.time() - (EXPIRY_DAYS * 86400)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if channel_id:
            c.execute("DELETE FROM knowledge WHERE channel_id = ? AND created_at < ? AND is_permanent = 0", (channel_id, cutoff))
        else:
            c.execute("DELETE FROM knowledge WHERE created_at < ? AND is_permanent = 0", (cutoff,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted
    
    def get_stats(self, channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if channel_id:
            c.execute("SELECT COUNT(*) FROM knowledge WHERE channel_id = ? AND (created_at > ? OR is_permanent = 1)", 
                      (channel_id, time.time() - (EXPIRY_DAYS * 86400),))
        else:
            c.execute("SELECT COUNT(*) FROM knowledge WHERE created_at > ? OR is_permanent = 1", 
                      (time.time() - (EXPIRY_DAYS * 86400),))
        total = c.fetchone()[0]
        
        if channel_id:
            c.execute("SELECT category, COUNT(*) FROM knowledge WHERE channel_id = ? AND (created_at > ? OR is_permanent = 1) GROUP BY category",
                      (channel_id, time.time() - (EXPIRY_DAYS * 86400),))
        else:
            c.execute("SELECT category, COUNT(*) FROM knowledge WHERE created_at > ? OR is_permanent = 1 GROUP BY category",
                      (time.time() - (EXPIRY_DAYS * 86400),))
        by_category = dict(c.fetchall())
        conn.close()
        
        return {'total': total, 'by_category': by_category}


class QAStore:
    def __init__(self):
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS qa_pairs (
            id INTEGER PRIMARY KEY,
            question TEXT,
            answer TEXT,
            source_user TEXT,
            source_message TEXT,
            created_at REAL,
            last_used REAL,
            use_count INTEGER DEFAULT 0
        )""")
        conn.commit()
        conn.close()
    
    def _similarity(self, s1, s2):
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()
    
    def store_qa(self, question, answer, source_user="unknown", source_message="", channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO qa_pairs (question, answer, source_user, source_message, created_at, last_used, use_count, channel_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (question, answer, source_user, source_message[:100], time.time(), time.time(), channel_id)
            )
            conn.commit()
            return True
        except:
            return False
        finally:
            conn.close()
    
    def find_answer(self, user_question, threshold=QA_SIMILARITY_THRESHOLD, channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if channel_id:
            c.execute("SELECT id, question, answer, use_count FROM qa_pairs WHERE channel_id = ?", (channel_id,))
        else:
            c.execute("SELECT id, question, answer, use_count FROM qa_pairs")
        all_qa = c.fetchall()
        conn.close()
        
        best_match = None
        best_score = 0
        
        for qa_id, question, answer, use_count in all_qa:
            score = self._similarity(user_question, question)
            adjusted_score = score + (use_count * 0.05)
            
            if adjusted_score > best_score and adjusted_score >= threshold:
                best_score = adjusted_score
                best_match = {
                    'id': qa_id,
                    'question': question,
                    'answer': answer,
                    'score': adjusted_score
                }
        
        if best_match:
            self._increment_use(best_match['id'])
        
        return best_match
    
    def _increment_use(self, qa_id):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE qa_pairs SET use_count = use_count + 1, last_used = ? WHERE id = ?",
                  (time.time(), qa_id))
        conn.commit()
        conn.close()
    
    def get_all_qa(self, channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if channel_id:
            c.execute("SELECT question, answer, use_count FROM qa_pairs WHERE channel_id = ? ORDER BY use_count DESC LIMIT 20", (channel_id,))
        else:
            c.execute("SELECT question, answer, use_count FROM qa_pairs ORDER BY use_count DESC LIMIT 20")
        results = c.fetchall()
        conn.close()
        return [{'question': q, 'answer': a, 'uses': u} for q, a, u in results]
    
    def get_qa_count(self, channel_id=None):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if channel_id:
            c.execute("SELECT COUNT(*) FROM qa_pairs WHERE channel_id = ?", (channel_id,))
        else:
            c.execute("SELECT COUNT(*) FROM qa_pairs")
        count = c.fetchone()[0]
        conn.close()
        return count


class StyleTracker:
    def __init__(self, max_messages=500):
        self.max_messages = max_messages
        self.recent_messages = deque(maxlen=max_messages)
        self.abbreviation_counts = Counter()
        self.abbreviations = ['gm', 'gn', 'wbu', 'hbu', 'fr', 'ngl', 'imo', 'lol', 'btw', 'ong', 'nvm', 'tbh', 'rn', 'dm', 'idk']
        self.total_words = 0
        self.total_messages = 0
        self.emoji_count = 0
        self.message_count_since_update = 0
        self.current_style_prompt = ""
        
    def add_message(self, content):
        if not content or len(content.strip()) < 2:
            return
        
        self.recent_messages.append(content)
        self.total_messages += 1
        self.message_count_since_update += 1
        
        words = content.split()
        self.total_words += len(words)
        
        content_lower = content.lower()
        for abbr in self.abbreviations:
            if abbr in content_lower:
                self.abbreviation_counts[abbr] += 1
        
        emojis = re.findall(r'[\U0001F300-\U0001F9FF]', content)
        self.emoji_count += len(emojis)
        
        if self.message_count_since_update >= self.max_messages:
            self.update_style_prompt()
    
    def get_avg_message_length(self):
        if self.total_messages == 0:
            return 0
        return self.total_words / self.total_messages
    
    def get_top_abbreviations(self, top_n=5):
        return [abbr for abbr, count in self.abbreviation_counts.most_common(top_n) if count > 0]
    
    def get_emoji_ratio(self):
        if self.total_messages == 0:
            return 0
        return self.emoji_count / self.total_messages
    
    def extract_common_phrases(self, n=2, top_n=5):
        if len(self.recent_messages) < 10:
            return []
        
        ngrams = Counter()
        for msg in self.recent_messages:
            words = msg.lower().split()
            for i in range(len(words) - n + 1):
                phrase = ' '.join(words[i:i+n])
                if len(phrase) > 3:
                    ngrams[phrase] += 1
        
        return [phrase for phrase, count in ngrams.most_common(top_n) if count >= 2]
    
    def analyze_style(self):
        avg_length = self.get_avg_message_length()
        top_abbrs = self.get_top_abbreviations()
        common_phrases = self.extract_common_phrases()
        emoji_ratio = self.get_emoji_ratio()
        
        return {
            'avg_words': avg_length,
            'slang': top_abbrs,
            'phrases': common_phrases,
            'emoji_heavy': emoji_ratio > 0.3,
            'emoji_ratio': emoji_ratio
        }
    
    def get_example_messages(self, count=3):
        examples = []
        recent = list(self.recent_messages)[-20:]
        for msg in recent:
            if len(msg) > 5 and len(msg) < 100:
                examples.append(msg)
                if len(examples) >= count:
                    break
        return '\n'.join(examples)
    
    def update_style_prompt(self):
        if self.total_messages < 50:
            self.current_style_prompt = ""
            self.message_count_since_update = 0
            return
        
        style = self.analyze_style()
        example_msgs = self.get_example_messages(3)
        
        emoji_usage = "Heavy" if style['emoji_heavy'] else "Light"
        
        self.current_style_prompt = f"""CHAT STYLE (learned from this community):
- Average message length: {style['avg_words']:.1f} words
- Common slang: {', '.join(style['slang']) if style['slang'] else 'none yet'}
- Phrases: {', '.join(style['phrases']) if style['phrases'] else 'none yet'}
- Emoji usage: {emoji_usage}
- IMPORTANT: Use slang like 'fr' sparingly - not every response

Examples of how people chat here:
{example_msgs}

When responding, sound like the examples above - casual, like one of the group."""
        
        self.message_count_since_update = 0
    
    def get_style_prompt(self):
        if not self.current_style_prompt and self.total_messages >= 50:
            self.update_style_prompt()
        return self.current_style_prompt
    
    def get_message_count(self):
        return self.total_messages

from collections import Counter

style_tracker = StyleTracker(max_messages=500)
knowledge_base = KnowledgeBase()
qa_store = QAStore()
