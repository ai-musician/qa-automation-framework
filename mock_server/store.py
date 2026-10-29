"""
Where the API keeps its users and transactions: Postgres when DATABASE_URL is set, otherwise Python dicts.

    DATABASE_URL=postgresql://qa:...@db:5432/finpay    Postgres (docker-compose.yml starts one, in its own container)
    not set                                             in memory: a plain `pytest`, Kubernetes and the AWS QA environment

Both stores have the same methods and return the same dicts, so mock_server/app.py doesn't know which one it has.
"""
from datetime import timezone
from pathlib import Path
import threading

SCHEMA = Path(__file__).with_name("schema.sql")


class EmailTaken(Exception):
    """Another user already has this email (case-insensitive)."""


def connect(database_url, seed_users):
    store = PostgresStore(database_url) if database_url else MemoryStore()
    store.seed(seed_users)
    return store


class MemoryStore:
    def __init__(self):
        self.users: dict[str, dict] = {}            # user id -> user
        self.transactions: dict[str, dict] = {}     # transaction id -> transaction
        self.lock = threading.Lock()                # check-then-write as one step, as the database's unique index does

    def seed(self, seed_users):
        self.users.update({user["id"]: dict(user) for user in seed_users})

    def _email_taken(self, email, except_id=None):
        return any(u["email"].lower() == email.lower() and u["id"] != except_id for u in self.users.values())

    def email_taken(self, email, except_id=None):
        return self._email_taken(email, except_id)

    def get_user(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def list_users(self):
        return [dict(u) for u in self.users.values()]

    def add_user(self, user):
        with self.lock:
            if self._email_taken(user["email"]):
                raise EmailTaken
            self.users[user["id"]] = dict(user)

    def update_user(self, user_id, changes):
        """The updated user, or None if there is no such user."""
        with self.lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            if "email" in changes and self._email_taken(changes["email"], except_id=user_id):
                raise EmailTaken
            user.update(changes)
            return dict(user)

    def delete_user(self, user_id):
        """True if the user existed. Their transactions go too, as in Postgres."""
        with self.lock:
            if self.users.pop(user_id, None) is None:
                return False
            for tx_id in [t["id"] for t in self.transactions.values() if t["userId"] == user_id]:
                del self.transactions[tx_id]
            return True

    def add_transaction(self, tx):
        self.transactions[tx["id"]] = dict(tx)

    def transactions_for(self, user_id):
        return [dict(t) for t in self.transactions.values() if t["userId"] == user_id]


class PostgresStore:
    # API field -> column; also the only fields an update may set
    USER_COLUMNS = {"name": "name", "email": "email", "accountType": "account_type"}

    def __init__(self, database_url):
        # imported here: the in-memory store must work without the Postgres driver installed
        from psycopg import errors
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.UniqueViolation = errors.UniqueViolation
        # A pool of open connections, shared by the server's threads: opening a new connection for every request would
        # add several milliseconds to each one, which the load tests would measure.
        self.pool = ConnectionPool(database_url, min_size=2, max_size=20, kwargs={"row_factory": dict_row}, open=True)
        self.pool.wait(timeout=30)
        with self.pool.connection() as conn:
            conn.execute(SCHEMA.read_text())

    def seed(self, seed_users):
        with self.pool.connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO users (id, name, email, account_type, created_at)"
                " VALUES (%(id)s, %(name)s, %(email)s, %(accountType)s, %(createdAt)s::timestamptz)"
                " ON CONFLICT (id) DO NOTHING", seed_users)

    @staticmethod
    def _iso(dt):
        """2026-10-19T08:15:00.123Z, the format the in-memory store keeps (app.now())"""
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _user(self, row):
        return {"id": row["id"], "name": row["name"], "email": row["email"],
                "accountType": row["account_type"], "createdAt": self._iso(row["created_at"])}

    def _transaction(self, row):
        return {"id": row["id"], "userId": row["user_id"], "amount": float(row["amount"]), "type": row["type"],
                "recipientId": row["recipient_id"], "status": row["status"], "createdAt": self._iso(row["created_at"])}

    def email_taken(self, email, except_id=None):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE lower(email) = lower(%s) AND id IS DISTINCT FROM %s",
                               (email, except_id)).fetchone()
        return row is not None

    def get_user(self, user_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return self._user(row) if row else None

    def list_users(self):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY seq").fetchall()
        return [self._user(row) for row in rows]

    def add_user(self, user):
        try:
            with self.pool.connection() as conn:
                conn.execute("INSERT INTO users (id, name, email, account_type, created_at)"
                             " VALUES (%(id)s, %(name)s, %(email)s, %(accountType)s, %(createdAt)s::timestamptz)", user)
        except self.UniqueViolation:            # the unique index on lower(email): someone got there first
            raise EmailTaken from None

    def update_user(self, user_id, changes):
        sets = ", ".join(f"{self.USER_COLUMNS[field]} = %({field})s" for field in changes)
        try:
            with self.pool.connection() as conn:
                row = conn.execute(f"UPDATE users SET {sets} WHERE id = %(id)s RETURNING *",
                                   {**changes, "id": user_id}).fetchone()
        except self.UniqueViolation:
            raise EmailTaken from None
        return self._user(row) if row else None

    def delete_user(self, user_id):
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM users WHERE id = %s", (user_id,)).rowcount == 1

    def add_transaction(self, tx):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO transactions (id, user_id, amount, type, recipient_id, status, created_at)"
                         " VALUES (%(id)s, %(userId)s, %(amount)s, %(type)s, %(recipientId)s, %(status)s,"
                         " %(createdAt)s::timestamptz)", tx)

    def transactions_for(self, user_id):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM transactions WHERE user_id = %s ORDER BY seq", (user_id,)).fetchall()
        return [self._transaction(row) for row in rows]
