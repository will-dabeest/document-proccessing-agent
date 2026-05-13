$ErrorActionPreference = "Stop"
$endpoint = "http://localhost:4566"

Write-Host "S3 buckets:"
aws --endpoint-url=$endpoint s3 ls

Write-Host "`nSQS queues:"
aws --endpoint-url=$endpoint sqs list-queues

Write-Host "`nDynamoDB tables:"
aws --endpoint-url=$endpoint dynamodb list-tables

Write-Host "`nSecrets (names):"
aws --endpoint-url=$endpoint secretsmanager list-secrets --query "SecretList[].Name" --output text
