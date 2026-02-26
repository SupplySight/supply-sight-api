data "aws_caller_identity" "current" {}

resource "aws_dynamodb_table" "test_table" {
  name         = "test_table"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "attr1"
    type = "S"
  }

  attribute {
    name = "attr2"
    type = "B"
  }
}


output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "caller_arn" {
  value = data.aws_caller_identity.current.arn
}
