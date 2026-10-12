terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 6.0"
    }
  }

  # State location is supplied by CI. Keeping this partial prevents account
  # identifiers from being committed and keeps validate usable offline.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = "aegis"
      environment  = var.environment
      phase        = "scale-zero-foundation"
      "managed-by" = "terraform"
    }
  }
}
