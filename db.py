import os
import psycopg2


class Database:
    _initialized = False 

    def __init__(self, host, database, user, password, port):
        self.conn = psycopg2.connect(
            host=host,
            database=database,
            user=user,
            password=password,
            port=port
        )
        self.conn.autocommit = True

        self.init_db() # TODO: find better way to not re init db on every call
        

    def init_db(self):
        """Create table and ensure missing columns exist"""
        with self.conn.cursor() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS opportunities (
                    opp_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    title TEXT,
                    opp_type TEXT,
                    deadline TEXT,
                    priority TEXT,
                    description TEXT,
                    message_text TEXT,
                    archived INTEGER DEFAULT 0,
                    done INTEGER DEFAULT 0
                )
            ''')
            
            c.execute("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS link TEXT")
            c.execute("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS missed_notified INTEGER DEFAULT 0")

    # ---------------- CRUD Methods ---------------- #

    def add_opportunity(self, opp_id, user_id, title, opp_type, deadline, priority, desc, message_text, link=None):
        with self.conn.cursor() as c:
            c.execute('''
                INSERT INTO opportunities 
                (opp_id, user_id, title, opp_type, deadline, priority, description, message_text, link) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (opp_id, user_id, title, opp_type, deadline, priority, desc, message_text, link))

    def get_missed_opportunities(self, now_iso):
        with self.conn.cursor() as c:
            c.execute('''
                SELECT user_id, opp_id, title, description, opp_type, link, deadline 
                FROM opportunities 
                WHERE deadline < %s AND archived = 0 AND done = 0 AND missed_notified = 0
            ''', (now_iso,))
            return c.fetchall()

    def mark_missed_notified(self, opp_id):
        with self.conn.cursor() as c:
            c.execute('UPDATE opportunities SET missed_notified = 1 WHERE opp_id = %s', (opp_id,))

    def mark_done(self, opp_id, user_id):
        with self.conn.cursor() as c:
            c.execute('UPDATE opportunities SET done=1, archived=1 WHERE opp_id = %s AND user_id = %s', (opp_id, user_id))
            return c.rowcount

    def get_active_opportunities(self, user_id):
        with self.conn.cursor() as c:
            c.execute('''
                SELECT opp_id, title, opp_type, deadline, priority, description 
                FROM opportunities 
                WHERE user_id = %s AND archived = 0 AND done = 0 ORDER BY deadline
            ''', (user_id,))
            return c.fetchall()

    def delete_opportunity(self, opp_id, user_id):
        with self.conn.cursor() as c:
            c.execute('DELETE FROM opportunities WHERE opp_id = %s AND user_id = %s', (opp_id, user_id))
            return c.rowcount

    def archive_opportunity(self, opp_id, user_id):
        with self.conn.cursor() as c:
            c.execute('UPDATE opportunities SET archived=1 WHERE opp_id = %s AND user_id = %s', (opp_id, user_id))
            return c.rowcount

    def get_weekly_summary(self, user_id, now_iso, week_end_iso):
        with self.conn.cursor() as c:
            c.execute('''
                SELECT COUNT(*) as count, opp_type FROM opportunities 
                WHERE user_id = %s AND deadline >= %s AND deadline <= %s AND archived=0 AND done=0 
                GROUP BY opp_type
            ''', (user_id, now_iso, week_end_iso))
            return c.fetchall()

    def get_all_active_reminders(self):
        with self.conn.cursor() as c:
            c.execute('''
                SELECT user_id, opp_id, title, deadline, priority, description, opp_type, link 
                FROM opportunities 
                WHERE archived = 0 AND done = 0
            ''')
            return c.fetchall()