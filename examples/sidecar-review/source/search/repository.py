"""The repository layer. SQL belongs here, and R1 says so."""


class SearchRepository:
    def __init__(self, conn):
        self._conn = conn

    def matching(self, query, limit):
        return self._conn.execute(
            "SELECT id, title FROM documents WHERE title LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
