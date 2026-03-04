import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError


DYNAMO_TABLE_NAME = os.environ["TABLE_NAME"]
NEWSAPI_KEY = os.environ["NEWSAPI_KEY"]
SAGEMAKER_ENDPOINT_NAME = os.environ["SAGEMAKER_ENDPOINT_NAME"]
RECENCY_DAYS = int(os.environ.get("RECENCY_DAYS", "30"))
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "20"))
NEWSAPI_Q_MAX_LENGTH = 500
NEWS_KEYWORDS: list[str] = [
    # Labor / human rights
    "forced labor",
    "child labor",
    "slavery",
    "human trafficking",
    "sweatshop",
    "abuse",
    "exploitation",
    "unsafe",

    # Legal / regulatory / fines
    "lawsuit",
    "class action",
    "investigation",
    "probe",
    "fine",
    "violation",
    "sanctions",

    # Supply chain disruption / operations
    "fire",
    "explosion",
    "closure",
    "shutdown",
    "export ban",
    "import ban",

    # Product safety / recalls
    "recall",
    "contamination",
    "toxic",

    # Financial distress / governance
    "fraud",
    "scandal",
    "embezzlement",
    "bribery",
    "corruption",
    "whistleblower",
]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMO_TABLE_NAME)
sagemaker_runtime = boto3.client("sagemaker-runtime")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # Allow Chrome extension (and other web clients) to call this API.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }


def _parse_brand_name(event: dict) -> str:
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return ""
    elif body is None and isinstance(event, dict):
        body = event

    if not isinstance(body, dict):
        return ""

    return (
        body.get("brand_name")
        or body.get("brandName")
        or body.get("brand")
        or ""
    )


def _get_cached_result(brand_name: str):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENCY_DAYS)

    try:
        resp = table.get_item(Key={"BrandName": brand_name})
    except ClientError:
        return None

    item = resp.get("Item")
    if not item:
        return None

    time_str = item.get("TimeUpdated")
    if not time_str:
        return None

    try:
        updated = datetime.fromisoformat(
            time_str.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if updated < cutoff:
        return None

    risk_score = item.get("RiskScore")
    if isinstance(risk_score, Decimal):
        risk_score = float(risk_score)

    return {
        "brand_name": brand_name,
        "risk_score": risk_score,
        "last_updated": updated.isoformat(),
        "source": "cache",
    }


def _fetch_news_articles(brand_name: str):
    base_url = "https://newsapi.org/v2/everything"
    brand = (brand_name or "").strip()
    if not brand:
        return []

    # Build a boolean query that ALWAYS requires the brand,
    # then adds as many risk keywords as will fit under the 500-char limit.
    brand_expr = f"\"{brand}\""
    kw_or = ""
    for kw in NEWS_KEYWORDS:
        expr = f"\"{kw}\""
        if not expr:
            continue
        candidate = expr if not kw_or else f"{kw_or} OR {expr}"
        projected_q = f"({brand_expr}) AND ({candidate})"
        if len(projected_q) <= NEWSAPI_Q_MAX_LENGTH:
            kw_or = candidate
        else:
            break

    if kw_or:
        q = f"({brand_expr}) AND ({kw_or})"
    else:
        q = brand_expr

    params = {
        "q": q,
        "apiKey": NEWSAPI_KEY,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": MAX_ARTICLES,
        # Explicitly search in title, description, and content so we can
        # reliably post-filter for brand mentions across all visible fields.
        "searchIn": "title,description,content",
    }

    url = f"{base_url}?{urlencode(params)}"
    req = Request(url)

    with urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    raw_articles = payload.get("articles", []) or []

    # Filter on our side to GUARANTEE the brand is present in one of the
    # human-visible fields (title, description, or content).
    brand_lower = brand.lower()
    articles = []
    for a in raw_articles:
        title = (a.get("title") or "")
        description = (a.get("description") or "")
        content = (a.get("content") or "")
        combined = f"{title} {description} {content}".lower()
        if brand_lower in combined:
            articles.append(a)

    print(
        json.dumps(
            {
                "brand_name": brand_name,
                "article_count": len(articles),
                "articles": articles,
            },
            default=str,
        )
    )

    texts = []
    for a in articles:
        title = a.get("title") or ""
        description = a.get("description") or ""
        content = a.get("content") or ""
        combined = " ".join(
            t for t in [title, description, content] if t
        )
        if combined:
            texts.append(combined)

    return texts


def _call_sagemaker_model(brand_name: str, texts):
    # Combine fetched article texts for the risk model.
    # HF container expects only `text` (or `text_target`); passing other keys
    # (e.g. headline) causes tokenizer to get unexpected keyword args.
    combined_text = " ".join(texts)
    payload = {"text": combined_text}

    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=SAGEMAKER_ENDPOINT_NAME,
        ContentType="application/json",
        Body=json.dumps(payload).encode("utf-8"),
    )

    body_bytes = response["Body"].read()
    model_output = json.loads(body_bytes.decode("utf-8"))

    risk_score = model_output.get("risk_score")
    if isinstance(risk_score, str):
        try:
            risk_score = float(risk_score)
        except ValueError:
            risk_score = None

    return risk_score, model_output


def _float_to_decimal(obj):
    """Recursively convert floats to Decimal for DynamoDB (no native float support)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _float_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_float_to_decimal(v) for v in obj]
    return obj


def _save_result(brand_name: str, risk_score: float, model_output: dict):
    now = datetime.now(timezone.utc)
    model_output_safe = _float_to_decimal(model_output)

    item = {
        "BrandName": brand_name,
        "RiskScore": Decimal(str(risk_score)),
        "TimeUpdated": now.isoformat(),
        "ModelOutput": model_output_safe,
        "Status": model_output.get("status"),
    }

    table.put_item(Item=item)

    return {
        "brand_name": brand_name,
        "risk_score": risk_score,
        "status": model_output.get("status"),
        "last_updated": now.isoformat(),
        "source": "fresh",
    }


def lambda_handler(event, context):
    brand_name = _parse_brand_name(event or {})
    if not brand_name:
        return _response(
            400,
            {"message": "brand_name is required in the request body"},
        )

    try:
        cached = _get_cached_result(brand_name)
        if cached:
            return _response(200, cached)

        texts = _fetch_news_articles(brand_name)
        if not texts:
            safe_model_output = {
                "status": "GREEN",
                "breakdown": {
                    "critical_risk": 0.0,
                    "moderate_risk": 0.0,
                    "safe": 1.0,
                },
            }
            result = _save_result(brand_name, 0.0, safe_model_output)
            return _response(200, result)

        risk_score, model_output = _call_sagemaker_model(
            brand_name, texts
        )
        if risk_score is None:
            return _response(
                502,
                {"message": "SageMaker model did not return a risk_score"},
            )

        result = _save_result(brand_name, risk_score, model_output)
        return _response(200, result)

    except Exception as exc:
        return _response(
            500,
            {"message": "Internal server error", "detail": str(exc)},
        )

