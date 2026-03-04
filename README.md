## Supply Sight API

This directory contains the backend API for the Supply Sight project. It exposes a single HTTP endpoint that the Chrome extension calls to fetch an ethical/ESG risk score for a given brand.

The stack is built with:

- **AWS Lambda** for serverless compute
- **AWS API Gateway (HTTP API)** for HTTPS access
- **AWS DynamoDB** for caching risk scores
- **Amazon SageMaker** (external model endpoint) for risk-scoring news content
- **Terraform** for infrastructure as code and deployment

---

### High‑level architecture

- The **Chrome extension** extracts a manufacturer/brand name from an e‑commerce product page and sends a `POST` request with `brand_name` to this API.
- **API Gateway (HTTP API)** receives the request and forwards it via a Lambda proxy integration to the **Lambda function** defined in `lambda/handler.py`.
- The Lambda function:
    - Checks **DynamoDB** for a recent cached score for the brand.
    - If no fresh cache entry exists:
        - Queries the external **NewsAPI** for recent news articles about the brand, focused on risk-related keywords.
        - Aggregates article text and calls a **SageMaker inference endpoint** to obtain a risk score and risk breakdown.
        - Stores the result in DynamoDB for future requests.
    - Returns a JSON response with a normalized risk score and metadata.

All infrastructure resources live in `api/*.tf` and are managed via Terraform.

---

### AWS resources used

Defined in `api/main.tf` and related files:

- **Lambda**
    - `aws_lambda_function.brand_risk_function`
    - Code: `api/lambda/handler.py` (zipped via `data "archive_file" "lambda_zip"`)
    - Runtime: Python 3.11
    - Environment variables:
        - `TABLE_NAME` – DynamoDB table name
        - `NEWSAPI_KEY` – NewsAPI API key
        - `SAGEMAKER_ENDPOINT_NAME` – target SageMaker endpoint name
        - `RECENCY_DAYS` – cache validity window in days
        - `MAX_ARTICLES` – max number of NewsAPI articles to fetch

- **DynamoDB**
    - `aws_dynamodb_table.brand_risk`
    - Table name: `brand-risk-scores`
    - Primary key: `BrandName` (string)

- **IAM**
    - `aws_iam_role.lambda_role` – execution role for the Lambda.
    - `aws_iam_role_policy_attachment.lambda_basic_execution` – attaches AWS managed `AWSLambdaBasicExecutionRole`.
    - `aws_iam_role_policy.lambda_dynamodb_sagemaker` – allows:
        - `dynamodb:GetItem`, `dynamodb:PutItem` on the `brand-rish-scores` table.
        - `sagemaker:InvokeEndpoint` on endpoints in the configured region/account.

- **API Gateway (HTTP API)**
    - `aws_apigatewayv2_api.brand_risk_api` – HTTP API with CORS enabled for browser/extension access.
    - `aws_apigatewayv2_integration.brand_risk_integration` – Lambda proxy integration to the `brand_risk_function`.
    - `aws_apigatewayv2_route.brand_risk_route` – route `POST /brand-risk`.
    - `aws_apigatewayv2_stage.brand_risk_stage` – `prod` stage with auto deploy.
    - `aws_lambda_permission.allow_apigw_invoke_brand_risk` – permission for API Gateway to invoke the Lambda.
    - Output: `brand_risk_api_url` – base invoke URL used by the Chrome extension.

- **Terraform backend/providers**
    - `backend "s3"` in `backend.tf` – remote state stored in S3 (bucket configuration supplied externally).
    - `providers.tf`, `versions.tf`, `variables.tf` – pin provider versions and define input variables.

---

### Endpoint specification

**Base URL**

- The Terraform output `brand_risk_api_url` exposes the fully qualified base URL, for example:
    - `https://<api-id>.execute-api.<region>.amazonaws.com/prod`

**Endpoint**

- **Method**: `POST`
- **Path**: `/brand-risk`
- **Content-Type**: `application/json`

**Request body**

```json
{
	"brand_name": "Nike"
}
```

The handler is flexible and also accepts `brandName` or `brand` keys, but `brand_name` is the canonical field.

**Successful response (200)**

Example shape (fields may be extended over time):

```json
{
	"brand_name": "Nike",
	"risk_score": 72.5,
	"status": "RED",
	"last_updated": "2026-03-03T19:10:42.123456+00:00",
	"source": "cache"
}
```

- `risk_score` – float between 0–100 computed from the model’s class probabilities.
- `status` – `"GREEN"`, `"YELLOW"`, or `"RED"`, derived from the score.
- `source` – `"cache"` if served from DynamoDB, `"fresh"` if recomputed.

**Error responses**

- `400` – missing or invalid `brand_name`:
    - `{"message": "brand_name is required in the request body"}`
- `502` – SageMaker did not return a valid risk score:
    - `{"message": "SageMaker model did not return a risk_score"}`
- `500` – unexpected internal error:
    - `{"message": "Internal server error", "detail": "<error details>"}`

All responses include CORS headers:

- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Headers: Content-Type`
- `Access-Control-Allow-Methods: OPTIONS,POST`

---

### Lambda behavior (`lambda/handler.py`)

The `lambda_handler` flow is:

1. **Parse brand name**
    - Reads and normalizes the JSON request body to extract a brand name from `brand_name`, `brandName`, or `brand`.
2. **Check DynamoDB cache**
    - Looks up `BrandName` in the `brand-risk-scores` table.
    - Verifies the `TimeUpdated` timestamp is within `RECENCY_DAYS`.
    - If valid, returns a 200 response with the cached score (`source: "cache"`).
3. **Fetch news articles**
    - Uses the external NewsAPI (`https://newsapi.org/v2/everything`) with a boolean query built from:
        - The brand name (required)
        - A curated list of risk-related keywords
    - Enforces NewsAPI query-length constraints (max 500 chars).
    - Filters results to ensure the brand actually appears in the title/description/content.
4. **Model inference via SageMaker**
    - Concatenates the filtered article texts into a single string.
    - Calls `sagemaker:InvokeEndpoint` on the configured `SAGEMAKER_ENDPOINT_NAME`, passing JSON with a `text` field.
    - Expects back a JSON with `risk_score` and a `breakdown` of probabilities.
5. **Persist result**
    - Converts floats to `Decimal` as required by DynamoDB.
    - Stores `BrandName`, `RiskScore`, `TimeUpdated`, `ModelOutput`, and `Status` in the table.
6. **Return API response**
    - Returns a simplified JSON for the client with `brand_name`, `risk_score`, `status`, `last_updated`, and `source`.

If NewsAPI returns no qualifying articles, the function assumes a safe default (`status: GREEN`, score `0.0`), stores it, and returns that to the client.

---

### Configuration & variables

Key variables (see `variables.tf` for full definitions):

- `aws_region` – region for Lambda, DynamoDB, and API Gateway.
- `newsapi_key` – NewsAPI key (sensitive).
- `sagemaker_endpoint_name` – name of the deployed SageMaker endpoint.
- `recency_days` – integer; used to decide when to refresh cache entries.
- `max_articles` – integer; maximum number of articles to request from NewsAPI.

These are wired into the Lambda function via environment variables in `main.tf`.

---

### Deploying with Terraform

From the `supply-sight-api/api` directory:

1. **Initialize Terraform**

```bash
terraform init
```

2. **Review the plan**

```bash
terraform plan -var 'aws_region=<your-region>' \
               -var 'newsapi_key=<your-newsapi-key>' \
               -var 'sagemaker_endpoint_name=<your-sagemaker-endpoint>'
```

3. **Apply changes**

```bash
terraform apply \
  -var 'aws_region=<your-region>' \
  -var 'newsapi_key=<your-newsapi-key>' \
  -var 'sagemaker_endpoint_name=<your-sagemaker-endpoint>'
```

4. **Get the API URL**

After a successful apply, Terraform will output:

```text
brand_risk_api_url = https://<api-id>.execute-api.<region>.amazonaws.com/prod
```

Use this value as the base URL in the Chrome extension (e.g. in `productController.js`).

This should return a JSON object with the fields described above.
