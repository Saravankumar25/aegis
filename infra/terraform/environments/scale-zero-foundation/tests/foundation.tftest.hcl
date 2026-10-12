mock_provider "aws" {}

override_data {
  target = data.aws_region.current
  values = { name = "us-east-1" }
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

override_data {
  target = data.aws_iam_policy_document.github_assume
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.github_plan
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.lambda_assume
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.lambda_runtime
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.worker_assume
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.worker_runtime
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.artifacts
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

override_data {
  target = data.aws_iam_policy_document.rds_monitoring_assume
  values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
}

run "foundation_contract" {
  command = plan

  variables {
    github_repository          = "example/aegis"
    github_oidc_provider_arn   = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    terraform_state_bucket_arn = "arn:aws:s3:::example-terraform-state"
    terraform_lock_table_arn   = "arn:aws:dynamodb:us-east-1:123456789012:table/example-terraform-lock"
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].min_capacity == 0
    error_message = "Aurora must retain the zero-ACU pause floor."
  }

  assert {
    condition     = aws_rds_cluster_instance.writer.instance_class == "db.serverless"
    error_message = "The only Aurora writer must use Serverless v2 capacity."
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets
    error_message = "Artifact storage must remain private."
  }

  assert {
    condition     = aws_subnet.data["us-east-1a"].map_public_ip_on_launch == false
    error_message = "Data subnets must never assign public addresses."
  }
}
