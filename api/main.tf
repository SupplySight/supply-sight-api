data "aws_caller_identity" "current" {}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/handler.py"
  output_path = "${path.module}/lambda/lambda_function_payload.zip"
}

resource "aws_dynamodb_table" "brand_risk" {
  name         = "brand-risk-scores"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "BrandName"

  attribute {
    name = "BrandName"
    type = "S"
  }
}

resource "aws_iam_role" "lambda_role" {
  name = "supply-sight-api-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_dynamodb_sagemaker" {
  name = "supply-sight-api-lambda-dynamodb-sagemaker"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.brand_risk.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sagemaker:InvokeEndpoint"
        ]
        Resource = "arn:aws:sagemaker:${var.aws_region}:${data.aws_caller_identity.current.account_id}:endpoint/*"
      }
    ]
  })
}

resource "aws_lambda_function" "brand_risk_function" {
  function_name = "brand-risk-function"
  role          = aws_iam_role.lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 60

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      TABLE_NAME              = aws_dynamodb_table.brand_risk.name
      NEWSAPI_KEY             = var.newsapi_key
      SAGEMAKER_ENDPOINT_NAME = var.sagemaker_endpoint_name
      RECENCY_DAYS            = tostring(var.recency_days)
      MAX_ARTICLES            = tostring(var.max_articles)
    }
  }
}
