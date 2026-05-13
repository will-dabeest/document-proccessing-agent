output "s3_bucket_name" {
  value = aws_s3_bucket.uploads.bucket
}

output "sqs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.jobs_dlq.url
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.audit.name
}

output "secretsmanager_secret_arn" {
  value = aws_secretsmanager_secret.llm_config.arn
}
