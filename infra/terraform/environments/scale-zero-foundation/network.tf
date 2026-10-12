locals {
  name = "aegis-${var.environment}"
  public_worker_cidrs = [
    cidrsubnet(var.vpc_cidr, 4, 0),
    cidrsubnet(var.vpc_cidr, 4, 1),
  ]
  private_data_cidrs = [
    cidrsubnet(var.vpc_cidr, 4, 8),
    cidrsubnet(var.vpc_cidr, 4, 9),
  ]
}

#checkov:skip=CKV2_AWS_11:Flow logs and their finite-retention destination are created with Phase 4 observability.
resource "aws_vpc" "this" {
  #checkov:skip=CKV2_AWS_11:Flow logs and finite retention are added with Phase 4 observability before deployment.
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-default-deny" }
}

resource "aws_internet_gateway" "worker_egress" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-worker-egress" }
}

resource "aws_subnet" "worker" {
  for_each = { for index, az in var.availability_zones : az => local.public_worker_cidrs[index] }

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-worker-${each.key}", tier = "public-egress" }
}

resource "aws_subnet" "data" {
  for_each = { for index, az in var.availability_zones : az => local.private_data_cidrs[index] }

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-data-${each.key}", tier = "private-data" }
}

resource "aws_route_table" "worker" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-worker" }
}

resource "aws_route" "worker_internet" {
  route_table_id         = aws_route_table.worker.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.worker_egress.id
}

resource "aws_route_table_association" "worker" {
  for_each       = aws_subnet.worker
  subnet_id      = each.value.id
  route_table_id = aws_route_table.worker.id
}

resource "aws_route_table" "data" {
  for_each = aws_subnet.data
  vpc_id   = aws_vpc.this.id
  tags     = { Name = "${local.name}-data-${each.key}" }
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data[each.key].id
}

# There is intentionally no aws_nat_gateway. Future bounded Fargate workers
# receive a public IP only while a task is alive; they accept no ingress. Data
# subnets have no default route. This is the documented low-volume cost choice.
#checkov:skip=CKV2_AWS_5:Attached by the bounded Phase 4 ECS task definition; no placeholder ENI is created.
resource "aws_security_group" "worker" {
  #checkov:skip=CKV2_AWS_5:Attached by the bounded Phase 4 ECS task definition; no placeholder ENI is created.
  name        = "${local.name}-worker"
  description = "Ephemeral workers: no ingress, outbound provider access only."
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-worker" }
}

resource "aws_vpc_security_group_egress_rule" "worker_https" {
  security_group_id = aws_security_group.worker.id
  description       = "TLS to approved AWS and external provider endpoints"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "worker_postgres" {
  security_group_id            = aws_security_group.worker.id
  description                  = "PostgreSQL to the system of record"
  referenced_security_group_id = aws_security_group.database.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

#checkov:skip=CKV2_AWS_5:Attached by the Phase 4 Lambda function; no placeholder ENI is created.
resource "aws_security_group" "lambda" {
  #checkov:skip=CKV2_AWS_5:Attached by the Phase 4 Lambda function; no placeholder ENI is created.
  name        = "${local.name}-lambda"
  description = "Future API Lambda: no ingress; database and private AWS endpoints only."
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-lambda" }
}

resource "aws_vpc_security_group_egress_rule" "lambda_postgres" {
  security_group_id            = aws_security_group.lambda.id
  description                  = "PostgreSQL to the system of record"
  referenced_security_group_id = aws_security_group.database.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "Aurora ingress from API Lambda and bounded workers only."
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-database" }
}

resource "aws_vpc_security_group_ingress_rule" "database_lambda" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from API Lambda"
  referenced_security_group_id = aws_security_group.lambda.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "database_worker" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from bounded workers"
  referenced_security_group_id = aws_security_group.worker.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

data "aws_region" "current" {}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = values(aws_route_table.data)[*].id
  tags              = { Name = "${local.name}-s3" }
}
