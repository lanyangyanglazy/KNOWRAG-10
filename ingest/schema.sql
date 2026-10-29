CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title               text NOT NULL,
  dept                text NOT NULL,
  classification      smallint CHECK (classification IN (0,1,2)),
  effective_date      date,
  status              text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','active','inactive','deleting')),
  current_version_id  uuid,
  created_by          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz
);

CREATE TABLE IF NOT EXISTS doc_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid NOT NULL REFERENCES documents(id),
  version_no      int NOT NULL,
  sha256          char(64) NOT NULL,
  filename        text NOT NULL,
  mime            text NOT NULL,
  size_bytes      bigint NOT NULL,
  storage_path    text NOT NULL,
  parser          text NOT NULL,
  parser_version  text NOT NULL,
  status          text NOT NULL CHECK (status IN
                  ('uploaded','scanning','parsing','needs_review','embedding',
                   'ready','failed','quarantined')),
  ir              jsonb,
  warnings        jsonb NOT NULL DEFAULT '[]',
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  activated_at    timestamptz,
  UNIQUE (document_id, version_no),
  UNIQUE (document_id, sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
  id              bigserial PRIMARY KEY,
  document_id     uuid NOT NULL REFERENCES documents(id),
  version_id      uuid NOT NULL REFERENCES doc_versions(id),
  ordinal         int NOT NULL,
  chunk_type      text NOT NULL CHECK (chunk_type IN
                  ('text','table_summary','table_rows')),
  content         text NOT NULL,
  content_tokens  text NOT NULL,
  tsv             tsvector,
  embedding       vector(1024),
  embed_model     text NOT NULL,
  dept            text NOT NULL,
  classification  smallint,
  metadata        jsonb NOT NULL DEFAULT '{}',
  chunk_hash      char(64) NOT NULL,
  active          boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS doc_tables (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_id    uuid NOT NULL REFERENCES doc_versions(id),
  table_key     text NOT NULL,
  sheet         text,
  cell_range    text,
  header_rows   int[] NOT NULL,
  columns       jsonb NOT NULL,
  row_count     int NOT NULL,
  footer_rows   int[] NOT NULL DEFAULT '{}',
  confidence    real NOT NULL,
  source        text NOT NULL,
  storage_ref   text,
  UNIQUE (version_id, table_key)
);

CREATE TABLE IF NOT EXISTS jobs (
  id            bigserial PRIMARY KEY,
  kind          text NOT NULL,
  payload       jsonb NOT NULL,
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
  priority      int NOT NULL DEFAULT 100,
  attempts      int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 3,
  run_after     timestamptz NOT NULL DEFAULT now(),
  locked_by     text,
  locked_at     timestamptz,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE IF NOT EXISTS review_items (
  id            bigserial PRIMARY KEY,
  version_id    uuid NOT NULL REFERENCES doc_versions(id),
  kind          text NOT NULL CHECK (kind IN
                ('structure','classification','metadata','parse_warning')),
  status        text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','resolved','waived')),
  payload       jsonb NOT NULL,
  resolved_by   text,
  resolved_at   timestamptz,
  note          text
);

CREATE TABLE IF NOT EXISTS review_approvals (
  id              bigserial PRIMARY KEY,
  review_item_id  bigint NOT NULL REFERENCES review_items(id),
  user_id         text NOT NULL,
  decision        text NOT NULL CHECK (decision IN ('approve','reject')),
  note            text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_item_id, user_id)
);

CREATE TABLE IF NOT EXISTS layout_templates (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  scope_dept    text,
  signature     jsonb NOT NULL,
  config        jsonb NOT NULL,
  hits          int NOT NULL DEFAULT 0,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_vec_l0 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 0;
CREATE INDEX IF NOT EXISTS chunks_vec_l1 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 1;
CREATE INDEX IF NOT EXISTS chunks_vec_l2 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 2;
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv) WHERE active;
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (document_id, active);
CREATE INDEX IF NOT EXISTS chunks_dept ON chunks (dept, classification) WHERE active;
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (version_id, chunk_hash);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (priority, run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS review_open ON review_items (version_id) WHERE status = 'open';
