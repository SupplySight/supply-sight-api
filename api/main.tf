data "aws_caller_identity" "current" {}

resource "aws_dynamodb_table" "test_table" {
  name         = "test_table"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "N"
  }

  attribute {
    name = "attr2"
    type = "B"
  }
}