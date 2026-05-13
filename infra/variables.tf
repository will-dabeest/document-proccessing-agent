variable "aws_region" {
  type        = string
  description = "AWS region for LocalStack resources"
  default     = "us-east-1"
}

variable "localstack_endpoint" {
  type        = string
  description = "LocalStack edge endpoint"
  default     = "http://localhost:4566"
}
