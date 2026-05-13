provider "aws" {
  region                      = var.aws_region
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    s3             = var.localstack_endpoint
    sqs            = var.localstack_endpoint
    dynamodb       = var.localstack_endpoint
    secretsmanager = var.localstack_endpoint
  }
}

resource "aws_s3_bucket" "uploads" {
  bucket = "doc-storage"
}

resource "aws_sqs_queue" "jobs_dlq" {
  name = "jobs-dlq"
}

resource "aws_sqs_queue" "jobs" {
  name                       = "jobs"
  visibility_timeout_seconds = 60
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_dynamodb_table" "audit" {
  name         = "ProcessLog"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "MessageId"

  attribute {
    name = "MessageId"
    type = "S"
  }
}

resource "aws_secretsmanager_secret" "llm_config" {
  name = "llm-config"
}
