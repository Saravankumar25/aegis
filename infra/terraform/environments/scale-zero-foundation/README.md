# Scale-zero foundation

This root is Phase 3 infrastructure only. It is safe to format, initialize,
validate and plan; the delivery workflow must not apply it until the separate
deployment gate is approved.

## Egress decision

The foundation deliberately creates **no NAT Gateway**. Bounded Fargate workers
will run in the worker subnets with an ephemeral public IP, no inbound security
group rules, and outbound TCP 443 only. Interface endpoints in two AZs have a
fixed hourly charge even when no investigation is running, so this phase does
not create them. Private S3 traffic uses its free gateway endpoint.

Aurora occupies data subnets with no default route. The future API Lambda
security group can reach Aurora only. Phase 4 must explicitly resolve access to
Firebase keys and any required AWS services before attaching the Lambda to this
group; silently adding a NAT Gateway or hourly interface endpoints is not an
acceptable workaround.

The worker public address is an egress mechanism, not an ingress surface:
`map_public_ip_on_launch` stays false and `RunTask` must opt in, the worker
security group has no ingress rules, and tasks exist only for a bounded job.

## Cost and safety bounds

- Aurora Serverless v2: one writer, 0–2 ACU by default, auto-pause after 15
  minutes, no readers and no RDS Proxy.
- One rotating customer-managed KMS key protects Aurora, S3 artifacts and
  secret containers.
- S3: block-public-access, TLS-only policy, versioning, transitions and finite
  expiry.
- Secrets Manager: Terraform creates containers only; secret values are never
  stored in configuration or state.
- GitHub OIDC: the plan role trusts only `main` of one repository and has no
  apply or administrative permissions.

## Validate

```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
terraform test
```

Use `terraform plan` only with a reviewed target-account variable file and
read-only plan role. No resource in this root authorizes deployment.
