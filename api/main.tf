data "aws_caller_identity" "current" {}

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

  filename         = "${path.module}/lambda/lambda_function_payload.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda/lambda_function_payload.zip")

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.brand_risk.name
    }
  }
}
