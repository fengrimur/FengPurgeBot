-- SPDX-FileCopyrightText: 2026 Fengrímur
-- SPDX-License-Identifier: AGPL-3.0-only
-- See NOTICE for additional terms.

-- identity fields do not change
-- target state determines open fanout load
-- attempt rows determine budget use over the last 24 hours

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE schema_version (
  version    INT          NOT NULL,
  applied_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

INSERT INTO schema_version (version, applied_at) VALUES (1, UTC_TIMESTAMP(6));

CREATE TABLE surfaces (
  page_id                 BIGINT UNSIGNED NOT NULL,
  kind                    VARCHAR(16)     NOT NULL,
  state                   VARCHAR(16)     NOT NULL,
  observed_title          VARCHAR(255)    NULL,
  last_revision_id        BIGINT UNSIGNED NULL,
  last_revision_author    VARCHAR(255)    NULL,
  last_revision_timestamp DATETIME(6)     NULL,
  content_sha256          BINARY(32)      NULL,
  reason_code             VARCHAR(255)    NULL,
  created_at              DATETIME(6)     NOT NULL,
  updated_at              DATETIME(6)     NOT NULL,
  PRIMARY KEY (page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE requests (
  surface_page_id        BIGINT UNSIGNED NOT NULL,
  request_id             VARCHAR(128)    NOT NULL,
  action                 VARCHAR(32)     NOT NULL,
  target                 VARCHAR(255)    NOT NULL,
  target_namespace       INT             NOT NULL,
  schedule_key           VARCHAR(64)     NOT NULL,
  discussion_url         VARCHAR(512)    NULL,
  semantic_sha256        BINARY(32)      NOT NULL,
  introduced_revision_id BIGINT UNSIGNED NOT NULL,
  introduced_author      VARCHAR(255)    NOT NULL,
  introduced_at          DATETIME(6)     NOT NULL,
  latest_revision_id     BIGINT UNSIGNED NOT NULL,
  active                 TINYINT(1)      NOT NULL,
  suspended              TINYINT(1)      NOT NULL DEFAULT 0,
  created_at             DATETIME(6)     NOT NULL,
  updated_at             DATETIME(6)     NOT NULL,
  PRIMARY KEY (surface_page_id, request_id),
  KEY idx_requests_claimable (active, suspended, surface_page_id),
  CONSTRAINT fk_requests_surface FOREIGN KEY (surface_page_id) REFERENCES surfaces (page_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- jobs are never deleted
-- claim_key prevents duplicate one time jobs and scheduled runs
CREATE TABLE jobs (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  claim_key        BINARY(32)      NOT NULL,
  surface_page_id  BIGINT UNSIGNED NOT NULL,
  request_id       VARCHAR(128)    NOT NULL,
  due_slot         DATETIME(6)     NULL,
  action           VARCHAR(32)     NOT NULL,
  is_fanout        TINYINT(1)      NOT NULL,
  state            VARCHAR(24)     NOT NULL,
  selector_sha256  BINARY(32)      NULL,
  staging_failures INT             NOT NULL DEFAULT 0,
  not_before       DATETIME(6)     NULL,
  retry_deadline   DATETIME(6)     NULL,
  reason_code      VARCHAR(64)     NULL,
  created_at       DATETIME(6)     NOT NULL,
  updated_at       DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_jobs_claim_key (claim_key),
  KEY idx_jobs_work (state, not_before, id),
  KEY idx_jobs_request (surface_page_id, request_id, state),
  CONSTRAINT fk_jobs_request FOREIGN KEY (surface_page_id, request_id)
    REFERENCES requests (surface_page_id, request_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE targets (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id            BIGINT UNSIGNED NOT NULL,
  page_id           BIGINT UNSIGNED NOT NULL,
  namespace_id      INT             NOT NULL,
  staged_title      VARCHAR(255)    NOT NULL,
  state             VARCHAR(24)     NOT NULL,
  not_before        DATETIME(6)     NULL,
  retry_deadline    DATETIME(6)     NULL,
  singleton_replays INT             NOT NULL DEFAULT 0,
  last_code         VARCHAR(64)     NULL,
  created_at        DATETIME(6)     NOT NULL,
  updated_at        DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_targets_job_page (job_id, page_id),
  KEY idx_targets_work (job_id, state, not_before, id),
  KEY idx_targets_open (state, job_id),
  CONSTRAINT fk_targets_job FOREIGN KEY (job_id) REFERENCES jobs (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- inserting an attempt PERMANENTLY uses send budget
CREATE TABLE attempts (
  id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id                  BIGINT UNSIGNED NOT NULL,
  state                   VARCHAR(16)     NOT NULL,
  force_link_update       TINYINT(1)      NOT NULL,
  target_count            INT             NOT NULL,
  payload_sha256          BINARY(32)      NOT NULL,
  authorizing_revision_id BIGINT UNSIGNED NOT NULL,
  authorizing_author      VARCHAR(255)    NOT NULL,
  reserved_at             DATETIME(6)     NOT NULL,
  post_started_at         DATETIME(6)     NOT NULL,
  finished_at             DATETIME(6)     NULL,
  http_status             INT             NULL,
  api_code                VARCHAR(64)     NULL,
  retry_after_s           INT             NULL,
  response_sha256         BINARY(32)      NULL,
  PRIMARY KEY (id),
  KEY idx_attempts_reserved_at (reserved_at, id),
  KEY idx_attempts_state (state, id),
  KEY idx_attempts_job (job_id, id),
  CONSTRAINT fk_attempts_job FOREIGN KEY (job_id) REFERENCES jobs (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE attempt_targets (
  attempt_id    BIGINT UNSIGNED NOT NULL,
  target_id     BIGINT UNSIGNED NOT NULL,
  request_title VARCHAR(255)    NOT NULL,
  outcome       VARCHAR(24)     NULL,
  PRIMARY KEY (attempt_id, target_id),
  KEY idx_attempt_targets_target (target_id, attempt_id),
  CONSTRAINT fk_attempt_targets_attempt FOREIGN KEY (attempt_id) REFERENCES attempts (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_attempt_targets_target FOREIGN KEY (target_id) REFERENCES targets (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE operator_events (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id     BIGINT UNSIGNED NULL,
  target_id  BIGINT UNSIGNED NULL,
  operation  VARCHAR(32)     NOT NULL,
  operator   VARCHAR(255)    NOT NULL,
  reason     VARCHAR(512)    NOT NULL,
  created_at DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_operator_events_job (job_id, id),
  CONSTRAINT fk_operator_events_job FOREIGN KEY (job_id) REFERENCES jobs (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_operator_events_target FOREIGN KEY (target_id) REFERENCES targets (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
