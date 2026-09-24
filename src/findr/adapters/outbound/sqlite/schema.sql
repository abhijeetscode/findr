-- Findr SQLite schema — the part SQLAlchemy's ORM can't express.
--
-- Regular tables (users, sessions, documents, ...) are ORM models in
-- models.py, created via Base.metadata.create_all(). SQLite FTS5 virtual
-- tables and triggers aren't representable as ORM models, so they live here
-- as raw SQL, applied by db.init_db() right after create_all().
--
-- documents_fts is an "external content" FTS5 table: it doesn't store the
-- indexed text itself, just a search index over the documents table's rows
-- (content_rowid='id' ties each FTS row to documents.id / SQLite rowid).
-- The triggers below are what keep it in sync — the repository's upserts
-- must use INSERT ... ON CONFLICT DO UPDATE (not INSERT OR REPLACE), since
-- REPLACE's implicit delete doesn't reliably fire the AFTER DELETE trigger.

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    subject, sender, body_text, content='documents', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, subject, sender, body_text)
    VALUES (new.id, new.subject, new.sender, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, subject, sender, body_text)
    VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, subject, sender, body_text)
    VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
    INSERT INTO documents_fts(rowid, subject, sender, body_text)
    VALUES (new.id, new.subject, new.sender, new.body_text);
END;
