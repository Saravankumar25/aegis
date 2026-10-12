data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "github_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name                 = "${local.name}-github-plan"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
  tags                 = { Name = "${local.name}-github-plan" }
}

#checkov:skip=CKV_AWS_356:AWS Describe APIs used by a read-only Terraform plan do not support resource scoping.
data "aws_iam_policy_document" "github_plan" {
  #checkov:skip=CKV_AWS_356:AWS Describe APIs used by a read-only Terraform plan do not support resource scoping.
  statement {
    sid       = "ReadState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.terraform_state_bucket_arn, "${var.terraform_state_bucket_arn}/*"]
  }
  statement {
    sid       = "LockState"
    effect    = "Allow"
    actions   = ["dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [var.terraform_lock_table_arn]
  }
  statement {
    sid    = "ReadFoundation"
    effect = "Allow"
    actions = [
      "ec2:DescribeAvailabilityZones", "ec2:DescribeInternetGateways",
      "ec2:DescribeRouteTables", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets",
      "ec2:DescribeVpcEndpoints", "ec2:DescribeVpcs", "iam:GetRole", "iam:GetRolePolicy",
      "rds:DescribeDBClusterParameters", "rds:DescribeDBClusters", "rds:DescribeDBInstances",
      "rds:DescribeDBSubnetGroups", "s3:GetBucketLifecycleConfiguration", "s3:GetBucketLocation",
      "s3:GetBucketPolicy", "s3:GetBucketPublicAccessBlock", "s3:GetBucketVersioning",
      "secretsmanager:DescribeSecret"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_plan" {
  name   = "read-foundation-plan"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_plan.json
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_runtime" {
  name               = "${local.name}-lambda-runtime"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = { Name = "${local.name}-lambda-runtime" }
}

#checkov:skip=CKV_AWS_111:Lambda VPC ENI lifecycle requires EC2 write actions on wildcard resources.
#checkov:skip=CKV_AWS_356:Lambda VPC ENI APIs do not support resource-level permissions.
data "aws_iam_policy_document" "lambda_runtime" {
  #checkov:skip=CKV_AWS_111:Lambda VPC ENI lifecycle requires EC2 write actions on wildcard resources.
  #checkov:skip=CKV_AWS_356:Lambda VPC ENI APIs do not support resource-level permissions.
  statement {
    sid     = "ReadApplicationSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.application["alert-ingest-token"].arn,
      aws_secretsmanager_secret.application["firebase-service-account"].arn,
    ]
  }
  statement {
    sid       = "DecryptApplicationSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "WriteFunctionLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name}-*:*"]
  }
  statement {
    sid       = "ManageVpcInterfaces"
    effect    = "Allow"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_runtime" {
  name   = "runtime-minimum"
  role   = aws_iam_role.lambda_runtime.id
  policy = data.aws_iam_policy_document.lambda_runtime.json
}

data "aws_iam_policy_document" "worker_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "worker_runtime" {
  name               = "${local.name}-worker-runtime"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
  tags               = { Name = "${local.name}-worker-runtime" }
}

data "aws_iam_policy_document" "worker_runtime" {
  statement {
    sid       = "ReadApplicationSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = values(aws_secretsmanager_secret.application)[*].arn
  }
  statement {
    sid       = "DecryptApplicationSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "ArtifactObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    sid       = "ListArtifactPrefixes"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["investigations/*", "evidence/*", "executions/*", "evaluations/*"]
    }
  }
}

resource "aws_iam_role_policy" "worker_runtime" {
  name   = "runtime-minimum"
  role   = aws_iam_role.worker_runtime.id
  policy = data.aws_iam_policy_document.worker_runtime.json
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
  tags               = { Name = "${local.name}-rds-monitoring" }
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
