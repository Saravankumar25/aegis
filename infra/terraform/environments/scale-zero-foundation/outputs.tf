output "vpc_id" {
  description = "Scale-zero VPC id."
  value       = aws_vpc.this.id
}

output "worker_subnet_ids" {
  description = "Public-route subnets for bounded tasks. RunTask must explicitly assign a public IP."
  value       = values(aws_subnet.worker)[*].id
}

output "data_subnet_ids" {
  description = "Private subnets with no internet default route."
  value       = values(aws_subnet.data)[*].id
}

output "worker_security_group_id" {
  description = "No-ingress security group for bounded workers."
  value       = aws_security_group.worker.id
}

output "lambda_security_group_id" {
  description = "No-ingress security group reserved for the Phase 4 API Lambda integration."
  value       = aws_security_group.lambda.id
}

output "aurora_cluster_endpoint" {
  description = "Private Aurora writer endpoint."
  value       = aws_rds_cluster.this.endpoint
}

output "aurora_master_secret_arn" {
  description = "RDS-managed master credential secret."
  value       = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "artifact_bucket_name" {
  description = "Private, lifecycle-managed artifact bucket."
  value       = aws_s3_bucket.artifacts.bucket
}

output "application_secret_arns" {
  description = "Secret containers that must be populated outside Terraform."
  value       = { for name, secret in aws_secretsmanager_secret.application : name => secret.arn }
}

output "github_plan_role_arn" {
  description = "Read-only GitHub Actions OIDC role for reviewed plans from main."
  value       = aws_iam_role.github_plan.arn
}
