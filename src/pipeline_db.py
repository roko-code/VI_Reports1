"""
Infrastructure module for LLM call logging, checkpointing, and run management.
"""
import sqlite3
import json
import time
import uuid
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
import threading


class PipelineDB:
    """SQLite database for run management, checkpointing, and call logging."""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=30000")
        return self._local.conn
    
    def _init_schema(self):
        """Initialize database schema."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                extractor TEXT NOT NULL,
                company TEXT NOT NULL,
                fiscal_year INTEGER NOT NULL,
                as_reported_in_doc TEXT,
                is_current BOOLEAN DEFAULT 1,
                field TEXT NOT NULL,
                value REAL,
                value_text TEXT,
                unit TEXT NOT NULL,
                unit_scale TEXT NOT NULL,
                currency TEXT NOT NULL,
                basis TEXT NOT NULL CHECK (basis IN ('statutory','realised','target','accounting','disclosed')),
                entity TEXT,
                value_aud REAL,
                fx_rate REAL,
                fx_rate_source_page INTEGER,
                source_doc_id TEXT NOT NULL,
                source_pdf_page INTEGER NOT NULL,
                printed_page INTEGER NOT NULL,
                quote_snippet TEXT,
                extractor_version TEXT NOT NULL,
                extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, extractor, company, fiscal_year, field, entity, as_reported_in_doc)
            );
            
            CREATE TABLE IF NOT EXISTS validation_flags (
                id INTEGER PRIMARY KEY,
                fact_id INTEGER NOT NULL REFERENCES facts(id),
                rule TEXT NOT NULL,
                expected TEXT,
                actual TEXT,
                severity TEXT CHECK (severity IN ('error','warning','info')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS extraction_checkpoints (
                run_id TEXT NOT NULL,
                extractor TEXT NOT NULL,
                fiscal_year INTEGER NOT NULL,
                status TEXT CHECK (status IN ('pending','running','completed','failed')),
                attempt INTEGER DEFAULT 0,
                result_json TEXT,
                error TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                PRIMARY KEY (run_id, extractor, fiscal_year)
            );
            
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                extractor TEXT NOT NULL,
                fiscal_year INTEGER,
                model TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                latency_ms INTEGER,
                status TEXT CHECK (status IN ('success','error','retry','fallback')),
                error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY,
                doc_id TEXT NOT NULL,
                pdf_page_idx INTEGER NOT NULL,
                printed_page INTEGER NOT NULL,
                text TEXT,
                tables_json TEXT,
                content_hash TEXT NOT NULL,
                UNIQUE(doc_id, pdf_page_idx)
            );
            
            CREATE INDEX IF NOT EXISTS idx_facts_run ON facts(run_id, extractor, fiscal_year);
            CREATE INDEX IF NOT EXISTS idx_facts_company ON facts(company, fiscal_year, field);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON extraction_checkpoints(run_id, extractor, fiscal_year);
            CREATE INDEX IF NOT EXISTS idx_call_log_run ON call_log(run_id, extractor, fiscal_year);
            CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(doc_id, pdf_page_idx);
        """)
    
    @contextmanager
    def transaction(self):
        """Context manager for transactions."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    # Run management
    def create_run(self, run_id: str = None) -> str:
        """Create a new run ID."""
        if run_id is None:
            run_id = str(uuid.uuid4())[:8]
        return run_id
    
    # Checkpoint management
    def init_checkpoints(self, run_id: str, extractors: List[str], years: List[int]):
        """Initialize checkpoints for all extractor/year combinations."""
        with self.transaction() as conn:
            for extractor in extractors:
                for year in years:
                    conn.execute("""
                        INSERT OR IGNORE INTO extraction_checkpoints 
                        (run_id, extractor, fiscal_year, status, attempt)
                        VALUES (?, ?, ?, 'pending', 0)
                    """, (run_id, extractor, year))
    
    def start_checkpoint(self, run_id: str, extractor: str, year: int) -> int:
        """Mark checkpoint as running, return attempt number."""
        with self.transaction() as conn:
            cursor = conn.execute("""
                UPDATE extraction_checkpoints 
                SET status='running', attempt=attempt+1, started_at=datetime('now')
                WHERE run_id=? AND extractor=? AND fiscal_year=?
                RETURNING attempt
            """, (run_id, extractor, year))
            row = cursor.fetchone()
            return row[0] if row else 1
    
    def complete_checkpoint(self, run_id: str, extractor: str, year: int, result: dict = None, error: str = None):
        """Mark checkpoint as completed or failed."""
        status = 'failed' if error else 'completed'
        with self.transaction() as conn:
            conn.execute("""
                UPDATE extraction_checkpoints 
                SET status=?, result_json=?, error=?, completed_at=datetime('now')
                WHERE run_id=? AND extractor=? AND fiscal_year=?
            """, (status, json.dumps(result) if result else None, error, run_id, extractor, year))
    
    def get_checkpoint_status(self, run_id: str, extractor: str, year: int) -> Optional[dict]:
        """Get checkpoint status."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT status, attempt, result_json, error, started_at, completed_at
            FROM extraction_checkpoints
            WHERE run_id=? AND extractor=? AND fiscal_year=?
        """, (run_id, extractor, year))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def get_pending_checkpoints(self, run_id: str) -> List[dict]:
        """Get all pending checkpoints for a run."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT extractor, fiscal_year, attempt, status
            FROM extraction_checkpoints
            WHERE run_id=? AND status IN ('pending', 'running', 'failed')
            ORDER BY extractor, fiscal_year
        """, (run_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_completed_checkpoints(self, run_id: str, extractor: str) -> List[int]:
        """Get completed years for an extractor."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT fiscal_year FROM extraction_checkpoints
            WHERE run_id=? AND extractor=? AND status='completed'
        """, (run_id, extractor))
        return [row[0] for row in cursor.fetchall()]
    
    # Call logging
    def log_call(self, run_id: str, extractor: str, fiscal_year: Optional[int],
                 model: str, prompt_tokens: int, completion_tokens: int,
                 latency_ms: int, status: str, error: str = None):
        """Log an LLM call."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO call_log 
                (run_id, extractor, fiscal_year, model, prompt_tokens, 
                 completion_tokens, latency_ms, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, extractor, fiscal_year, model, 
                  prompt_tokens, completion_tokens, latency_ms, status, error))
    
    def get_call_stats(self, run_id: str) -> Dict[str, Any]:
        """Get call statistics for a run."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT 
                COUNT(*) as total_calls,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successes,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN status='fallback' THEN 1 ELSE 0 END) as fallbacks,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                AVG(latency_ms) as avg_latency_ms
            FROM call_log WHERE run_id=?
        """, (run_id,))
        row = cursor.fetchone()
        return dict(row) if row else {}
    
    # Call log metadata (for _call_llm)
    def write_call_metadata(self, supporting_data_dir: Path, call_name: str, metadata: dict):
        """Write LLM call metadata to JSON file."""
        supporting_data_dir.mkdir(parents=True, exist_ok=True)
        meta_file = supporting_data_dir / f"llm_call_{call_name}.json"
        with open(meta_file, 'w') as f:
            json.dump(metadata, f, indent=2)
    
    # Page storage
    def store_page(self, doc_id: str, pdf_page_idx: int, printed_page: int,
                   text: str, tables: list, content_hash: str):
        with self.transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pages 
                (doc_id, pdf_page_idx, printed_page, text, tables_json, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (doc_id, printed_page, text, json.dumps(tables), content_hash))
    
    def get_pages(self, doc_id: str) -> List[dict]:
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT pdf_page_idx, printed_page, text, tables_json
            FROM pages WHERE doc_id=? ORDER BY pdf_page_idx
        """, (doc_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    # Fact storage
    def insert_fact(self, fact: dict) -> int:
        with self.transaction() as conn:
            cursor = conn.execute("""
                INSERT INTO facts
                (run_id, extractor, company, fiscal_year, as_reported_in_doc, is_current,
                 field, value, value_text, unit, unit_scale, currency, basis, entity,
                 value_aud, fx_rate, fx_rate_source_page, source_doc_id, source_pdf_page,
                 printed_page, quote_snippet, extractor_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fact['run_id'], fact['extractor'], fact['company'], fact['fiscal_year'],
                fact.get('as_reported_in_doc'), fact.get('is_current', 1),
                fact['field'], fact.get('value'), fact.get('value_text'),
                fact['unit'], fact['unit_scale'], fact['currency'], fact['basis'],
                fact.get('entity'), fact.get('value_aud'), fact.get('fx_rate'),
                fact.get('fx_rate_source_page'), fact['source_doc_id'],
                fact['source_pdf_page'], fact['printed_page'], fact.get('quote_snippet'),
                fact['extractor_version']
            ))
            return cursor.lastrowid
    
    def insert_validation_flag(self, fact_id: int, rule: str, expected: str,
                                actual: str, severity: str):
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO validation_flags (fact_id, rule, expected, actual, severity)
                VALUES (?, ?, ?, ?, ?)
            """, (fact_id, rule, expected, actual, severity))
    
    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


def get_db(supporting_data_dir: Path) -> PipelineDB:
    """Get or create database instance."""
    db_path = supporting_data_dir / "pipeline.db"
    return PipelineDB(db_path)


def init_run_db(supporting_data_dir: Path, run_id: str = None) -> tuple[PipelineDB, str]:
    """Initialize database for a run."""
    db = get_db(supporting_data_dir)
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]
    return db, run_id