variable "aws_region" {
  type = string
}

variable "newsapi_key" {
  description = "API key for NewsAPI"
  type        = string
  sensitive   = true
}

variable "sagemaker_endpoint_name" {
  description = "Name of the SageMaker inference endpoint"
  type        = string
}

variable "recency_days" {
  description = "How many days a cached brand score is considered fresh"
  type        = number
  default     = 30
}

variable "max_articles" {
  description = "Maximum number of NewsAPI articles to fetch per brand"
  type        = number
  default     = 20
}