resource "aws_kms_key" "data" {
  #checkov:skip=CKV2_AWS_64:The omitted policy deliberately uses the AWS default account-root key policy; grants remain IAM-controlled.
  description             = "Aegis scale-zero system-of-record and artifact encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Name = "${local.name}-data" }
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-aurora"
  subnet_ids = values(aws_subnet.data)[*].id
  tags       = { Name = "${local.name}-aurora" }
}

resource "aws_rds_cluster_parameter_group" "this" {
  name   = "${local.name}-aurora-pg16"
  family = "aurora-postgresql16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  parameter {
    name  = "log_statement"
    value = "ddl"
  }
}

#checkov:skip=CKV2_AWS_8:Native Aurora backups are retained; centralized AWS Backup is a deployment-gate decision.
resource "aws_rds_cluster" "this" {
  #checkov:skip=CKV2_AWS_8:Native Aurora backups are retained; centralized AWS Backup is a deployment-gate decision.
  cluster_identifier                  = "${local.name}-postgres"
  engine                              = "aurora-postgresql"
  engine_mode                         = "provisioned"
  engine_version                      = var.aurora_engine_version
  database_name                       = "aegis"
  master_username                     = "aegis_admin"
  manage_master_user_password         = true
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.data.arn
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  db_subnet_group_name                = aws_db_subnet_group.this.name
  vpc_security_group_ids              = [aws_security_group.database.id]
  db_cluster_parameter_group_name     = aws_rds_cluster_parameter_group.this.name
  backup_retention_period             = 7
  preferred_backup_window             = "03:00-04:00"
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.deletion_protection
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"

  serverlessv2_scaling_configuration {
    min_capacity             = var.aurora_min_acu
    max_capacity             = var.aurora_max_acu
    seconds_until_auto_pause = var.aurora_auto_pause_seconds
  }

  lifecycle {
    precondition {
      condition     = var.aurora_min_acu == 0
      error_message = "Aurora must be configured to auto-pause at zero ACU."
    }
  }

  tags = { Name = "${local.name}-postgres", authority = "system-of-record" }
}

# Exactly one writer. Readers and RDS Proxy are intentionally absent: both add
# idle cost and a proxy prevents a paused database from being genuinely idle.
resource "aws_rds_cluster_instance" "writer" {
  identifier                      = "${local.name}-postgres-writer"
  cluster_identifier              = aws_rds_cluster.this.id
  instance_class                  = "db.serverless"
  engine                          = aws_rds_cluster.this.engine
  engine_version                  = aws_rds_cluster.this.engine_version
  publicly_accessible             = false
  auto_minor_version_upgrade      = true
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.data.arn
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn
}

#checkov:skip=CKV_AWS_18:Access logging needs the Phase 4 logging destination and finite-retention policy.
#checkov:skip=CKV_AWS_144:This non-production single-region foundation deliberately excludes cross-region replication.
#checkov:skip=CKV2_AWS_62:No event consumer exists until Phase 4 orchestration is created.
resource "aws_s3_bucket" "artifacts" {
  #checkov:skip=CKV_AWS_18:Access logging needs the Phase 4 logging destination and finite-retention policy.
  #checkov:skip=CKV_AWS_144:This non-production single-region foundation deliberately excludes cross-region replication.
  #checkov:skip=CKV2_AWS_62:No event consumer exists until Phase 4 orchestration is created.
  bucket_prefix = "${local.name}-artifacts-"
  force_destroy = false
  tags          = { Name = "${local.name}-artifacts", data_class = "evidence" }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.data.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "finite-retention"
    status = "Enabled"
    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration { days = var.artifact_retention_days }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

data "aws_iam_policy_document" "artifacts" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifacts.json
}

# Containers only: secret values are populated out-of-band so neither source,
# plan output nor Terraform state can contain credentials.
#checkov:skip=CKV2_AWS_57:Values and rotation Lambdas are intentionally created outside this container-only foundation.
resource "aws_secretsmanager_secret" "application" {
  #checkov:skip=CKV2_AWS_57:Values and rotation Lambdas are intentionally created outside this container-only foundation.
  for_each                = toset(["alert-ingest-token", "firebase-service-account", "llm-provider-key"])
  name                    = "${local.name}/${each.value}"
  recovery_window_in_days = 30
  kms_key_id              = aws_kms_key.data.arn
  tags                    = { Name = "${local.name}-${each.value}" }
}
