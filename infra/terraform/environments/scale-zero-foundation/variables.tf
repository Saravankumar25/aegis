variable "aws_region" {
  description = "AWS region for the scale-zero environment."
  type        = string
  default     = "us-east-1"
}

variable "availability_zones" {
  description = "Exactly two AZs in aws_region. Explicit input makes an offline plan deterministic."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]

  validation {
    condition     = length(var.availability_zones) == 2 && alltrue([for az in var.availability_zones : startswith(az, var.aws_region)])
    error_message = "availability_zones must contain exactly two zones in aws_region."
  }
}

variable "environment" {
  description = "Non-production scale-zero environment name."
  type        = string
  default     = "staging-zero"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.environment))
    error_message = "environment must be a short lowercase name."
  }
}

variable "vpc_cidr" {
  description = "Private address range split into two public worker and two private data subnets."
  type        = string
  default     = "10.72.0.0/20"
}

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL release that supports Serverless v2 auto-pause in the target region."
  type        = string
  default     = "16.4"
}

variable "aurora_min_acu" {
  description = "Minimum ACU. Zero enables auto-pause and must remain zero for the scale-zero environment."
  type        = number
  default     = 0

  validation {
    condition     = var.aurora_min_acu == 0
    error_message = "The scale-zero foundation requires aurora_min_acu = 0."
  }
}

variable "aurora_max_acu" {
  description = "Hard database capacity ceiling."
  type        = number
  default     = 2

  validation {
    condition     = var.aurora_max_acu >= 0.5 && var.aurora_max_acu <= 8
    error_message = "aurora_max_acu must be between 0.5 and 8 ACU."
  }
}

variable "aurora_auto_pause_seconds" {
  description = "Idle interval before Aurora pauses."
  type        = number
  default     = 900

  validation {
    condition     = var.aurora_auto_pause_seconds >= 300 && var.aurora_auto_pause_seconds <= 86400
    error_message = "Aurora auto-pause must be between 300 seconds and 24 hours."
  }
}

variable "deletion_protection" {
  description = "Protect the system-of-record cluster from Terraform deletion."
  type        = bool
  default     = true
}

variable "artifact_retention_days" {
  description = "Finite lifetime for sample and generated artifacts."
  type        = number
  default     = 365

  validation {
    condition     = var.artifact_retention_days >= 30 && var.artifact_retention_days <= 2555
    error_message = "Artifact retention must be between 30 days and seven years."
  }
}

variable "github_repository" {
  description = "GitHub repository in owner/name form allowed to assume the plan role."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must use owner/name form."
  }
}

variable "github_oidc_provider_arn" {
  description = "Existing account-level GitHub Actions OIDC provider ARN."
  type        = string
}

variable "terraform_state_bucket_arn" {
  description = "Existing Terraform state bucket ARN readable by the GitHub plan role."
  type        = string
}

variable "terraform_lock_table_arn" {
  description = "Existing DynamoDB lock table ARN used by Terraform."
  type        = string
}
